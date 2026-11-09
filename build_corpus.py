#!/usr/bin/env python3
"""Build a provenance-aware A-name candidate corpus from public word/name lists.

No third-party packages are required. Run from any directory with Python 3:
    python build_corpus.py

The script only downloads source lists and builds local candidate artifacts. It
does not contact RuneScape or test username availability.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import argparse
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
USER_AGENT = "RareNameResearchCorpusBuilder/1.0 (public source list download)"
URLS = {
    "en_US": "https://raw.githubusercontent.com/en-wl/wordlist-diff/diff/en_US-large.txt",
    "en_GB": "https://raw.githubusercontent.com/en-wl/wordlist-diff/diff/en_GB-large.txt",
    "wordlist_license": "https://raw.githubusercontent.com/en-wl/wordlist-diff/diff/Copyright",
    "ssa_national_names": "https://www.ssa.gov/oact/babynames/names.zip",
}
CURATED_GIVEN_NAMES_A = """Aaron Abby Abel Ada Adam Adele Adrian Aiden Aisha Alan Albert Alex Alice Alicia Alina Alison Amir Amy Andrea Andrew Angel Anita Anna Anthony April Archer Ari Aria Ariel Arthur Ashley Aspen Atlas Audrey August Aurora Ava Avery Axel""".split()
VALID = re.compile(r"^[A-Za-z0-9 _]{1,12}$", re.ASCII)
STARTS_A = re.compile(r"^a", re.IGNORECASE | re.ASCII)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch(url: str) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = response.read()
        return payload, {
            "url": url,
            "fetched_utc": utc_now(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "http_status": str(response.status),
            "content_type": response.headers.get("Content-Type", ""),
            "bytes": str(len(payload)),
        }


def canonical(name: str) -> str:
    # RuneScape usernames are case-insensitive and use spaces/underscores as
    # equivalent display separators for deduplication.
    return re.sub(r"[ _]+", " ", name.strip()).lower()


def valid_a_name(raw: str) -> str | None:
    name = raw.strip()
    if not VALID.fullmatch(name) or not STARTS_A.match(name):
        return None
    if not name[0].isalpha():
        return None
    return name


def add_candidate(store: dict[str, dict], name: str, category: str, source: str,
                  frequency: int | None = None) -> None:
    key = canonical(name)
    record = store.setdefault(key, {
        "name": name.strip(), "normalized": key, "categories": set(),
        "sources": set(), "frequency": None,
    })
    record["categories"].add(category)
    record["sources"].add(source)
    if frequency is not None:
        record["frequency"] = (record["frequency"] or 0) + frequency
    # Prefer a compact spelling and preserve the spelling from the earlier
    # source when equally short (dictionary lists precede SSA data).
    if len(name.strip()) < len(record["name"]):
        record["name"] = name.strip()


def parse_words(payload: bytes, source_key: str, store: dict[str, dict]) -> dict:
    text = payload.decode("utf-8-sig", errors="replace")
    accepted = 0
    rejected = 0
    lengths: Counter[int] = Counter()
    for raw in text.splitlines():
        candidate = valid_a_name(raw)
        if candidate is None:
            rejected += 1
            continue
        add_candidate(store, candidate, "dictionary", source_key)
        accepted += 1
        lengths[len(candidate)] += 1
    return {"accepted": accepted, "rejected_or_non_a": rejected,
            "accepted_by_length": dict(sorted(lengths.items()))}


def parse_ssa(payload: bytes, store: dict[str, dict]) -> dict:
    accepted_rows = 0
    rejected_rows = 0
    name_frequency: Counter[str] = Counter()
    archive = zipfile.ZipFile(BytesIO(payload))
    for member in archive.namelist():
        match = re.fullmatch(r"yob\d{4}\.txt", Path(member).name, re.IGNORECASE)
        if not match:
            continue
        data = archive.read(member).decode("utf-8-sig", errors="replace")
        for line in data.splitlines():
            fields = line.split(",")
            if len(fields) != 3:
                rejected_rows += 1
                continue
            name, sex, count_text = fields
            candidate = valid_a_name(name)
            if candidate is None:
                rejected_rows += 1
                continue
            try:
                count = int(count_text)
            except ValueError:
                rejected_rows += 1
                continue
            # Sum both reported sexes and all years; retain this as a ranking
            # signal, not a claim about an individual person's identity.
            name_frequency[canonical(candidate)] += count
            accepted_rows += 1
    # Reconstruct canonical label from the frequency key for names not already
    # represented in the English word lists.
    for key, frequency in name_frequency.items():
        label = store[key]["name"] if key in store else key.title()
        add_candidate(store, label, "given_name", "ssa_national_names", frequency)
    return {"accepted_name_rows": accepted_rows,
            "rejected_or_non_a_rows": rejected_rows,
            "unique_names": len(name_frequency),
            "summed_frequency_total": sum(name_frequency.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-only", action="store_true",
                        help="write the complete generated A-prefixed 1–3 character alphanumeric set without downloading sources")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    source_meta: dict[str, dict] = {}
    source_bytes: dict[str, bytes] = {}
    source_errors: dict[str, str] = {}
    if not args.generated_only:
        for key, url in URLS.items():
            try:
                source_bytes[key], source_meta[key] = fetch(url)
                print(f"Fetched {key}: {source_meta[key]['bytes']} bytes")
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                print(f"Could not fetch {key} from {url}: {exc}", file=sys.stderr)
                source_errors[key] = str(exc)

    store: dict[str, dict] = {}
    counts = {}
    if not args.generated_only:
        for key, tag in (("en_US", "en_US_wordlist"), ("en_GB", "en_GB_wordlist")):
            if key in source_bytes:
                counts[key] = parse_words(source_bytes[key], tag, store)
        if "ssa_national_names" in source_bytes:
            counts["ssa_national_names"] = parse_ssa(source_bytes["ssa_national_names"], store)
        for name in CURATED_GIVEN_NAMES_A:
            candidate = valid_a_name(name)
            if candidate:
                add_candidate(store, candidate, "curated_given_name", "curated_seed_unverified")
        counts["curated_given_name_seeds"] = len(CURATED_GIVEN_NAMES_A)

    # All A-prefixed 1–3 character names over 36 alphanumeric characters:
    # 1 + 36 + 36^2 = 1,333 candidates.
    import itertools
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    combinatorial = 0
    for length in range(1, 4):
        for suffix in itertools.product(alphabet, repeat=length - 1):
            candidate = "a" + "".join(suffix)
            add_candidate(store, candidate, "alphanumeric_short", "generated_alphanumeric_1_3")
            combinatorial += 1

    candidates = []
    for record in store.values():
        candidates.append({
            "name": record["name"],
            "normalized": record["normalized"],
            "categories": sorted(record["categories"]),
            "sources": sorted(record["sources"]),
            "frequency": record["frequency"],
        })
    candidates.sort(key=lambda c: (len(c["name"]), c["normalized"]))

    length_counts = Counter(len(c["name"]) for c in candidates)
    payload = {
        "metadata": {
            "generated_utc": utc_now(),
            "sources": source_meta,
            "source_files_saved": [],
            "sources_not_downloaded": list(URLS) if args.generated_only else [],
            "source_errors": source_errors,
            "source_counts": counts,
            "candidate_count": len(candidates),
            "candidate_counts_by_length": dict(sorted(length_counts.items())),
            "generated_alphanumeric_a_1_3_count": combinatorial,
            "normalization": "case-insensitive; spaces and underscores collapse to one space",
            "filter": "A-prefixed; 1–12 characters; ASCII letters, digits, spaces, underscore only",
            "limits": [
                "English dictionary coverage is limited to the supplied US and UK wordlists.",
                "SSA national names source was unavailable in this run; curated given-name seeds are unverified and not exhaustive.",
                "No guarantee of international, cultural, or transliteration coverage.",
                "Corpus membership does not establish RuneScape username validity or availability.",
                "SSA frequency aggregates reported births across all years and sexes; it is only a broad priority signal.",
            ],
        },
        "candidates": candidates,
    }
    (DATA / "a_candidates.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (DATA / "a_candidates.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "normalized", "categories", "sources", "frequency"])
        writer.writeheader()
        for row in candidates:
            writer.writerow({**row, "categories": ";".join(row["categories"]), "sources": ";".join(row["sources"])})
    (DATA / "a_names.txt").write_text("\n".join(c["name"] for c in candidates) + "\n", encoding="utf-8")
    priority = [c for c in candidates if 3 <= len(c["name"]) <= 6 and
                ("dictionary" in c["categories"] or "given_name" in c["categories"] or "curated_given_name" in c["categories"])]
    priority.sort(key=lambda c: (len(c["name"]), 0 if "given_name" in c["categories"] or "curated_given_name" in c["categories"] else 1, -(c["frequency"] or 0), c["normalized"]))
    (DATA / "a_priority.txt").write_text("\n".join(c["name"] for c in priority) + "\n", encoding="utf-8")
    (DATA / "a_priority.csv").write_text("name,normalized,categories,sources,frequency\n" + "\n".join(
        ",".join('"' + str(value).replace('"', '""') + '"' for value in (
            c["name"], c["normalized"], ";".join(c["categories"]), ";".join(c["sources"]), c["frequency"] or ""))
        for c in priority) + "\n", encoding="utf-8")
    # Preserve every fetched payload, including the wordlist copyright notice,
    # so the generated corpus retains its source and license context.
    saved_sources = []
    if not args.generated_only:
        sources_dir = DATA / "sources"
        sources_dir.mkdir(exist_ok=True)
        for key, raw_bytes in source_bytes.items():
            suffix = ".zip" if key == "ssa_national_names" else ".txt"
            filename = key + suffix
            (sources_dir / filename).write_bytes(raw_bytes)
            saved_sources.append(str(Path("sources") / filename))
        payload["metadata"]["source_files_saved"] = saved_sources
    (DATA / "source_metadata.json").write_text(json.dumps({
        "generated_utc": utc_now(),
        "sources": source_meta,
        "sources_not_downloaded": {key: URLS[key] for key in URLS if key not in source_meta},
        "source_errors": source_errors,
        "source_files_saved": saved_sources,
        "coverage_status": "generated_only; dictionary and person-name source coverage is absent" if args.generated_only else ("partial; SSA names source unavailable" if source_errors else "downloaded"),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(candidates)} deduplicated candidates to {DATA}")
    print("Counts by name length:", dict(sorted(length_counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
