#!/usr/bin/env python
"""存量回填：旧提炼游标之后的 DB 消息按 MD 镜像格式回填 F1。缺省 dry-run。"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.md_mirror import append_record, format_message_record, F1_PATH

DEFAULT_DB = Path.home() / ".niu" / "messages.db"


def _existing_ids(path: str) -> set[str]:
    """目标 F1 已有 msg_id 集合（文件不存在返回空集；坏行跳过）。"""
    ids: set[str] = set()
    if not os.path.exists(path):
        return ids
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith('{"msg_id":'):
                try:
                    mid = json.loads(line)["msg_id"]
                except Exception:
                    continue
                if mid:
                    ids.add(mid)
    return ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--cursor", default="",
                    help="游标 JSON 文件（含 last_entity_extract_id）；缺省时须显式给 --from-id")
    ap.add_argument("--f1", default=F1_PATH)
    ap.add_argument("--from-id", default="", help="游标文件缺失时显式给起点消息 uuid")
    ap.add_argument("--confirm", action="store_true", help="真写（缺省 dry-run）")
    args = ap.parse_args(argv)

    if not args.cursor and not args.from_id:
        print("未给 --cursor：默认游标文件 last_entity_extract.json 已退役（工程四 T2 清算）。"
              "请显式给 --cursor 或 --from-id，本次不执行。", file=sys.stderr)
        return 2

    from_id = args.from_id
    if not from_id and Path(args.cursor).exists():
        from_id = json.loads(Path(args.cursor).read_text(encoding="utf-8")).get("last_entity_extract_id", "")
    if not from_id:
        print("缺少起点：游标文件不存在且未给 --from-id", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid FROM messages WHERE id = ?", (from_id,)).fetchone()
    if row is None:
        print(f"起点 {from_id} 不在 DB", file=sys.stderr)
        return 2
    rows = conn.execute(
        "SELECT id, role, content, tool_calls, tool_call_id, degraded_reason, created_at"
        " FROM messages WHERE rowid > ? ORDER BY rowid",
        (row["rowid"],),
    ).fetchall()

    existing = _existing_ids(args.f1)
    todo = [r for r in rows if r["id"] not in existing]
    skipped = len(rows) - len(todo)

    print(f"待回填 {len(todo)} 条（跳过已存在 {skipped}）；confirm={args.confirm}")
    if not args.confirm:
        for r in todo[:5]:
            print(f"  [{r['role']}] {r['id']} {(r['content'] or '')[:40]!r}")
        return 0

    ok = 0
    for r in todo:
        try:
            tool_calls = json.loads(r["tool_calls"] or "[]")
        except Exception:
            tool_calls = []
        block = format_message_record(
            msg_id=r["id"], created_at=r["created_at"] or "", role=r["role"] or "",
            content=r["content"] or "", tool_calls=tool_calls,
            tool_call_id=r["tool_call_id"] or "", degraded_reason=r["degraded_reason"] or "",
        )
        if block and append_record(block, args.f1):
            ok += 1
    print(f"回填完成 {ok}/{len(todo)}（跳过已存在 {skipped}）→ {args.f1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
