"""Release morning invariants for the local name queue."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hiscores_tool as core
from name_service import NameService


class NameServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db_path = Path(self.tmp.name) / "names.sqlite3"
        self.service = NameService(self.db_path)
        with self.service.db() as db:
            db.execute("UPDATE ui_batch SET prefixes=?", (json.dumps(["A"]),))
            for name in ("A", "Ace", "Able", "A B", "Ash"):
                core.merge_candidate(db, name, "test", person_tag=name in ("Ace", "Able"))
            for name in ("Ace", "Able", "A B", "Ash"):
                key = core.canonicalize(name)
                for game in ("osrs", "rs3"):
                    db.execute("""INSERT INTO checks(canonical,game,result,http_status,schema_valid,checked_at,attempts)
                                  VALUES(?,?,'not_listed',404,0,?,1)""", (key, game, core.utc_now()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unavailable_persists_and_oneoff_keeps_queue(self):
        svc = self.service
        svc.runner_start("brave-1")
        first = svc.runner_next("brave-1")
        repeat = svc.runner_next("brave-1")
        self.assertEqual(first["lease_id"], repeat["lease_id"])
        self.assertEqual(first["candidate"], repeat["candidate"])
        with self.assertRaises(ValueError):
            svc.runner_start("brave-2")
        queued = svc.runner_oneoff("brave-1", name="Ash")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(svc.runner_next("brave-1")["lease_id"], first["lease_id"])
        verdict = {"runner_id": "brave-1", "candidate_id": first["candidate"]["id"],
                   "lease_id": first["lease_id"], "verdict": "unavailable"}
        self.assertFalse(svc.runner_verdict(verdict)["duplicate"])
        self.assertTrue(svc.runner_verdict(verdict)["duplicate"])
        self.assertNotIn(first["candidate"]["id"], [x["id"] for x in svc.state()["shortlist"]])
        self.assertEqual(svc.runner_next("brave-1")["candidate"]["id"], "ash")
        self.assertIn(first["candidate"]["id"], [x["id"] for x in NameService(self.db_path).state()["history"]])

    def test_direct_oneoff_defers_interrupted_lease(self):
        svc = self.service
        svc.runner_start("brave-1")
        interrupted = svc.runner_next("brave-1")
        svc.runner_action("pause")
        oneoff = svc.runner_oneoff("brave-1", candidate_id="Ash", defer_pending=True)
        self.assertEqual(oneoff["candidate"]["id"], "ash")
        with self.assertRaises(ValueError):
            svc.runner_verdict({"runner_id":"brave-1", "candidate_id":interrupted["candidate"]["id"],
                                "lease_id":interrupted["lease_id"], "verdict":"unavailable"})
        svc.runner_verdict({"runner_id":"brave-1", "candidate_id":"ash", "lease_id":oneoff["lease_id"],
                            "verdict":"unavailable"})
        self.assertEqual(svc.runner_next("brave-1")["candidate"]["id"], interrupted["candidate"]["id"])

    def test_available_pauses_and_skipped_can_be_restored(self):
        svc = self.service
        svc.set_candidate("Ace", "skipped", True)
        self.assertIn("ace", [x["id"] for x in svc.state()["skipped"]])
        svc.set_candidate("Ace", "skipped", False)
        self.assertIn("ace", [x["id"] for x in svc.state()["shortlist"]])
        svc.runner_start("brave-1")
        job = svc.runner_next("brave-1")
        svc.runner_verdict({"runner_id": "brave-1", "candidate_id": job["candidate"]["id"],
                            "lease_id": job["lease_id"], "verdict": "available"})
        self.assertEqual(svc.state()["runner"]["status"], "paused")
        self.assertEqual(svc.runner_next("brave-1")["status"], "paused")

    def test_pause_and_restart_do_not_reset_session_limits(self):
        svc = self.service
        svc.runner_start("brave-1")
        with svc.db() as db:
            db.execute("UPDATE ui_runner SET session_start=session_start-20,remaining_seconds=300,remaining_attempts=80 WHERE id=1")
        svc.runner_action("pause")
        self.assertLess(svc.state()["runner"]["remaining_seconds"], 281)
        svc.runner_action("stop")
        self.assertEqual(svc.runner_start("brave-1")["status"], "running")
        self.assertLess(svc.state()["runner"]["remaining_seconds"], 281)
        self.assertEqual(svc.state()["runner"]["remaining_attempts"], 80)

    def test_failed_public_refresh_keeps_prior_shortlist(self):
        def failed_check(db, cid, game, **_):
            db.execute("UPDATE checks SET result='unknown',http_status=500 WHERE canonical=? AND game=?", (cid, game))
            db.commit()
            return {"result": "unknown", "game": game, "checked_at": core.utc_now(), "error": "HTTP 500"}

        with patch("name_service.core.check_candidate", side_effect=failed_check):
            self.assertEqual(self.service.lookup("Ace")["result"], "unknown")
        self.assertIn("ace", [x["id"] for x in self.service.state()["shortlist"]])

    def test_common_word_filter_and_length_tiers(self):
        names = ("B", "B0", "Bad", "Ben", "Bear", "Beth", "Beach", "Basket", "Beautiful", "Balthazarian",
                 "Bibb", "Bhai", "Bema", "Bdrm", "Bawd", "Bari")
        with self.service.db() as db:
            db.execute("UPDATE ui_batch SET prefixes=?", (json.dumps(["B"]),))
            for name in names:
                key = core.canonicalize(name)
                core.merge_candidate(db, name, "test", person_tag=name in ("Ben", "Beth", "Balthazarian"),
                                     word_tag=len(name) >= 3)
                for game in ("osrs", "rs3"):
                    db.execute("""INSERT INTO checks(canonical,game,result,http_status,schema_valid,checked_at,attempts)
                                  VALUES(?,?,'not_listed',404,0,?,1)""", (key, game, core.utc_now()))
        rows = self.service.state()["shortlist"]
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, ["b", "b0", "ben", "bad", "beth", "bear", "beach", "basket", "beautiful", "balthazarian"])
        with self.service.db() as db:
            scan_ids = [row["canonical"] for row in self.service._eligible_rows(db, require_verified=False)]
        self.assertEqual(scan_ids, ids)

    def test_ranking_migration_keeps_current_lease_and_removes_junk_queue(self):
        svc = self.service
        with svc.db() as db:
            db.execute("UPDATE ui_batch SET prefixes=?", (json.dumps(["B"]),))
            for name in ("B0", "Bad", "Bibb"):
                key = core.canonicalize(name)
                core.merge_candidate(db, name, "test", word_tag=len(name) >= 3)
                for game in ("osrs", "rs3"):
                    db.execute("""INSERT INTO checks(canonical,game,result,http_status,schema_valid,checked_at,attempts)
                                  VALUES(?,?,'not_listed',404,0,?,1)""", (key, game, core.utc_now()))
            db.execute("UPDATE ui_runner SET runner_id='brave-1',status='paused',current_id='b0',lease_id='lease-old',lease_name='B0' WHERE id=1")
            db.execute("INSERT INTO ui_runner_queue VALUES('brave-1',0,'bibb')")
            db.execute("INSERT INTO ui_runner_queue VALUES('brave-1',1,'b0')")
            db.execute("INSERT INTO ui_runner_queue VALUES('brave-1',2,'bad')")
            db.execute("UPDATE ui_meta SET value='1' WHERE key='ranking_version'")
        migrated = NameService(self.db_path)
        with migrated.db() as db:
            queue = [r[0] for r in db.execute("SELECT candidate_id FROM ui_runner_queue WHERE runner_id='brave-1' ORDER BY seq")]
            runner = db.execute("SELECT current_id,lease_id FROM ui_runner WHERE id=1").fetchone()
        self.assertEqual(queue, ["b0", "bad"])
        self.assertEqual(tuple(runner), ("b0", "lease-old"))

    def test_stale_running_scan_is_marked_interrupted(self):
        with self.service.db() as db:
            db.execute("UPDATE ui_scan SET status='running',checked=8,total=20 WHERE id=1")
        restarted = NameService(self.db_path)
        scan = restarted.state()["scan"]
        self.assertEqual(scan["status"], "interrupted")
        self.assertEqual(scan["checked"], 8)

    def test_expired_runner_can_start_new_browser_session(self):
        svc = self.service
        svc.runner_start("old-browser-session")
        with svc.db() as db:
            db.execute("UPDATE ui_runner SET status='paused',session_start=session_start-301 WHERE id=1")
        self.assertEqual(svc.runner_start("old-browser-session")["status"], "expired")
        self.assertEqual(svc.runner_start("new-browser-session")["status"], "running")


if __name__ == "__main__":
    unittest.main()
