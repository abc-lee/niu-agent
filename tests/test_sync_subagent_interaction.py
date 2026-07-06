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


def test_call_subagent_sync_uses_agent_name_as_unique_name(monkeypatch):
    """同步路径 unique_name 等于 agent_name（无随机 hex 后缀）"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry

    captured_unique_names = []

    def fake_run_agent_loop(**kwargs):
        handler = kwargs["handler"]
        captured_unique_names.append(getattr(handler, "_subagent_unique_name", ""))
        return "子 Agent 完成", {"result": "EXITED", "messages": [], "finish_reason": "exited"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        subagent.call_subagent(
            agent_name="browser-operator",
            task="测试任务",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        assert captured_unique_names == ["browser-operator"], f"unique_name 应为 browser-operator，实际：{captured_unique_names}"
    finally:
        for name in captured_unique_names:
            SubagentRegistry.unregister(name)


def test_call_subagent_sync_second_call_with_answer_resumes_suspended_session(monkeypatch):
    """第二次 call_subagent 传 answer + answer_unique_name=agent_name 能进入第三分支恢复 session"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 先注册一个同步挂起 session（模拟第一次 call_subagent 挂起的状态）
    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    instance = SubagentRegistry.get("browser-operator")
    instance.state = "waiting_for_answer"
    instance.suspended_messages = [{"role": "system", "content": "挂起的 messages"}]
    instance.suspended_handler = None
    instance.suspended_client = None
    instance.suspended_tools_schema = []
    instance.suspended_system_message = None

    # mock _run_agent_loop 第二次调用（回复路径）返回正常结束
    resumed_messages_seen = []
    def fake_run_agent_loop(**kwargs):
        resumed_messages_seen.append(kwargs.get("resumed_messages"))
        return "子 Agent 完成", {"result": "EXITED", "messages": [], "finish_reason": "exited"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        result = subagent.call_subagent(
            agent_name="browser-operator",
            task="",
            answer="@browser-operator 我选择 2",
            answer_unique_name="browser-operator",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        # 第三分支应进入并恢复 session（不应报错）
        assert "[错误]" not in result, f"不应报错，实际：{result}"
        # resumed_messages 应被透传（含挂起的 messages + 主 Agent 回答）
        assert len(resumed_messages_seen) == 1
        resumed = resumed_messages_seen[0]
        assert resumed is not None
        # 最后一条应是 [主 Agent 回答]
        assert "[主 Agent 回答]" in resumed[-1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_call_subagent_sync_second_call_same_agent_name_conflict(monkeypatch):
    """同步路径同类型已在跑 + 第二次不传 answer → 报错提示用 answer 参数"""
    from agent import subagent
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

    # 防御性 mock：修改前 product code 不抛 ValueError 时会跑到 LLM，避免真调 LLM
    def fake_run_agent_loop(**kwargs):
        return "不应被调到", {"result": "EXITED", "messages": [], "finish_reason": "exited"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        result = subagent.call_subagent(
            agent_name="browser-operator",
            task="第二个任务",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        assert "[错误]" in result
        assert "chat-with-browser-operator" in result or "已在运行" in result
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_call_subagent_gen_fallback_unique_name_to_agent_name(monkeypatch):
    """LLM 调 chat-with-browser-operator 传 answer 不传 unique_name → fallback 到 agent_name"""
    from agent import handler, subagent, runner as runner_mod
    from agent.handler import NiuHandler

    call_subagent_calls = []

    def fake_call_subagent(**kwargs):
        call_subagent_calls.append(kwargs.copy())
        return "子 Agent 完成"

    # mock handler 必需依赖
    h = NiuHandler.__new__(NiuHandler)
    h.mcp_client = None

    # _call_subagent_gen 内部 from .subagent import call_subagent → patch subagent.call_subagent
    monkeypatch.setattr(subagent, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    # _call_subagent_gen 内部 from .runner import get_runner → patch runner.get_runner
    monkeypatch.setattr(runner_mod, "get_runner", lambda: type("R", (), {"llm_config": {"model": "t", "api_key": "t", "base_url": "h"}})())

    # LLM 调 chat-with-browser-operator 传 answer 不传 unique_name
    gen = h._call_subagent_gen("browser-operator", {
        "task": "",
        "answer": "@browser-operator 我选择 2",
        # 不传 unique_name
    })
    list(gen)  # 消费生成器

    assert len(call_subagent_calls) == 1
    call_kwargs = call_subagent_calls[0]
    # answer_unique_name 应 fallback 到 agent_name
    assert call_kwargs.get("answer_unique_name") == "browser-operator", \
        f"answer_unique_name 应 fallback 到 browser-operator，实际：{call_kwargs.get('answer_unique_name')}"
    assert call_kwargs.get("answer") == "@browser-operator 我选择 2"


def test_call_subagent_gen_explicit_unique_name_overrides_fallback(monkeypatch):
    """LLM 显式传 unique_name 时不用 fallback"""
    from agent import handler, subagent, runner as runner_mod
    from agent.handler import NiuHandler

    call_subagent_calls = []
    def fake_call_subagent(**kwargs):
        call_subagent_calls.append(kwargs.copy())
        return "子 Agent 完成"

    h = NiuHandler.__new__(NiuHandler)
    h.mcp_client = None

    monkeypatch.setattr(subagent, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(runner_mod, "get_runner", lambda: type("R", (), {"llm_config": {"model": "t", "api_key": "t", "base_url": "h"}})())

    gen = h._call_subagent_gen("browser-operator", {
        "task": "",
        "answer": "@browser-operator 回答",
        "unique_name": "browser-operator",  # 显式传
    })
    list(gen)

    assert call_subagent_calls[0].get("answer_unique_name") == "browser-operator"


def test_e2e_main_agent_content_reply_intercepted_before_yield(monkeypatch):
    """端到端：主 Agent content 误回复同步挂起子 Agent 时，拦截层在 yield 前捕获，LLM 重做。

    模拟主 Agent 下一轮输出 content @browser-operator 回答 但无 tool_calls：
    - 拦截层应返回 FORMAT_ERROR（v3 复用现有常量）
    - agent_runner_loop 应 continue（不 yield content 给前端）
    - messages 应含 assistant content + user 错误提示
    - LLM 下一轮应看到错误提示并改用工具回复
    """
    from agent.generic.agent_loop import _intercept_at_prefix_content, FORMAT_ERROR, NO_INTERCEPTION
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册同步挂起 session
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
        # 第一轮：主 Agent 误用 content 回复
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR
        assert len(messages) == 2
        assert "chat-with-browser-operator" in messages[1]["content"]

        # 模拟 LLM 下一轮看到错误提示后改用工具
        # tool_calls 非空时拦截层应返回 NO_INTERCEPTION（正常工具调用）
        messages.clear()
        status, _ = _intercept_at_prefix_content(
            content="",  # 调工具时 content 通常为空
            tool_calls=[{"type": "function", "function": {"name": "chat-with-browser-operator"}}],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0  # 工具调用不拦截
    finally:
        SubagentRegistry.unregister("browser-operator")
