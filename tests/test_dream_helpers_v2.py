"""工程三 Task2：compat 梦境辅助函数单测（mock 一切外部依赖，零真实 LLM/图谱/DB）。"""

import niu_api.compat as compat


def _patch_call(monkeypatch, fn):
    monkeypatch.setattr("agent.subagent.call_subagent_with_auto_answer", fn)


def _patch_drop(monkeypatch, fn):
    monkeypatch.setattr("agent.md_mirror.drop_f2_prefix", fn)


class TestCallDreamEvolverOnF3:
    def test_task_contains_f3_path_and_agent_name(self, monkeypatch):
        calls = {}

        def fake_call(**kwargs):
            calls.update(kwargs)
            return "ok"

        _patch_call(monkeypatch, fake_call)
        result = compat._call_dream_evolver_on_f3({"model": "m"})
        assert result == "ok"
        assert calls["agent_name"] == "dream-evolver"
        # T4-D 起 conftest 将 F3_PATH 一并隔离到 tmp——只断言反引号路径形态，不断言规范文件名
        assert "`" in calls["task"] and calls["task"].rstrip().endswith("processed_line 行号。")
        # 无 history 注入：签名只允许这五个关键字
        assert set(calls) == {"agent_name", "task", "llm_config", "mcp_client", "context_fifo_threshold"}
        assert calls["llm_config"] == {"model": "m"}
        assert calls["context_fifo_threshold"] == -1

    def test_f3_path_override(self, monkeypatch):
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return ""

        _patch_call(monkeypatch, fake_call)
        compat._call_dream_evolver_on_f3(None, f3_path="/tmp/custom_f3.md")
        assert "/tmp/custom_f3.md" in captured["task"]


class TestParseAndDropF2:
    def test_parse_success_passthrough(self, monkeypatch):
        seen = {}
        sentinel = (42, "msg-42")

        def fake_drop(n_lines, max_lines=None, f2_path=None):
            seen["args"] = (n_lines, max_lines, f2_path)
            return sentinel

        _patch_drop(monkeypatch, fake_drop)
        result = compat._parse_and_drop_f2("... processed_line=42 @end", 100, f2_path="/tmp/f2.md")
        assert result == sentinel
        assert seen["args"] == (42, 100, "/tmp/f2.md")

    def test_loose_separators(self, monkeypatch):
        def make_drop(seen):
            def drop(n_lines, max_lines=None, f2_path=None):
                seen.append((n_lines, max_lines))
                return (n_lines, "")
            return drop

        for text in ["processed_line: 7", "processed_line 7", "processed_line= 07"]:
            seen = []
            _patch_drop(monkeypatch, make_drop(seen))
            result = compat._parse_and_drop_f2(text, 50)
            assert result == (7, ""), f"failed on {text!r}"
            assert seen == [(7, 50)]

    def test_no_marker_warning_and_zero(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        records = []
        sink_id = compat.logger.add(records.append, level="WARNING")
        try:
            result = compat._parse_and_drop_f2("没有任何标记", 30)
        finally:
            compat.logger.remove(sink_id)
        assert result == (0, "")
        assert any("processed_line" in r for r in records)

    def test_none_result(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        assert compat._parse_and_drop_f2(None, 10) == (0, "")  # type: ignore[arg-type]

    def test_empty_result(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        assert compat._parse_and_drop_f2("", 10) == (0, "")

    def test_drop_zero_tuple_passthrough_unchanged(self, monkeypatch):
        _patch_drop(monkeypatch, lambda n_lines, max_lines=None, f2_path=None: (0, ""))
        result = compat._parse_and_drop_f2("processed_line=5", 3)  # n_lines 超上界，drop 自行判 0
        assert result == (0, "")
