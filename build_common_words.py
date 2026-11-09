#!/usr/bin/env python3
"""Build the runtime common-word shortlist from saved wordlists and wordfreq.

This is a build-time helper. The name-search server reads the generated TSV and
does not require the wordfreq package to be installed.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
THRESHOLD = 3.7  # About five occurrences per million words.
WORD_RE = re.compile(r"[a-z]{3,12}\Z")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "common_words.tsv")
    args = parser.parse_args()

    try:
        from wordfreq import zipf_frequency
    except ImportError as exc:
        raise SystemExit("Install wordfreq==3.1.1 to build this file; it is not needed at runtime.") from exc

    words: set[str] = set()
    for path in (ROOT / "data" / "sources" / "en_US.txt", ROOT / "data" / "sources" / "en_GB.txt"):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            # Lowercase entries exclude proper nouns and all-caps abbreviations.
            if WORD_RE.fullmatch(line):
                words.add(line)

    scored = [(word, zipf_frequency(word, "en")) for word in words]
    kept = sorted((word, score) for word, score in scored if score >= THRESHOLD)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as out:
        out.write("# wordfreq 3.1.1 English Zipf >= 3.7; lowercase US/GB dictionary entries; lengths 3-12\n")
        out.write("# word\tzipf\n")
        for word, score in kept:
            out.write(f"{word}\t{score:.2f}\n")
    print(f"Kept {len(kept)} common words from {len(words)} local dictionary entries: {args.output}")


if __name__ == "__main__":
    main()
