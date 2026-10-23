"""compat 梦境辅助函数单测（mock 一切外部依赖，零真实 LLM/图谱/DB）。

契约（plan §2）：`_parse_and_drop_f2(dream_result, result: BuildResult, f2_path=None)`——
完成标记是 **msg_id**（不是行号），删除位置来自 BuildResult 的三张表。
"""

import niu_api.compat as compat
from agent.md_mirror import BuildResult


def _patch_call(monkeypatch, fn):
    monkeypatch.setattr("agent.subagent.call_subagent_with_auto_answer", fn)


def _patch_drop(monkeypatch, fn):
    monkeypatch.setattr("agent.md_mirror.drop_f2_prefix", fn)


def _build_result(msg_id_to_turn_end, turn_last_msg_ids, f2_lines=100):
    """按 §2 造一个 ok 态 BuildResult（行数字段除 f2_lines 外与判序无关）。"""
    return BuildResult(
        status="ok", f3_lines=10, f2_lines=f2_lines, est_tokens=10, turns=1,
        compressed=False, f3_est_tokens=10,
        msg_id_to_turn_end=dict(msg_id_to_turn_end),
        turn_last_msg_ids=list(turn_last_msg_ids),
        f3_turn_ends=[],
    )


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
        task = calls["task"]
        assert "`" in task
        assert "processed_msg_id=" in task and "processed_line" not in task
        assert "msg_id" in task and "meta" in task
        assert "uuid" not in task.lower()
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
        res = _build_result({"msg-42": 42}, ["msg-42"], f2_lines=100)
        result = compat._parse_and_drop_f2("... processed_msg_id=msg-42 @end", res, f2_path="/tmp/f2.md")
        assert result == sentinel
        assert seen["args"] == (42, 100, "/tmp/f2.md")

    def test_loose_separators(self, monkeypatch):
        def make_drop(seen):
            def drop(n_lines, max_lines=None, f2_path=None):
                seen.append((n_lines, max_lines))
                return (n_lines, "")
            return drop

        for text in ['processed_msg_id: m7', 'processed_msg_id m7', 'processed_msg_id= m7',
                     "processed_msg_id='m7'", 'processed_msg_id="m7"']:
            seen = []
            _patch_drop(monkeypatch, make_drop(seen))
            res = _build_result({"m7": 7}, ["m7"], f2_lines=50)
            result = compat._parse_and_drop_f2(text, res)
            assert result == (7, ""), f"failed on {text!r}"
            assert seen == [(7, 50)]

    def test_no_marker_warning_and_zero(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        records = []
        sink_id = compat.logger.add(records.append, level="WARNING")
        try:
            result = compat._parse_and_drop_f2("没有任何标记", _build_result({"m7": 7}, ["m7"], 30))
        finally:
            compat.logger.remove(sink_id)
        assert result == (0, "")
        assert any("processed_msg_id" in r for r in records)

    def test_unknown_msg_id_rejected(self, monkeypatch):
        """§2 判序 1：不在 msg_id_to_turn_end（未知）→ 删 0 + WARNING，不调 drop。"""
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        records = []
        sink_id = compat.logger.add(records.append, level="WARNING")
        try:
            result = compat._parse_and_drop_f2(
                "processed_msg_id=m999", _build_result({"m7": 7}, ["m7"]), f2_path="/tmp/f2.md")
        finally:
            compat.logger.remove(sink_id)
        assert result == (0, "")
        assert any("未知" in r and "m999" in r for r in records)

    def test_half_turn_msg_id_rejected(self, monkeypatch):
        """§2 判序 2：在 map 但不是轮末条（半轮）→ 删 0 + WARNING，不调 drop。"""
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        records = []
        sink_id = compat.logger.add(records.append, level="WARNING")
        try:
            result = compat._parse_and_drop_f2(
                "processed_msg_id=m6", _build_result({"m6": 7, "m7": 7}, ["m7"]), f2_path="/tmp/f2.md")
        finally:
            compat.logger.remove(sink_id)
        assert result == (0, "")
        assert any("半轮" in r and "m6" in r for r in records)

    def test_none_result(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        assert compat._parse_and_drop_f2(None, _build_result({}, [])) == (0, "")  # type: ignore[arg-type]

    def test_empty_result(self, monkeypatch):
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        assert compat._parse_and_drop_f2("", _build_result({}, [])) == (0, "")

    def test_empty_build_result_never_drops(self, monkeypatch):
        """非 ok 态 BuildResult（三张表空）：任何上报都落「未知」态 → 删 0。"""
        _patch_drop(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drop 不应被调用")))
        assert compat._parse_and_drop_f2("processed_msg_id=m1", BuildResult("no_complete_turn", 0, 0, 0, 0, False, 0, {}, [], [])) == (0, "")

    def test_drop_zero_passthrough_with_warning(self, monkeypatch):
        records = []
        _patch_drop(monkeypatch, lambda n_lines, max_lines=None, f2_path=None: (0, ""))
        sink_id = compat.logger.add(records.append, level="WARNING")
        try:
            result = compat._parse_and_drop_f2(
                "processed_msg_id=m5", _build_result({"m5": 5}, ["m5"], f2_lines=3))
        finally:
            compat.logger.remove(sink_id)
        assert result == (0, "")
        assert any("drop 无进度" in r for r in records)
