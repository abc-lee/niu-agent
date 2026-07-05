"""同步子 Agent 交互单元测试"""


def test_ask_main_agent_impl_sync_appends_assistant_and_returns_wrapped():
    """_ask_main_agent_impl_sync 调用后 messages append assistant content + 返回 [unique_name] question"""
    from agent import subagent

    messages = [{"role": "user", "content": "开始"}]
    fake_handler = object()  # 不需要 handler 属性

    wrapped = subagent._ask_main_agent_impl_sync(
        question="我应该选择哪个选项？",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent 我应该选择哪个选项？",
    )

    # 断言：messages append assistant content
    assert messages[-1] == {"role": "assistant", "content": "@niu-agent 我应该选择哪个选项？"}
    # 断言：返回 wrapped 文本
    assert wrapped == "[test-ab12] 我应该选择哪个选项？"
    # 断言：messages 末尾是 assistant（不是 user）
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_ask_main_agent_impl_sync_sanitizes_question():
    """_ask_main_agent_impl_sync 对 question 做 sanitization（限 2000 字符 + strip 行首 @）"""
    from agent import subagent

    messages = []
    fake_handler = object()

    # 超长 question 截断
    long_question = "x" * 3000
    wrapped = subagent._ask_main_agent_impl_sync(
        question=long_question,
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent ...",
    )
    assert len(wrapped) < 3000  # 已截断

    # question 行首 @ 被 strip
    wrapped2 = subagent._ask_main_agent_impl_sync(
        question="@嵌套@问题",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent @嵌套@问题",
    )
    assert wrapped2 == "[test-ab12] 嵌套@问题"  # 行首 @ 被 strip


def test_agent_runner_loop_resumed_messages_skips_construction(monkeypatch):
    """agent_runner_loop 收到 resumed_messages → 跳过 system_message + history + user_input 构造"""
    from agent.generic import agent_loop
    from unittest import mock

    # mock LLM client——必须返回生成器（agent_loop.py 用 exhaust(response_gen) 调 next()）
    # MagicMock 不是迭代器，next() 会抛 TypeError，用 fake_chat_gen 模拟
    fake_response = mock.MagicMock()
    fake_response.content = "@end 任务完成"
    fake_response.tool_calls = None
    fake_response.usage = None

    def fake_chat_gen():
        """模拟流式生成器：yield 一个 chunk 后 StopIteration.value = fake_response"""
        yield
        return fake_response

    fake_client = mock.MagicMock()
    fake_client.chat.return_value = fake_chat_gen()

    fake_handler = mock.MagicMock()
    fake_handler._is_subagent = True
    fake_handler._is_sync_subagent = True  # 显式设，避免 truthy Mock 语义问题
    fake_handler._subagent_unique_name = "test-ab12"

    # resumed_messages：已是 LLM-ready 格式（含 system + 历史 + user）
    resumed = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "@niu-agent 问题"},
        {"role": "user", "content": "[主 Agent 回答] 选 A"},
    ]

    system_message = {"role": "system", "content": "你是子 Agent"}
    gen = agent_loop.agent_runner_loop(
        client=fake_client,
        system_prompt="",
        system_message=system_message,
        user_input="不应被用",
        handler=fake_handler,
        tools_schema=[],
        max_turns=5,
        initial_user_content=None,
        context_window_tokens=100000,
        context_fifo_threshold=75000,
        context_target_threshold=30000,
        history=[],
        memory_context=None,
        resumed_messages=resumed,
    )

    events = list(gen)
    # 验证：LLM 调用时 messages 是 resumed，不含"不应被用"的 user_input
    call_kwargs = fake_client.chat.call_args
    messages_passed = call_kwargs.kwargs.get("messages", call_kwargs.args[0] if call_kwargs.args else None)
    # resumed 的最后一条是 user "[主 Agent 回答] 选 A"，不是"不应被用"
    assert messages_passed[-1]["content"] == "[主 Agent 回答] 选 A"


def test_call_subagent_top_validation_no_task_no_answer():
    """call_subagent 顶部校验：无 task + 无 answer → 返回错误文本"""
    from agent import subagent
    result = subagent.call_subagent(
        agent_name="file-processor",
        task="",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
    )
    assert "[错误]" in result
    assert "必须传 task" in result or "answer" in result


def test_call_subagent_third_branch_agent_type_mismatch(monkeypatch):
    """第三分支 agent_type 不匹配 → 返回错误文本"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry, RunningSubagent
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册一个 agent_type="A" 的 session
    sq = SubagentSupplementQueue(unique_name="")
    unique_name = SubagentRegistry.register("A", sq)
    sq.unique_name = unique_name
    instance = SubagentRegistry.get(unique_name)
    instance.state = "waiting_for_answer"

    # 用 agent_name="B" 调第三分支
    result = subagent.call_subagent(
        agent_name="B",
        task="",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        answer="@xxx 回答",
        answer_unique_name=unique_name,
    )

    SubagentRegistry.unregister(unique_name)  # 清理
    assert "[错误]" in result
    assert "不属于" in result
