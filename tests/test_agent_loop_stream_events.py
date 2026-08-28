"""测试 agent_runner_loop 所有 yield 点都返回 StreamEvent 而非裸 str。

TDD: 先写测试，确认失败，再改代码。
"""
import json
from unittest.mock import Mock

from agent.generic.agent_loop import (
    BaseHandler,
    StepOutcome,
    StreamEvent,
    agent_runner_loop,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(content="hello", tool_calls=None, stream_error=False, context_overflow=False):
    """创建一个模拟的 LLM 响应对象。

    现役 agent_runner_loop 会检查 response.stream_error / context_overflow 的真值：
    Mock 未显式设置的属性是自动生成的 truthy Mock，会把循环短路进
    LLM_ERROR / CONTEXT_OVERFLOW 分支（后续 StreamEvent 流不产生），
    因此必须显式置 False。
    """
    resp = Mock()
    resp.content = content
    resp.tool_calls = tool_calls or []
    resp.stream_error = stream_error
    resp.context_overflow = context_overflow
    resp.finish_reason = "stop"
    resp.usage = None
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
    """创建一个 mock client，按顺序返回 responses 中的生成器。

    每个 response 会被包装成一个生成器（模拟流式输出）。
    """
    client = Mock()
    client.last_tools = ""
    idx = [0]

    def chat(**kwargs):
        resp = responses[idx[0]]
        idx[0] += 1
        def gen():
            yield resp
            return resp
        return gen()

    client.chat = chat
    return client


def _make_handler(dispatch_fn=None):
    """创建一个 mock handler，确保 dispatch 返回生成器。

    重要：agent_runner_loop 中，即使 tool_name=="no_tool" 也会调用
    handler.dispatch，所以 dispatch 必须总是返回生成器。
    """
    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    # Mock 自动真值属性会误入子 Agent @前缀拦截分支（FORMAT_ERROR 死循环），
    # 显式置 False 走主 Agent 路径；next_prompt_patcher 透传避免 Mock 对象注入 messages
    handler._is_subagent = False
    handler._is_sync_subagent = False
    handler.next_prompt_patcher = lambda prompt, _outcome, _turn: prompt

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

    return handler


def _collect_events(gen):
    """收集生成器产生的所有 yield 值（StreamEvent 或 str），忽略返回值。"""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration:
        pass
    return events


# ---------------------------------------------------------------------------
# 测试 1: verbose=True 时，LLM Running 标记是 StreamEvent(type="system")
# ---------------------------------------------------------------------------

def test_verbose_llm_running_marker_is_system_event():
    """verbose=True 时，'LLM Running (Turn N)' 标记应为 StreamEvent(type="system")。"""
    resp = _make_mock_response(content="hi", tool_calls=[])
    client = _make_client([resp])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hello",
        handler=handler,
        tools_schema=[],
        verbose=True,
    )
    events = _collect_events(gen)

    # 找到 LLM Running 标记
    running_markers = [e for e in events if isinstance(e, StreamEvent) and "LLM Running" in e.content]
    raw_markers = [e for e in events if isinstance(e, str) and "LLM Running" in e]

    assert len(running_markers) >= 1, "应该至少有一个 LLM Running StreamEvent"
    for m in running_markers:
        assert m.type == "system", f"LLM Running 标记应为 system 类型，实际: {m.type}"
    assert len(raw_markers) == 0, f"不应有裸 str 的 LLM Running 标记，实际: {raw_markers}"


# ---------------------------------------------------------------------------
# 测试 2: verbose=True 时，流式回复后的 \n\n 分隔符是 StreamEvent(type="system")
# ---------------------------------------------------------------------------

def test_verbose_streaming_reply_separator_is_system_event():
    """verbose=True 时，LLM 流式回复后的 '\\n\\n' 分隔符应为 StreamEvent(type="system")。"""
    resp = _make_mock_response(content="world", tool_calls=[])
    client = _make_client([resp])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hello",
        handler=handler,
        tools_schema=[],
        verbose=True,
    )
    events = _collect_events(gen)

    newline_events = [e for e in events if isinstance(e, StreamEvent) and e.content == "\n\n"]
    raw_newlines = [e for e in events if isinstance(e, str) and e == "\n\n"]

    assert len(newline_events) >= 1, "应该至少有一个 \\n\\n StreamEvent"
    for e in newline_events:
        assert e.type == "system", f"\\n\\n 分隔符应为 system 类型，实际: {e.type}"
    assert len(raw_newlines) == 0, "不应有裸 str 的 \\n\\n 分隔符"


# ---------------------------------------------------------------------------
# 测试 3: verbose=True 时，工具调用标记是 StreamEvent(type="tool_marker")
# ---------------------------------------------------------------------------

def test_verbose_tool_call_marker_is_tool_marker_event():
    """verbose=True 时，工具调用标记（🛠️ ...）应为 StreamEvent(type="tool_marker")。"""
    tc = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    # 第二轮：纯文本回复，结束循环
    resp2 = _make_mock_response(content="done", tool_calls=[])
    client = _make_client([resp1, resp2])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="search for test",
        handler=handler,
        tools_schema=[],
        verbose=True,
    )
    events = _collect_events(gen)

    tool_markers = [e for e in events if isinstance(e, StreamEvent) and e.type == "tool_marker"]
    raw_tool_markers = [e for e in events if isinstance(e, str) and "正在调用工具" in e]

    assert len(tool_markers) >= 1, "应该至少有一个 tool_marker StreamEvent"
    found_search = any("search" in e.content for e in tool_markers)
    assert found_search, "tool_marker 内容应包含工具名 'search'"
    assert len(raw_tool_markers) == 0, "不应有裸 str 的工具调用标记"


# ---------------------------------------------------------------------------
# 测试 4: verbose=True 时，代码块围栏是 StreamEvent(type="tool_marker")
# ---------------------------------------------------------------------------

def test_verbose_code_fence_is_tool_marker_event():
    """verbose=True 时，代码块围栏 ```` 应为 StreamEvent(type="tool_marker")。"""
    tc = _make_tool_call(name="calc", args={"x": 1}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    resp2 = _make_mock_response(content="done", tool_calls=[])
    client = _make_client([resp1, resp2])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="calc",
        handler=handler,
        tools_schema=[],
        verbose=True,
    )
    events = _collect_events(gen)

    fence_events = [e for e in events if isinstance(e, StreamEvent) and "````" in e.content]
    raw_fences = [e for e in events if isinstance(e, str) and "````" in e]

    assert len(fence_events) >= 2, f"应该至少有 2 个代码块围栏 StreamEvent，实际: {len(fence_events)}"
    for e in fence_events:
        assert e.type == "tool_marker", f"代码块围栏应为 tool_marker 类型，实际: {e.type}"
    assert len(raw_fences) == 0, "不应有裸 str 的代码块围栏"


# ---------------------------------------------------------------------------
# 测试 5: verbose=False 时，LLM纯文本回复是 StreamEvent(type="reply")
# ---------------------------------------------------------------------------

def test_nonverbose_text_reply_is_reply_event():
    """verbose=False 时，LLM 纯文本回复应为 StreamEvent(type="reply")。"""
    resp = _make_mock_response(content="hello world", tool_calls=[])
    client = _make_client([resp])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events = _collect_events(gen)

    reply_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "reply"]
    raw_replies = [e for e in events if isinstance(e, str) and "hello world" in e]

    assert len(reply_events) >= 1, "应该至少有一个 reply StreamEvent"
    assert any("hello world" in e.content for e in reply_events), "reply 内容应包含 'hello world'"
    assert len(raw_replies) == 0, "不应有裸 str 的回复内容"


# ---------------------------------------------------------------------------
# 测试 6: verbose=False 时，工具执行静默（无额外 StreamEvent）
# ---------------------------------------------------------------------------

def test_nonverbose_tool_execution_silent():
    """verbose=False 时，工具执行不应产生 tool_marker 或额外的 system 事件。"""
    tc = _make_tool_call(name="search", args={"q": "test"}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    resp2 = _make_mock_response(content="final answer", tool_calls=[])
    client = _make_client([resp1, resp2])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="search",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events = _collect_events(gen)

    tool_markers = [e for e in events if isinstance(e, StreamEvent) and e.type == "tool_marker"]
    running_markers = [e for e in events if isinstance(e, StreamEvent) and "LLM Running" in e.content]
    fence_events = [e for e in events if isinstance(e, StreamEvent) and "````" in e.content]

    assert len(tool_markers) == 0, f"verbose=False 时不应有 tool_marker 事件，实际: {tool_markers}"
    assert len(running_markers) == 0, f"verbose=False 时不应有 LLM Running 标记，实际: {running_markers}"
    assert len(fence_events) == 0, f"verbose=False 时不应有代码块围栏，实际: {fence_events}"

    reply_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "reply"]
    assert len(reply_events) >= 1, "verbose=False 时应有最终的 reply StreamEvent"


# ---------------------------------------------------------------------------
# 测试 7: no_tool 场景不产生 tool_marker yield
# ---------------------------------------------------------------------------

def test_no_tool_produces_no_tool_marker_events():
    """no_tool 场景（LLM 纯文本回复，无工具调用）不应产生 tool_marker 事件。"""
    resp = _make_mock_response(content="just text", tool_calls=[])
    client = _make_client([resp])
    handler = _make_handler()

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events = _collect_events(gen)

    tool_markers = [e for e in events if isinstance(e, StreamEvent) and e.type == "tool_marker"]
    assert len(tool_markers) == 0, f"no_tool 场景不应有 tool_marker，实际: {tool_markers}"


# ---------------------------------------------------------------------------
# 测试 8: 未知工具提示是 StreamEvent(type="system")
# ---------------------------------------------------------------------------

def test_unknown_tool_is_system_event():
    """BaseHandler.dispatch 中未知工具提示应为 StreamEvent(type="system")。"""
    handler = BaseHandler()
    gen = handler.dispatch("nonexistent_tool", {}, Mock())
    events = _collect_events(gen)

    system_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "system"]
    raw_events = [e for e in events if isinstance(e, str) and "未知工具" in e]

    assert len(system_events) >= 1, "未知工具应产生 system StreamEvent"
    assert any("nonexistent_tool" in e.content for e in system_events), "system 事件内容应包含工具名"
    assert len(raw_events) == 0, "不应有裸 str 的未知工具提示"
