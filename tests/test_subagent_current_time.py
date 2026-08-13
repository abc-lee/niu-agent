"""子 Agent Current Time 会话内每轮刷新测试。

背景：子 Agent system_message 在 call_subagent 启动时构建一次（含启动时刻 Current Time，
build_subagent_system_segments 实时取 datetime.now），长任务（context-manager 压缩 20+ 轮、
dream-evolver 跨午夜）会话内时间漂移。修复后 call_subagent 三处 _run_agent_loop 调用传
on_before_llm=_refresh_subagent_current_time——每轮 LLM 前刷新 Current Time。
"""
from unittest.mock import patch

from agent.subagent import _refresh_subagent_current_time


def test_refresh_string_format():
    """字符串格式：Current Time 行更新为实时值，其余文本不变。"""
    messages = [{"role": "system", "content": "STATIC\n\nCurrent Time: 2026-08-13 10:00:00\n\n任务"}]
    with patch("datetime.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-08-13 18:30:00"
        _refresh_subagent_current_time(messages, 1)
    content = messages[0]["content"]
    assert "Current Time: 2026-08-13 18:30:00" in content
    assert "Current Time: 2026-08-13 10:00:00" not in content
    assert content.startswith("STATIC")
    assert "任务" in content


def test_refresh_claude_list_format():
    """Claude list 格式：只更新动态段 Current Time，静态段与 cache_control 不变。"""
    messages = [{"role": "system", "content": [
        {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nCurrent Time: 2026-08-13 10:00:00"},
    ]}]
    with patch("datetime.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-08-13 18:30:00"
        _refresh_subagent_current_time(messages, 1)
    assert messages[0]["content"][0]["text"] == "STATIC"
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[0]["content"][1]["text"] == "\n\nCurrent Time: 2026-08-13 18:30:00"


def test_refresh_no_match_unchanged():
    """无 Current Time 行时 content 原样保留（不抛异常）。"""
    messages = [{"role": "system", "content": "STATIC\n\n任务说明"}]
    with patch("datetime.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-08-13 18:30:00"
        _refresh_subagent_current_time(messages, 1)
    assert messages[0]["content"] == "STATIC\n\n任务说明"


def test_refresh_non_system_first_msg_skipped():
    """messages[0] 非 system 时跳过（不抛异常）。"""
    messages = [{"role": "user", "content": "hello"}]
    _refresh_subagent_current_time(messages, 1)
    assert messages[0]["content"] == "hello"


def test_call_subagent_passes_on_before_llm(monkeypatch):
    """call_subagent 同步路径必须把 _refresh_subagent_current_time 透传给 _run_agent_loop。

    接线测试（R1-P1 修复）：_run_agent_loop 签名 + 三调用点传参必须有测试锁定，
    否则绿相通过时生产 TypeError（unexpected keyword argument 'on_before_llm'）。
    范式照抄 TestSubagentMaxTurnsPassthrough（tests/test_subagent_overflow.py）。
    """
    from unittest.mock import Mock

    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured.update(kwargs)
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

    subagent.call_subagent(
        agent_name="test-agent",
        task="test",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert "on_before_llm" in captured, f"_run_agent_loop 未收到 on_before_llm: {captured}"
    assert captured["on_before_llm"] is subagent._refresh_subagent_current_time


def test_call_subagent_resume_passes_on_before_llm(monkeypatch):
    """resume 路径（answer 非 None）同样透传 on_before_llm（挂起 system 恢复后每轮刷新）。"""
    from unittest.mock import Mock

    import agent.runner as runner_mod
    from agent import subagent
    from agent.subagent_registry import RunningSubagent, SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured.update(kwargs)
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

    inst = RunningSubagent(
        unique_name="resume-ct",
        agent_type="test-agent",
        supplement_queue=SubagentSupplementQueue(unique_name="resume-ct"),
        state="waiting_for_answer",
        suspended_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "orig"}],
        suspended_handler=Mock(),
        suspended_client=Mock(),
        suspended_system_message={"role": "system", "content": "sys"},
        suspended_tools_schema=[],
        source="user",
    )
    SubagentRegistry._instances["resume-ct"] = inst
    try:
        subagent.call_subagent(
            agent_name="test-agent",
            task="ignored (resume path)",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            answer="继续任务",
            answer_unique_name="resume-ct",
        )
    finally:
        SubagentRegistry._instances.pop("resume-ct", None)

    assert "on_before_llm" in captured, f"resume 路径未收到 on_before_llm: {captured}"
    assert captured["on_before_llm"] is subagent._refresh_subagent_current_time


def test_call_subagent_async_passes_on_before_llm(monkeypatch):
    """异步路径（unique_name 非 None + answer None）同样透传 on_before_llm（R2-P2 补齐）。

    三处调用点必须都有测试锁定——漏改异步调用点绿相仍通过（on_before_llm 默认 None
    无 TypeError），异步子 Agent 静默不刷新。
    """
    from unittest.mock import Mock

    import agent.runner as runner_mod
    from agent import subagent
    from agent.subagent_registry import RunningSubagent, SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured.update(kwargs)
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

    inst = RunningSubagent(
        unique_name="async-ct",
        agent_type="test-agent",
        supplement_queue=SubagentSupplementQueue(unique_name="async-ct"),
        state="running",
        suspended_messages=[],
        suspended_handler=Mock(),
        suspended_client=Mock(),
        suspended_system_message=None,
        suspended_tools_schema=[],
        source="program",
    )
    SubagentRegistry._instances["async-ct"] = inst
    try:
        subagent.call_subagent(
            agent_name="test-agent",
            task="async task",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            unique_name="async-ct",
        )
    finally:
        SubagentRegistry._instances.pop("async-ct", None)

    assert "on_before_llm" in captured, f"异步路径未收到 on_before_llm: {captured}"
    assert captured["on_before_llm"] is subagent._refresh_subagent_current_time
