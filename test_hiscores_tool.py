import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hiscores_tool as tool


def good_payload(level=1234):
    return {
        "skills": [
            {"name": "Overall", "rank": 123, "level": level, "xp": 987654},
            {"name": "Attack", "rank": 456, "level": 99, "xp": 13034431},
        ],
        "activities": [{"name": "League Points", "rank": -1, "score": 0}],
    }


class ProbeFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db = tool.connect(Path(self.tmp.name) / "test.sqlite3")
        tool.merge_candidate(self.db, "Ace", "fixture")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def check(self, status, body=b"", headers=None, game="osrs", retry_count=0):
        calls = []
        def fake(url, timeout):
            calls.append((url, timeout))
            return status, "application/json", body, headers or {}
        with patch.object(tool.time, "sleep"):
            row = tool.check_candidate(self.db, "ace", game, retries=retry_count, requester=fake)
        return row, calls

    def test_valid_200_is_listed_and_uses_overall_row(self):
        row, _ = self.check(200, json.dumps(good_payload()).encode())
        self.assertEqual(row["result"], "listed")
        self.assertTrue(row["schema_valid"])
        self.assertEqual(row["overall_level"], 1234)

    def test_404_is_only_not_listed(self):
        row, _ = self.check(404)
        self.assertEqual(row["result"], "not_listed")
        self.assertIsNone(row["overall_level"])

    def test_403_stops_provider_and_remains_unknown(self):
        row, _ = self.check(403)
        self.assertEqual(row["result"], "unknown")
        with self.assertRaises(tool.ProbeStop):
            tool.check_candidate(self.db, "ace", "osrs", requester=lambda *args: self.fail("must remain stopped"))

    def test_429_stores_retry_after_and_stops_provider(self):
        row, _ = self.check(429, headers={"Retry-After": "120"})
        self.assertEqual(row["result"], "unknown")
        state = self.db.execute("SELECT stopped,retry_after_utc FROM provider_state WHERE game='osrs'").fetchone()
        self.assertEqual(state["stopped"], 1)
        self.assertIsNotNone(state["retry_after_utc"])

    def test_500_bounded_retries_then_unknown(self):
        row, calls = self.check(500, retry_count=2)
        self.assertEqual(row["result"], "unknown")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(len(calls), 3)

    def test_invalid_200_is_unknown(self):
        row, _ = self.check(200, b'{"skills": [], "activities": []}')
        self.assertEqual(row["result"], "unknown")
        self.assertFalse(row["schema_valid"])

    def test_name_canonicalization_and_1333_generated(self):
        self.assertEqual(tool.canonicalize(" A__B "), "a b")
        self.assertIsNone(tool.canonicalize("_Ace"))
        self.assertIsNone(tool.canonicalize("Ace!"))
        first = tool.merge_candidate(self.db, "A_B", "source-one")
        second = tool.merge_candidate(self.db, "a b", "source-two", person_tag=True)
        self.assertTrue(first and second)
        rows = self.db.execute("SELECT * FROM candidates WHERE canonical='a b'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["person_tag"])
        self.assertEqual(len(tool.generate_a_names()), 1333)
        self.assertEqual(len(set(map(str.casefold, tool.generate_a_names()))), 1333)

    def test_builder_json_categories_and_sources(self):
        path = Path(self.tmp.name) / "candidates.json"
        path.write_text(json.dumps({"metadata": {}, "candidates": [
            {"name": "Ada", "normalized": "ada", "categories": ["dictionary", "person"],
             "sources": ["SSA", "ESDB"], "frequency": 10}
        ]}), encoding="utf-8")
        good, bad = tool.import_json(self.db, path)
        self.assertEqual((good, bad), (1, 0))
        row = self.db.execute("SELECT * FROM candidates WHERE canonical='ada'").fetchone()
        self.assertTrue(row["person_tag"])
        self.assertTrue(row["word_tag"])
        self.assertIn("SSA", row["sources_json"])


if __name__ == "__main__":
    unittest.main()
