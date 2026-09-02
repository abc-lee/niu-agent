"""Task 4：动态块使用率仪表盘 + 压实触发线配置化。

覆盖 spec §5 / §9：
- 仪表盘行格式（有/无可折叠输出，单一口径「合计 p%」）
- 组装时缓存 _fold_stats（n=未折叠 tool 消息数 / p=output_pct 快照合计 / usage）
- 迁移失败降级：仪表盘省略可折叠段（只留使用率+压缩线，R2-B P3）
- trigger_ratio 默认 0.80 / 自定义配置跟随 / clamp [0.50, 0.94]
- trigger < warningThreshold 时倒置 warning（相等不误报，R2-A P2）
- HARD_BUDGET=min(0.80, trigger) / RESET=trigger−0.02

全部 tmp 目录 + user-config 重定向，禁碰 ~/.niu。
"""

import json

import pytest

from agent.context_assembler import compaction
from agent.context_manager import ContextManager
from agent.session import MessageStore


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    """user-config 重定向到 tmp 文件（缺省空配置=全默认；隔离真实 ~/.niu）。"""
    from niu_api import config as cfg_mod
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    return p


def _set_context_cfg(cfg: object, context: dict) -> None:
    data = json.loads(cfg.read_text(encoding="utf-8"))  # type: ignore[arg-type]
    data["context"] = context
    cfg.write_text(json.dumps(data), encoding="utf-8")  # type: ignore[arg-type]


# ============== 仪表盘行格式（_fold_stats 注入，格式化契约） ==============


class TestDashboardLineFormat:
    def _cm(self, tmp_path):
        """同步构造：绕过 async init——直接建实例不触碰 DB（格式化纯读缓存）。"""
        return ContextManager.__new__(ContextManager)

    def test_foldable_section(self, tmp_path):
        """单一口径：n=未折叠 tool 数，合计只含有 output_pct 快照者"""
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 8, "p": 23.4, "usage": 0.62}
        assert c.get_fold_dashboard_line() == \
            "[上下文使用率 62.0% · 强制压缩线 80% · 可折叠输出 8 条（合计 23.4%）]"

    def test_no_foldable_omits_section(self, tmp_path):
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 0, "p": 0.0, "usage": 0.62}
        assert c.get_fold_dashboard_line() == "[上下文使用率 62.0% · 强制压缩线 80%]"

    def test_no_cache_returns_empty(self, tmp_path):
        c = self._cm(tmp_path)
        c._fold_stats = None
        assert c.get_fold_dashboard_line() == ""

    def test_configured_trigger_shown(self, tmp_path, _cfg):
        """仪表盘显示的线 = 实际触发的线（配置跟随，spec §5）"""
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.75})
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 2, "p": 4.2, "usage": 0.6}
        assert "强制压缩线 75%" in c.get_fold_dashboard_line()

    # ---- M2-F1 真值化：usage_override 三级优先（override>0 > stats.usage > 省略）----

    def test_usage_override_preferred_over_stats(self, tmp_path):
        """override>0（LLM API 真值）→ 用真值，不用 _fold_stats 估算。"""
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 0, "p": 0.0, "usage": 0.365}
        line = c.get_fold_dashboard_line(usage_override=0.427)
        assert "上下文使用率 42.7%" in line

    def test_no_override_reads_stats_usage(self, tmp_path):
        """无 override（真值缺失/已清 0）→ 读 _fold_stats['usage']。"""
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 1, "p": 5.0, "usage": 0.365}
        assert "上下文使用率 36.5%" in c.get_fold_dashboard_line()

    def test_zero_override_falls_back_to_stats(self, tmp_path):
        """override=0（fold/压实清零后的陈旧真值）→ 不视为真值，落估算兜底。"""
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 0, "p": 0.0, "usage": 0.365}
        assert "上下文使用率 36.5%" in c.get_fold_dashboard_line(usage_override=0)

    def test_none_stats_wins_over_override(self, tmp_path):
        """_fold_stats=None 最高优先 → ""（即使传 override——delete_messages 陈旧真值路径）。"""
        c = self._cm(tmp_path)
        c._fold_stats = None
        assert c.get_fold_dashboard_line(usage_override=0.427) == ""

    def test_stats_usage_none_returns_empty(self, tmp_path):
        """stats 在场但 usage=None（组装后未回填）且无 override → 省略整行。"""
        c = self._cm(tmp_path)
        c._fold_stats = {"n": 0, "p": 0.0, "usage": None}
        assert c.get_fold_dashboard_line() == ""


# ============== /new 清理面：reset_derived_state 作废 fold 仪表盘缓存（M2-F2） ==============


class TestNewSessionClearsFoldStats:
    def test_reset_derived_state_clears_fold_stats(self, tmp_path, monkeypatch):
        """/new 后 _fold_stats=None——防陈旧 usage 被页面三级链/动态块拾取。"""
        import agent.context_manager as cm_mod

        class _Cm:
            def __init__(self):
                self._fold_stats = {"n": 2, "p": 5.0, "usage": 0.5}
                self.est = -1

            def set_system_token_estimate(self, n):
                self.est = n

        cm = _Cm()
        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        from agent.context_assembler import reset_derived_state
        # 派生文件全部指向 tmp（禁碰 ~/.niu）
        reset_derived_state(blocks_db_path=tmp_path / "blocks.db",
                            calibration_path=tmp_path / "token_calibration.json")
        assert cm._fold_stats is None
        assert cm.est == 0


# ============== 组装时缓存 _fold_stats（集成） ==============


@pytest.mark.asyncio
async def test_assembly_populates_stats(tmp_path, _cfg):
    """n=未折叠 tool 消息数 / p=output_pct 快照合计（无快照行不贡献数值）；已折叠不计。"""
    import agent.context_assembler.calibration as cal
    old = cal._cached_ratio
    cal._cached_ratio = 1.0  # 隔离真实持久化倍率
    try:
        store = MessageStore(str(tmp_path / "m.db"))
        await store.init_db()
        await store.add_message(role="user", content="question")
        await store.add_message(role="assistant", content="", tool_calls=[
            {"id": "tc1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "tc2", "type": "function",
             "function": {"name": "grep", "arguments": "{}"}},
        ])
        await store.add_message(role="tool", content="A" * 100,
                                tool_call_id="tc1", output_pct=4.2)
        await store.add_message(role="tool", content="B" * 100,
                                tool_call_id="tc2")  # 无 output_pct 快照
        # 第三条已折叠（fold 工具置位，此处直接 SQL 模拟）——不计入 n
        await store.add_message(role="assistant", content="", tool_calls=[
            {"id": "tc3", "type": "function",
             "function": {"name": "grep", "arguments": "{}"}}])
        await store.add_message(role="tool", content="C" * 100,
                                tool_call_id="tc3", output_pct=1.5)
        import sqlite3
        conn = sqlite3.connect(store.db_path)
        conn.execute("UPDATE messages SET folded=1 WHERE rowid=(SELECT MAX(rowid) FROM messages)")
        conn.commit()
        conn.close()

        c = ContextManager(store, max_tokens=100000, blocks_db_path=tmp_path / "b.db")
        await c.get_context_for_chat(exclude_last=False)
        s = c._fold_stats
        assert s["n"] == 2 and s["p"] == 4.2
        line = c.get_fold_dashboard_line()
        assert "可折叠输出 2 条（合计 4.2%）" in line
        assert line.startswith("[上下文使用率 ") and line.endswith("]")
    finally:
        cal._cached_ratio = old


@pytest.mark.asyncio
async def test_degraded_omits_fold_section(tmp_path, _cfg, monkeypatch):
    """迁移失败降级（R2-B P3）：省略可折叠段——不误导 LLM 调必报错的工具。"""
    import agent.session as sess
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()  # 先正常迁移（生产失败态=启动时 ALTER 抛错，此处模拟后置位）
    monkeypatch.setattr(sess, "_fold_columns_available", False)
    await store.add_message(role="user", content="question")
    await store.add_message(role="assistant", content="", tool_calls=[
        {"id": "tc1", "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}}])
    await store.add_message(role="tool", content="A" * 100,
                            tool_call_id="tc1", output_pct=4.2)
    c = ContextManager(store, max_tokens=100000, blocks_db_path=tmp_path / "b.db")
    await c.get_context_for_chat(exclude_last=False)
    line = c.get_fold_dashboard_line()
    assert "可折叠输出" not in line
    assert line.startswith("[上下文使用率 ") and "强制压缩线 80%" in line


# ============== trigger_ratio 配置化 ==============


class TestTriggerRatio:
    def test_default(self, _cfg):
        assert compaction.trigger_ratio() == 0.80

    def test_follows_config(self, _cfg):
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.75})
        assert compaction.trigger_ratio() == 0.75

    def test_clamp_low(self, _cfg):
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.3})
        assert compaction.trigger_ratio() == 0.50

    def test_clamp_high(self, _cfg):
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.97})
        assert compaction.trigger_ratio() == 0.94

    def test_reset_follows_trigger_minus_002(self, _cfg):
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.75})
        assert compaction.reset_ratio() == pytest.approx(0.73)

    def test_hard_budget_min_of_default_and_trigger(self, _cfg):
        # 默认：min(0.80, 0.80)=0.80；配低触发线时回落预算不得高于触发线（spec §5）
        assert compaction.hard_budget_ratio() == 0.80
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.55})
        assert compaction.hard_budget_ratio() == 0.55


class TestInversionWarning:
    def _record_warnings(self, monkeypatch):
        import agent.subagent as sub
        warns = []
        monkeypatch.setattr(sub.logger, "warning",
                            lambda msg, *a, **k: warns.append(str(msg)))
        return warns

    def test_below_warning_threshold_warns(self, _cfg, monkeypatch):
        """trigger 严格 < warningThreshold → 提前窗口倒置 warning（R1-B P3）"""
        warns = self._record_warnings(monkeypatch)
        _set_context_cfg(_cfg, {"compactionTriggerRatio": 0.75})  # < 默认 warning 0.80
        compaction.trigger_ratio()
        assert any("inverted" in w for w in warns)

    def test_equal_no_false_positive(self, _cfg, monkeypatch):
        """相等=默认 0.80/0.80 既有常态，不误报（R2-A P2）"""
        warns = self._record_warnings(monkeypatch)
        compaction.trigger_ratio()  # 默认 0.80 == warning 0.80
        assert not any("inverted" in w for w in warns)
