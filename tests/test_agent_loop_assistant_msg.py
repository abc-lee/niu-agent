"""测试 agent_runner_loop 中 assistant 消息的追加行为。

V4 契约：带 tool_calls 的 assistant 消息追加进 messages（上下文锚点）；
纯文本回复不追加进 messages，经 persist StreamEvent 逐条落库
（runner.py 消费 persist 事件写 DB）。

验证场景：
1. 纯文本回复（没有 tool_calls）：assistant 消息经 persist 事件携带
2. 有 tool_calls 的回复：assistant 消息应该被追加到 messages 列表，且包含 tool_calls 字段
3. 多轮对话：先有 tool_calls，工具执行后 LLM 返回纯文本回复，
   带 tool_calls 的 assistant 在 messages 中，纯文本 assistant 在 persist 事件中
"""
import json
from unittest.mock import Mock

from agent.generic.agent_loop import (
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
    LLM_ERROR / CONTEXT_OVERFLOW 分支（返回 dict 无 messages key），
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
# 测试 1: 纯文本回复 — assistant 消息被追加到 messages
# ---------------------------------------------------------------------------

def test_pure_text_reply_appends_assistant_message():
    """当 LLM 返回纯文本（无 tool_calls）时，assistant 消息经 persist 事件携带（V4：不追加进 messages）。"""
    resp = _make_mock_response(content="你好，我是助手", tool_calls=[])
    client = _make_client([resp])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1
    # Mock 自动真值属性会误入子 Agent @前缀拦截分支，显式置 False 走主 Agent 路径
    handler._is_subagent = False
    handler._is_sync_subagent = False

    def dispatch_no_tool(tool_name, args, response, index=0):
        yield
        return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_no_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="你是助手",
        user_input="你好",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    messages = return_value["messages"]

    # V4 契约：纯文本 assistant 回复不追加进 messages（下一轮上下文由 DB 重建），
    # 而是经 persist StreamEvent 逐条落库——runner.py 消费 persist 事件写 DB。
    assert [m["role"] for m in messages] == ["system", "user"], (
        f"纯文本回复后 messages 应为 [system, user]，实际: {[m['role'] for m in messages]}"
    )

    # persist 事件应携带 role=assistant 的完整回复
    persist_msgs = [json.loads(e.content) for e in events
                    if isinstance(e, StreamEvent) and e.type == "persist"]
    assistant_msgs = [m for m in persist_msgs
                      if m.get("role") == "assistant" and "tool_calls" not in m]
    assert len(assistant_msgs) == 1, (
        f"应有 1 条 assistant persist 消息，实际有 {len(assistant_msgs)}"
    )

    # 验证 assistant 消息内容
    assert assistant_msgs[0]["content"] == "你好，我是助手", (
        f"assistant 消息内容应为 '你好，我是助手'，实际为 {assistant_msgs[0]['content']!r}"
    )

    # 纯文本回复不应有 tool_calls 字段
    assert "tool_calls" not in assistant_msgs[0], (
        f"纯文本回复不应包含 tool_calls 字段，实际有: {assistant_msgs[0].keys()}"
    )

    # reply 事件应携带回复内容（前端展示通道）
    reply_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "reply"]
    assert any("你好，我是助手" in (e.content or "") for e in reply_events), (
        "应有包含回复内容的 reply StreamEvent"
    )


# ---------------------------------------------------------------------------
# 测试 2: 有 tool_calls 的回复 — assistant 消息包含 tool_calls 字段
# ---------------------------------------------------------------------------

def test_tool_call_reply_appends_assistant_message_with_tool_calls():
    """当 LLM 返回带 tool_calls 的回复时，assistant 消息应被追加且包含 tool_calls。"""
    tc = _make_tool_call(name="search", args={"query": "天气"}, tid="call_abc")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    # 工具执行后 LLM 需要再回复一次（纯文本），循环才会结束
    resp2 = _make_mock_response(content="搜索完成", tool_calls=[])
    client = _make_client([resp1, resp2])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1
    # Mock 自动真值属性会误入子 Agent @前缀拦截分支，显式置 False 走主 Agent 路径
    handler._is_subagent = False
    handler._is_sync_subagent = False

    def dispatch_search(tool_name, args, response, index=0):
        yield
        return StepOutcome(data="晴天", next_prompt="继续", should_exit=False)

    handler.dispatch = dispatch_search
    # next_prompt_patcher 透传（避免 Mock 对象被当作 user 消息内容注入 messages）
    handler.next_prompt_patcher = lambda prompt, _outcome, _turn: prompt

    gen = agent_runner_loop(
        client=client,
        system_prompt="你是助手",
        user_input="查天气",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    messages = return_value["messages"]

    # 找到 assistant 消息
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1, (
        f"应至少有 1 条 assistant 消息，实际有 {len(assistant_msgs)}"
    )

    # 第一条 assistant 消息应包含 tool_calls
    first_assistant = assistant_msgs[0]
    assert "tool_calls" in first_assistant, (
        "带 tool_calls 的回复，assistant 消息应包含 tool_calls 字段"
    )

    # 验证 tool_calls 结构
    tool_calls_in_msg = first_assistant["tool_calls"]
    assert len(tool_calls_in_msg) == 1, (
        f"应有 1 个 tool_call，实际有 {len(tool_calls_in_msg)}"
    )

    tc_in_msg = tool_calls_in_msg[0]
    assert tc_in_msg["id"] == "call_abc", (
        f"tool_call id 应为 'call_abc'，实际为 {tc_in_msg['id']!r}"
    )
    assert tc_in_msg["type"] == "function", (
        f"tool_call type 应为 'function'，实际为 {tc_in_msg['type']!r}"
    )
    assert tc_in_msg["function"]["name"] == "search", (
        f"tool_call function name 应为 'search'，实际为 {tc_in_msg['function']['name']!r}"
    )
    # arguments 应为 JSON 字符串
    args_parsed = json.loads(tc_in_msg["function"]["arguments"])
    assert args_parsed == {"query": "天气"}, (
        f"tool_call arguments 应为 {{'query': '天气'}}，实际为 {args_parsed}"
    )

    # 验证消息顺序：system -> user -> assistant(带 tool_calls) -> tool -> user(下一轮)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert "tool_calls" in messages[2]
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_abc"


# ---------------------------------------------------------------------------
# 测试 3: 多轮对话 — tool_calls + 纯文本回复，两轮 assistant 消息都在 messages 中
# ---------------------------------------------------------------------------

def test_multi_turn_both_assistant_messages_in_messages():
    """多轮对话：第一轮有 tool_calls（进 messages），第二轮纯文本（经 persist 事件）。"""
    # 第一轮：LLM 返回 tool_calls
    tc = _make_tool_call(name="search", args={"query": "天气"}, tid="call_001")
    resp1 = _make_mock_response(content="", tool_calls=[tc])
    # 第二轮：LLM 返回纯文本
    resp2 = _make_mock_response(content="今天北京晴天，气温25度", tool_calls=[])
    client = _make_client([resp1, resp2])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1
    # Mock 自动真值属性会误入子 Agent @前缀拦截分支，显式置 False 走主 Agent 路径
    handler._is_subagent = False
    handler._is_sync_subagent = False

    call_count = [0]

    def dispatch_search_then_done(tool_name, args, response, index=0):
        call_count[0] += 1
        yield
        if call_count[0] == 1:
            # 第一次：工具执行后继续
            return StepOutcome(data="晴天，25度", next_prompt="请根据搜索结果回答用户", should_exit=False)
        else:
            # 不会走到这里（第二轮是纯文本，不调 dispatch）
            return StepOutcome(data=None, next_prompt=None, should_exit=False)

    handler.dispatch = dispatch_search_then_done
    # next_prompt_patcher 透传（避免 Mock 对象被当作 user 消息内容注入 messages）
    handler.next_prompt_patcher = lambda prompt, _outcome, _turn: prompt

    gen = agent_runner_loop(
        client=client,
        system_prompt="你是天气助手",
        user_input="北京天气怎么样",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    messages = return_value["messages"]

    # V4 契约：带 tool_calls 的 assistant 消息追加进 messages；纯文本回复不追加，
    # 经 persist StreamEvent 落库。故 messages 内只有第一轮的 assistant(tool_calls)。
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, (
        f"messages 内应有 1 条 assistant 消息（tool_calls 轮），"
        f"实际有 {len(assistant_msgs)}。messages: {messages}"
    )

    # 第一条 assistant 消息：带 tool_calls
    first_assistant = assistant_msgs[0]
    assert "tool_calls" in first_assistant, (
        "第一轮 assistant 消息应包含 tool_calls 字段"
    )
    assert first_assistant["tool_calls"][0]["function"]["name"] == "search", (
        "第一轮 tool_call name 应为 'search'"
    )

    # 第二条 assistant 消息：纯文本，经 persist 事件携带（V4 契约）
    persist_msgs = [json.loads(e.content) for e in events
                    if isinstance(e, StreamEvent) and e.type == "persist"]
    second_assistant = [m for m in persist_msgs
                        if m.get("role") == "assistant" and "tool_calls" not in m]
    assert len(second_assistant) == 1, (
        f"persist 事件中应有 1 条纯文本 assistant 消息（第二轮），"
        f"实际有 {len(second_assistant)}"
    )
    assert second_assistant[0]["content"] == "今天北京晴天，气温25度", (
        f"第二轮 assistant 消息内容应为 '今天北京晴天，气温25度'，"
        f"实际为 {second_assistant[0]['content']!r}"
    )
    assert "tool_calls" not in second_assistant[0], (
        "第二轮纯文本回复不应包含 tool_calls 字段"
    )

    # 验证完整消息顺序
    # system -> user -> assistant(tool_calls) -> tool -> user(next_prompt)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "你是天气助手"

    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "北京天气怎么样"

    assert messages[2]["role"] == "assistant"
    assert "tool_calls" in messages[2]

    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_001"

    assert messages[4]["role"] == "user"
    assert messages[4]["content"] == "请根据搜索结果回答用户"

    assert len(messages) == 5, f"messages 长度应为 5，实际 {len(messages)}: {messages}"


# ---------------------------------------------------------------------------
# 测试 4: 多个 tool_calls 的 assistant 消息
# ---------------------------------------------------------------------------

def test_multiple_tool_calls_in_single_assistant_message():
    """当 LLM 在一次回复中调用多个工具时，assistant 消息应包含所有 tool_calls。"""
    tc1 = _make_tool_call(name="search", args={"query": "天气"}, tid="call_001")
    tc2 = _make_tool_call(name="search", args={"query": "温度"}, tid="call_002")
    resp1 = _make_mock_response(content="", tool_calls=[tc1, tc2])
    # 工具执行后 LLM 需要再回复一次（纯文本），循环才会结束
    resp2 = _make_mock_response(content="搜索完成", tool_calls=[])
    client = _make_client([resp1, resp2])

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1
    # Mock 自动真值属性会误入子 Agent @前缀拦截分支，显式置 False 走主 Agent 路径
    handler._is_subagent = False
    handler._is_sync_subagent = False

    def dispatch_search(tool_name, args, response, index=0):
        yield
        return StepOutcome(data="结果", next_prompt="继续", should_exit=False)

    handler.dispatch = dispatch_search
    # next_prompt_patcher 透传（避免 Mock 对象被当作 user 消息内容注入 messages）
    handler.next_prompt_patcher = lambda prompt, _outcome, _turn: prompt

    gen = agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="查天气和温度",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )
    events, return_value = _collect_and_get_return(gen)

    messages = return_value["messages"]

    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1

    first_assistant = assistant_msgs[0]
    assert "tool_calls" in first_assistant
    assert len(first_assistant["tool_calls"]) == 2, (
        f"应有 2 个 tool_calls，实际有 {len(first_assistant['tool_calls'])}"
    )

    # 验证两个 tool_call 的 id 和 name
    names = [tc["function"]["name"] for tc in first_assistant["tool_calls"]]
    ids = [tc["id"] for tc in first_assistant["tool_calls"]]
    assert names == ["search", "search"]
    assert ids == ["call_001", "call_002"]

    # 验证对应的 tool 消息
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2, (
        f"应有 2 条 tool 消息，实际有 {len(tool_msgs)}"
    )
    assert tool_msgs[0]["tool_call_id"] == "call_001"
    assert tool_msgs[1]["tool_call_id"] == "call_002"
