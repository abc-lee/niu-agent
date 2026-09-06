"""visible_only（可见过滤下沉 SQL）行为测试——spec 2026-09-06 历史滚动分页修复 AC1-AC8。

背景：/api/context/messages 原为取 limit 条原始消息后 Python 层过滤，tool 密集段
整页滤后为空 → 前端误判"没有更多历史"。过滤下沉 SQL 后 limit 语义 = 可见消息条数。
"""

import os
import sqlite3

import aiosqlite
import pytest

from agent.session import MessageStore, _safe_json

REAL_DB = os.path.expanduser("~/.niu/messages.db")
# 锚点：2026-09-06 10:59"智慧民生项目总结"assistant 消息（spec 诊断时 rowid）；
# 其前 rowid 3250-3343 为 tool 密集段（tool 49 + 空 assistant 占位 44 + user 1），
# 更早 10:11 hn 对话在 rowid 3204-3215。
ANCHOR_ROWID = 3344


async def _store(tmp_path) -> MessageStore:
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    return store


async def _insert_group(store: MessageStore, i: int, md_path: str):
    """一组混合消息：user + 有 content 的 assistant（可见）/ tool + 空 assistant 占位（不可见）。"""
    await store.add_message(role="user", content=f"q{i}", md_path=md_path)
    await store.add_message(role="assistant", content=f"a{i}", md_path=md_path)
    await store.add_message(
        role="tool", content=f"t{i}", tool_call_id=f"tc{i}", md_path=md_path
    )
    await store.add_message(
        role="assistant",
        content="",
        tool_calls=[{"id": f"tc{i}", "name": "x", "args": {}}],
        md_path=md_path,
    )


def _is_visible_python(msg) -> bool:
    """端点原 Python 层过滤条件（改前行为基准）。"""
    return msg.role != "tool" and not (
        msg.role == "assistant" and not (msg.content or "").strip() and msg.tool_calls
    )


async def test_ac1_exactly_limit_visible_ascending_no_tool(tmp_path):
    """AC1：恰 limit 条可见、正序、无 tool/空 assistant 占位（limit 语义 = 可见条数）"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(11):  # 44 行，22 可见
        await _insert_group(store, i, md)

    msgs = await store.get_messages(20, visible_only=True)
    assert len(msgs) == 20
    assert all(m.role != "tool" for m in msgs)
    assert all(_is_visible_python(m) for m in msgs)
    # 正序（rowid 升序）；恰好是最新的 20 条可见——最早一组的 2 条可见被 limit 切掉
    assert [m.rowid for m in msgs] == sorted(m.rowid for m in msgs)
    assert msgs[0].content == "q1" and msgs[-1].content == "a10"


async def test_ac2_default_includes_tool_unchanged(tmp_path):
    """AC2：默认 visible_only=False 含 tool，与改前一致（取 limit 条原始行）"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(11):
        await _insert_group(store, i, md)

    raw = await store.get_messages(20)
    assert len(raw) == 20
    assert any(m.role == "tool" for m in raw)
    # 默认路径 = 最新 20 行（不论 role），正序——与全量尾部逐条一致
    all_rows = await store.get_messages()
    assert [m.id for m in raw] == [m.id for m in all_rows[-20:]]


async def test_ac3_pagination_no_overlap_no_gap(tmp_path):
    """AC3：before_id 连续翻页无重无漏（可见集分区），非末页恰 limit"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(6):  # 24 行，12 可见
        await _insert_group(store, i, md)

    full = await store.get_messages(None, visible_only=True)
    assert len(full) == 12

    pages, before_id = [], None
    while True:
        page = await store.get_messages(5, before_id, visible_only=True)
        if not page:
            break
        pages.append(page)
        before_id = page[0].id
        assert len(pages) < 20, "翻页未收敛"

    # 页序 = 最新在前（前端 prepend 语义）：倒序拼接各页 = 全量正序可见集（无重无漏）
    collected = [m for p in reversed(pages) for m in p]
    # 与全量逐位相等即无重无漏（full 内 id 唯一）
    assert [m.id for m in collected] == [m.id for m in full]
    page_lens = [len(p) for p in pages]
    assert page_lens[:-1] == [5, 5], f"非末页应恰 5 条: {page_lens}"


def _anchor_id():
    """真库锚点消息 id；不存在（无库 / /new 清库）返回 None → skip"""
    if not os.path.exists(REAL_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT id FROM messages WHERE rowid = ?", (ANCHOR_ROWID,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


async def test_ac4_real_db_crosses_tool_dense_segment():
    """AC4：真库回归——rowid<3344 起翻 20 可见，越过 tool 密集段达 10:11 hn；非末页恰 20

    改前此页 0 条可见 → 前端置 oldestMessageId=null 永久停止加载。
    """
    anchor = _anchor_id()
    if anchor is None:
        pytest.skip(f"真库 {REAL_DB} 缺失或锚点 rowid={ANCHOR_ROWID} 不存在（可能已被 /new 清库）")

    # 只读使用：仅 get_messages SELECT，不调 init_db（避免对生产库写 WAL）
    store = MessageStore(REAL_DB)
    page = await store.get_messages(20, anchor, visible_only=True)
    assert len(page) == 20, "非末页应恰 20 条可见（改前为 0——整页滤后空）"
    # 越过 tool 密集段（rowid 3250-3343），到达 10:11 hn 对话（rowid 3204-3215，created_at 10:1x）
    assert any(m.created_at.startswith("2026-09-06T10:1") for m in page)

    # 继续翻页：非末页恰 20、无空页
    before_id = page[0].id
    for _ in range(2):
        nxt = await store.get_messages(20, before_id, visible_only=True)
        assert len(nxt) == 20, "越过密集段后仍应满页"
        before_id = nxt[0].id


async def test_ac5_missing_before_id_falls_back(tmp_path):
    """AC5：before_id 不存在（并发 /new 清库）→ fallback 取最新 limit 条可见"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(4):  # 8 可见
        await _insert_group(store, i, md)

    fb = await store.get_messages(3, "no-such-id", visible_only=True)
    latest = await store.get_messages(3, None, visible_only=True)
    assert [m.id for m in fb] == [m.id for m in latest]
    # 最新 3 条可见 = a2, q3, a3（正序）
    assert [m.content for m in fb] == ["a2", "q3", "a3"]


async def test_ac7_limit_none_all_visible(tmp_path):
    """AC7：limit=None + visible_only → 全部可见消息（正序）"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(6):  # 24 行，12 可见
        await _insert_group(store, i, md)

    msgs = await store.get_messages(None, visible_only=True)
    assert len(msgs) == 12
    assert all(m.role != "tool" for m in msgs)
    assert all(_is_visible_python(m) for m in msgs)
    assert [m.rowid for m in msgs] == sorted(m.rowid for m in msgs)


async def test_ac8_empty_db_returns_empty(tmp_path):
    """AC8：空库 → []（前端正确停止加载，既有行为不回归）"""
    store = await _store(tmp_path)
    assert await store.get_messages(10, visible_only=True) == []


async def test_ac8_all_tool_db_returns_empty(tmp_path):
    """AC8：全 tool/空 assistant 占位库 → []"""
    store = await _store(tmp_path)
    md = str(tmp_path / "f1.md")
    for i in range(5):
        await store.add_message(role="tool", content=f"t{i}", tool_call_id=f"tc{i}", md_path=md)
        await store.add_message(
            role="assistant", content="", tool_calls=[{"id": f"tc{i}"}], md_path=md
        )
    assert await store.get_messages(10, visible_only=True) == []


async def test_tool_calls_shapes_equivalent_to_python_filter(tmp_path):
    """tool_calls 列各形态 × 空 content：SQL 谓词与端点原 Python 过滤行为等价

    覆盖写路径/旧行可产生的形态：NULL / '' / 'null' / '[]' / 合法非空 JSON。
    """
    store = await _store(tmp_path)
    shapes = [
        # (tag, role, content, tool_calls_raw)
        ("tc_null", "assistant", "", "null"),
        ("tc_empty_str", "assistant", "", ""),
        ("content_null_brackets", "assistant", None, "[]"),
        ("tc_nonempty_hidden", "assistant", "", '[{"id": "x"}]'),
        ("whitespace_content_hidden", "assistant", "   ", '[{"id": "y"}]'),
        ("user_empty_visible", "user", "", "[]"),
        ("assistant_plain_visible", "assistant", "hi", None),
    ]
    async with aiosqlite.connect(store.db_path) as db:
        for tag, role, content, tc_raw in shapes:
            await db.execute(
                "INSERT INTO messages (id, role, content, tool_calls, tool_results,"
                " tool_call_id, degraded_reason, created_at, folded, output_pct)"
                " VALUES (?, ?, ?, ?, '[]', '', '', '2026-09-06T10:00:00', 0, NULL)",
                (tag, role, content, tc_raw),
            )
        await db.commit()

    msgs = await store.get_messages(None, visible_only=True)
    got = {m.id for m in msgs}
    # 基准 = 端点原 Python 过滤条件（_safe_json 为生产读取助手）
    expected = {
        tag
        for tag, role, content, tc_raw in shapes
        if role != "tool"
        and not (role == "assistant" and not (content or "").strip() and _safe_json(tc_raw))
    }
    assert got == expected
