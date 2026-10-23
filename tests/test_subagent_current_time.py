"""子 Agent system 日期（会话内恒定）、旧回调退役、接线锁定。"""
import inspect
from pathlib import Path
from unittest.mock import Mock, patch

from agent import subagent


def test_build_segments_dynamic_has_date_no_time():
    with patch.object(subagent, "_build_user_info_section", return_value=""), \
         patch.object(subagent, "get_subagent_prompt", return_value="BODY"):
        static, dynamic = subagent.build_subagent_system_segments("test-agent")
    assert "Current Time" not in dynamic
    assert subagent._TOOL_TIME_HINT in dynamic  # 元信息说明必须随 system 下发
    assert dynamic.rstrip().splitlines()[-1].startswith("Current Date: ")


def test_refresh_system_current_date_str():
    from datetime import date

    msg = {"role": "system", "content": "STATIC\n\nCurrent Time: 2026-08-13 10:00:00\n\n任务"}
    subagent._refresh_system_current_date(msg)
    assert "Current Time" not in msg["content"]
    assert msg["content"].startswith("STATIC")
    assert "任务" in msg["content"]
    assert msg["content"].rstrip().splitlines()[-1] == f"Current Date: {date.today().isoformat()}"


def test_refresh_moves_date_to_last_line():
    from datetime import date

    msg = {"role": "system", "content": "STATIC\n\nCurrent Time: 2026-08-13 10:00:00\n\n## 脑区\nA、B"}
    subagent._refresh_system_current_date(msg)
    assert msg["content"].rstrip().splitlines()[-1] == f"Current Date: {date.today().isoformat()}"
    assert "## 脑区" in msg["content"]


def test_refresh_is_idempotent():
    msg = {"role": "system", "content": "STATIC\n\nCurrent Time: 2026-08-13 10:00:00\n\n任务"}
    subagent._refresh_system_current_date(msg)
    once = msg["content"]
    subagent._refresh_system_current_date(msg)
    assert msg["content"] == once


def test_refresh_system_current_date_list():
    msg = {"role": "system", "content": [
        {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nCurrent Date: 2020-01-01"},
    ]}
    subagent._refresh_system_current_date(msg)
    assert msg["content"][0]["text"] == "STATIC"
    assert msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "2020-01-01" not in msg["content"][1]["text"]
    assert msg["content"][1]["text"].startswith("\n\n")  # 与静态块保持间隔


def test_refresh_never_rewrites_cached_block():
    from datetime import date

    msg = {"role": "system", "content": [
        {"type": "text", "text": "STATIC\n\nCurrent Date: 2020-01-01",
         "cache_control": {"type": "ephemeral"}},
    ]}
    subagent._refresh_system_current_date(msg)
    # 静态缓存块一个字不动
    assert msg["content"][0]["text"] == "STATIC\n\nCurrent Date: 2020-01-01"
    last = msg["content"][-1]["text"]
    assert last.rstrip().splitlines()[-1] == f"Current Date: {date.today().isoformat()}"

    from datetime import date

    msg = {"role": "system", "content": [
        {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": ""},
    ]}
    subagent._refresh_system_current_date(msg)
    assert msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    last = msg["content"][-1]["text"]
    assert last.rstrip().splitlines()[-1] == f"Current Date: {date.today().isoformat()}"
    assert "[HH:MM:SS]" in last


def test_refresh_appends_date_when_absent():
    from datetime import date

    msg = {"role": "system", "content": "STATIC\n\n任务说明"}
    subagent._refresh_system_current_date(msg)
    assert msg["content"].startswith("STATIC")
    assert msg["content"].rstrip().splitlines()[-1] == f"Current Date: {date.today().isoformat()}"


def test_refresh_system_current_date_non_system_skipped():
    msg = {"role": "user", "content": "hello"}
    subagent._refresh_system_current_date(msg)
    assert msg["content"] == "hello"


def test_run_agent_loop_passes_tool_timestamp(monkeypatch):
    from agent.generic import agent_loop as loop_mod

    captured = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)
        return iter(())

    monkeypatch.setattr(loop_mod, "agent_runner_loop", fake_loop)
    subagent._run_agent_loop(
        client=Mock(), system_prompt="sys", user_input="task",
        handler=Mock(_subagent_unique_name=None), tools_schema=[],
    )
    assert captured.get("tool_timestamp") is True
    assert "on_before_llm" not in captured


def test_no_callback_symbol_and_no_param():
    src = (Path(__file__).resolve().parents[1] / "agent/subagent.py").read_text(encoding="utf-8")
    assert "_refresh_subagent_current_time" not in src
    assert "on_before_llm" not in inspect.signature(subagent._run_agent_loop).parameters


def _capture_system_message(monkeypatch, *, break_builder=False):
    from unittest.mock import Mock
    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "BODY")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    if break_builder:
        monkeypatch.setattr(subagent, "build_subagent_system_segments",
                            Mock(side_effect=RuntimeError("boom")))

    subagent.call_subagent(
        agent_name="test-agent", task="t",
        llm_config={"apikey": "x", "apibase": "http://test", "model": "test"},
    )
    return captured


def test_assembled_system_ends_with_date(monkeypatch):
    from datetime import date

    cap = _capture_system_message(monkeypatch)
    content = cap["system_message"]["content"]
    assert content.rstrip().endswith(f"Current Date: {date.today().isoformat()}")
    assert subagent._TOOL_TIME_HINT in content


def test_degraded_builder_still_has_date(monkeypatch):
    from datetime import date

    cap = _capture_system_message(monkeypatch, break_builder=True)
    content = cap["system_message"]["content"]
    assert content.rstrip().endswith(f"Current Date: {date.today().isoformat()}")
    assert subagent._TOOL_TIME_HINT in content
