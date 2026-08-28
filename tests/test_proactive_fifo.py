"""最小验证测试：确保 agent_runner_loop 的 mock 方式正确"""
import json
from unittest.mock import Mock

from agent.generic.agent_loop import StepOutcome, StreamEvent, agent_runner_loop
from agent.generic.llmcore import MockResponse


def _make_mock_response(content="hello", tool_calls=None):
    """创建一个模拟的 LLM 响应对象。"""
    resp = Mock()
    resp.content = content
    resp.tool_calls = tool_calls or []
    resp.context_overflow = False
    resp.stream_error = False  # 关键：裸 Mock 的 stream_error 自动属性为真，会触发 LLM_ERROR 提前退出
    return resp


def _make_tool_call(name="some_tool", args=None, tid="call_1"):
    """创建一个模拟的 tool_call 对象。"""
    tc = Mock()
    tc.id = tid
    tc.function = Mock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _make_client(responses):
    """创建 mock client — chat 必须返回生成器且 StopIteration.value = 最后一个 response。

    关键发现：agent_runner_loop 中 verbose=False 时用 exhaust() 消费生成器，
    exhaust() 依赖 StopIteration.value 获取最终 response。
    所以生成器必须 yield resp 然后 return resp。
    """
    client = Mock()
    client.last_tools = ""
    idx = [0]
    call_count = [0]  # 跟踪调用次数

    def chat(**kwargs):
        call_count[0] += 1
        resp = responses[idx[0]]
        idx[0] += 1
        def gen():
            yield resp
            return resp
        return gen()

    chat.call_count = property(lambda self: call_count[0])  # 兼容 Mock.call_count 风格
    client.chat = chat
    client._chat_call_count = call_count  # 供测试直接访问
    return client


def _make_handler(dispatch_fn=None):
    """创建可用的 handler mock。

    关键发现：handler.dispatch 必须是生成器函数（yield + return），
    因为 agent_runner_loop 中用 yield from 或 exhaust() 消费它。
    """
    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler._current_messages = []
    handler.current_turn = 0
    # 关键：裸 Mock 的 _is_sync_subagent/_bypass_at_prefix 自动属性 truthy，
    # 会令 _intercept_at_prefix_content 把普通 content 回复误判为子 Agent 格式错误。显式置 False。
    handler._is_sync_subagent = False
    handler._bypass_at_prefix = False

    if dispatch_fn:
        handler.dispatch = dispatch_fn
    else:
        def default_dispatch(tool_name, args, response, index=0):
            # no_tool 场景：next_prompt 为空，触发退出
            if tool_name == "no_tool":
                yield
                return StepOutcome(data=None, next_prompt=None, should_exit=False)
            # 其他工具：返回 next_prompt 继续循环
            yield
            return StepOutcome(data="ok", next_prompt="继续", should_exit=False)
        handler.dispatch = default_dispatch

    handler.next_prompt_patcher = lambda np, outcome, turn: np
    return handler


def _collect_events(gen):
    """收集生成器产生的所有 yield 值，忽略返回值。"""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration:
        pass
    return events


def test_agent_loop_basic_no_tool_calls():
    """验证：agent_loop 能正常消费 mock client，返回 CURRENT_TASK_DONE。

    场景：verbose=False，LLM 返回纯文本回复（无工具调用）。
    预期：产生 chat_busy -> reply -> persist -> chat_idle 事件序列，
          最终返回 {"result": "CURRENT_TASK_DONE", ...}。
    """
    handler = _make_handler()
    mock_client = _make_client([
        _make_mock_response(content="Hello!", tool_calls=[]),
    ])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="hi",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
    )
    events = _collect_events(gen)

    # 应该有 reply 事件
    reply_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "reply"]
    assert len(reply_events) == 1, f"Expected 1 reply event, got {len(reply_events)}: {reply_events}"
    assert "Hello!" in reply_events[0].content, f"Expected 'Hello!' in reply, got: {reply_events[0].content}"

    # 应该有 chat_busy 和 chat_idle 系统事件
    system_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "system"]
    system_contents = [e.content for e in system_events]
    assert "chat_busy" in system_contents, f"Expected chat_busy in system events, got: {system_contents}"
    assert "chat_idle" in system_contents, f"Expected chat_idle in system events, got: {system_contents}"

    # 应该有 persist 事件（纯文本回复的 assistant 消息）
    persist_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "persist"]
    assert len(persist_events) >= 1, f"Expected at least 1 persist event, got {len(persist_events)}"


def test_agent_loop_with_tool_call():
    """验证：agent_loop 能正确处理工具调用场景。

    场景：verbose=False，LLM 先调用工具，然后返回纯文本回复。
    预期：两轮循环，最终返回 CURRENT_TASK_DONE。
    """
    tc = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    resp2 = _make_mock_response(content="Search result: found", tool_calls=[])

    mock_client = _make_client([resp1, resp2])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="search for test",
        handler=handler, tools_schema=[], max_turns=5, verbose=False,
    )
    events = _collect_events(gen)

    # 应该有最终的 reply 事件
    reply_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "reply"]
    assert len(reply_events) >= 1, f"Expected at least 1 reply event, got {len(reply_events)}"
    assert "Search result: found" in reply_events[-1].content

    # 应该有 persist 事件（assistant tool_calls + tool result + final assistant）
    persist_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "persist"]
    assert len(persist_events) >= 2, f"Expected at least 2 persist events, got {len(persist_events)}"


def test_mockresponse_usage_parameter():
    """验证：MockResponse 接受 usage 参数"""
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    resp = MockResponse(thinking="", content="test", tool_calls=[], raw="", usage=usage)
    assert resp.usage == usage
    # 不传 usage 时默认为 None
    resp2 = MockResponse(thinking="", content="test", tool_calls=[], raw="")
    assert resp2.usage is None


def test_client_chat_generator_pattern():
    """验证：client.chat 返回的生成器必须 yield resp + return resp。

    agent_runner_loop 中 verbose=False 时用 exhaust() 消费生成器：
    exhaust() 反复调用 next() 直到 StopIteration，然后取 StopIteration.value。
    所以生成器必须 return 最终的 response，否则 exhaust() 返回 None。
    """
    resp = _make_mock_response(content="test", tool_calls=[])

    # 正确模式：yield resp + return resp
    def correct_gen():
        yield resp
        return resp

    from agent.generic.agent_loop import exhaust
    result = exhaust(correct_gen())
    assert result is resp, f"exhaust() should return the response, got {result}"

    # 错误模式：只 yield 不 return（StopIteration.value 为 None）
    def wrong_gen():
        yield resp

    result2 = exhaust(wrong_gen())
    assert result2 is None, f"exhaust() without return should give None, got {result2}"


def test_handler_dispatch_must_be_generator():
    """验证：handler.dispatch 必须是生成器函数。

    agent_runner_loop 中：
    - verbose=True 时用 yield from 消费 dispatch
    - verbose=False 时用 exhaust() 消费 dispatch
    两者都要求 dispatch 返回生成器。
    """
    handler = _make_handler()

    # dispatch 返回生成器
    gen = handler.dispatch("no_tool", {}, Mock(), index=0)
    assert hasattr(gen, "__iter__"), "dispatch must return an iterable (generator)"
    assert hasattr(gen, "__next__"), "dispatch must return a generator (has __next__)"


def test_is_context_overflow_error_all_patterns():
    """验证 _is_context_overflow_error 覆盖所有已知模式"""
    from agent.generic.litellm_adapter import _is_context_overflow_error

    # 所有已知的 overflow 模式
    assert _is_context_overflow_error(Exception("context_length_exceeded"))
    assert _is_context_overflow_error(Exception("maximum context length"))
    assert _is_context_overflow_error(Exception("prompt is too long"))
    assert _is_context_overflow_error(Exception("prompt: length"))
    assert _is_context_overflow_error(Exception("exceed context limit"))
    assert _is_context_overflow_error(Exception("is longer than the model's context length"))
    assert _is_context_overflow_error(Exception("input tokens exceed the configured limit"))
    assert _is_context_overflow_error(Exception("exceeds the maximum number of tokens"))
    assert _is_context_overflow_error(Exception("input is too long"))
    assert _is_context_overflow_error(Exception("context window exceeded"))

    # 非 overflow 不匹配
    assert not _is_context_overflow_error(Exception("rate limit exceeded"))
    assert not _is_context_overflow_error(Exception("internal server error"))


# =============================================================================
# 上下文使用率检测测试（prompt_tokens 驱动）
# =============================================================================


def test_main_agent_calls_callback_on_high_usage():
    """主 Agent prompt_tokens > 80% → 调用 on_context_high_usage 回调，循环不退出

    关键：第一轮必须有 tool_calls 才能继续到第二轮（无 tool_calls 会退出）。
    所以我们让第一轮有 tool_calls，第二轮无 tool_calls（正常结束）。
    """
    handler = _make_handler()
    callback_called = {"count": 0, "args": None}

    def my_callback(messages, tokens, limit):
        callback_called["count"] += 1
        callback_called["args"] = (tokens, limit)

    # 第一轮：高使用率（170K/200K = 85% > 80%）+ 有 tool_calls（继续循环）
    tc1 = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc1])
    resp1.usage = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}

    # 第二轮：正常使用率（90K/200K = 45% < 80%）+ 无 tool_calls（退出）
    resp2 = _make_mock_response(content="Done", tool_calls=[])
    resp2.usage = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}

    mock_client = _make_client([resp1, resp2])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=5, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    )
    _collect_events(gen)

    # 回调应该被调用（第二轮开始时检测到第一轮的 170K prompt_tokens）
    assert callback_called["count"] >= 1, f"Callback should be called at least once, got {callback_called['count']}"
    assert callback_called["args"][0] == 170000, f"Expected tokens=170000, got {callback_called['args']}"
def test_gate_deferred_callback_does_not_cooldown(monkeypatch):
    """P2 回归：回调被闸门拒绝返回 False（真值低于 80% 触发线）时不得置
    _compress_cooldown——本 loop 内后续轮次真值达线须再次触发回调。

    旧行为：回调后无条件冷却 → warningThreshold(70%)<触发线(80%) 时首次
    回调即停摆本 loop 检测，第二次 85% 永远不会被评估。
    """
    import agent.generic.agent_loop as loop_mod
    monkeypatch.setattr(loop_mod, "_read_warning_threshold", lambda: 0.70)

    handler = _make_handler()
    calls = []

    def my_callback(messages, tokens, limit):
        calls.append(tokens)
        return len(calls) == 2  # 第 1 次=闸门拒绝 False；第 2 次=过闸压实 True

    tc1 = _make_tool_call(name="search", args={"q": "a"}, tid="call_1")
    tc2 = _make_tool_call(name="search", args={"q": "b"}, tid="call_2")
    resp1 = _make_mock_response(content="", tool_calls=[tc1])
    resp1.usage = {"prompt_tokens": 150000, "completion_tokens": 500,
                   "total_tokens": 150500}  # 75%：>warning(70%) <触发线(80%)
    resp2 = _make_mock_response(content="", tool_calls=[tc2])
    resp2.usage = {"prompt_tokens": 170000, "completion_tokens": 500,
                   "total_tokens": 170500}  # 85%：达线
    resp3 = _make_mock_response(content="Done", tool_calls=[])
    resp3.usage = {"prompt_tokens": 90000, "completion_tokens": 200,
                   "total_tokens": 90200}

    mock_client = _make_client([resp1, resp2, resp3])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=5, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    )
    _collect_events(gen)

    assert calls == [150000, 170000], (
        f"闸门拒绝不置冷却：两次真值均应触发回调，实际 {calls}"
    )
    assert mock_client._chat_call_count[0] == 3


def test_sub_agent_fifo_pruning():
    """子 Agent prompt_tokens > 80% → FIFO 裁剪（不调回调）

    子 Agent 特征：on_context_high_usage=None
    """
    handler = _make_handler()

    # 第一轮：高使用率 + 有 tool_calls
    tc1 = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc1])
    resp1.usage = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}

    # 第二轮：正常 + 无 tool_calls
    resp2 = _make_mock_response(content="Done", tool_calls=[])
    resp2.usage = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}

    mock_client = _make_client([resp1, resp2])

    # 构建大量 history 让 FIFO 有东西可裁剪
    big_history = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "y" * 2000}] * 40

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=5, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=None,
        history=big_history,
    )
    _collect_events(gen)

    # 子Agent不调回调（on_context_high_usage=None）
    # 第二轮调用应该发生（循环没退出）
    assert mock_client._chat_call_count[0] == 2, f"Expected 2 chat calls, got {mock_client._chat_call_count[0]}"


def test_no_pruning_when_below_warning():
    """prompt_tokens < 80% 时不裁剪也不调回调"""
    handler = _make_handler()
    callback_called = {"count": 0}

    def my_callback(messages, tokens, limit):
        callback_called["count"] += 1

    # 使用率 50%（100K/200K < 80%）
    resp = _make_mock_response(content="OK", tool_calls=[])
    resp.usage = {"prompt_tokens": 100000, "completion_tokens": 500, "total_tokens": 100500}
    mock_client = _make_client([resp])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    )
    _collect_events(gen)

    # 回调不应该被调用（因为 50% < 80%）
    assert callback_called["count"] == 0, f"Callback should not be called, got {callback_called['count']}"


def test_context_overflow_still_works():
    """context_overflow 标记仍然触发 CONTEXT_OVERFLOW"""
    handler = _make_handler()
    resp = _make_mock_response(content="", tool_calls=[])
    resp.context_overflow = True
    resp.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    mock_client = _make_client([resp])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=lambda m, t, limit: None,
    )
    events = _collect_events(gen)

    # 应该返回 CONTEXT_OVERFLOW（通过 StopIteration.value）
    # 注意：_collect_events 只收集 yield 值，不捕获返回值
    # 我们需要检查是否有 chat_idle 事件（CONTEXT_OVERFLOW 退出时会 yield chat_idle）
    system_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "system"]
    system_contents = [e.content for e in system_events]
    assert "chat_idle" in system_contents, f"Expected chat_idle in system events, got {system_contents}"


def test_callback_receives_correct_messages():
    """回调接收的 messages 参数应该是当前消息列表"""
    handler = _make_handler()
    received_messages = {"msgs": None}

    def my_callback(messages, tokens, limit):
        received_messages["msgs"] = list(messages)  # 复制一份

    # 第一轮：高使用率 + 有 tool_calls
    tc1 = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc1])
    resp1.usage = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}

    # 第二轮：正常 + 无 tool_calls
    resp2 = _make_mock_response(content="Done", tool_calls=[])
    resp2.usage = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}

    mock_client = _make_client([resp1, resp2])

    gen = agent_runner_loop(
        client=mock_client, system_prompt="system prompt here", user_input="user input here",
        handler=handler, tools_schema=[], max_turns=5, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    )
    _collect_events(gen)

    # 回调应该被调用
    assert received_messages["msgs"] is not None, "Callback should have been called"
    # immediate_check 在 prompt_tokens 提取后立即触发，此时 messages 可能只有 system + user
    # （assistant 消息在 prompt_tokens 提取后、回调前可能尚未追加）
    msgs = received_messages["msgs"]
    assert len(msgs) >= 2, f"Expected at least 2 messages, got {len(msgs)}"
    # 第一条应该是 system
    assert msgs[0]["role"] == "system", f"First message should be system, got {msgs[0]['role']}"


def test_truncate_tool_content_with_name():
    """截断标记应包含工具名"""
    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, _truncate_tool_content
    long_content = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(long_content, "memory-server/user_memory_remember")
    assert "memory-server/user_memory_remember" in result
    assert "[截断]" in result
    assert len(result) <= MAX_TOOL_RESULT_CHARS


def test_truncate_tool_content_without_name():
    """无工具名时截断标记显示通用标签"""
    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, _truncate_tool_content
    long_content = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(long_content)
    assert "工具" in result
    assert "memory-server" not in result
    assert len(result) <= MAX_TOOL_RESULT_CHARS


def test_sub_agent_placeholderize_before_fifo():
    """子 Agent 80% 触发 → 先占位符化；仍超 target 才 FIFO 兜底（两级串联、顺序可证）。

    - 触发：第一轮 LLM 响应后立即检测（usage.prompt_tokens=170000 = 85% > 80%）
    - 阶段 1：12 轮含超大 tool 输出的 history + agent_loop 追加的当前 user（user 总数 > 13）→ 最早若干轮 tool 可替换，
      总量估算远 > target(100000) → 占位符化 3 条后仍超 → 阶段 2 FIFO 兜底
    - 顺序证据：spy 里检查 FIFO 收到的 messages 已含占位符（阶段 1 先于阶段 2 执行）
    """
    from unittest import mock

    import agent.generic.agent_loop as al

    handler = _make_handler()

    tc1 = _make_tool_call(name="read", args={}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc1])
    resp1.usage = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}
    resp2 = _make_mock_response(content="Done", tool_calls=[])
    resp2.usage = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}
    mock_client = _make_client([resp1, resp2])

    tool_msgs = [
        {"role": "assistant", "content": "调工具", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "read 输出很长" * 8000, "name": "read"},
        {"role": "user", "content": "下一步"},
    ]
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "任务"}] + tool_msgs * 12
    # agent_loop 会在 history 后追加当前 user（L649-652），第一轮响应后 dispatch 再追加 tool/next_prompt
    # ——messages user 总数 > 10 → 最早若干轮 tool 可替换（断言不依赖具体轮数）

    calls = {"fifo": 0, "placeholder_seen": False}
    orig_fifo = al._fifo_prune

    def spy_fifo(messages, target_tokens, is_resumed=False):
        calls["fifo"] += 1
        # 累积 OR：子 Agent 分支不设 _compress_cooldown、不重置 last_prompt_tokens，
        # 第 2 轮轮顶会再次触发 FIFO spy，此时 turn1 的 FIFO 已把占位符删光 + 新轮 dispatch 的
        # tool('ok') 非占位符 → 直接赋值会覆盖 flag=False。用 or 累积保证首次 True 不被覆盖。
        calls["placeholder_seen"] = calls["placeholder_seen"] or any(
            m.get("role") == "tool" and str(m.get("content", "")).endswith(("输出已裁剪]", "获取]"))
            for m in messages
        )
        return orig_fifo(messages, target_tokens, is_resumed=is_resumed)

    with mock.patch.object(al, "count_messages_tokens", return_value=500000), \
         mock.patch.object(al, "_fifo_prune", side_effect=spy_fifo):
        gen = agent_runner_loop(
            client=mock_client, system_prompt="test", user_input="test",
            handler=handler, tools_schema=[], max_turns=5, verbose=False,
            context_window_tokens=200000, context_fifo_threshold=0,
            context_target_threshold=100000, on_context_high_usage=None,
            history=history,
        )
        _collect_events(gen)

    # 两级顺序：FIFO 兜底收到的 messages 里已有占位符化的 tool 输出（阶段 1 先执行）
    assert calls["placeholder_seen"], "FIFO received messages without placeholderized tool outputs"
    # count_messages_tokens 被 patch 为常数 500000（恒 > target 100000）→ 占位符化后必仍超 → FIFO 兜底必被调用
    assert calls["fifo"] >= 1
    # 子 Agent 循环继续（不退出）
    assert mock_client._chat_call_count[0] == 2
