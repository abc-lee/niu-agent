"""Task 2 验收：组装器替换 get_context_for_chat。

覆盖（计划 §Task 2 测试清单）：
1. 无块全新：全量进窗口、无索引消息、与旧实现基准快照逐消息 deep-equal
2. 有块场景：预置块+超预算消息集 → 索引行正确、窗口从单元边界开始、tool 配对完整
3. 预算截断：最老单元出窗且成块；重复组装幂等（块数不翻倍）
4. 前缀稳定：追加一条消息重新组装，后视图以先视图为前缀（序列化 startswith）

mock store / TokenCalculator，禁真实 LLM。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.context_assembler.blocks import load_all
from agent.context_manager import ContextManager
from agent.session import Message


# ---------------------------------------------------------------------------
# 测试基建：FakeStore + 确定性 token 计数
# ---------------------------------------------------------------------------

class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages: list[Message]):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(self, messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。"""
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _msg(idx, role, content, created="2026-08-12T10:00:00"):
    return Message(
        id=f"m{idx:03d}",
        role=role,
        content=content,
        tool_calls=[],
        tool_call_id="",
        created_at=created,
        rowid=idx + 1,
    )


def _unit(start_idx, q, a, created_q, created_a):
    """一个最小会话单元：user → assistant。"""
    return [
        _msg(start_idx, "user", q, created_q),
        _msg(start_idx + 1, "assistant", a, created_a),
    ]


@pytest.fixture
def cm_factory(tmp_path, monkeypatch):
    """构造注入了临时块 DB 与确定性 token 计数的 ContextManager 工厂。"""
    monkeypatch.setattr(ContextManager, "count_tokens_simple", _fake_count_tokens)

    def _make(store, max_tokens=200):
        return ContextManager(
            store,
            max_tokens=max_tokens,
            blocks_db_path=tmp_path / "context_blocks.db",
        )

    return _make


# ---------------------------------------------------------------------------
# 1. 无块全新：deep-equal 旧实现基准快照
# ---------------------------------------------------------------------------

class TestFreshNoBlocks:
    async def test_full_window_no_index_and_matches_legacy_snapshot(self, cm_factory):
        # 两轮极小对话（远小于任何预算），exclude_last=True 排除最后一条
        msgs = [
            _msg(0, "user", "你好"),
            _msg(1, "assistant", "你好！有什么可以帮你？"),
            _msg(2, "user", "今天天气如何"),
            _msg(3, "assistant", "今天晴天。"),
        ]
        cm = cm_factory(FakeStore(msgs))

        view = await cm.get_context_for_chat(exclude_last=True)

        # 旧实现基准快照（硬编码）：load_history 转换 + 去掉最后一条，无任何前导
        legacy_snapshot = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你？"},
            {"role": "user", "content": "今天天气如何"},
        ]
        assert view == legacy_snapshot

    async def test_fresh_produces_no_blocks_and_no_index(self, cm_factory, tmp_path):
        msgs = [m for k, (q, a) in enumerate([("Q0", "A0"), ("Q1", "A1")])
                for m in _unit(k * 2, q, a, "2026-08-12T09:00:00", "2026-08-12T09:01:00")]
        cm = cm_factory(FakeStore(msgs), max_tokens=10_000)

        view = await cm.get_context_for_chat(exclude_last=False)

        assert len(view) == 4  # 全量在窗内
        assert all("[历史索引]" not in m["content"] for m in view)
        assert load_all(tmp_path / "context_blocks.db") == []  # 无挤出 → 不产块

    async def test_tool_fields_preserved_in_window(self, cm_factory):
        msgs = [
            _msg(0, "user", "查一下"),
            _msg(1, "assistant", ""),
            _msg(2, "tool", '{"r": 1}'),
            _msg(3, "assistant", "结果是 1"),
        ]
        msgs[1].tool_calls = [{"id": "c1", "type": "function",
                               "function": {"name": "t", "arguments": "{}"}}]
        msgs[2].tool_call_id = "c1"
        cm = cm_factory(FakeStore(msgs))

        view = await cm.get_context_for_chat(exclude_last=False)

        assert view[1]["tool_calls"][0]["id"] == "c1"
        assert view[2]["tool_call_id"] == "c1"


# ---------------------------------------------------------------------------
# 2+3. 有块场景 / 预算截断 / 幂等
# ---------------------------------------------------------------------------

def _three_unit_msgs():
    """三个会话单元，每条约 76 tokens（30 字 content × 2 条 + 开销）；
    第二个单元含完整 tool 配对链。预算 100 时窗口只装最新单元。"""
    msgs = []
    idx = 0
    for day, (q, a) in enumerate(
        [("Q" * 30, "A" * 30), ("R" * 30, None), ("S" * 30, "T" * 30)], start=12
    ):
        created = f"2026-08-{day:02d}T10:00:00"
        msgs.append(_msg(idx, "user", q, created)); idx += 1
        if a is not None:
            msgs.append(_msg(idx, "assistant", a, created)); idx += 1
        else:
            assistant = _msg(idx, "assistant", "", created); idx += 1
            assistant.tool_calls = [{"id": "call_x", "type": "function",
                                     "function": {"name": "t", "arguments": "{}"}}]
            msgs.append(assistant)
            tool = _msg(idx, "tool", "Z" * 30, created); idx += 1
            tool.tool_call_id = "call_x"
            msgs.append(tool)
    return msgs


class TestBlockArchiveAndTruncation:
    async def test_oldest_units_archived_index_correct_window_at_unit_boundary(
        self, cm_factory, tmp_path
    ):
        db = tmp_path / "context_blocks.db"
        cm = cm_factory(FakeStore(_three_unit_msgs()), max_tokens=200)  # 预算 100

        view = await cm.get_context_for_chat(exclude_last=False)

        # 窗口 = 最新单元（U3）整体；前两个单元出窗
        window = [m for m in view if "[历史索引]" not in m["content"]]
        assert window[0]["role"] == "user"
        assert window[0]["content"] == "S" * 30
        assert len(window) == 2

        # 块归档：两个出窗单元各一块
        blocks = load_all(db)
        assert [b.id for b in blocks] == [1, 2]
        assert [b.count for b in blocks] == [2, 3]

        # 索引行格式（机械成分：块号/时间范围/条数/首问）
        index_text = view[0]["content"]
        lines = index_text.split("\n")
        assert lines[0] == "[历史索引]"
        assert lines[1] == "共 2 块早期对话已归档，可用 read_history_block 工具按块号取回原文。"
        assert lines[2] == '[块#1] 08-12~08-12 · 2条 · 首问:"' + "Q" * 30 + '"'
        assert lines[3].startswith('[块#2] 08-13~08-13 · 3条')

    async def test_tool_pairing_complete_across_boundary(self, cm_factory, tmp_path):
        cm = cm_factory(FakeStore(_three_unit_msgs()), max_tokens=200)

        view = await cm.get_context_for_chat(exclude_last=False)

        # 窗口起点恒为单元边界 → 窗内不出现孤立 tool/tool_calls
        window = [m for m in view if "[历史索引]" not in m["content"]]
        for i, m in enumerate(window):
            if m["role"] == "tool":
                assert any(
                    p.get("tool_calls") and p["role"] == "assistant"
                    for p in window[:i]
                ), "orphaned tool message in window"

    async def test_reassembly_is_idempotent_blocks_not_duplicated(
        self, cm_factory, tmp_path
    ):
        db = tmp_path / "context_blocks.db"
        store = FakeStore(_three_unit_msgs())
        cm = cm_factory(store, max_tokens=200)

        first = await cm.get_context_for_chat(exclude_last=False)
        second = await cm.get_context_for_chat(exclude_last=False)

        assert load_all(db) == load_all(db)  # 同库读稳定
        blocks_after_first = load_all(db)
        assert len(blocks_after_first) == 2  # 块数不翻倍
        assert first == second  # 视图完全一致


# ---------------------------------------------------------------------------
# 4. 前缀稳定（prompt cache 守卫）
# ---------------------------------------------------------------------------

class TestPrefixStability:
    async def test_appended_message_keeps_prefix(self, cm_factory):
        store = FakeStore(_three_unit_msgs())
        cm = cm_factory(store, max_tokens=200)

        v1 = await cm.get_context_for_chat(exclude_last=False)

        # 追加一条新消息（开启新单元），重新组装
        store.messages.append(_msg(99, "user", "new question", "2026-08-25T12:00:00"))
        v2 = await cm.get_context_for_chat(exclude_last=False)

        s1 = json.dumps(v1, ensure_ascii=False)
        s2 = json.dumps(v2, ensure_ascii=False)
        assert s2.startswith(s1[:-1]), "追加消息后视图必须以先视图为前缀"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
