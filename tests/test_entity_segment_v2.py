"""工程二 Task4：切换接线源码级断言 + 门控行为测试（锁定 4 处调用点的新旧形态）。"""

import inspect


class TestSwitchSurface:
    def _compat(self):
        return open("niu_api/compat.py", encoding="utf-8").read()

    def _runner(self):
        return open("agent/runner.py", encoding="utf-8").read()

    def _md(self):
        return open("config/agents/entity-extractor.md", encoding="utf-8").read()

    def test_sleep_v2_wiring_present(self):
        src = self._compat()
        assert "align_f1_with_store(store, f1_path)" in src
        assert "_call_entity_extractor_on_f1(llm_config, f1_path)" in src
        assert "F1 空/不存在，跳过提炼" in src

    def test_old_paths_removed(self):
        src = self._compat()
        assert "entity_cursor_path" not in src
        assert "entity_force_prompt" not in src
        assert "_build_plain_history(entity_incremental_msgs)" not in src

    def test_runner_switch_surface(self):
        r = self._runner()
        # nap 切自读；force 的 entity 调用摘除（含 _run_subagent_step 位置参数形态，审查 B-P2）
        assert '"entity-extractor"' not in r
        assert "_parse_and_relay_f1" in r

    def test_compat_helper_single_definition(self):
        src = self._compat()
        assert src.count('agent_name="entity-extractor"') == 1  # 仅 _call_entity_extractor_on_f1 内

    def test_clear_truncates_relay_files(self):
        assert "truncate_relay_files()" in self._compat()

    def test_prompt_contract(self):
        md = self._md()
        assert "allowBaseTools:" in md and "- read" in md
        assert "F1_extract_source.md" in md
        assert "processed_line=" in md
        assert "[N]" not in md and "processed_up_to" not in md


def test_truncate_relay_files(tmp_path):
    from agent.md_mirror import truncate_relay_files
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("data", encoding="utf-8")
    b.write_text("data", encoding="utf-8")
    truncate_relay_files(str(a), str(b))
    assert a.read_text(encoding="utf-8") == "" and b.read_text(encoding="utf-8") == ""
    truncate_relay_files(str(tmp_path / "g1.md"), str(tmp_path / "g2.md"))  # 不存在不抛


def test_caught_up_entity_leg_switched():
    """睡眠版 entity 腿以 F1 空性为据；force 变体与 runner 版无 entity 腿、无 UUID 游标残留。"""
    from agent import runner
    from niu_api import compat
    sleep_src = inspect.getsource(compat._cursors_caught_up)
    assert "last_entity_extract" not in sleep_src
    assert "F1_PATH" in sleep_src or "getsize" in sleep_src  # 收紧：防 docstring 提及恒真（审查 B-P3）
    assert hasattr(compat, "_cursors_caught_up_dream_only")
    dream_only = inspect.getsource(compat._cursors_caught_up_dream_only)
    assert "last_entity_extract" not in dream_only
    runner_src = inspect.getsource(runner._cursors_caught_up)
    assert "last_entity_extract" not in runner_src


def test_sleep_gate_f1_emptiness(tmp_path, monkeypatch):
    """行为：F1 非空 → 睡眠门控判未追平（本次不压缩）；F1 空 → 追平（dream 腿已隔离）。"""
    import agent.md_mirror as mdm
    from niu_api import compat

    class FakeMsg:
        def __init__(self, mid):
            self.id = mid

    msgs = [FakeMsg(f"m{i}") for i in range(3)]
    # dream 腿隔离：游标指向末条消息 id（空列表会在函数头短路 True，必须非空）
    monkeypatch.setattr(compat, "_read_cursor_value", lambda path, key: "m2")

    f1 = tmp_path / "f1.md"
    f1.write_text('{"msg_id": "m0"}\ncontent\n', encoding="utf-8")
    monkeypatch.setattr(mdm, "F1_PATH", str(f1))
    assert compat._cursors_caught_up(msgs, 0) is False

    f1.write_text("", encoding="utf-8")
    assert compat._cursors_caught_up(msgs, 0) is True
