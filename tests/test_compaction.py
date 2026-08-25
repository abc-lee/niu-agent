"""批量压实测试（agent/context_assembler/compaction.py，计划 Task 3 清单）。

覆盖：滞回判别（79% 不触发 / 80% 触发 / <78% 复位）/ 回写断言（视图骤减、
首条为索引、system 原样保留、tool 配对完整）/ 水位回落 / D15 顺序
（先占位符化后减轮）/ 95% 应急终态 / 索引超预算合并 / 块归档幂等 /
compact_now 与自动触发共用同一压实入口（spy）。全部 mock，无 LLM 调用。
"""

from types import SimpleNamespace

import pytest

from agent.context_assembler import compaction
from agent.context_assembler.compaction import (
    CompactionGate,
    build_compact_view,
    render_index_grouped,
)
from agent.context_manager import ContextManager

CW = 5000  # 测试用上下文窗口


@pytest.fixture
def ratio_one():
    """倍率固定 1.0，隔离真实持久化状态。"""
    import agent.context_assembler.calibration as cal
    old = cal._cached_ratio
    cal._cached_ratio = 1.0
    yield
    cal._cached_ratio = old


def msg(role, content, mid, rowid, tool_calls=None, tool_call_id=None):
    return SimpleNamespace(
        id=mid, rowid=rowid, role=role, content=content,
        tool_calls=tool_calls, tool_call_id=tool_call_id,
        created_at="2026-08-25T10:00:00",
    )


UNIT_BODY_CHARS = 2000  # 单元正文体量：确保挤出 2 个单元的收益显著大于索引行开销


def plain_unit(i, rowid_base):
    """一个 user/assistant 会话单元。"""
    return [
        msg("user", f"question {i} " + "q" * UNIT_BODY_CHARS, f"u{i}", rowid_base),
        msg("assistant", f"answer {i} " + "a" * UNIT_BODY_CHARS, f"a{i}", rowid_base + 1),
    ]


def tool_unit(i, rowid_base, tool_chars=100):
    """一个 user/assistant(tool_call)/tool 会话单元。"""
    return [
        msg("user", f"question {i}", f"u{i}", rowid_base),
        msg("assistant", "", f"a{i}", rowid_base + 1,
            tool_calls=[{"id": f"tc{i}", "type": "function",
                         "function": {"name": "search", "arguments": "{}"}}]),
        msg("tool", "y" * tool_chars, f"t{i}", rowid_base + 2, tool_call_id=f"tc{i}"),
    ]


class TestHysteresisGate:
    def test_79_no_trigger(self):
        gate = CompactionGate()
        assert gate.try_acquire(0.79) is False

    def test_80_triggers_and_latches(self):
        gate = CompactionGate()
        assert gate.try_acquire(0.80) is True
        # 闩锁后即使仍 ≥80% 也不重复触发（同轮双触发去重）
        assert gate.try_acquire(0.85) is False

    def test_reset_below_78_then_retrigger(self):
        gate = CompactionGate()
        assert gate.try_acquire(0.81) is True
        # 水位回落到滞回复位线以下 → 解除闩锁（本次返回 False）
        assert gate.try_acquire(0.70) is False
        # 回落后再次达线可重新触发
        assert gate.try_acquire(0.82) is True

    def test_release_forces_unlocked(self):
        gate = CompactionGate()
        assert gate.try_acquire(0.90) is True
        gate.release()
        assert gate.try_acquire(0.90) is True
    def test_compaction_success_release_retriggers_in_hysteresis_band(self):
        """P1 回归：压实成功后水位常落 [78%,80%)——出口 release() 契约复位闩锁。

        旧行为：闩锁态下仅 <78% 才解锁，[78%,80%) 永久闩死 → 自动压实
        进程级失效。序列：0.81 触发 → 压实后 0.79（滞回带内）→ release() →
        再次 0.81 仍能触发。
        """
        gate = CompactionGate()
        assert gate.try_acquire(0.81) is True   # 达线触发并闩锁（执行压实）
        assert gate.try_acquire(0.79) is False  # 压实后水位落滞回带，不触发
        gate.release()                          # 压实成功出口契约：复位闩锁
        assert gate.try_acquire(0.79) is False  # 复位后未达线不得误触发
        assert gate.try_acquire(0.81) is True   # 再次达线仍能触发

    def test_global_gate_exists(self):
        from agent.context_assembler.compaction import AUTO_GATE
        assert isinstance(AUTO_GATE, CompactionGate)


class TestCompactView:
    def test_write_back_shape_and_water_level_drop(self, ratio_one, tmp_path):
        # 5 个单元全量进视图 vs 压实后只留最近 3 轮 + 索引
        messages = []
        for i in range(5):
            messages.extend(plain_unit(i, i * 10))
        system_msg = {"role": "system", "content": "SYS",
                      "cache_control": {"type": "ephemeral"}}  # cache_control 断点不可丢

        full_view = [system_msg] + [ContextManager._message_to_dict(m) for m in messages]
        before = _est(full_view)

        view, stats = build_compact_view(
            messages, system_msg=system_msg, keep_turns=3,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )

        # 回写断言：长度骤减且首条为 system（原样，含 cache_control）
        assert len(view) < len(full_view)
        assert view[0] is system_msg
        assert view[0]["cache_control"] == {"type": "ephemeral"}
        # 第二条为索引消息
        assert view[1]["role"] == "user"
        assert view[1]["content"].startswith("[历史索引]")
        # 其余为窗口原文（最近 3 单元 = 6 条）
        window = view[2:]
        assert len(window) == 6
        assert         window[0]["content"].startswith("question 2")

        # 水位回落断言：校准后总量估算低于 80% 触发线
        after = _est(view)
        assert after < before
        assert stats["usage"] < compaction.TRIGGER_RATIO
        assert stats["blocks_archived"] == 2

    def test_no_system_first_entry_is_index(self, ratio_one, tmp_path):
        messages = []
        for i in range(4):
            messages.extend(plain_unit(i, i * 10))
        view, stats = build_compact_view(
            messages, system_msg=None, keep_turns=2,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        # 无 system 时首条为索引消息（组装出口场景由 agent_loop 自拼 system）
        assert view[0]["role"] == "user"
        assert view[0]["content"].startswith("[历史索引]")
        assert stats["keep_turns"] == 2

    def test_tool_pairing_preserved(self, ratio_one, tmp_path):
        messages = []
        for i in range(5):
            messages.extend(tool_unit(i, i * 10))
        view, _stats = build_compact_view(
            messages, system_msg=None, keep_turns=3,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        tc_ids = {e["tool_call_id"] for e in view if e.get("role") == "tool"}
        call_ids = {tc["id"] for e in view if e.get("role") == "assistant"
                    for tc in e.get("tool_calls", [])}
        assert tc_ids == call_ids  # tool_calls/tool_call_id 配对完整

    def test_archive_idempotent(self, ratio_one, tmp_path):
        messages = []
        for i in range(4):
            messages.extend(plain_unit(i, i * 10))
        _, s1 = build_compact_view(
            messages, system_msg=None, keep_turns=2,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        assert s1["blocks_archived"] == 2
        _, s2 = build_compact_view(
            messages, system_msg=None, keep_turns=2,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        assert s2["blocks_archived"] == 0  # 重复组装不多生


class TestD15Order:
    def test_placeholderize_before_turn_reduction(self, ratio_one, tmp_path):
        # 4 个含超大工具输出的单元；硬预算内连占位符化都不够 → 先占位符化后减轮
        messages = []
        for i in range(4):
            messages.extend(tool_unit(i, i * 10, tool_chars=30000))
        view, stats = build_compact_view(
            messages, system_msg=None, keep_turns=3,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        # 占位符化确实发生（D15 步骤 A 先于减轮执行）
        assert stats["tools_placeholderized"] >= 1
        # 轮数被压到目标之下（D15 步骤 B）
        assert stats["keep_turns"] < 3
        # 窗口内存在占位符文本
        assert any(
            isinstance(e.get("content"), str) and e["content"].endswith("输出已裁剪]")
            for e in view if e.get("role") == "tool"
        )

    def test_emergency_terminal_state_release(self, ratio_one, tmp_path, caplog):
        # 极端单轮超大场景：唯一单元为纯 user 大文本（无工具可裁），
        # 终态兜底后仍 ≥95% → 放行 + error 日志
        messages = [
            msg("user", "z" * 60000, "u0", 0),
            msg("assistant", "a", "a0", 1),
        ]
        with pytest.MonkeyPatch.context() as mp:
            logged = {}
            mp.setattr(compaction.logger, "error",
                       lambda m, *a, **k: logged.setdefault("emergency", str(m)))
            view, stats = build_compact_view(
                messages, system_msg=None, keep_turns=3,
                blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
            )
        assert stats["emergency"] is True
        assert "Emergency" in logged.get("emergency", "") or "95%" in logged.get("emergency", "")
        # 放行：视图仍产出（接受超限发送走服务端降级）
        assert view and view[-1]["content"] == "a"


class TestIndexMerge:
    def test_merge_under_budget(self, ratio_one):
        blocks = []
        for i in range(20):
            b = SimpleNamespace(id=i + 1, count=10, time_start="2026-08-01T00:00:00",
                                time_end="2026-08-02T00:00:00",
                                first_user=f"很长的首问内容用来撑大索引行体积编号{i}")
            b.start_msg_id = b.end_msg_id = ""
            b.start_rowid = b.end_rowid = 0
            blocks.append(b)
        text = render_index_grouped(blocks, budget_tokens=200,
                                    count_fn=ContextManager.count_tokens_simple)
        assert "块#1~" in text  # 最老相邻块已合并为一行
        assert "共 20 块" in text


class FakeStore:
    def __init__(self, messages):
        self._messages = messages

    async def get_messages(self, limit=None):
        return list(self._messages)


class TestSharedEntrySpy:
    async def test_compact_now_delegates_to_build_compact_view(self, ratio_one, monkeypatch):
        calls = []

        def spy(messages, **kwargs):
            calls.append((list(messages), kwargs))
            return [{"role": "user", "content": "compacted"}], {
                "usage": 0.3, "tokens_estimate": 1500, "context_window": CW,
                "keep_turns": 3, "units_total": 1, "blocks_archived": 0,
                "blocks_total": 0, "tools_placeholderized": 0, "emergency": False,
            }

        monkeypatch.setattr(compaction, "build_compact_view", spy)
        store = FakeStore([msg("user", "hi", "u0", 0)])
        view = await compaction.compact_now(store, keep_turns=3,
                                            context_window_tokens=CW)
        assert view == [{"role": "user", "content": "compacted"}]
        assert len(calls) == 1  # 手动 /compact 与自动触发共用同一压实入口

    async def test_compact_now_detailed_returns_stats(self, ratio_one, tmp_path):
        messages = []
        for i in range(4):
            messages.extend(plain_unit(i, i * 10))
        view, stats = await compaction.compact_now_detailed(
            FakeStore(messages), system_msg=None, keep_turns=2,
            blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
        )
        assert stats["keep_turns"] == 2
        assert set(stats) >= {"usage", "tokens_estimate", "context_window",
                              "keep_turns", "blocks_archived", "emergency"}


def _est(view) -> float:
    """与实现同源的校准总量估算（测试内基准）。"""
    import agent.context_assembler.calibration as cal
    return ContextManager.count_tokens_simple(list(view)) * cal.get_ratio()
