#!/usr/bin/env python3
"""messages.db 历史脏数据一次性清理：assistant 消息 tool_calls 里 function.name 的 server/ 斜杠前缀。

根因（2026-08-17 实证）：runner.py _assemble_tools_schema 曾把 ToolRegistry 带斜杠注册键
（如 brain-region-server/brain_region_activate）直接发给 LLM 作 function name，模型回传的
tool_calls.function.name 落库 messages.db——严格校验服务（OpenAI 规范 name 禁 /）重放历史
会话即 400。runner/adapter 修复后新数据干净，本脚本清理存量。

模式对齐 docs/superpowers/plans/2026-08-13-empty-assistant-cleanup.md Task 2：
- 备份（sqlite3 在线 backup API，WAL 安全）后才写库
- 禁 LIKE 判定（arguments 里文件路径含斜杠会假阳性）——逐行 JSON 解析 tool_calls
- 解析失败的行保守跳过并计数，绝不修改
- 只改 function.name 字段剥前缀（split('/', 1)[1]），id/tool_call_id/arguments 原样保留
- executemany 按 id 精确 UPDATE → 重读验证 → 不符即回滚不提交

用法：
    python3 scripts/cleanup_tool_name_prefix.py --dry-run          # 只读预览（默认 DB）
    python3 scripts/cleanup_tool_name_prefix.py                    # 正式清理（自动备份）
    python3 scripts/cleanup_tool_name_prefix.py --db /tmp/x.db     # 指定 DB
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".niu" / "messages.db"


def scan(conn: sqlite3.Connection):
    """逐行扫描 assistant 消息，返回 (命中列表, 跳过数)。

    命中条件：tool_calls JSON 解析成功 且 至少一个 call 的 function.name 含 '/'。
    解析失败/结构异常的行保守跳过（计数打印），绝不修改。
    """
    rows = list(conn.execute(
        "SELECT id, tool_calls FROM messages WHERE role='assistant' AND tool_calls IS NOT NULL AND tool_calls != ''"
    ))
    hits = []      # (id, parsed_tool_calls, slash_names)
    skipped = 0    # JSON 解析失败或结构异常
    for mid, tcs in rows:
        try:
            calls = json.loads(tcs)
            if not isinstance(calls, list):
                raise ValueError("not a list")
        except (json.JSONDecodeError, TypeError, ValueError):
            skipped += 1
            continue
        slash_names = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if isinstance(name, str) and "/" in name:
                slash_names.append(name)
        if slash_names:
            hits.append((mid, calls, slash_names))
    return rows, hits, skipped


def fix_calls(calls: list) -> list:
    """仅改 function.name 字段剥 server/ 前缀；其余字段（id/type/arguments）原样保留。"""
    for call in calls:
        fn = call.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and "/" in name:
                fn["name"] = name.split("/", 1)[1]
    return calls


def verify(conn: sqlite3.Connection, ids: list) -> list:
    """重读验证：每个已修 id 的 tool_calls 必须 JSON 合法且无残留斜杠名。返回错误列表。"""
    errors = []
    for mid in ids:
        tcs = conn.execute("SELECT tool_calls FROM messages WHERE id=?", (mid,)).fetchone()[0]
        try:
            calls = json.loads(tcs)
        except json.JSONDecodeError as e:
            errors.append(f"{mid}: 修复后 JSON 不合法: {e}")
            continue
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str) and "/" in name:
                errors.append(f"{mid}: 残留斜杠名 {name}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"messages.db 路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--dry-run", action="store_true", help="只读预览：不备份、不写库")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"错误: DB 不存在: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    rows, hits, skipped = scan(conn)
    total = len(rows)

    print(f"扫描 assistant 消息（tool_calls 非空）: {total} 条")
    print(f"命中（function.name 含 /）: {len(hits)} 条")
    print(f"跳过（JSON 解析失败/结构异常，保守不改）: {skipped} 条")

    if hits:
        all_names = sorted({n for _, _, names in hits for n in names})
        print(f"命中行工具名清单: {all_names}")
        for mid, _, names in hits[:10]:
            print(f"  {mid}: {names}")
        if len(hits) > 10:
            print(f"  ... 其余 {len(hits) - 10} 条略")

    if args.dry_run:
        print(f"[dry-run] 不写库。正式运行将修复 {len(hits)} 条。")
        conn.close()
        return 0

    if not hits:
        print("无命中，无需修复。")
        conn.close()
        return 0

    # 备份（sqlite3 在线 backup API，WAL 安全）——对齐 2026-08-13 先例
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = db_path.with_name(f"{db_path.name}.bak-toolname-{ts}")
    bak = sqlite3.connect(str(bak_path))
    conn.backup(bak)
    bak.close()
    print(f"备份完成: {bak_path}")

    # 修复：executemany 按 id 精确 UPDATE
    updates = []
    for mid, calls, _ in hits:
        fixed = fix_calls(calls)
        updates.append((json.dumps(fixed, ensure_ascii=False), mid))
    conn.executemany("UPDATE messages SET tool_calls=? WHERE id=?", updates)

    # 重读验证：计数 + 每行 JSON 合法 + 无残留斜杠名——不符即回滚不提交
    ids = [mid for mid, _, _ in hits]
    errors = verify(conn, ids)
    fixed_count = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchone()[0]
    if errors or fixed_count != len(ids):
        conn.rollback()
        print(f"验证失败（更新行数 {fixed_count}/{len(ids)}，错误 {len(errors)} 条），已回滚不提交:")
        for e in errors[:20]:
            print(f"  {e}")
        conn.close()
        return 1

    conn.commit()
    conn.close()
    print(f"修复完成: {len(ids)} 条（重读验证通过：JSON 合法、无残留斜杠名）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
