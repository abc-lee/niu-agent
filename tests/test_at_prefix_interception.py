"""@前缀子Agent意图识别单元测试"""
from unittest import mock


def test_ask_main_agent_impl_callable_directly(monkeypatch):
    """_ask_main_agent_impl 可被 content 拦截层直接调用（不经 MCP 工具派发）

    现有签名是 (question, unique_name) -> str，content 拦截层直接调即可。
    """
    from agent import subagent
    from agent.ask_main_agent import AskMainAgentFuture
    import agent.subagent_registry as sr_module
    import agent.main_agent_request_queue as maq_module

    # mock registry（SubagentRegistry 在 subagent_registry 模块，函数内 import）
    fake_instance = mock.MagicMock()
    fake_instance._ask_terminated = False
    monkeypatch.setattr(sr_module, "SubagentRegistry", mock.MagicMock())
    sr_module.SubagentRegistry.get = mock.Mock(return_value=fake_instance)

    # mock push queue（get_main_agent_request_queue 在 main_agent_request_queue 模块）
    pushed = []
    fake_queue = mock.MagicMock()
    fake_queue.push = mock.Mock(side_effect=lambda x: pushed.append(x))
    monkeypatch.setattr(maq_module, "get_main_agent_request_queue", mock.Mock(return_value=fake_queue))

    # mock future wait 立即返回
    with mock.patch.object(AskMainAgentFuture, "wait", return_value="主 Agent 的回答"):
        result = subagent._ask_main_agent_impl(
            question="我应该选择哪个选项？",
            unique_name="test-agent-abc1",
        )

    assert result == "主 Agent 的回答"
    assert len(pushed) == 1
    assert "test-agent-abc1" in pushed[0]
    assert "我应该选择哪个选项？" in pushed[0]


def test_ask_main_agent_impl_returns_terminated_when_cancelled(monkeypatch):
    """子 Agent 被 cancel 后（_ask_terminated=True），_ask_main_agent_impl 返回终止状态文本"""
    from agent import subagent
    import agent.subagent_registry as sr_module

    fake_instance = mock.MagicMock()
    fake_instance._ask_terminated = True  # 已被 cancel
    monkeypatch.setattr(sr_module, "SubagentRegistry", mock.MagicMock())
    sr_module.SubagentRegistry.get = mock.Mock(return_value=fake_instance)

    result = subagent._ask_main_agent_impl(
        question="问题",
        unique_name="test-agent-abc1",
    )
    # _ask_terminated=True 时返回终止提示文本（不是 TERMINATED_SIGNAL）
    assert "已终止" in result
    assert "停止指令" in result
