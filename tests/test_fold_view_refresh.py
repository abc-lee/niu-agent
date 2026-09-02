"""assemble_view_sync 纯组装直测（折叠后视图刷新修复 Task 1）。

覆盖（计划 Task 1 Step 1）：
1. assemble_view_sync 直接调用：折叠占位符/头行渲染正确；_fold_stats 统计
   （n/m/p）与 usage=校准值（monkeypatch 校准倍率 1.5 → usage=raw×1.5/max_tokens）
2. exclude_last 双向：False 含末条 / True 不含
3. 不触发压实：usage 人为抬高（小 max_tokens）→ 仍返回原始视图
   （非压实产物、无新块归档）

入口行为零变化回归锁在 test_get_context_for_chat_v2.py + test_compaction.py +
test_calibration.py（Step 4 合跑）。mock store / 校准倍率 / token 计数，
禁真实 LLM；tmp DB 禁碰 ~/.niu。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.context_assembler.calibration as calibration
import agent.session as session_mod
from agent.context_assembler.blocks import load_all
from agent.context_assembler.compaction import AUTO_GATE
from agent.context_manager import ContextManager
from agent.session import Message


# ---------------------------------------------------------------------------
# 测试基建：FakeStore + 确定性 token 计数 + 校准/闸门隔离（同 v2 测试约定）
# ---------------------------------------------------------------------------

class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages: list[Message]):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。"""
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _msg(idx, role, content, tool_calls=None, tool_call_id="",
         folded=0, output_pct=None):
    return Message(
        id=f"m{idx:03d}",
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_call_id=tool_call_id,
        folded=folded,
        output_pct=output_pct,
        created_at="2026-09-02T10:00:00",
        rowid=idx + 1,
    )


def _conversation():
    """一段对话：Q1 → assistant(read_file) → tool(未折叠, pct=5.0)
    → assistant(grep) → tool(已折叠, pct=7.5) → Q2（当前输入）。"""
    return [
        _msg(0, "user", "Q1"),
        _msg(1, "assistant", "", tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}]),
        _msg(2, "tool", "RAW_BODY_1", tool_call_id="c1", output_pct=5.0),
        _msg(3, "assistant", "", tool_calls=[
            {"id": "c2", "type": "function",
             "function": {"name": "grep", "arguments": "{}"}}]),
        _msg(4, "tool", "RAW_BODY_2", tool_call_id="c2", folded=1, output_pct=7.5),
        _msg(5, "user", "Q2"),
    ]


@pytest.fixture
def fold_env(monkeypatch, tmp_path):
    """校准倍率 1.5 + fold 列标志 True + 确定性计数 + AUTO_GATE 复位。"""
    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.5
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))
    AUTO_GATE.release()
    yield tmp_path
    calibration._cached_ratio = old_ratio
    AUTO_GATE.release()


def _make_cm(store, max_tokens, blocks_db):
    return ContextManager(store, max_tokens=max_tokens, blocks_db_path=blocks_db)


# ---------------------------------------------------------------------------
# 1. 折叠占位符/头行渲染 + _fold_stats（usage=校准值）
# ---------------------------------------------------------------------------

class TestAssembleViewSyncRendering:
    def test_folded_placeholder_and_header_line(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=True)

        # 无块 → 无索引前导；history=前 5 条（Q2 被排除）
        assert [e["role"] for e in view] == ["user", "assistant", "tool", "assistant", "tool"]
        # 未折叠 tool：头行 + 原文（编号=rowid，pct 落库固化值）
        assert view[2]["content"] == "[输出#3 · read_file · 占上下文 5.0%]\nRAW_BODY_1"
        # 折叠 tool：占位符含 pct 快照，以「获取]」收尾（agent_loop 识别契约）
        assert view[4]["content"] == (
            "[输出#5 已折叠：grep({})，产生时占上下文 7.5%。如需原文请重新调用该工具获取]"
        )

    def test_fold_stats_and_calibrated_usage(self, fold_env):
        msgs = _conversation()
        cm = _make_cm(FakeStore(msgs), 10_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(msgs, exclude_last=True)

        # n=窗口未折叠 tool 数（仅 RAW_BODY_1）；m/p=有快照者条数与合计
        assert cm._fold_stats["n"] == 1
        assert cm._fold_stats["m"] == 1
        assert cm._fold_stats["p"] == 5.0
        # usage=校准值：raw × 倍率 1.5 ÷ max_tokens（非 raw）
        base = _fake_count_tokens(view)
        assert cm._fold_stats["usage"] == pytest.approx(base * 1.5 / 10_000)


# ---------------------------------------------------------------------------
# 2. exclude_last 双向语义
# ---------------------------------------------------------------------------

class TestExcludeLast:
    def test_exclude_last_true_drops_current_input(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=True)
        assert all(e.get("content") != "Q2" for e in view)

    def test_exclude_last_false_keeps_current_input(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=False)
        assert view[-1] == {"role": "user", "content": "Q2"}

    def test_empty_messages(self, fold_env):
        cm = _make_cm(FakeStore([]), 1_000_000, fold_env / "blocks.db")
        assert cm.assemble_view_sync([], exclude_last=True) == []


# ---------------------------------------------------------------------------
# 3. 不触发压实（深审 P1：rebuild 不得触发归档）
# ---------------------------------------------------------------------------

class TestNoCompactionInAssemble:
    def test_high_usage_does_not_compact(self, fold_env):
        msgs = _conversation()
        blocks_db = fold_env / "blocks.db"
        # max_tokens=10 → usage 远超任何触发线，assemble_view_sync 仍不得压实
        cm = _make_cm(FakeStore(msgs), 10, blocks_db)
        view = cm.assemble_view_sync(msgs, exclude_last=True)

        # 原始视图原样返回：未折叠 tool 全文在场（非压实产物）
        assert any("RAW_BODY_1" in e.get("content", "") for e in view)
        # 无新块归档（压实是唯一归档者，assemble_view_sync 不触发）
        assert load_all(blocks_db) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
