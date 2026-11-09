import json
import csv
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import hiscores_fast as fast
import hiscores_tool as core


def valid_payload(level=900):
    return {"skills": [{"name": "Overall", "rank": 1, "level": level, "xp": 123}],
            "activities": [{"name": "x", "rank": -1, "score": 0}]}


class FastStrategyFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db = core.connect(Path(self.temp.name) / "fast.sqlite3")
        core.merge_candidate(self.db, "A", "generated", generated=True)
        core.merge_candidate(self.db, "A1", "generated", generated=True)
        core.merge_candidate(self.db, "Aaa", "generated", generated=True)
        core.merge_candidate(self.db, "Ace", "dictionary", person_tag=True, word_tag=True)
        core.merge_candidate(self.db, "Able", "dictionary", word_tag=True)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_one(self, state, replies, retry_unknown=True):
        calls = []
        def fake(url, timeout):
            calls.append(url)
            return replies.pop(0)
        limiter = fast.GlobalRateLimiter(3)
        with patch.object(limiter, "acquire"):
            rows = fast._scan_one("ace", state, False, fake, limiter, threading.Event(), 10, 0, retry_unknown)
        return rows, calls

    def test_minimum_length_and_priority_order(self):
        names = fast.select_candidates(self.db, "A", 3, "fast")
        self.assertNotIn("a", names)
        self.assertNotIn("a1", names)
        self.assertLess(names.index("ace"), names.index("able"))
        self.assertLess(names.index("able"), names.index("aaa"))

    def test_osrs_listed_skips_rs3(self):
        rows, calls = self.run_one({}, [(200, "application/json", json.dumps(valid_payload()).encode(), {})])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["game"], "osrs")
        self.assertEqual(rows[0]["result"], "listed")
        self.assertEqual(len(calls), 1)

    def test_rs3_is_queried_only_after_osrs_404(self):
        rows, calls = self.run_one({}, [
            (404, "text/html", b"", {}),
            (200, "application/json", json.dumps(valid_payload()).encode(), {}),
        ])
        self.assertEqual([r["game"] for r in rows], ["osrs", "rs3"])
        self.assertEqual([r["result"] for r in rows], ["not_listed", "listed"])
        self.assertEqual(len(calls), 2)

    def test_unknown_osrs_stays_unknown_and_does_not_query_rs3(self):
        rows, calls = self.run_one({}, [(500, "text/html", b"", {})])
        self.assertEqual(rows[0]["result"], "unknown")
        self.assertEqual(len(calls), 1)

    def test_cached_unknown_is_retried_when_requested(self):
        rows, calls = self.run_one({"osrs": "unknown"}, [(404, "text/html", b"", {}), (404, "text/html", b"", {})])
        self.assertEqual([r["game"] for r in rows], ["osrs", "rs3"])
        self.assertEqual(len(calls), 2)

    def test_shortlist_export_requires_both_404_and_minimum_length_three(self):
        now = core.utc_now()
        for name in ("a1", "aaa", "ace"):
            for game in ("osrs", "rs3"):
                self.db.execute("INSERT INTO checks(canonical,game,result,http_status,content_type,schema_valid,checked_at,attempts) VALUES(?,?,?,404,'text/html',0,?,1)",
                                (name, game, "not_listed", now))
        self.db.commit()
        csv_path = Path(self.temp.name) / "shortlist.csv"
        json_path = Path(self.temp.name) / "shortlist.json"
        fast.save_exports(self.db, csv_path, json_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            names = {row["canonical"] for row in csv.DictReader(handle)}
        self.assertEqual(names, {"aaa", "ace"})

    def test_403_and_429_stop_all(self):
        for code in (403, 429):
            with self.subTest(code=code):
                stop = threading.Event()
                def fake(url, timeout):
                    return code, "text/html", b"", {"Retry-After": "120"}
                row = fast.perform_request("ace", "osrs", fake, fast.GlobalRateLimiter(3), stop, 10, 0)
                self.assertEqual(row["result"], "unknown")
                self.assertTrue(stop.is_set())


if __name__ == "__main__":
    unittest.main()
