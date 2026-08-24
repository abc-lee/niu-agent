"""工程二 Task4：切换接线源码级断言（门控已随工程四 T2 删除，仅存孤儿清除断言）。"""


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
        # v3：nap 整链删除——runner 不再引用 entity-extractor，也不再有 relay 剪切调用点
        assert '"entity-extractor"' not in r
        assert "_parse_and_relay_f1" not in r

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


def test_gating_orphans_removed():
    """门控三孤儿随工程四 T2 删除（决策 2 收尾）；runner 版早已随 force 摘除（7e）。"""
    from agent import runner
    from niu_api import compat
    assert not hasattr(compat, "_cursors_caught_up")
    assert not hasattr(compat, "_cursors_caught_up_dream_only")
    assert not hasattr(compat, "_read_cursor_value")
    assert not hasattr(runner, "_cursors_caught_up"), "runner 版门控已随 force 摘除删除（7e）"
