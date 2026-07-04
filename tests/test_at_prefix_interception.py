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


def test_at_niu_prefix_triggers_ask_main_agent(monkeypatch):
    """子 Agent content 以 @niu 开头时，拦截层调 _ask_main_agent_impl 并把回答注入 messages"""
    from agent.generic import agent_loop
    from agent import subagent

    # mock _ask_main_agent_impl 返回固定回答
    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="主 Agent 的回答")
    )

    # 构造 messages 列表
    messages = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始测试"},
        {"role": "assistant", "content": "好的"},
    ]

    # 构造 handler 带 _subagent_unique_name
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"

    # 调拦截函数（注意：无 agent_name 参数）
    result = agent_loop._intercept_at_prefix_content(
        content="@niu 我应该选择哪个选项？",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),  # 非 None（异步子 Agent）
    )

    # 断言：_ask_main_agent_impl 被调用（只传 question + unique_name）
    subagent._ask_main_agent_impl.assert_called_once()
    call_kwargs = subagent._ask_main_agent_impl.call_args
    assert call_kwargs.kwargs["question"] == "我应该选择哪个选项？"
    assert call_kwargs.kwargs["unique_name"] == "test-agent-abc1"

    # 断言：messages 被追加了 assistant content + user 回答（不是 tool 消息）
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "@niu 我应该选择哪个选项？"
    assert messages[-1]["role"] == "user"
    assert "主 Agent 的回答" in messages[-1]["content"]

    # 断言：返回 INTERCEPTED（让 agent_loop continue）
    assert result == agent_loop.INTERCEPTED


def test_at_end_prefix_allows_exit_with_space(monkeypatch):
    """@end 带空格时允许退出"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@end 任务已完成，结果：成功",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.EXIT
    assert len(messages) == 1  # messages 不被追加


def test_at_end_prefix_allows_exit_without_space(monkeypatch):
    """@end 无空格（如 @end任务完成）也允许退出"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@end任务完成",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.EXIT
    assert len(messages) == 1


def test_no_at_prefix_no_tool_calls_returns_format_error(monkeypatch):
    """子 Agent content 无 @ 前缀且无 tool_calls 时，返回 FORMAT_ERROR 并追加提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="我应该选择哪个选项？",  # 无 @ 前缀
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.FORMAT_ERROR
    # messages 被追加 assistant content + user 格式错误提示
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "我应该选择哪个选项？"
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]
    assert "@niu" in messages[-1]["content"]
    assert "@end" in messages[-1]["content"]


def test_no_interception_for_sync_subagent(monkeypatch):
    """同步子 Agent（memory_context=None）不拦截，允许 content 直接返回"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="任务完成的结果",  # 无 @ 前缀
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步子 Agent
    )

    assert result == agent_loop.NO_INTERCEPTION
    assert len(messages) == 1  # messages 不被追加


def test_no_interception_when_tool_calls_present(monkeypatch):
    """有 tool_calls 时不拦截（正常工具调用）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="调工具",
        tool_calls=[{"id": "tc1", "function": {"name": "browser_navigate"}}],  # 有工具调用
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.NO_INTERCEPTION


def test_at_niu_without_question_returns_format_error(monkeypatch):
    """@niu 后无问题内容时返回 FORMAT_ERROR"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@niu",  # @niu 后无内容
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.FORMAT_ERROR
    # messages 被追加格式错误提示
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]


def test_at_niu_without_unique_name_returns_format_error(monkeypatch):
    """handler 无 _subagent_unique_name 时返回 FORMAT_ERROR"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = ""  # 空 unique_name
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@niu 问题",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == agent_loop.FORMAT_ERROR
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]
