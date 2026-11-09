"""Merge a completed bounded Hiscores database into the resumable main DB."""
import json
import sqlite3
import sys
from pathlib import Path


def merge(main_path: Path, source_path: Path) -> dict[str, int]:
    target = sqlite3.connect(main_path)
    target.row_factory = sqlite3.Row
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    counts = {"candidates_added": 0, "candidates_merged": 0, "checks_added_or_updated": 0, "provider_states_updated": 0}
    try:
        for row in source.execute("SELECT * FROM candidates"):
            old = target.execute("SELECT * FROM candidates WHERE canonical=?", (row["canonical"],)).fetchone()
            if old is None:
                target.execute("INSERT INTO candidates(canonical,display_name,char_length,person_tag,word_tag,sources_json,generated,imported_at) VALUES(?,?,?,?,?,?,?,?)",
                               tuple(row[k] for k in ("canonical", "display_name", "char_length", "person_tag", "word_tag", "sources_json", "generated", "imported_at")))
                counts["candidates_added"] += 1
            else:
                sources = set(json.loads(old["sources_json"])) | set(json.loads(row["sources_json"]))
                target.execute("UPDATE candidates SET person_tag=?,word_tag=?,sources_json=?,generated=? WHERE canonical=?",
                               (max(old["person_tag"], row["person_tag"]), max(old["word_tag"], row["word_tag"]),
                                json.dumps(sorted(sources)), max(old["generated"], row["generated"]), row["canonical"]))
                counts["candidates_merged"] += 1
        for row in source.execute("SELECT * FROM checks"):
            old = target.execute("SELECT checked_at FROM checks WHERE canonical=? AND game=?", (row["canonical"], row["game"])).fetchone()
            if old is None or row["checked_at"] > old["checked_at"]:
                target.execute("""INSERT INTO checks(canonical,game,result,http_status,content_type,schema_valid,overall_level,checked_at,attempts,error)
                  VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical,game) DO UPDATE SET
                  result=excluded.result,http_status=excluded.http_status,content_type=excluded.content_type,
                  schema_valid=excluded.schema_valid,overall_level=excluded.overall_level,checked_at=excluded.checked_at,
                  attempts=excluded.attempts,error=excluded.error""",
                  tuple(row[k] for k in ("canonical", "game", "result", "http_status", "content_type", "schema_valid", "overall_level", "checked_at", "attempts", "error")))
                counts["checks_added_or_updated"] += 1
        for row in source.execute("SELECT * FROM provider_state"):
            old = target.execute("SELECT stopped_at FROM provider_state WHERE game=?", (row["game"],)).fetchone()
            if old is None or (row["stopped_at"] or "") > (old["stopped_at"] or ""):
                target.execute("""INSERT INTO provider_state(game,stopped,stopped_at,retry_after_utc,reason) VALUES(?,?,?,?,?)
                  ON CONFLICT(game) DO UPDATE SET stopped=excluded.stopped,stopped_at=excluded.stopped_at,
                  retry_after_utc=excluded.retry_after_utc,reason=excluded.reason""",
                  tuple(row[k] for k in ("game", "stopped", "stopped_at", "retry_after_utc", "reason")))
                counts["provider_states_updated"] += 1
        target.commit()
        return counts
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    print(json.dumps(merge(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
