"""续跑：JSON 档与挂起恢复两条路径的 system 日期都改写为今天。"""
from datetime import date
from pathlib import Path

from agent.subagent import _prepare_resume_messages


def test_archive_resume_rewrites_legacy_time():
    out = _prepare_resume_messages([
        {"role": "system", "content": "S\n\nCurrent Time: 2020-01-01 10:00:00"},
        {"role": "user", "content": "task"},
    ])
    assert out and "Current Time" not in out[0]["content"]
    assert date.today().isoformat() in out[0]["content"]


def test_archive_resume_refreshes_stale_date():
    out = _prepare_resume_messages([
        {"role": "system", "content": "S\n\nCurrent Date: 2020-01-01"},
        {"role": "user", "content": "task"},
    ])
    assert out and "2020-01-01" not in out[0]["content"]
    assert date.today().isoformat() in out[0]["content"]


def test_archive_resume_list_content():
    out = _prepare_resume_messages([
        {"role": "system", "content": [
            {"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n\nCurrent Date: 2020-01-01"},
        ]},
        {"role": "user", "content": "task"},
    ])
    assert out and "2020-01-01" not in out[0]["content"][1]["text"]
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_resume_does_not_mutate_input():
    archive = [
        {"role": "system", "content": "S\n\nCurrent Date: 2020-01-01"},
        {"role": "user", "content": "task"},
    ]
    _prepare_resume_messages(archive)
    assert "2020-01-01" in archive[0]["content"]


def test_both_resume_paths_call_refresh():
    src = (Path(__file__).resolve().parents[1] / "agent/subagent.py").read_text(encoding="utf-8")
    assert src.count("_refresh_system_current_date(cleaned[0])") == 1
    assert src.count("_refresh_system_current_date(suspended_messages[0])") == 1
    # 顺序锁：必须在 _run_agent_loop 之前刷新（放到 return 之后同样命中字符串，但首请求仍是旧日期）
    assert src.index("_refresh_system_current_date(suspended_messages[0])") < src.index("resumed_messages=suspended_messages")


def test_suspended_resume_refreshes_system_date(monkeypatch):
    """行为锁：挂起恢复时传给循环的 resumed_messages[0] 已是今天的日期。"""
    import agent.runner as runner_mod
    from unittest.mock import Mock
    from agent import subagent
    from agent.subagent_registry import RunningSubagent, SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run)
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

    inst = RunningSubagent(
        unique_name="resume-date",
        agent_type="test-agent",
        supplement_queue=SubagentSupplementQueue(unique_name="resume-date"),
        state="waiting_for_answer",
        suspended_messages=[
            {"role": "system", "content": "S\n\nCurrent Time: 2020-01-01 10:00:00"},
            {"role": "user", "content": "orig"},
        ],
        suspended_handler=Mock(),
        suspended_client=Mock(),
        suspended_system_message={"role": "system", "content": "S"},
        suspended_tools_schema=[],
        source="user",
    )
    SubagentRegistry._instances["resume-date"] = inst
    try:
        subagent.call_subagent(
            agent_name="test-agent", task="ignored",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            answer="继续", answer_unique_name="resume-date",
        )
    finally:
        SubagentRegistry._instances.pop("resume-date", None)

    msgs = captured["resumed_messages"]
    assert date.today().isoformat() in msgs[0]["content"]
    assert "Current Time" not in msgs[0]["content"]
