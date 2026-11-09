# Common-word filter provenance

`../common_words.tsv` was generated from the saved `en_US.txt` and `en_GB.txt`
wordlists, retaining lowercase ASCII words of 3–12 characters with an English
Zipf frequency of at least 3.7 according to `wordfreq` 3.1.1. A Zipf value of
3.7 represents roughly five occurrences per million words. The `wordfreq`
project is by Robyn Speer and is licensed under Apache 2.0; its license is
included as `wordfreq_LICENSE.txt`. See <https://github.com/rspeer/wordfreq>.

The source wordlist license is in `wordlist_license.txt`. The generated TSV is
a build artifact; the runtime server reads it without requiring `wordfreq`.

To rebuild after changing the source lists, install `wordfreq==3.1.1` in a
separate build environment, then run `python build_common_words.py` from the
project root.
