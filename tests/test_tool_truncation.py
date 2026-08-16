"""工具输出截断测试。"""
import pytest

from agent.generic.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    _truncate_dict_result,
    _truncate_tool_content,
)


def test_truncate_tool_content_str_under_limit():
    """字符串短于上限原样返回。"""
    assert _truncate_tool_content("hello", "test_tool") == "hello"


def test_truncate_tool_content_str_over_limit():
    """字符串超上限被截断 + 加 [截断] 标记。"""
    big = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(big, "test_tool")
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result
    assert "test_tool" in result


def test_truncate_dict_result_small_dict():
    """小 dict 原样返回（序列化后不超限）。"""
    d = {"status": "ok", "data": [1, 2, 3]}
    result = _truncate_dict_result(d, "test_tool")
    assert result == d


def test_truncate_dict_result_large_dict():
    """大 dict 序列化后超限，返回截断提示 dict。"""
    big_data = "x" * (MAX_TOOL_RESULT_CHARS + 5000)
    d = {"status": "ok", "data": big_data}
    result = _truncate_dict_result(d, "lightrag_get_graph")
    # 返回 dict 含截断提示
    assert isinstance(result, dict)
    assert result.get("status") == "truncated"
    assert "[截断]" in result.get("message", "")
    assert "lightrag_get_graph" in result.get("message", "")
    # data 字段被截断到合理大小
    assert len(result.get("data", "")) <= MAX_TOOL_RESULT_CHARS
    # 验证返回 dict 序列化后总长度 <= MAX_TOOL_RESULT_CHARS（核心契约）
    import json
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_TOOL_RESULT_CHARS


def test_truncate_dict_result_non_serializable():
    """不可序列化的对象降级为 str 截断。"""
    class Foo:
        def __str__(self):
            return "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_dict_result(Foo(), "test_tool")
    assert isinstance(result, str)
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result


def test_unified_gate_truncates_large_dict_from_dispatch(monkeypatch):
    """统一关口在 dispatch 返回后截断超大 dict 结果。

    构造一个返回 97 万字符 dict 的工具，通过 agent_runner_loop 调用，
    验证 messages 里的 tool 结果被截断到 ≤ MAX_TOOL_RESULT_CHARS。
    """
    from types import SimpleNamespace

    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, agent_runner_loop
    from agent.handler import StepOutcome

    # 构造超大 dict 结果
    large_result = {"nodes": [{"id": i, "data": "x" * 1000} for i in range(1000)]}

    # mock handler.dispatch 直接返回含超大 dict 的 StepOutcome
    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(large_result, next_prompt="")
        def tool_before_callback(self, *a, **kw):
            return
            yield  # 让方法成为生成器（try_call_generator 兼容）
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    # mock client：chat 必须是生成器，FakeResponse.tool_calls 必须是对象列表
    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield  # 生成器：yield 后 return（agent_loop 用 exhaust/yield from 消费）
            if self._call_count == 1:
                # 第一轮：返回工具调用
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="test_tool", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            # 第二轮：返回空 tool_calls + content，触发 L489 `if not response.tool_calls:` 退出
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    # 跑一轮 agent_runner_loop
    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": "test_tool", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    # 用 StopIteration.value 拿 agent_runner_loop 的 return 值
    # （list(gen) 会消费所有 yield 但丢弃 StopIteration.value）
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    messages = final_return.get("messages", []) if final_return else []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "should have tool message"
    content = tool_msgs[0].get("content", "")
    assert len(content) <= MAX_TOOL_RESULT_CHARS, (
        f"unified gate should truncate to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
    )


def test_disk_large_str_result_gets_truncated(monkeypatch):
    """disk 工具返回超大 str 时，由 agent_loop 统一关口截断到 MAX_TOOL_RESULT_CHARS。

    Task 2 移除了 handler 内部 str 截断，str 路径也走 agent_loop 统一关口。
    """
    from types import SimpleNamespace

    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, agent_runner_loop
    from agent.handler import StepOutcome

    big_str = "x" * (MAX_TOOL_RESULT_CHARS + 5000)

    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            yield
            return StepOutcome(big_str, next_prompt="")
        def tool_before_callback(self, *a, **kw):
            return
            yield
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="disk", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": "disk", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    messages = final_return.get("messages", []) if final_return else []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[0].get("content", "")
    assert len(content) <= MAX_TOOL_RESULT_CHARS, (
        f"unified gate should truncate str to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
    )
    assert "[截断]" in content


def test_explore_node_returns_full_result_no_internal_truncation(monkeypatch):
    """explore_node 不再在 adapter 内部截断，返回完整结果。

    截断由 agent_loop 统一关口处理（见 test_unified_gate_truncates_large_dict_from_dispatch）。
    前端 API 和内部业务调 explore_node 拿完整结果。
    """
    import json

    from niu_api.internal.lightrag_adapter import LIGHTRAG_GRAPH_MAX_CHARS, LightRAGAdapter

    # mock _get_rag 返回 FakeRag（有 get_knowledge_graph 方法）
    class FakeRag:
        def get_knowledge_graph(self, entity_name, max_depth=2):
            return None  # 返回 None，让 call_async 的 mock 忽略参数返回 FakeKG
    class FakeNode:
        def __init__(self, i):
            self.id = f"node_{i}"
            self.properties = {"entity_type": "person", "description": "x" * 500, "file_path": "", "source_id": ""}
    class FakeEdge:
        def __init__(self, i):
            self.source = f"node_{i}"
            self.target = f"node_{i+1}"
            self.properties = {"keywords": "knows", "description": "x" * 200, "weight": 1.0}
    class FakeKG:
        nodes = [FakeNode(i) for i in range(500)]
        edges = [FakeEdge(i) for i in range(500)]

    adapter = LightRAGAdapter.__new__(LightRAGAdapter)
    monkeypatch.setattr(adapter, "_get_rag", lambda: FakeRag())
    import niu_api.internal.lightrag_adapter as la_module
    monkeypatch.setattr(la_module, "call_async", lambda coro, timeout=120: FakeKG())

    result = adapter.explore_node(entity_name="test", depth=3)

    serialized = json.dumps(result, ensure_ascii=False)
    # 现在不截断，返回完整结果（可能 > 20K）
    assert len(serialized) > LIGHTRAG_GRAPH_MAX_CHARS, (
        f"explore_node should return full result (>{LIGHTRAG_GRAPH_MAX_CHARS} chars), got {len(serialized)}"
    )
    assert result.get("status") != "truncated", "explore_node should not truncate internally"


def test_explore_node_small_result_not_truncated(monkeypatch):
    """explore_node 小图原样返回（不截断）。"""
    import json

    from niu_api.internal.lightrag_adapter import LightRAGAdapter

    class FakeRag:
        def get_knowledge_graph(self, entity_name, max_depth=2):
            return None
    class FakeNode:
        def __init__(self, i):
            self.id = f"node_{i}"
            self.properties = {"entity_type": "person", "description": f"desc_{i}", "file_path": "", "source_id": ""}
    class FakeKG:
        nodes = [FakeNode(i) for i in range(5)]
        edges = []

    adapter = LightRAGAdapter.__new__(LightRAGAdapter)
    monkeypatch.setattr(adapter, "_get_rag", lambda: FakeRag())
    import niu_api.internal.lightrag_adapter as la_module
    monkeypatch.setattr(la_module, "call_async", lambda coro, timeout=120: FakeKG())

    result = adapter.explore_node(entity_name="test", depth=1)

    assert result.get("status") != "truncated", "小图不应被截断"
    assert len(result.get("nodes", [])) == 5
    assert "截断" not in json.dumps(result, ensure_ascii=False)


def test_enforce_message_budget_under_limit():
    """单消息 tool 内容合计 < 200K 原样返回。"""
    from agent.generic.agent_loop import _enforce_message_budget
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 50000},
        {"role": "tool", "tool_call_id": "call_2", "content": "y" * 50000},
    ]
    result = _enforce_message_budget(messages)
    assert result == messages  # 原样返回


def test_enforce_message_budget_over_limit():
    """单消息 tool 内容合计 > 200K，最大的 tool 结果被截断。

    策略：按 tool content 大小降序，依次截断最大的到 MAX_TOOL_RESULT_CHARS，
    直到合计 <= 200K。本例 call_3 (100K) 最大，截断后释放 70K，合计 170K <= 200K 停止。
    """
    from agent.generic.agent_loop import MAX_TOOL_RESULTS_PER_MESSAGE_CHARS, _enforce_message_budget
    # 3 个 tool 结果，合计 240K > 200K
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 50000},
        {"role": "tool", "tool_call_id": "call_2", "content": "y" * 90000},
        {"role": "tool", "tool_call_id": "call_3", "content": "z" * 100000},  # 最大
    ]
    result = _enforce_message_budget(messages)
    # 最大的 call_3 (100K) 被截断到 30K（释放 70K），合计 170K <= 200K
    total = sum(len(m.get("content", "")) for m in result if m.get("role") == "tool")
    assert total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS, f"聚合后应 <= {MAX_TOOL_RESULTS_PER_MESSAGE_CHARS}，实际 {total}"
    # 最大的 call_3 应被截断（含 [截断] 标记）
    call_3_result = next(m for m in result if m.get("tool_call_id") == "call_3")
    assert "[截断]" in call_3_result["content"]
    # 较小的 call_1 (50K) 和 call_2 (90K) 不应被截断
    call_1_result = next(m for m in result if m.get("tool_call_id") == "call_1")
    call_2_result = next(m for m in result if m.get("tool_call_id") == "call_2")
    assert "[截断]" not in call_1_result["content"], "call_1 (50K) 不应被截断"
    assert "[截断]" not in call_2_result["content"], "call_2 (90K) 不应被截断"


def test_enforce_message_budget_no_tool_messages():
    """无 tool 消息时原样返回。"""
    from agent.generic.agent_loop import _enforce_message_budget
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = _enforce_message_budget(messages)
    assert result == messages


def test_explore_node_center_huge_description_not_truncated_internally(monkeypatch):
    """center.description 超大时，adapter 不再内部截断，返回完整 description。

    截断由 agent_loop 统一关口处理。
    """
    from niu_api.internal.lightrag_adapter import LightRAGAdapter

    class FakeRag:
        def get_knowledge_graph(self, entity_name, max_depth=2):
            return None
    class FakeNode:
        def __init__(self, i):
            self.id = f"node_{i}"
            self.properties = {"entity_type": "person", "description": "x" * 500, "file_path": "", "source_id": ""}
    class FakeKG:
        nodes = [FakeNode(i) for i in range(10)]  # 少量 nodes
        edges = []

    # center 是第一个 node，description 超大（60K）
    big_center = FakeNode(0)
    big_center.properties["description"] = "y" * 60000
    FakeKG.nodes[0] = big_center

    adapter = LightRAGAdapter.__new__(LightRAGAdapter)
    monkeypatch.setattr(adapter, "_get_rag", lambda: FakeRag())
    import niu_api.internal.lightrag_adapter as la_module
    monkeypatch.setattr(la_module, "call_async", lambda coro, timeout=120: FakeKG())

    result = adapter.explore_node(entity_name="test", depth=1)

    center = result.get("center", {})
    desc = center.get("description", "")
    # 现在不截断，description 完整保留
    assert len(desc) > 5000, f"center.description should be full (>{5000} chars), got {len(desc)}"


def test_unified_gate_truncates_large_dict_from_mcp_path(monkeypatch):
    """统一关口截断 MCP 工具路径的超大 dict 结果。

    与 test_unified_gate_truncates_large_dict_from_dispatch 同构，
    但 tool_name 含 '/'（MCP 路径），覆盖 MCP / 分支的统一关口。
    """
    from types import SimpleNamespace

    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, agent_runner_loop
    from agent.handler import StepOutcome

    large_result = {"nodes": [{"id": i, "data": "x" * 1000} for i in range(1000)]}
    mcp_tool = "lightrag-server/lightrag_get_graph"

    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(large_result, next_prompt="")
        def tool_before_callback(self, *a, **kw):
            return
            yield
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name=mcp_tool, arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": mcp_tool, "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    # 用 StopIteration.value 拿 agent_runner_loop 的 return 值
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    messages = final_return.get("messages", []) if final_return else []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[0].get("content", "")
    assert len(content) <= MAX_TOOL_RESULT_CHARS, (
        f"unified gate should truncate MCP path to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
    )


def test_unified_gate_truncates_large_list_result(monkeypatch):
    """统一关口截断超大 list 结果（list 类型在 Task 1 Step 1 新增）。

    list 截断后返回 {"status": "truncated", "message": ..., "data": 截断字符串}
    dict（与 _truncate_dict_result 一致），LLM 看到的是结构化 dict 而非裸 str。
    """
    import json
    from types import SimpleNamespace

    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, agent_runner_loop
    from agent.handler import StepOutcome

    large_list = [{"id": i, "data": "x" * 1000} for i in range(1000)]
    list_str = json.dumps(large_list, ensure_ascii=False)
    assert len(list_str) > MAX_TOOL_RESULT_CHARS, "test setup: list should be large"

    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(large_list, next_prompt="")
        def tool_before_callback(self, *a, **kw):
            return
            yield
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="list_tool", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": "list_tool", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    # 用 StopIteration.value 拿 agent_runner_loop 的 return 值
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    messages = final_return.get("messages", []) if final_return else []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[0].get("content", "")
    assert len(content) <= MAX_TOOL_RESULT_CHARS, (
        f"unified gate should truncate list to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
    )
    # list 截断后是 truncated dict（含 status/message/data 字段），不是裸 str
    assert "truncated" in content, "truncated list should have 'truncated' marker"
    assert "[截断]" in content, "truncated list should have [截断] marker"


def test_unified_gate_preserves_small_dict(monkeypatch):
    """小 dict 不被截断，原样返回。"""
    from types import SimpleNamespace

    from agent.generic.agent_loop import agent_runner_loop
    from agent.handler import StepOutcome

    small_result = {"status": "success", "data": "small"}  # 远小于 30K

    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(small_result, next_prompt="")
        def tool_before_callback(self, *a, **kw):
            return
            yield
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="test_tool", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": "test_tool", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    # 用 StopIteration.value 拿 agent_runner_loop 的 return 值
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    messages = final_return.get("messages", []) if final_return else []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[0].get("content", "")
    # 小 dict 不截断，content 是完整 json.dumps(small_result)，无 truncated 标记
    assert "truncated" not in content, f"small dict should not be truncated, got: {content}"
    assert "small" in content


def test_unified_gate_truncates_should_exit_path(monkeypatch):
    """should_exit 路径的 data 也被统一关口截断。"""
    from types import SimpleNamespace

    from agent.generic.agent_loop import MAX_TOOL_RESULT_CHARS, agent_runner_loop
    from agent.handler import StepOutcome

    large_result = {"nodes": [{"id": i, "data": "x" * 1000} for i in range(1000)]}

    class FakeHandler:
        current_turn = 0
        max_turns = 1
        def dispatch(self, tool_name, args, response, index=0):
            # should_exit=True：触发 L557 分支，return {"result": "EXITED", "data": outcome.data, ...}
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(large_result, next_prompt="", should_exit=True)
        def tool_before_callback(self, *a, **kw):
            return
            yield
        def tool_after_callback(self, *a, **kw):
            return
            yield
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0
        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="test_tool", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    handler = FakeHandler()
    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[{"type": "function", "function": {"name": "test_tool", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    # 用 StopIteration.value 拿 agent_runner_loop 的 return 值
    result_events = []
    final_return = None
    try:
        while True:
            result_events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    assert final_return is not None, "agent_runner_loop should return final dict"
    # should_exit 路径返回 {"result": "EXITED", "data": outcome.data, "messages": ...}
    assert final_return.get("result") == "EXITED"
    messages = final_return.get("messages", [])
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[0].get("content", "")
    assert len(content) <= MAX_TOOL_RESULT_CHARS, (
        f"should_exit path should also be truncated, got {len(content)}"
    )


# ============================================================================
# E4-15：序列化三层兜底——坏 __str__ / 自引用结构不崩，错误文本进工具结果
# ============================================================================

class _BadStrRecursion:
    """str()/repr() 都抛 RecursionError 的坏对象（dict/list 的 str() 走 repr）。"""

    def __str__(self):
        return self.__str__()  # 无限递归 → RecursionError

    __repr__ = __str__


def test_truncate_dict_result_bad_str_value_returns_error_dict():
    """dict 内值坏 __str__（str(dict) 走 repr 同样抛）→ 外层 except → 错误 dict（修复②）。"""
    result = _truncate_dict_result({"a": _BadStrRecursion()}, "test_tool")
    assert result == {"error": "[工具结果序列化失败: dict]"}


def _self_referencing_list():
    lst = []
    lst.append(lst)
    return lst


def _run_loop_with_tool_data(data):
    """跑一轮 agent_runner_loop：工具返回 data，下一轮退出；返回 (final_return, events)。"""
    import json
    from types import SimpleNamespace

    from agent.generic.agent_loop import agent_runner_loop
    from agent.handler import StepOutcome

    class FakeHandler:
        current_turn = 0
        max_turns = 40

        def dispatch(self, tool_name, args, response, index=0):
            yield  # 让方法成为生成器（agent_loop 用 exhaust/yield from 消费）
            return StepOutcome(data, next_prompt="")

        def tool_before_callback(self, *a, **kw):
            return
            yield

        def tool_after_callback(self, *a, **kw):
            return
            yield

        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    class FakeClient:
        def __init__(self):
            self._call_count = 0

        def chat(self, **kw):
            self._call_count += 1
            yield
            if self._call_count == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="tc1",
                        function=SimpleNamespace(name="data_tool", arguments="{}"),
                    )],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                finish_reason="stop",
            )

    gen = agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=FakeHandler(),
        tools_schema=[{"type": "function", "function": {"name": "data_tool", "parameters": {"type": "object", "properties": {}}}}],
        verbose=False,
    )
    events = []
    final_return = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        final_return = e.value
    return final_return, events


@pytest.mark.parametrize("make_data, expected_substr", [
    (lambda: [_BadStrRecursion()], "[无法序列化: _BadStrRecursion]"),   # 修复① json_default str(o) 兜底
    (lambda: _BadStrRecursion(), "[工具结果序列化失败: _BadStrRecursion]"),  # 修复④ 裸对象直调 str() 兜底
    (_self_referencing_list, '{"error": "[工具结果序列化失败: list]"}'),  # 修复③ list 分支 json.dumps 兜底
])
def test_unified_gate_serialization_fallback(make_data, expected_substr):
    """E4-15：坏 __str__/自引用结构不崩——错误文本进工具消息（单工具降级，防整轮失败）。"""
    rv, _events = _run_loop_with_tool_data(make_data())
    assert rv is not None, "agent_runner_loop should return final dict"
    messages = rv.get("messages", [])
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "expected tool message for serialization-fallback data"
    content = tool_msgs[0].get("content", "")
    assert expected_substr in content, (
        f"expected {expected_substr!r} in tool content, got: {content[:200]!r}"
    )
