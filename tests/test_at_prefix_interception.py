"""@前缀子Agent意图识别单元测试"""
from unittest import mock


def test_ask_main_agent_impl_callable_directly(monkeypatch):
    """_ask_main_agent_impl 可被 content 拦截层直接调用（不经 MCP 工具派发）

    现有签名是 (question, unique_name) -> str，content 拦截层直接调即可。
    """
    import agent.main_agent_request_queue as maq_module
    import agent.subagent_registry as sr_module
    from agent import subagent
    from agent.ask_main_agent import AskMainAgentFuture

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
    import agent.subagent_registry as sr_module
    from agent import subagent

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
    """子 Agent content 以 @niu-agent 开头时，拦截层调 _ask_main_agent_impl 并把回答注入 messages"""
    from agent import subagent
    from agent.generic import agent_loop

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
    fake_handler._is_sync_subagent = False  # 显式设为 False，模拟异步子 Agent

    # 调拦截函数（注意：无 agent_name 参数）
    result = agent_loop._intercept_at_prefix_content(
        content="@niu-agent 我应该选择哪个选项？",
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
    assert messages[-2]["content"] == "@niu-agent 我应该选择哪个选项？"
    assert messages[-1]["role"] == "user"
    assert "主 Agent 的回答" in messages[-1]["content"]

    # 断言：返回 (INTERCEPTED, None)（让 agent_loop continue）
    assert result == (agent_loop.INTERCEPTED, None)


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

    assert result == (agent_loop.EXIT, None)
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

    assert result == (agent_loop.EXIT, None)
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

    assert result == (agent_loop.FORMAT_ERROR, None)
    # messages 被追加 assistant content + user 格式错误提示
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "我应该选择哪个选项？"
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]
    assert "@niu-agent" in messages[-1]["content"]
    assert "@end" in messages[-1]["content"]


def test_main_agent_path_not_intercepted(monkeypatch):
    """主 Agent 路径（_is_sync_subagent=False, memory_context=None）不拦截，允许 content 直接返回"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._is_sync_subagent = False  # 显式设为 False，模拟主 Agent 路径
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="任务完成的结果",  # 无 @ 前缀
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 主 Agent
    )

    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == 1  # messages 不被追加


def test_no_interception_when_tool_calls_present(monkeypatch):
    """有 tool_calls 时不拦截（正常工具调用）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._is_sync_subagent = False  # 显式设为 False，避免 MagicMock truthy 干扰
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="调工具",
        tool_calls=[{"id": "tc1", "function": {"name": "browser_navigate"}}],  # 有工具调用
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.NO_INTERCEPTION, None)


def test_at_niu_without_question_returns_format_error(monkeypatch):
    """@niu-agent 后无问题内容时返回 FORMAT_ERROR"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@niu-agent",  # @niu-agent 后无内容
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.FORMAT_ERROR, None)
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
        content="@niu-agent 问题",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.FORMAT_ERROR, None)
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]


def test_agent_runner_loop_intercepts_at_niu(monkeypatch):
    """agent_runner_loop 在 L473 拦截点调用 _intercept_at_prefix_content"""
    from agent.generic import agent_loop

    # 验证拦截函数可被 agent_loop 模块访问
    assert hasattr(agent_loop, "_intercept_at_prefix_content")
    assert hasattr(agent_loop, "INTERCEPTED")
    assert hasattr(agent_loop, "EXIT")
    assert hasattr(agent_loop, "FORMAT_ERROR")
    assert hasattr(agent_loop, "NO_INTERCEPTION")


def test_main_agent_not_intercepted(monkeypatch):
    """主 Agent 路径（memory_context=None）不被拦截，返回 NO_INTERCEPTION"""
    from agent import subagent
    from agent.generic import agent_loop

    # mock _ask_main_agent_impl 确保不被调用
    mock_ask = mock.Mock()
    monkeypatch.setattr(subagent, "_ask_main_agent_impl", mock_ask)

    fake_handler = mock.MagicMock()
    fake_handler._is_sync_subagent = False  # 显式设为 False，模拟主 Agent 路径
    messages = [{"role": "user", "content": "开始"}]

    # 主 Agent 路径：memory_context=None + LLM 返回纯文本（无 @ 前缀）
    result = agent_loop._intercept_at_prefix_content(
        content="这是主 Agent 的正常回复",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 主 Agent
    )

    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == 1  # messages 不被追加
    mock_ask.assert_not_called()  # _ask_main_agent_impl 不被调用


def test_sync_subagent_at_niu_returns_intercepted_sync(monkeypatch):
    """同步子 Agent（_is_sync_subagent=True, memory_context=None）输出 @niu-agent → 返回 (INTERCEPTED_SYNC, wrapped)"""
    from unittest import mock

    from agent import subagent
    from agent.generic import agent_loop

    monkeypatch.setattr(subagent, "_ask_main_agent_impl_sync", mock.Mock(return_value="[test] 问题"))

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test"
    fake_handler._is_sync_subagent = True  # 同步子 Agent
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@niu-agent 我应该选哪个？",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步子 Agent
    )

    status, payload = result
    assert status == agent_loop.INTERCEPTED_SYNC
    assert payload == "[test] 问题"


def test_main_agent_not_intercepted_after_change(monkeypatch):
    """主 Agent 路径（_is_sync_subagent=False, memory_context=None）仍返回 (NO_INTERCEPTION, None)"""
    from unittest import mock

    from agent.generic import agent_loop
    fake_handler = mock.MagicMock()
    fake_handler._is_sync_subagent = False  # 主 Agent
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="这是主 Agent 的正常回复",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )

    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == 1  # messages 不被追加


def test_intercept_main_agent_content_reply_to_sync_suspended_session():
    """主 Agent content @<同步挂起子名> 但无 tool_calls → 返回 FORMAT_ERROR（复用现有常量）"""
    from agent.generic.agent_loop import FORMAT_ERROR, _intercept_at_prefix_content
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    instance = SubagentRegistry.get("browser-operator")
    instance.state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        status, payload = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR
        assert payload is None
        assert len(messages) == 2
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "@browser-operator 我选择 2"
        assert messages[1]["role"] == "user"
        assert "chat-with-browser-operator" in messages[1]["content"]
        assert "answer" in messages[1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_no_suspended_session_no_interception():
    """主 Agent content @子名 但子名不在注册表 → NO_INTERCEPTION（不拦截）"""
    from agent.generic.agent_loop import NO_INTERCEPTION, _intercept_at_prefix_content

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    status, payload = _intercept_at_prefix_content(
        content="@browser-operator 我选择 2",
        tool_calls=[],
        messages=messages,
        handler=handler,
        memory_context=None,
    )
    assert status == NO_INTERCEPTION
    assert payload is None
    assert len(messages) == 0


def test_intercept_main_agent_with_tool_calls_no_interception():
    """主 Agent 调 chat-with-browser-operator 工具 → NO_INTERCEPTION（不拦截，正常工具调用）"""
    from agent.generic.agent_loop import NO_INTERCEPTION, _intercept_at_prefix_content
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    try:
        messages = []
        status, payload = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[{"type": "function", "function": {"name": "chat-with-browser-operator"}}],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_async_running_session_no_interception():
    """主 Agent content @<异步 running 子名> → NO_INTERCEPTION（异步路径不拦截，保持 db_monitor 原逻辑）"""
    from agent.generic.agent_loop import NO_INTERCEPTION, _intercept_at_prefix_content
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        is_sync=False,
    )

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    try:
        messages = []
        status, payload = _intercept_at_prefix_content(
            content=f"@{name} 补充上下文",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0
    finally:
        SubagentRegistry.unregister(name)


def test_intercept_main_agent_content_with_hex_suffix_old_format():
    """主 Agent content @browser-operator-708b（hex 后缀旧格式）→ 仍能拦截（兼容 LLM 复读历史日志）"""
    from agent.generic.agent_loop import FORMAT_ERROR, _intercept_at_prefix_content
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator-708b 我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR
        assert len(messages) == 2
        assert "chat-with-browser-operator" in messages[1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_content_with_chinese_punctuation():
    """主 Agent content @browser-operator。我选择 2（无空格中文句号）→ 仍能提取子名并拦截"""
    from agent.generic.agent_loop import FORMAT_ERROR, _intercept_at_prefix_content
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator。我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR
        assert len(messages) == 2
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_at_end_with_backtick_wrapper_allows_exit(monkeypatch):
    """@end 被反引号包装时也允许退出（识别范围放宽）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="`@end 任务完成`",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.EXIT, None)
    assert len(messages) == 1  # messages 不被追加


def test_at_end_with_double_quote_wrapper_allows_exit(monkeypatch):
    """@end 被双引号包装时也允许退出"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content='"@end 任务完成"',
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.EXIT, None)
    assert len(messages) == 1


def test_at_end_in_middle_allows_exit(monkeypatch):
    """@end 在 content 中间位置时也允许退出"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="blah blah @end 任务完成",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.EXIT, None)
    assert len(messages) == 1


def test_at_end_with_escape_prefix_not_recognized(monkeypatch):
    r"""@end 前字符是 \\ 时不识别为指令（转义）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content=r"\@end 任务完成",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    # 转义后 @end 不被识别为退出指令，落入格式错误分支
    assert result == (agent_loop.FORMAT_ERROR, None)
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]


def test_at_end_double_backslash_not_recognized(monkeypatch):
    r"""@end 前两个字符是 \\\\ 时按简单规则仍不识别（紧邻前字符是 \\）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content=r"\\@end 任务完成",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    # 简单规则：紧邻前一个字符是 \\ 就不识别（不判断是否为转义后的 \\）
    assert result == (agent_loop.FORMAT_ERROR, None)
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]


def test_at_niu_with_backtick_wrapper_triggers_intercept(monkeypatch):
    """@niu-agent 被反引号包装时也触发询问"""
    from agent import subagent
    from agent.generic import agent_loop

    # mock _ask_main_agent_impl 返回固定回答
    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="主 Agent 的回答")
    )

    messages = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始测试"},
    ]

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False  # 显式设为 False，走异步路径

    result = agent_loop._intercept_at_prefix_content(
        content="`@niu-agent 我该选哪个？`",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),  # 非 None（异步子 Agent）
    )

    # 断言：_ask_main_agent_impl 被调用
    subagent._ask_main_agent_impl.assert_called_once()
    call_kwargs = subagent._ask_main_agent_impl.call_args
    assert call_kwargs.kwargs["unique_name"] == "test-agent-abc1"

    # 断言：返回 (INTERCEPTED, None)
    assert result == (agent_loop.INTERCEPTED, None)


def test_at_end_priority_over_at_niu(monkeypatch):
    """@end 和 @niu-agent 同时出现时，@end 优先（子 Agent 已结束，处理提问无意义）"""
    from agent import subagent
    from agent.generic import agent_loop

    # mock _ask_main_agent_impl 返回固定回答（不应被调用）
    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="主 Agent 的回答")
    )

    messages = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始测试"},
    ]

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False

    result = agent_loop._intercept_at_prefix_content(
        content="@niu-agent 问个问题 @end 顺便退出",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    # 断言：走 @end 分支（EXIT），不走 @niu-agent 阻塞
    assert result == (agent_loop.EXIT, None)
    subagent._ask_main_agent_impl.assert_not_called()
