"""Task 1（存储层）：messages.db folded/output_pct 两列迁移 + output_pct 落库挂接。

覆盖 spec §3/§8/§9：
- 迁移加两列（幂等，各只出现一次）
- output_pct roundtrip + rowid 编号
- 旧行默认值（output_pct None / folded 0）
- 迁移失败降级（spec §8，R1-B P2）：init_db 不终止、标志 False、读写正常裁剪
"""

import aiosqlite

from agent.session import MessageStore


async def test_migration_adds_two_columns(tmp_path):
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    async with aiosqlite.connect(store.db_path) as db:
        cursor = await db.execute("PRAGMA table_info(messages)")
        cols = [r[1] for r in await cursor.fetchall()]
    assert "folded" in cols and "output_pct" in cols


async def test_output_pct_roundtrip(tmp_path):
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    await store.add_message(role="tool", content="x" * 1000, tool_call_id="tc1", output_pct=4.2)
    msgs = await store.get_messages()
    assert msgs[-1].output_pct == 4.2 and msgs[-1].folded == 0
    assert msgs[-1].rowid > 0


async def test_old_rows_default(tmp_path):
    # 手工建旧表（无两列）+ 插入一行 → init_db 迁移 → 旧行 output_pct None / folded 0
    db = str(tmp_path / "m.db")
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT, content TEXT,"
            " tool_calls TEXT, tool_results TEXT, tool_call_id TEXT, degraded_reason TEXT, created_at TEXT)")
        await conn.execute("INSERT INTO messages VALUES ('a','tool','x','[]','[]','','','2026-09-02')")
        await conn.commit()
    store = MessageStore(db)
    await store.init_db()
    msgs = await store.get_messages()
    assert msgs[0].output_pct is None and msgs[0].folded == 0


async def test_init_db_idempotent(tmp_path):
    # spec §9 迁移幂等（R2-B P3）：重复 init_db 不抛错，PRAGMA 两列各只出现一次
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    await store.init_db()  # 第二次不抛错
    async with aiosqlite.connect(store.db_path) as db:
        cursor = await db.execute("PRAGMA table_info(messages)")
        cols = [r[1] for r in await cursor.fetchall()]
    assert cols.count("folded") == 1 and cols.count("output_pct") == 1


async def test_migration_failure_degrades(tmp_path, monkeypatch):
    # spec §8（R1-B P2）：ALTER 失败 → 不终止启动、标志 False、INSERT/SELECT 裁剪新列照常工作
    import agent.session as session_mod

    store = MessageStore(str(tmp_path / "m.db"))
    orig_execute = aiosqlite.Connection.execute

    async def failing_execute(self, sql, *args):
        if "ADD COLUMN folded" in sql:
            raise aiosqlite.OperationalError("simulated ALTER failure")
        return await orig_execute(self, sql, *args)

    monkeypatch.setattr(aiosqlite.Connection, "execute", failing_execute)
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)

    await store.init_db()  # 不抛错（降级而非终止）
    assert session_mod._fold_columns_available is False

    # 写入正常（INSERT 裁剪新列）+ 读取正常（SELECT 裁剪新列，行映射容错默认值）
    await store.add_message(role="tool", content="x", output_pct=3.1)
    msgs = await store.get_messages()
    assert msgs[-1].folded == 0 and msgs[-1].output_pct is None


async def test_fold_columns_available_flag(tmp_path):
    from agent.session import fold_columns_available

    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    assert fold_columns_available() is True
