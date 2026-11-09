#!/usr/bin/env python3
"""Fast, resumable OSRS-first Hiscores shortlist scan.

An OSRS listing is sufficient to exclude a name from the no-Hiscores shortlist,
so RS3 is queried only after OSRS returns 404. Unknown outcomes are never
treated as absent or available. This is a configurable client-side pace, not a
claim about Jagex's published rate limit.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import sqlite3
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import hiscores_tool as core


class StopAll(Exception):
    pass


class GlobalRateLimiter:
    """Process-wide minimum start spacing shared by workers and retries."""
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_at - now)
            if wait_for:
                time.sleep(wait_for)
            self._next_at = max(time.monotonic(), self._next_at) + self.interval


def perform_request(name: str, game: str, requester: Callable, limiter: GlobalRateLimiter,
                    stop_event: threading.Event, timeout: float, retries: int) -> dict[str, Any]:
    """Perform one bounded check; does not touch SQLite."""
    url = core.GAMES[game] + urllib.parse.quote(name, safe="")
    checked_at = core.utc_now()
    status = content_type = None
    body = b""
    headers: dict[str, str] = {}
    error = None
    retry_after = None
    attempts = 0
    for attempt in range(retries + 1):
        if stop_event.is_set():
            raise StopAll("provider stop flag is set")
        limiter.acquire()
        if stop_event.is_set():
            raise StopAll("provider stop flag is set")
        attempts += 1
        try:
            status, content_type, body, headers = requester(url, timeout)
        except Exception as exc:
            error = f"network error: {type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(8.0, 2.0 ** attempt))
                continue
            break
        if status in (403, 429):
            stop_event.set()
            retry_after = core.parse_retry_after(headers.get("Retry-After") or headers.get("retry-after"))
            error = f"HTTP {status}; stopped all requests"
            break
        if status is not None and 500 <= status <= 599:
            error = f"HTTP {status}"
            if attempt < retries:
                time.sleep(min(8.0, 2.0 ** attempt))
                continue
            break
        error = None
        break

    result = "unknown"
    valid = False
    level = None
    if status == 404:
        result = "not_listed"
    elif status == 200:
        try:
            valid, level, reason = core.valid_hiscores_payload(json.loads(body.decode("utf-8")))
            if valid:
                result = "listed"
            else:
                error = reason
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = f"invalid JSON: {type(exc).__name__}"
    elif status is not None and status not in (403, 429):
        error = error or f"HTTP {status}"
    return {"canonical": name, "game": game, "result": result, "http_status": status,
            "content_type": content_type, "schema_valid": valid, "overall_level": level,
            "checked_at": checked_at, "attempts": attempts, "error": error,
            "retry_after_utc": retry_after}


def save_result(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    db.execute("""INSERT INTO checks(canonical,game,result,http_status,content_type,schema_valid,overall_level,checked_at,attempts,error)
      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical,game) DO UPDATE SET
      result=excluded.result,http_status=excluded.http_status,content_type=excluded.content_type,
      schema_valid=excluded.schema_valid,overall_level=excluded.overall_level,checked_at=excluded.checked_at,
      attempts=excluded.attempts,error=excluded.error""",
      (row["canonical"], row["game"], row["result"], row["http_status"], row["content_type"],
       int(row["schema_valid"]), row["overall_level"], row["checked_at"], row["attempts"], row["error"]))
    if row["http_status"] in (403, 429):
        db.execute("""INSERT INTO provider_state(game,stopped,stopped_at,retry_after_utc,reason)
          VALUES(?,1,?,?,?) ON CONFLICT(game) DO UPDATE SET stopped=1,stopped_at=excluded.stopped_at,
          retry_after_utc=excluded.retry_after_utc,reason=excluded.reason""",
          (row["game"], row["checked_at"], row["retry_after_utc"], f"HTTP {row['http_status']}"))
    db.commit()


def save_exports(db: sqlite3.Connection, csv_path: Path, json_path: Path) -> None:
    rows = []
    for r in db.execute("""SELECT c.canonical,c.display_name,c.char_length,c.person_tag,c.word_tag,c.sources_json,c.generated,
        o.checked_at AS osrs_checked_at,r.checked_at AS rs3_checked_at
        FROM candidates c
        JOIN checks o ON o.canonical=c.canonical AND o.game='osrs' AND o.result='not_listed'
        JOIN checks r ON r.canonical=c.canonical AND r.game='rs3' AND r.result='not_listed'
        WHERE c.char_length>=3
        ORDER BY c.char_length,c.person_tag DESC,c.word_tag DESC,c.canonical"""):
        rows.append({"name": r["display_name"], "canonical": r["canonical"], "length": r["char_length"],
                     "person_tag": bool(r["person_tag"]), "word_tag": bool(r["word_tag"]),
                     "sources": json.loads(r["sources_json"]), "generated": bool(r["generated"]),
                     "osrs_result": "not_listed", "rs3_result": "not_listed",
                     "osrs_checked_at": r["osrs_checked_at"], "rs3_checked_at": r["rs3_checked_at"]})
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else ["name", "canonical", "length", "person_tag", "word_tag", "sources", "generated"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["sources"] = ";".join(out["sources"])
            writer.writerow(out)
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _existing(db: sqlite3.Connection, canonical: str) -> dict[str, str]:
    return {r["game"]: r["result"] for r in db.execute(
        "SELECT game,result FROM checks WHERE canonical=?", (canonical,))}


def _provider_blocked(db: sqlite3.Connection, game: str) -> bool:
    row = db.execute("SELECT stopped,reason,retry_after_utc FROM provider_state WHERE game=?", (game,)).fetchone()
    if not row or not row["stopped"]:
        return False
    retry = dt.datetime.fromisoformat(row["retry_after_utc"]) if row["retry_after_utc"] else None
    if row["reason"] == "HTTP 429" and retry and dt.datetime.now(dt.timezone.utc) >= retry:
        db.execute("UPDATE provider_state SET stopped=0,reason=NULL WHERE game=?", (game,))
        db.commit()
        return False
    print(f"Provider {game} remains stopped; Retry-After={row['retry_after_utc']}", file=sys.stderr)
    return True


def pending_requests(db: sqlite3.Connection, candidates: list[str], retry_unknown: bool) -> int:
    n = 0
    for name in candidates:
        state = _existing(db, name)
        osrs = state.get("osrs")
        if osrs == "listed":
            continue
        if osrs is None or osrs == "unknown":
            n += 1
        elif osrs == "not_listed":
            rs3 = state.get("rs3")
            if rs3 is None or rs3 == "unknown":
                n += 1
    return n


def select_candidates(db: sqlite3.Connection, letter: str, min_length: int, order: str = "fast") -> list[str]:
    if order == "fast":
        priority = "CASE WHEN char_length=3 AND person_tag=1 THEN 0 WHEN char_length=3 AND word_tag=1 AND (sources_json LIKE '%en_US_wordlist%' OR sources_json LIKE '%en_GB_wordlist%') THEN 1 WHEN char_length BETWEEN 4 AND 6 AND person_tag=1 THEN 2 WHEN char_length BETWEEN 4 AND 6 AND word_tag=1 THEN 3 WHEN char_length=3 THEN 4 ELSE 5 END, "
    else:
        priority = ""
    return [r["canonical"] for r in db.execute(
        f"SELECT canonical FROM candidates WHERE substr(canonical,1,1)=? AND char_length>=? ORDER BY {priority}{core.rank_clause()}",
        (letter.casefold(), min_length))]


def _scan_one(name: str, saved: dict[str, str], refresh: bool, requester: Callable, limiter: GlobalRateLimiter,
              stop_event: threading.Event, timeout: float, retries: int, retry_unknown: bool) -> list[dict[str, Any]]:
    state = {} if refresh else dict(saved)
    if state.get("osrs") == "listed":
        return []
    if state.get("osrs") == "unknown" and not retry_unknown:
        return []
    if state.get("osrs") == "unknown" and retry_unknown:
        state.pop("osrs", None)
    if state.get("osrs") not in ("not_listed", "unknown"):
        first = perform_request(name, "osrs", requester, limiter, stop_event, timeout, retries)
        if first["http_status"] in (403, 429):
            return [first]
        state["osrs"] = first["result"]
        results = [first]
    else:
        results = []
    if state.get("osrs") != "not_listed" or stop_event.is_set():
        return results
    rs3 = state.get("rs3")
    if rs3 in ("listed", "not_listed") or (rs3 == "unknown" and not retry_unknown):
        return results
    second = perform_request(name, "rs3", requester, limiter, stop_event, timeout, retries)
    results.append(second)
    return results


def run(db_path: Path, letter: str, min_length: int, rate: float, workers: int,
        duration: int, timeout: float, retries: int, retry_unknown: bool, refresh: bool = False,
        order: str = "fast",
        requester: Callable = core.request_once, progress_every: int = 20) -> dict[str, Any]:
    if not letter.isascii() or not letter.isalpha() or len(letter) != 1:
        raise ValueError("letter must be one ASCII letter")
    if min_length < 1 or min_length > 12 or not (0 < rate <= 3) or not (1 <= workers <= 4):
        raise ValueError("min-length must be 1-12, rate 0-3 requests/sec, workers 1-4")
    if not (0 < timeout <= 10) or not (0 <= retries <= core.MAX_RETRIES):
        raise ValueError("timeout must be <=10 seconds and retries must be 0-2")
    start_wall = time.monotonic()
    started_at = core.utc_now()
    stop_at = start_wall + duration if duration > 0 else float("inf")
    limiter = GlobalRateLimiter(rate)
    stop_event = threading.Event()
    csv_path = db_path.parent / f"{letter.lower()}_shortlist.csv"
    json_path = db_path.parent / f"{letter.lower()}_shortlist.json"
    saved_count = 0
    stopped = False
    with core.connect(db_path) as db:
        if _provider_blocked(db, "osrs"):
            return {"status": "provider_stopped", "startedAtUtc": started_at, "checked": 0}
        if _provider_blocked(db, "rs3"):
            return {"status": "provider_stopped", "startedAtUtc": started_at, "checked": 0}
        candidates = select_candidates(db, letter, min_length, order)
        print(f"Candidates: {len(candidates)} | minimum length: {min_length} | strategy: OSRS first, RS3 after OSRS 404")
        print(f"Rate: {rate:g} requests/sec global | workers: {workers} | duration: {duration}s")
        total = len(candidates)

        def submit_one(executor, name):
            saved = _existing(db, name)
            return executor.submit(_scan_one, name, saved, refresh, requester, limiter, stop_event,
                                   timeout, retries, retry_unknown)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            active: dict[concurrent.futures.Future, str] = {}
            candidate_iter = iter(candidates)
            finished_names = 0

            def fill():
                while len(active) < workers and not stop_event.is_set() and time.monotonic() < stop_at:
                    try:
                        name = next(candidate_iter)
                    except StopIteration:
                        break
                    active[submit_one(executor, name)] = name

            fill()
            while active:
                remaining = max(0.0, stop_at - time.monotonic())
                done, _ = concurrent.futures.wait(active, timeout=min(1.0, remaining) if remaining else 0,
                                                   return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    if time.monotonic() >= stop_at:
                        break
                    if stop_event.is_set():
                        break
                    continue
                for future in done:
                    name = active.pop(future)
                    finished_names += 1
                    try:
                        outcomes = future.result()
                    except StopAll:
                        outcomes = []
                    except Exception as exc:
                        outcomes = [{"canonical": name, "game": "osrs", "result": "unknown", "http_status": None,
                                     "content_type": None, "schema_valid": False, "overall_level": None,
                                     "checked_at": core.utc_now(), "attempts": 0,
                                     "error": f"worker error: {type(exc).__name__}: {exc}", "retry_after_utc": None}]
                    for outcome in outcomes:
                        save_result(db, outcome)
                        saved_count += 1
                        if outcome["http_status"] in (403, 429):
                            stopped = True
                        print(json.dumps(outcome, ensure_ascii=False), flush=True)
                        if saved_count % progress_every == 0:
                            save_exports(db, csv_path, json_path)
                            pending = pending_requests(db, candidates, retry_unknown)
                            elapsed = int(time.monotonic() - start_wall)
                            eta = int(pending / rate) if rate else 0
                            print(f"PROGRESS checked={saved_count} candidates_done={finished_names}/{total} "
                                  f"elapsed={elapsed}s pending_requests={pending} min_eta={eta}s", flush=True)
                    if stop_event.is_set():
                        stopped = True
                if stopped:
                    for future in active:
                        future.cancel()
                    break
                fill()
            # Any in-flight calls finish within timeout; process their completed data.
            for future, name in list(active.items()):
                if future.cancelled():
                    continue
                try:
                    outcomes = future.result(timeout=timeout + 1)
                except Exception:
                    outcomes = []
                for outcome in outcomes:
                    save_result(db, outcome)
                    saved_count += 1
                    if outcome["http_status"] in (403, 429):
                        stopped = True
                    print(json.dumps(outcome, ensure_ascii=False), flush=True)

        save_exports(db, csv_path, json_path)
        if stop_event.is_set():
            stopped = True
        pending = pending_requests(db, candidates, retry_unknown)
        elapsed = round(time.monotonic() - start_wall, 1)
        status = "stopped_403_429" if stopped else ("time_limit" if time.monotonic() >= stop_at else "finished")
        summary = {"status": status, "startedAtUtc": started_at, "finishedAtUtc": core.utc_now(),
                   "elapsedSeconds": elapsed, "letter": letter.upper(), "candidateCount": total,
                   "resultsSaved": saved_count, "pendingRequests": pending,
                   "minimumEtaSecondsAtConfiguredRate": int(pending / rate),
                   "configuredRatePerSecond": rate, "workers": workers,
                   "csv": str(csv_path), "json": str(json_path)}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OSRS-first short-name Hiscores scan; 404 means not listed, never availability.")
    p.add_argument("--db", default="data/hiscores.sqlite3")
    p.add_argument("--letter", default="A")
    p.add_argument("--min-length", type=int, default=3)
    p.add_argument("--rate", type=float, default=3.0, help="client pacing cap, max 3 requests/sec; not an official limit")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--duration", type=int, default=600, help="wall-clock scan budget in seconds")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--retry-unknown", action="store_true")
    p.add_argument("--refresh", action="store_true", help="recheck cached results")
    p.add_argument("--order", choices=["fast", "length"], default="fast")
    args = p.parse_args(argv)
    try:
        summary = run(Path(args.db), args.letter, args.min_length, args.rate, args.workers,
                      args.duration, args.timeout, args.retries, args.retry_unknown, args.refresh, args.order)
    except ValueError as exc:
        p.error(str(exc))
    return 0 if summary["status"] in ("finished", "time_limit") else 2


if __name__ == "__main__":
    raise SystemExit(main())
