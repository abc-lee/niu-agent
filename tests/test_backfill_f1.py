"""工程二 Task3：回填脚本单测（tmp sqlite）。"""

import json
import os
import sqlite3

from scripts.backfill_f1_from_db import main


def _make_db(tmp_path, n=5):
    db = tmp_path / "m.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT, content TEXT,"
        " tool_calls TEXT DEFAULT '[]', tool_results TEXT DEFAULT '[]',"
        " tool_call_id TEXT DEFAULT '', degraded_reason TEXT DEFAULT '', created_at TEXT)"
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            (f"id{i}", "user", f"内容{i}", "[]", "[]", "", "", f"2026-08-24T10:0{i}"),
        )
    conn.commit(); conn.close()
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_entity_extract_id": "id1"}), encoding="utf-8")
    return str(db), str(cursor), str(tmp_path / "f1.md")


class TestBackfill:
    def test_dry_run_writes_nothing(self, tmp_path):
        db, cur, f1 = _make_db(tmp_path)
        assert main(["--db", db, "--cursor", cur, "--f1", f1]) == 0
        assert not os.path.exists(f1)

    def test_confirm_backfills_after_cursor(self, tmp_path):
        db, cur, f1 = _make_db(tmp_path)
        assert main(["--db", db, "--cursor", cur, "--f1", f1, "--confirm"]) == 0
        ids = __import__("re").findall(r'"msg_id":\s*"([^"]+)"', open(f1, encoding="utf-8").read())
        assert ids == ["id2", "id3", "id4"]

    def test_confirm_skips_existing_records(self, tmp_path):
        from agent.md_mirror import append_record, format_message_record
        db, cur, f1 = _make_db(tmp_path)
        for mid in ("id2", "id3"):
            append_record(format_message_record(msg_id=mid, created_at="t", role="user", content=f"内容{mid[-1]}"), f1)
        assert main(["--db", db, "--cursor", cur, "--f1", f1, "--confirm"]) == 0
        ids = __import__("re").findall(r'"msg_id":\s*"([^"]+)"', open(f1, encoding="utf-8").read())
        assert ids == ["id2", "id3", "id4"]

    def test_missing_cursor_requires_from_id(self, tmp_path):
        db, _, f1 = _make_db(tmp_path)
        assert main(["--db", db, "--f1", f1, "--confirm"]) == 2

    def test_from_id_explicit_start(self, tmp_path):
        db, _, f1 = _make_db(tmp_path)
        assert main(["--db", db, "--f1", f1, "--from-id", "id0", "--confirm"]) == 0
        ids = __import__("re").findall(r'"msg_id":\s*"([^"]+)"', open(f1, encoding="utf-8").read())
        assert ids == ["id1", "id2", "id3", "id4"]

    def test_from_id_not_in_db_returns_2(self, tmp_path):
        db, _, f1 = _make_db(tmp_path)
        assert main(["--db", db, "--f1", f1, "--from-id", "ghost", "--confirm"]) == 2
