"""工具输出截断测试。"""
from agent.generic.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    _truncate_tool_content,
    _truncate_dict_result,
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


def test_disk_large_dict_result_gets_truncated(monkeypatch):
    """disk 工具返回超大 dict 时，进 messages 前被截断到 MAX_TOOL_RESULT_CHARS。"""
    import json
    from agent.handler import NiuHandler
    from niu_api.internal.disk_engine import DiskResult

    # 构造超大 MCP 结果（模拟 lightrag_get_graph depth=3 limit=100 的返回）
    big_nodes = [{"id": f"node_{i}", "description": "x" * 500} for i in range(1000)]
    big_result = {"status": "ok", "center": big_nodes[0], "nodes": big_nodes, "edges": [], "stats": {}}
    serialized_len = len(json.dumps(big_result, ensure_ascii=False))
    assert serialized_len > MAX_TOOL_RESULT_CHARS, f"测试数据应超限，实际 {serialized_len}"

    # mock disk_engine.execute 返回 EXECUTE + big_result
    disk_result = DiskResult(action="EXECUTE", tool_path="lightrag/lightrag_get_graph", raw_result=big_result)

    # 用 NiuHandler.__new__ 绕过 __init__，手动设 dispatch 所需的全部属性
    handler = NiuHandler.__new__(NiuHandler)
    handler.disk_engine = type("FakeDiskEngine", (), {
        "execute": lambda self, cmd: disk_result,
        "config": type("FakeConfig", (), {"get_server_by_dir": lambda self, d: None})(),
    })()
    handler.tool_before_callback = lambda *a, **kw: None
    handler.tool_after_callback = lambda *a, **kw: None
    handler._is_subagent = True  # 跳过 brain_region reinforce（避免依赖）
    handler.cwd = "/tmp"
    handler.mcp_client = None
    handler._done_hooks = []
    handler.current_turn = 0

    # 调用 dispatch（公开方法，签名 dispatch(tool_name, args, response, index=0)）
    gen = handler.dispatch("disk", {"command": "/lightrag/lightrag_get_graph explore --entity test --depth 3 --limit 100"}, response=None, index=0)
    # 消费 generator 拿 StepOutcome
    outcome = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        outcome = e.value

    assert outcome is not None, "dispatch 应返回 StepOutcome"
    result = outcome.data
    # 验证被截断（不再是原始 big_result）
    result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
    assert len(result_str) <= MAX_TOOL_RESULT_CHARS, f"disk 结果应被截断到 {MAX_TOOL_RESULT_CHARS}，实际 {len(result_str)}"
    assert "截断" in result_str or "truncated" in result_str


def test_disk_large_str_result_gets_truncated(monkeypatch):
    """disk 工具返回超大 str 时，进 messages 前被截断。"""
    from agent.handler import NiuHandler
    from niu_api.internal.disk_engine import DiskResult

    big_str = "x" * (MAX_TOOL_RESULT_CHARS + 5000)
    disk_result = DiskResult(action="EXECUTE", tool_path="some/tool", raw_result=big_str)

    handler = NiuHandler.__new__(NiuHandler)
    handler.disk_engine = type("FakeDiskEngine", (), {
        "execute": lambda self, cmd: disk_result,
        "config": type("FakeConfig", (), {"get_server_by_dir": lambda self, d: None})(),
    })()
    handler.tool_before_callback = lambda *a, **kw: None
    handler.tool_after_callback = lambda *a, **kw: None
    handler._is_subagent = True
    handler.cwd = "/tmp"
    handler.mcp_client = None
    handler._done_hooks = []
    handler.current_turn = 0

    gen = handler.dispatch("disk", {"command": "/some/tool"}, response=None, index=0)
    outcome = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        outcome = e.value

    assert outcome is not None
    result = outcome.data
    assert isinstance(result, str)
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result


def test_explore_node_large_result_truncated(monkeypatch):
    """explore_node 返回超大图时，被截断到 LIGHTRAG_GRAPH_MAX_CHARS (20000) 字符。"""
    import json
    from niu_api.internal.lightrag_adapter import LightRAGAdapter, LIGHTRAG_GRAPH_MAX_CHARS

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
    assert len(serialized) <= LIGHTRAG_GRAPH_MAX_CHARS, f"explore_node 结果应截断到 {LIGHTRAG_GRAPH_MAX_CHARS}，实际 {len(serialized)}"
    # 验证含截断标记
    assert result.get("status") == "truncated" or "截断" in serialized


def test_explore_node_small_result_not_truncated(monkeypatch):
    """explore_node 小图原样返回（不截断）。"""
    import json
    from niu_api.internal.lightrag_adapter import LightRAGAdapter, LIGHTRAG_GRAPH_MAX_CHARS

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
    from agent.generic.agent_loop import _enforce_message_budget, MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
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
    from agent.generic.agent_loop import _enforce_message_budget, MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
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
