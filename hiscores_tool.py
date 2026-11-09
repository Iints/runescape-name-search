#!/usr/bin/env python3
"""Rate-conscious OSRS/RS3 public Hiscores inventory tool.

Hiscores results describe listing status only. A missing Hiscores entry is not
evidence that a display name is available, eligible, or claimable.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

GAMES = {
    "osrs": "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player=",
    "rs3": "https://secure.runescape.com/m=hiscore/index_lite.json?player=",
}
USER_AGENT = "RareNameResearchProbe/0.1"
MAX_RETRIES = 2
DEFAULT_DB = "hiscores.sqlite3"
NAME_RE = re.compile(r"^[A-Za-z0-9 _]{1,12}$", re.ASCII)
ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonicalize(value: str) -> str | None:
    value = value.strip()
    if not NAME_RE.fullmatch(value):
        return None
    if value.startswith("_") or value.endswith("_"):
        return None
    # Treat underscores and runs of spaces alike for deduplication.
    value = re.sub(r"[ _]+", " ", value).strip()
    return value.casefold() or None


def valid_hiscores_payload(payload: Any) -> tuple[bool, int | None, str | None]:
    """Validate index_lite JSON and return (valid, Overall.level, reason)."""
    if not isinstance(payload, dict):
        return False, None, "top-level JSON is not an object"
    skills = payload.get("skills")
    activities = payload.get("activities")
    if not isinstance(skills, list) or not isinstance(activities, list):
        return False, None, "skills/activities are not arrays"
    for row in skills:
        if not isinstance(row, dict) or not {"name", "rank", "level", "xp"}.issubset(row):
            return False, None, "skill row missing required fields"
    for row in activities:
        if not isinstance(row, dict) or not {"name", "rank", "score"}.issubset(row):
            return False, None, "activity row missing required fields"
    overall = next((row for row in skills if row.get("name") == "Overall"), None)
    if overall is None or not isinstance(overall.get("level"), (int, float)):
        return False, None, "Overall row/level missing"
    return True, int(overall["level"]), None


def parse_retry_after(value: str | None, now: dt.datetime | None = None) -> str | None:
    if not value:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        seconds = max(0, int(value.strip()))
        when = now + dt.timedelta(seconds=seconds)
    except ValueError:
        try:
            when = dt.datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS candidates (
        canonical TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        char_length INTEGER NOT NULL,
        person_tag INTEGER NOT NULL DEFAULT 0,
        word_tag INTEGER NOT NULL DEFAULT 0,
        sources_json TEXT NOT NULL DEFAULT '[]',
        generated INTEGER NOT NULL DEFAULT 0,
        imported_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS checks (
        canonical TEXT NOT NULL,
        game TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('listed','not_listed','unknown')),
        http_status INTEGER,
        content_type TEXT,
        schema_valid INTEGER NOT NULL DEFAULT 0,
        overall_level INTEGER,
        checked_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        error TEXT,
        PRIMARY KEY(canonical, game),
        FOREIGN KEY(canonical) REFERENCES candidates(canonical)
    );
    CREATE TABLE IF NOT EXISTS provider_state (
        game TEXT PRIMARY KEY,
        stopped INTEGER NOT NULL DEFAULT 0,
        stopped_at TEXT,
        retry_after_utc TEXT,
        reason TEXT
    );
    """)
    db.commit()
    return db


def merge_candidate(db: sqlite3.Connection, name: str, source: str,
                    person_tag: bool = False, word_tag: bool = False,
                    generated: bool = False) -> bool:
    canonical = canonicalize(name)
    if canonical is None:
        return False
    display = re.sub(r"[ _]+", " ", name.strip())
    row = db.execute("SELECT * FROM candidates WHERE canonical=?", (canonical,)).fetchone()
    sources = set(json.loads(row["sources_json"])) if row else set()
    sources.add(source)
    db.execute("""INSERT INTO candidates(canonical,display_name,char_length,person_tag,word_tag,sources_json,generated,imported_at)
       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(canonical) DO UPDATE SET
       person_tag=MAX(candidates.person_tag,excluded.person_tag),
       word_tag=MAX(candidates.word_tag,excluded.word_tag),
       sources_json=excluded.sources_json,
       generated=MAX(candidates.generated,excluded.generated)""",
       (canonical, display, len(canonical), int(person_tag), int(word_tag), json.dumps(sorted(sources)), int(generated), utc_now()))
    return True


def generate_a_names() -> list[str]:
    """A-prefixed ASCII alphanumeric strings of length 1 through 3: 1+36+1296."""
    return ["A"] + ["A" + x for x in ALNUM] + ["A" + x + y for x in ALNUM for y in ALNUM]


def import_text(db: sqlite3.Connection, file: Path, source: str, kind: str) -> tuple[int, int]:
    good = bad = 0
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if merge_candidate(db, candidate, source, kind == "people", kind == "words"):
            good += 1
        else:
            bad += 1
    db.commit()
    return good, bad


def import_json(db: sqlite3.Connection, file: Path, source: str | None = None) -> tuple[int, int]:
    """Import a JSON array of strings/objects or common candidates/names arrays."""
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        rows = data.get("candidates", data.get("names", []))
    else:
        rows = data
    if not isinstance(rows, list):
        raise ValueError("JSON input must be an array, or an object with candidates/names array")
    good = bad = 0
    default_source = source or str(file)
    for row in rows:
        if isinstance(row, str):
            name, row_sources, tags = row, [default_source], []
        elif isinstance(row, dict):
            name = row.get("name", row.get("display_name", row.get("candidate")))
            row_source = row.get("source", row.get("sources", default_source))
            if isinstance(row_source, list):
                row_sources = [str(source) for source in row_source] or [default_source]
            else:
                row_sources = [str(row_source)]
            tags = row.get("tags", row.get("categories", []))
            if isinstance(tags, str):
                tags = [tags]
        else:
            bad += 1
            continue
        if not isinstance(name, str):
            bad += 1
            continue
        tags_norm = {str(tag).casefold() for tag in tags}
        person_tag = bool(tags_norm & {"person", "people", "given_name", "curated_given_name", "curated_seed_unverified"})
        word_tag = bool(tags_norm & {"word", "dictionary"})
        generated = "alphanumeric_short" in tags_norm
        accepted = merge_candidate(db, name, row_sources[0], person_tag, word_tag, generated)
        if accepted:
            for extra_source in row_sources[1:]:
                merge_candidate(db, name, extra_source, person_tag, word_tag, generated)
            good += 1
        else:
            bad += 1
    db.commit()
    return good, bad


class ProbeStop(Exception):
    pass


def request_once(url: str, timeout: float) -> tuple[int, str | None, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type"), response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read(), dict(exc.headers.items())


def check_candidate(db: sqlite3.Connection, canonical: str, game: str,
                    interval: float = 1.0, timeout: float = 10.0,
                    retries: int = 2, requester: Callable = request_once) -> dict[str, Any]:
    if game not in GAMES:
        raise ValueError(f"unknown game {game}")
    if interval < 1.0:
        raise ValueError("request interval must be at least 1 second")
    if not (0 < timeout <= 10.0):
        raise ValueError("timeout must be greater than 0 and at most 10 seconds")
    if retries < 0 or retries > MAX_RETRIES:
        raise ValueError(f"retries must be between 0 and {MAX_RETRIES}")
    state = db.execute("SELECT * FROM provider_state WHERE game=?", (game,)).fetchone()
    if state and state["stopped"]:
        retry_after = state["retry_after_utc"]
        retry_dt = dt.datetime.fromisoformat(retry_after) if retry_after else None
        if state["reason"] == "HTTP 429" and retry_dt and dt.datetime.now(dt.timezone.utc) >= retry_dt:
            db.execute("UPDATE provider_state SET stopped=0,reason=NULL WHERE game=?", (game,))
            db.commit()
        else:
            suffix = f"; Retry-After {retry_after}" if retry_after else ""
            raise ProbeStop(f"{game} provider was stopped at {state['stopped_at']}: {state['reason'] or 'HTTP 403/429'}{suffix}")
    checked_at = utc_now()
    last_error = None
    status = content_type = None
    body = b""
    response_headers: dict[str, str] = {}
    attempts = 0
    url = GAMES[game] + urllib.parse.quote(canonical, safe="")
    for attempt in range(retries + 1):
        attempts += 1
        if attempt:
            time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        else:
            # A single CLI process runs checks serially; caller adds pacing between them.
            pass
        try:
            status, content_type, body, response_headers = requester(url, timeout)
        except Exception as exc:  # network errors remain unknown; retries are bounded
            last_error = f"network error: {type(exc).__name__}: {exc}"
            if attempt < retries:
                continue
            break
        if status == 500 or (status is not None and 500 <= status <= 599):
            last_error = f"HTTP {status}"
            if attempt < retries:
                continue
            break
        last_error = None
        break

    result, schema_valid, overall = "unknown", False, None
    retry_after = None
    if status == 404:
        result = "not_listed"
    elif status == 200:
        try:
            payload = json.loads(body.decode("utf-8"))
            schema_valid, overall, validation_error = valid_hiscores_payload(payload)
            if schema_valid:
                result = "listed"
            else:
                last_error = validation_error
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = f"invalid JSON: {type(exc).__name__}"
    elif status in (403, 429):
        result = "unknown"
        retry_after = parse_retry_after(response_headers.get("Retry-After") or response_headers.get("retry-after"))
        last_error = f"HTTP {status}; provider stopped"
        db.execute("""INSERT INTO provider_state(game,stopped,stopped_at,retry_after_utc,reason)
          VALUES(?,1,?,?,?) ON CONFLICT(game) DO UPDATE SET stopped=1,stopped_at=excluded.stopped_at,
          retry_after_utc=excluded.retry_after_utc,reason=excluded.reason""",
          (game, checked_at, retry_after, f"HTTP {status}"))
    elif status is not None:
        last_error = f"HTTP {status}"
    db.execute("""INSERT INTO checks(canonical,game,result,http_status,content_type,schema_valid,overall_level,checked_at,attempts,error)
      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical,game) DO UPDATE SET
      result=excluded.result,http_status=excluded.http_status,content_type=excluded.content_type,
      schema_valid=excluded.schema_valid,overall_level=excluded.overall_level,checked_at=excluded.checked_at,
      attempts=excluded.attempts,error=excluded.error""",
      (canonical, game, result, status, content_type, int(schema_valid), overall, checked_at, attempts, last_error))
    db.commit()
    return {"canonical": canonical, "game": game, "result": result, "http_status": status,
            "content_type": content_type, "schema_valid": schema_valid, "overall_level": overall,
            "checked_at": checked_at, "attempts": attempts, "error": last_error,
            "retry_after_utc": retry_after}


def rank_clause() -> str:
    # Length dominates; person-name and dictionary sources break ties in that order.
    return "char_length ASC, person_tag DESC, word_tag DESC, canonical ASC"


def pending_request_count(db: sqlite3.Connection) -> int:
    pending = 0
    for candidate in db.execute("SELECT canonical FROM candidates"):
        checked = {r["game"]: r["result"] for r in db.execute(
            "SELECT game,result FROM checks WHERE canonical=?", (candidate["canonical"],))}
        pending += sum(1 for game in GAMES if checked.get(game) not in {"listed", "not_listed"})
    return pending


def export_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(f"SELECT * FROM candidates ORDER BY {rank_clause()}").fetchall()
    total_pending = pending_request_count(db)
    result = []
    for row in rows:
        checks = {c["game"]: c for c in db.execute("SELECT * FROM checks WHERE canonical=?", (row["canonical"],))}
        item = {
            "name": row["display_name"], "canonical": row["canonical"], "length": row["char_length"],
            "person_tag": bool(row["person_tag"]), "word_tag": bool(row["word_tag"]),
            "sources": json.loads(row["sources_json"]), "generated": bool(row["generated"]),
        }
        for game in GAMES:
            c = checks.get(game)
            item[f"{game}_result"] = c["result"] if c else "unchecked"
            item[f"{game}_http_status"] = c["http_status"] if c else None
            item[f"{game}_content_type"] = c["content_type"] if c else None
            item[f"{game}_overall_level"] = c["overall_level"] if c else None
            item[f"{game}_checked_at"] = c["checked_at"] if c else None
            item[f"{game}_error"] = c["error"] if c else None
        item["pending_request_count"] = total_pending
        item["minimum_eta_seconds"] = total_pending
        result.append(item)
    return result


def cmd_init(args: argparse.Namespace) -> int:
    with connect(args.db) as db:
        pass
    print(f"Initialized {args.db}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    names = generate_a_names()
    with connect(args.db) as db:
        for name in names:
            merge_candidate(db, name, "generated:A-prefixed-alphanumeric-1-3", generated=True)
        db.commit()
    print(f"Generated/imported {len(names)} A-prefixed names (unique corpus total is database-wide).")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    with connect(args.db) as db:
        file = Path(args.file)
        if file.suffix.lower() == ".json":
            good, bad = import_json(db, file, args.source)
        else:
            good, bad = import_text(db, file, args.source or str(file), args.kind)
    print(f"Imported {good} valid rows; rejected {bad} rows. Duplicates merge provenance.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    with connect(args.db) as db:
        games = [args.game] if args.game != "both" else list(GAMES)
        if args.limit:
            candidates = db.execute(f"SELECT canonical FROM candidates ORDER BY {rank_clause()} LIMIT ?", (args.limit,)).fetchall()
        else:
            candidates = db.execute(f"SELECT canonical FROM candidates ORDER BY {rank_clause()}").fetchall()
        last_request = [0.0]

        def paced_request(url: str, timeout: float):
            if last_request[0]:
                time.sleep(max(0.0, args.interval - (time.monotonic() - last_request[0])))
            last_request[0] = time.monotonic()
            return request_once(url, timeout)

        for candidate in candidates:
            name = candidate["canonical"]
            for game in games:
                existing = db.execute("SELECT result FROM checks WHERE canonical=? AND game=?", (name, game)).fetchone()
                if existing and not args.refresh and (existing["result"] != "unknown" or not args.retry_unknown):
                    continue
                try:
                    result = check_candidate(db, name, game, args.interval, args.timeout, args.retries, paced_request)
                except ProbeStop as exc:
                    print(f"STOP: {exc}", file=sys.stderr)
                    return 2
                print(json.dumps(result, ensure_ascii=False))
                if result["http_status"] in (403, 429):
                    print("Provider stopped; resume only after reviewing Retry-After and provider state.", file=sys.stderr)
                    return 2
        remaining = pending_request_count(db)
        print(json.dumps({"pending_requests": remaining, "minimum_eta_seconds_at_1_request_per_second": remaining}))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with connect(args.db) as db:
        rows = export_rows(db)
    target = Path(args.output)
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        fields = list(rows[0].keys()) if rows else ["name", "canonical", "length", "person_tag", "word_tag", "sources", "generated"]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                out = dict(row)
                out["sources"] = ";".join(out["sources"])
                writer.writerow(out)
    print(f"Exported {len(rows)} ranked rows to {target}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Resumable public OSRS/RS3 Hiscores inventory; does not determine name availability.")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite state file (default: {DEFAULT_DB})")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize the database")
    init.set_defaults(func=cmd_init)
    gen = sub.add_parser("generate-a", help="add all 1,333 A-prefixed alphanumeric strings of length 1-3")
    gen.set_defaults(func=cmd_generate)
    imp = sub.add_parser("import", help="import a UTF-8 text list or JSON candidate list with provenance")
    imp.add_argument("file")
    imp.add_argument("--source", help="source URL/name; defaults to file path")
    imp.add_argument("--kind", choices=["names", "words", "people"], default="names", help="text list tag; JSON rows may provide tags")
    imp.set_defaults(func=cmd_import)
    scan = sub.add_parser("scan", help="scan candidates against public standard Hiscores")
    scan.add_argument("--game", choices=["osrs", "rs3", "both"], default="both")
    scan.add_argument("--limit", type=int, default=0, help="maximum ranked candidates; 0 means all")
    scan.add_argument("--interval", type=float, default=1.0, help="minimum global delay between GET requests; >=1")
    scan.add_argument("--timeout", type=float, default=10.0, help="per-request timeout; <=10 seconds")
    scan.add_argument("--retries", type=int, default=2, help="bounded retries for transport/5xx failures; max 2")
    scan.add_argument("--retry-unknown", action="store_true", help="recheck previously unknown outcomes")
    scan.add_argument("--refresh", action="store_true", help="recheck cached listed/not_listed outcomes too")
    scan.set_defaults(func=cmd_scan)
    exp = sub.add_parser("export", help="export the ranked corpus and listing statuses as CSV or JSON")
    exp.add_argument("output")
    exp.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "scan":
        if args.interval < 1.0 or not (0 < args.timeout <= 10.0) or not (0 <= args.retries <= MAX_RETRIES):
            parser().error("scan requires interval >=1, timeout <=10 and retries in 0..2")
        if args.limit < 0:
            parser().error("--limit must be zero or positive")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
