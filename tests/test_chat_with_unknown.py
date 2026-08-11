"""chat-with-* 调用不存在的子 Agent → 明确错误反馈。"""
import pytest


def test_chat_with_unknown_agent_rejected(monkeypatch):
    """不存在的 agent → StepOutcome error（主 Agent 收到明确反馈）。"""
    from agent.handler import NiuHandler
    h = NiuHandler.__new__(NiuHandler)
    # 模拟 get_subagent_config 返回空 dict（不存在）
    monkeypatch.setattr("agent.subagent.get_subagent_config", staticmethod(lambda name: {}))

    # 直接调用路由逻辑（提取为可测函数或走 dispatch）
    from agent.handler import _check_chat_with_agent_exists
    ok, msg = _check_chat_with_agent_exists("nonexistent-agent")
    assert ok is False
    assert "不存在" in msg


def test_chat_with_existing_agent_allowed(monkeypatch):
    """存在的 agent（~/.niu/agents/nutritionist.md）→ 放行。"""
    from agent.handler import _check_chat_with_agent_exists
    monkeypatch.setattr("agent.subagent.get_subagent_config",
                        staticmethod(lambda name: {"description": "x"} if name == "nutritionist" else {}))
    ok, msg = _check_chat_with_agent_exists("nutritionist")
    assert ok is True
