"""工程二 Task2：对齐扫描单测。FakeStore 零真实 DB。"""

import re

import pytest

from niu_api.md_alignment import align_f1_with_store


class FakeMsg:
    def __init__(self, id: str, role: str, content: str):
        self.id = id; self.role = role; self.content = content


class FakeStore:
    def __init__(self, msgs):
        self.messages = msgs

    async def get_messages(self):
        return list(self.messages)


def _records_ids(path):
    import os
    if not os.path.exists(path):
        return []
    return re.findall(r'"msg_id":\s*"([^"]+)"', open(path, encoding="utf-8").read())


@pytest.mark.asyncio
async def test_gap_backfilled_in_order(tmp_path):
    from agent.md_mirror import append_record, format_message_record
    msgs = [FakeMsg(f"id{i}", "user", f"内容{i}") for i in range(5)]
    store = FakeStore(msgs)
    f1 = tmp_path / "f1.md"
    for m in msgs[:3]:
        append_record(format_message_record(msg_id=m.id, created_at="t", role=m.role, content=m.content), str(f1))
    assert await align_f1_with_store(store, str(f1)) == 2
    assert _records_ids(f1) == [f"id{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_no_gap_returns_zero(tmp_path):
    from agent.md_mirror import append_record, format_message_record
    msgs = [FakeMsg(f"id{i}", "user", f"c{i}") for i in range(3)]
    store = FakeStore(msgs)
    f1 = tmp_path / "f1.md"
    for m in msgs:
        append_record(format_message_record(msg_id=m.id, created_at="t", role=m.role, content=m.content), str(f1))
    assert await align_f1_with_store(store, str(f1)) == 0


@pytest.mark.asyncio
async def test_empty_f1_bounded_backfill(tmp_path):
    msgs = [FakeMsg(f"id{i}", "user", f"c{i}") for i in range(10)]
    store = FakeStore(msgs)
    f1 = tmp_path / "f1.md"
    assert await align_f1_with_store(store, str(f1), max_backfill=4) == 4
    assert _records_ids(f1) == [f"id{i}" for i in range(6, 10)]


@pytest.mark.asyncio
async def test_ghost_tail_id_no_op(tmp_path):
    from agent.md_mirror import append_record, format_message_record
    store = FakeStore([FakeMsg("db1", "user", "hello")])
    f1 = tmp_path / "f1.md"
    append_record(format_message_record(msg_id="ghost", created_at="t", role="user", content="x"), str(f1))
    assert await align_f1_with_store(store, str(f1)) == 0
