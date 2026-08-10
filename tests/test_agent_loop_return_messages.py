"""测试 agent_runner_loop 所有 return value 都包含 messages 字段。

双管道架构 Phase 3：agent_runner_loop 的 return 值需要包含 messages，
让异步调用方（chat.py/compat.py）可以持久化到数据库。

TDD: 先写测试，确认失败，再改代码。
"""
import json
from unittest.mock import Mock

from agent.generic.agent_loop import (
    StepOutcome,
    agent_runner_loop,
)

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_agent_loop_stream_events.py)
# ---------------------------------------------------------------------------

def _make_mock_response(content="hello", tool_calls=None):
    """创建一个模拟的 LLM 响应对象。"""
    resp = Mock()
    resp.content = content
    resp.tool_calls = tool_calls or []
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
    """创建一个 mock client，按顺序返回 responses 中的生成器。"""
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


def _collect_and_get_return(gen):
    """收集生成器的所有 yield 值和 return 值。"""
    events = []
    return_value = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return_value = e.value
    return events, return_value


# ---------------------------------------------------------------------------
# 测试 1: 正常完成（no_tool，纯文本回复）— return should_exit 路径
# ---------------------------------------------------------------------------

def test_return_value_contains_messages_on_normal_completion():
    """正常完成时 return value 应包含 messages。

    当 LLM 不调用工具时，循环通过 `return should_exit` 退出，
    should_exit 为 {"result": "CURRENT_TASK_DONE", "data": None}。
    """
    resp = _make_mock_response(content="hello world", tool_calls=[])
    client = _make_client([resp])

    # no_tool 场景：dispatch 返回 next_prompt=None，触发 should_exit
    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    def dispatch_no_tool(tool_name, args, response, index=0):
        yield
        return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_no_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key in return value: {return_value}"
    assert isinstance(return_value["messages"], list), f"'messages' should be list, got {type(return_value['messages'])}"
    assert "result" in return_value, f"Missing 'result' key in return value: {return_value}"
    # 验证 messages 包含 system + user 消息
    assert len(return_value["messages"]) >= 2, f"Expected at least 2 messages, got {len(return_value['messages'])}"


# ---------------------------------------------------------------------------
# 测试 2: EXITED 路径 — outcome.should_exit=True
# ---------------------------------------------------------------------------

def test_return_value_contains_messages_on_should_exit():
    """should_exit=True 退出时 return value 应包含 messages。"""
    tc = _make_tool_call(name="exit_tool", args={}, tid="call_1")
    resp = _make_mock_response(content="", tool_calls=[tc])
    client = _make_client([resp])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    def dispatch_exit(tool_name, args, response, index=0):
        yield
        return StepOutcome(data="exit_data", next_prompt="done", should_exit=True)

    handler.dispatch = dispatch_exit

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="exit",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key in EXITED return: {return_value}"
    assert return_value["result"] == "EXITED", f"Expected result=EXITED, got {return_value.get('result')}"
    assert isinstance(return_value["messages"], list)


# ---------------------------------------------------------------------------
# 测试 3: CONTEXT_OVERFLOW 路径
# ---------------------------------------------------------------------------

def test_return_value_contains_messages_on_context_overflow():
    """上下文溢出退出时 return value 应包含 messages。"""
    resp = _make_mock_response(content="hi", tool_calls=[])
    client = _make_client([resp])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    def dispatch_no_tool(tool_name, args, response, index=0):
        yield
        return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_no_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        tools_schema=[],
        verbose=False,
        context_window_tokens=1,  # 极小的 token 限制，触发溢出
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key in CONTEXT_OVERFLOW return: {return_value}"
    assert return_value["result"] == "CONTEXT_OVERFLOW", f"Expected result=CONTEXT_OVERFLOW, got {return_value.get('result')}"
    assert isinstance(return_value["messages"], list)


# ---------------------------------------------------------------------------
# 测试 4: MAX_TURNS_EXCEEDED 路径
# ---------------------------------------------------------------------------

def test_return_value_contains_messages_on_max_turns_exceeded():
    """超过最大轮次退出时 return value 应包含 messages。"""
    # 每轮都调用工具，持续循环直到 max_turns
    tc = _make_tool_call(name="loop_tool", args={}, tid="call_1")
    resp = _make_mock_response(content="", tool_calls=[tc])
    client = _make_client([resp, resp, resp])  # 多个响应

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 2  # 只允许 2 轮
    handler.current_turn = 1

    def dispatch_loop(tool_name, args, response, index=0):
        yield
        # 返回有 next_prompt 的结果，让循环继续
        return StepOutcome(data="loop_data", next_prompt="继续", should_exit=False)

    handler.dispatch = dispatch_loop

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="loop",
        handler=handler,
        tools_schema=[],
        verbose=False,
        max_turns=2,
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key in MAX_TURNS_EXCEEDED return: {return_value}"
    assert return_value["result"] == "MAX_TURNS_EXCEEDED", f"Expected result=MAX_TURNS_EXCEEDED, got {return_value.get('result')}"
    assert isinstance(return_value["messages"], list)


# ---------------------------------------------------------------------------
# 测试 4b: max_turns=None（无上限）— 长程任务跑到底，不触发 MAX_TURNS_EXCEEDED
# ---------------------------------------------------------------------------

def test_return_value_no_max_turns_runs_until_natural_exit():
    """max_turns=None 时轮数无上限：24 轮工具调用 + 第 25 轮纯文本 → 自然退出 CURRENT_TASK_DONE。

    回归防护：子 Agent 无轮数上限（长程任务跑到底），
    while 条件 `handler.max_turns is None or turn < handler.max_turns` 必须放行 None。
    """
    # 24 轮 tool_calls + 第 25 轮纯文本
    tc = _make_tool_call(name="loop_tool", args={}, tid="call_loop")
    tool_resps = [_make_mock_response(content="", tool_calls=[tc]) for _ in range(24)]
    text_resp = _make_mock_response(content="final", tool_calls=[])
    # 既有 _make_mock_response 未设 stream_error/context_overflow → Mock 自动真值 →
    # LLM_ERROR/CONTEXT_OVERFLOW 短路；本测试显式置 False，让循环真正跑满 25 轮
    for _r in tool_resps + [text_resp]:
        _r.stream_error = False
        _r.context_overflow = False
    client = _make_client(tool_resps + [text_resp])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = None  # 无上限
    handler.current_turn = 1
    # Mock 自动真值属性会把纯文本轮推进子 Agent @前缀拦截 → FORMAT_ERROR 死循环；
    # 显式置 False 走主 Agent 分支（NO_INTERCEPTION）→ 纯文本自然退出 CURRENT_TASK_DONE
    handler._is_sync_subagent = False
    handler._is_subagent = False

    call_count = [0]

    def dispatch_loop(tool_name, args, response, index=0):
        call_count[0] += 1
        yield
        # 前 24 轮（tool_calls）：返回 next_prompt，让循环继续
        return StepOutcome(data="loop_data", next_prompt="继续", should_exit=False)

    handler.dispatch = dispatch_loop

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="loop",
        handler=handler,
        tools_schema=[],
        verbose=False,
        max_turns=None,
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key: {return_value}"
    # 25 轮（24 工具 + 1 纯文本）全部跑完仍不触发轮次上限
    assert return_value["result"] == "CURRENT_TASK_DONE", (
        f"Expected natural exit CURRENT_TASK_DONE with max_turns=None, "
        f"got {return_value.get('result')} (turn={handler.current_turn})"
    )
    assert call_count[0] == 24, f"Expected 24 tool dispatches, got {call_count[0]}"


# ---------------------------------------------------------------------------
# 测试 5: CURRENT_TASK_DONE 路径 — next_prompt 为空字符串
# ---------------------------------------------------------------------------

def test_return_value_contains_messages_on_empty_next_prompt():
    """next_prompt 为空时退出，return value 应包含 messages。"""
    tc = _make_tool_call(name="done_tool", args={}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    resp2 = _make_mock_response(content="final", tool_calls=[])
    client = _make_client([resp1, resp2])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    call_count = [0]

    def dispatch_done(tool_name, args, response, index=0):
        call_count[0] += 1
        yield
        if call_count[0] == 1:
            # 第一次调用：返回 next_prompt，继续循环
            return StepOutcome(data="ok", next_prompt="继续", should_exit=False)
        else:
            # 第二次调用：next_prompt 为空，触发退出
            return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_done

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="do task",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    assert isinstance(return_value, dict), f"Expected dict, got {type(return_value)}: {return_value}"
    assert "messages" in return_value, f"Missing 'messages' key in CURRENT_TASK_DONE return: {return_value}"
    assert isinstance(return_value["messages"], list)


# ---------------------------------------------------------------------------
# 测试 6: 统一格式验证 — 所有 return value 都有 result + messages
# ---------------------------------------------------------------------------

def test_all_return_values_have_result_and_messages_keys():
    """所有退出路径的 return value 都应包含 result 和 messages 键。"""
    # 用简单的 no_tool 场景验证格式
    resp = _make_mock_response(content="done", tool_calls=[])
    client = _make_client([resp])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    def dispatch_no_tool(tool_name, args, response, index=0):
        yield
        return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_no_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    # 验证统一格式
    assert isinstance(return_value, dict)
    assert "result" in return_value
    assert "messages" in return_value
    assert isinstance(return_value["result"], str)
    assert isinstance(return_value["messages"], list)
    # result 应该是已知的枚举值之一
    valid_results = {"CONTEXT_OVERFLOW", "EXITED", "CURRENT_TASK_DONE", "MAX_TURNS_EXCEEDED", "COMPLETED"}
    assert return_value["result"] in valid_results, f"Unknown result: {return_value['result']}"


# ---------------------------------------------------------------------------
# 测试 7: messages 内容完整性 — 包含 system + user + assistant/tool 消息
# ---------------------------------------------------------------------------

def test_messages_contains_full_conversation():
    """return value 中的 messages 应包含完整的对话历史。"""
    tc = _make_tool_call(name="my_tool", args={"x": 1}, tid="call_1")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    resp2 = _make_mock_response(content="final answer", tool_calls=[])
    client = _make_client([resp1, resp2])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    call_count = [0]

    def dispatch_tool(tool_name, args, response, index=0):
        call_count[0] += 1
        yield
        if call_count[0] == 1:
            return StepOutcome(data="tool_result", next_prompt="继续", should_exit=False)
        else:
            return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="use tool",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    messages = return_value["messages"]
    # 应包含：system + user + assistant(tool_calls) + tool + user + assistant(纯文本)
    # 至少有 system + user
    assert len(messages) >= 2
    # 第一条是 system
    assert messages[0]["role"] == "system"
    # 第二条是 user
    assert messages[1]["role"] == "user"
