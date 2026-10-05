"""T2b-1：当前视图内工具 rowid 可见集（ContextManager.visible_tool_rowids）。

折叠工具长期失败的根因是模型在热上下文里看不到 rowid 空间、凭猜测调
fold_tool_output。本步只建立「当前视图内工具 rowid 集合」及其生命周期
（组装时赋值 / reset_derived_state 作废），**不改任何工具行为**。

覆盖：
1. 组装后集合 = 视图内全部工具 rowid（含已折叠行）
2. 未组装前 = None（尚未组装过 ≠ 空集合）
3. reset_derived_state 后 = None（/new 内存态作废同处，不另建清理路径）
4. 不变量：exclude_last=False（入口）与 exclude_last=True（每工具轮）两集合恒等
5. 访问器返回副本——外部误改返回值不污染内部
6. 负向锁：被组装丢弃的消息（完全空）不进集合

基建沿用 tests/test_fold_view_refresh.py / tests/test_context_window_live_read.py
同款：FakeStore + 确定性 token 计数 + 校准钉 1.0 + tmp 块库；禁真实 LLM、
禁碰 ~/.niu（块库/校准文件全部 tmp 注入）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.context_assembler.calibration as calibration
import agent.context_manager as cm_mod
import agent.session as session_mod
from agent.context_assembler.compaction import AUTO_GATE
from agent.context_manager import ContextManager
from agent.session import Message

# _conversation() 期望的可见工具 rowid 全集（3 条工具，其中 1 条已折叠）
EXPECTED_TOOL_ROWIDS = {3, 5, 7}


# ---------------------------------------------------------------------------
# 测试基建：FakeStore + 确定性 token 计数 + 校准钉 1.0 + tmp 块库（同款约定）
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
        created_at="2026-10-05T10:00:00",
        rowid=idx + 1,
    )


def _tc(name, tc_id, args="{}"):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": args}}


def _conversation():
    """Q1 → 三轮工具调用（第 2 条已折叠）→ Q2（当前输入，exclude_last 剔除项）。"""
    return [
        _msg(0, "user", "Q1"),
        _msg(1, "assistant", "", tool_calls=[_tc("read_file", "c1", '{"path": "a"}')]),
        _msg(2, "tool", "RAW_BODY_1", tool_call_id="c1", output_pct=5.0),
        _msg(3, "assistant", "", tool_calls=[_tc("grep", "c2")]),
        _msg(4, "tool", "RAW_BODY_2", tool_call_id="c2", folded=1, output_pct=7.5),
        _msg(5, "assistant", "", tool_calls=[_tc("read_file", "c3", '{"path": "b"}')]),
        _msg(6, "tool", "RAW_BODY_3", tool_call_id="c3", output_pct=3.0),
        _msg(7, "user", "Q2"),
    ]


@pytest.fixture
def visible_env(monkeypatch, tmp_path):
    """校准倍率钉 1.0 + fold 列标志 True + 确定性 token 计数 + AUTO_GATE 复位。"""
    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))
    AUTO_GATE.release()
    yield tmp_path
    calibration._cached_ratio = old_ratio
    AUTO_GATE.release()


def _make_cm(messages, blocks_db):
    return ContextManager(FakeStore(messages), max_tokens=1_000_000,
                          blocks_db_path=blocks_db)


# ---------------------------------------------------------------------------
# 1~2：集合内容（含已折叠）+ 未组装前为 None
# ---------------------------------------------------------------------------


class TestVisibleToolRowids:
    def test_collects_all_tool_rowids_including_folded(self, visible_env):
        """视图内全部工具 rowid 入集合——已折叠行同样在场（模型仍可见编号）。"""
        msgs = _conversation()
        cm = _make_cm(msgs, visible_env / "blocks.db")
        cm.assemble_view_sync(msgs, exclude_last=True)
        assert cm.visible_tool_rowids() == EXPECTED_TOOL_ROWIDS

    def test_none_before_first_assembly(self, visible_env):
        """未组装过 → None（与「组装后为空集合」语义区分）。"""
        cm = _make_cm(_conversation(), visible_env / "blocks.db")
        assert cm.visible_tool_rowids() is None

    def test_empty_history_assembles_to_empty_set(self, visible_env):
        """组装过但窗口内无工具消息 → 空集合（非 None）。"""
        cm = _make_cm([], visible_env / "blocks.db")
        cm.assemble_view_sync([], exclude_last=True)
        assert cm.visible_tool_rowids() == set()

    def test_dropped_message_excluded(self, visible_env):
        """负向锁：完全空消息被 _message_to_dict 丢弃 → 不进集合。"""
        msgs = _conversation() + [
            _msg(8, "assistant", "", tool_calls=[_tc("noop", "c4")]),
            Message(id="m009", role="tool", content="", tool_calls=[],
                    tool_call_id="", folded=0, output_pct=None,
                    created_at="2026-10-05T10:00:00", rowid=10),
            _msg(10, "user", "Q3"),
        ]
        cm = _make_cm(msgs, visible_env / "blocks.db")
        view = cm.assemble_view_sync(msgs, exclude_last=True)
        assert all(e.get("tool_call_id") != "" for e in view)
        assert cm.visible_tool_rowids() == EXPECTED_TOOL_ROWIDS


# ---------------------------------------------------------------------------
# 3：/new 清理面作废（与 fold 仪表盘缓存同一内存态通道）
# ---------------------------------------------------------------------------


class TestResetDerivedStateClearsVisibleRowids:
    def test_reset_derived_state_sets_none(self, visible_env, monkeypatch):
        """/new 后 visible_tool_rowids() is None——防陈旧编号被折叠工具消费。"""
        from agent.context_assembler import reset_derived_state

        msgs = _conversation()
        cm = _make_cm(msgs, visible_env / "blocks.db")
        cm.assemble_view_sync(msgs, exclude_last=True)
        assert cm.visible_tool_rowids() == EXPECTED_TOOL_ROWIDS

        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        # 派生文件全部指向 tmp（禁碰 ~/.niu）
        reset_derived_state(blocks_db_path=visible_env / "blocks.db",
                            calibration_path=visible_env / "token_calibration.json")
        assert cm.visible_tool_rowids() is None


# ---------------------------------------------------------------------------
# 4：不变量——入口组装与每工具轮组装的工具 rowid 空间恒等
# ---------------------------------------------------------------------------


class TestExcludeLastInvariant:
    def test_both_assembly_modes_yield_same_set(self, visible_env):
        """exclude_last 差异恒为当前 user 行（role≠tool）→ 工具 rowid 集合恒等。"""
        msgs = _conversation()
        cm = _make_cm(msgs, visible_env / "blocks.db")

        cm.assemble_view_sync(msgs, exclude_last=False)  # 入口
        at_entry = cm.visible_tool_rowids()
        cm.assemble_view_sync(msgs, exclude_last=True)  # 每工具轮
        per_tool_round = cm.visible_tool_rowids()

        assert at_entry == per_tool_round == EXPECTED_TOOL_ROWIDS


# ---------------------------------------------------------------------------
# 5：访问器返回副本（防外部误改内部状态）
# ---------------------------------------------------------------------------


class TestAccessorReturnsCopy:
    def test_mutating_return_value_does_not_affect_internal(self, visible_env):
        msgs = _conversation()
        cm = _make_cm(msgs, visible_env / "blocks.db")
        cm.assemble_view_sync(msgs, exclude_last=True)

        got = cm.visible_tool_rowids()
        assert got is not cm._visible_tool_rowids
        got.add(999_999)
        got.clear()
        assert cm.visible_tool_rowids() == EXPECTED_TOOL_ROWIDS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
