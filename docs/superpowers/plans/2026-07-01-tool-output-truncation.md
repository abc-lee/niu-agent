# 工具输出截断修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复工具输出未截断导致单条 tool 消息超限（火山方舟报 "Total tokens of image and text exceed max message tokens"）的 bug——disk 路径绕过保底截断 + lightrag_get_graph 无截断 + 缺单消息聚合上限。

**Architecture:** 三层截断对齐 Claude Code 实践：(1) disk 路径补保底截断（handler.py 调 `_truncate_tool_content`）；(2) lightrag_get_graph / explore_node 加工具自己的截断（20K 字符，参考 Claude Code Grep）；(3) 新增单消息聚合上限 200K 字符（参考 Claude Code `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS`），在 messages 送 LLM 前检查。保底值保持 30000 字符（跟 Claude Code Bash 一致，不下调）。

**Tech Stack:** Python 3.11, litellm, 火山方舟 ark-code-latest（doubao-seed-2-0-code，单消息有独立上限）

**参考依据：** Claude Code `src/constants/toolLimits.ts`——`DEFAULT_MAX_RESULT_SIZE_CHARS=50000`（全局 per-tool）、`MAX_TOOL_RESULTS_PER_MESSAGE_CHARS=200000`（单消息聚合）、`BASH_MAX_OUTPUT_DEFAULT=30000`（Bash 特定）、Grep `maxResultSizeChars=20000`。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `agent/handler.py` | disk 结果返回前加保底截断 | Modify（L1059-1061 附近）|
| `agent/generic/agent_loop.py` | 新增单消息聚合上限检查 + `_truncate_dict_result` 辅助 | Modify |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | `lightrag_get_graph` 返回前截断 | Modify（L765-786）|
| `niu_api/internal/lightrag_adapter.py` | `explore_node` 返回前截断（工具层截断）| Modify（L563-650）|
| `tests/test_tool_truncation.py` | 截断测试 | Create |

---

## Task 1: `_truncate_dict_result` 辅助函数（处理 dict 结果）

**Files:**
- Modify: `agent/generic/agent_loop.py:180-191`（新增辅助函数）
- Test: `tests/test_tool_truncation.py`（新建）

- [ ] **Step 1: 写失败测试 — dict 结果截断**

创建 `tests/test_tool_truncation.py`：

```python
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


def test_truncate_dict_result_non_serializable():
    """不可序列化的对象降级为 str 截断。"""
    class Foo:
        def __str__(self):
            return "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_dict_result(Foo(), "test_tool")
    assert isinstance(result, str)
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py -v`
Expected: FAIL with `ImportError: cannot import name '_truncate_dict_result'`

- [ ] **Step 3: 在 agent_loop.py 新增 `_truncate_dict_result` 函数**

读 `agent/generic/agent_loop.py:180-191` 确认 `_truncate_tool_content` 现状。

在 `_truncate_tool_content` 函数之后（约 L192 附近）新增：

```python
def _truncate_dict_result(result, tool_name: str = ""):
    """对 dict 或任意对象做保底截断。

    dict 结果（如 lightrag_get_graph 返回的 {center, nodes, edges, stats}）
    序列化后可能超 MAX_TOOL_RESULT_CHARS。本函数：
    - 小 dict：原样返回
    - 大 dict：返回 {"status": "truncated", "message": "...", "data": 截断后的字符串}
    - 非 dict（不可序列化）：降级用 str() 后调 _truncate_tool_content

    这样既保留 dict 语义（status 检查），又避免超大结果进 messages。
    """
    import json
    try:
        serialized = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化，降级为 str 截断
        return _truncate_tool_content(str(result), tool_name)

    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return result  # 原样返回 dict

    # 超限：返回截断提示 dict
    label = f"{tool_name} " if tool_name else ""
    message = f"[截断] {label}原始输出 {len(serialized)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。如需完整内容，请调整查询参数（如缩小 depth/limit）或分页重新获取。"
    truncated_data = serialized[:MAX_TOOL_RESULT_CHARS - len(message) - 200]
    return {
        "status": "truncated",
        "message": message,
        "data": truncated_data,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read())"`
Expected: 无输出

- [ ] **Step 6: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/agent_loop.py tests/test_tool_truncation.py
git commit -m "feat(agent_loop): add _truncate_dict_result for dict tool output truncation

dict 结果（如 lightrag_get_graph 的 {center,nodes,edges,stats}）序列化后
可能超 MAX_TOOL_RESULT_CHARS。新增 _truncate_dict_result：
- 小 dict 原样返回
- 大 dict 返回 {status:truncated, message, data:截断字符串}
- 不可序列化降级为 str 截断

为 disk 路径补截断做准备。"
```

---

## Task 2: disk 路径补保底截断

**Files:**
- Modify: `agent/handler.py:1059-1101`（disk 结果返回前加截断）
- Test: `tests/test_tool_truncation.py`

- [ ] **Step 1: 写失败测试 — disk 大结果被截断**

在 `tests/test_tool_truncation.py` 追加：

```python
def test_disk_large_result_gets_truncated(monkeypatch):
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

    handler = NiuHandler.__new__(NiuHandler)
    handler.disk_engine = type("FakeDiskEngine", (), {"execute": lambda self, cmd: disk_result, "config": type("FakeConfig", (), {"get_server_by_dir": lambda self, d: None})()})()

    # 调用 _dispatch_tool（或 disk 处理逻辑），捕获返回的 StepOutcome.result
    # 由于 handler 内部是 generator，需要驱动
    captured = {}
    def fake_before(*a, **kw):
        yield None
    def fake_after(*a, **kw):
        yield None
    handler.tool_before_callback = fake_before
    handler.tool_after_callback = fake_after
    handler._is_subagent = False

    # 调用 _dispatch_tool_name 的 disk 分支
    gen = handler._dispatch_tool_name("disk", {"command": "/lightrag/lightrag_get_graph explore --entity test --depth 3 --limit 100"}, response=None)
    # 消费 generator 拿 StepOutcome
    outcome = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        outcome = e.value

    assert outcome is not None
    result = outcome.result
    # 验证被截断（不再是原始 big_result）
    result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
    assert len(result_str) <= MAX_TOOL_RESULT_CHARS, f"disk 结果应被截断到 {MAX_TOOL_RESULT_CHARS}，实际 {len(result_str)}"
    assert "截断" in result_str or "truncated" in result_str
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py::test_disk_large_result_gets_truncated -v`
Expected: FAIL（disk 结果未截断，长度超限）

- [ ] **Step 3: 在 handler.py disk 分支加截断**

读 `agent/handler.py:1059-1101` 确认 disk EXECUTE 分支现状。

当前代码（L1059-1061）：
```python
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查和 memory dirty flag
                result = disk_result.raw_result
```

改为（加截断）：
```python
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查和 memory dirty flag
                result = disk_result.raw_result
                # 保底截断（disk 路径绕过 agent_loop 的 _truncate_tool_content，需在此补）
                from agent.generic.agent_loop import _truncate_tool_content, _truncate_dict_result
                if isinstance(result, dict):
                    result = _truncate_dict_result(result, real_tool_name)
                elif isinstance(result, str):
                    result = _truncate_tool_content(result, real_tool_name)
```

注意：`real_tool_name` 在 L1063-1069 计算，但截断在 L1061 之后。需要把 `real_tool_name` 计算提前到截断之前。读实际代码确认调整顺序：

```python
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查和 memory dirty flag
                result = disk_result.raw_result
                # Map /dir/tool → server-name/tool using DiskConfig（提前计算 real_tool_name 供截断用）
                real_tool_name = tool_name
                parts = disk_result.tool_path.strip("/").split("/", 1)
                if len(parts) == 2:
                    dir_name, tool = parts
                    server = self.disk_engine.config.get_server_by_dir(dir_name)
                    if server is not None:
                        real_tool_name = f"{server.server_name}/{tool}"
                # 保底截断（disk 路径绕过 agent_loop 的 _truncate_tool_content，需在此补）
                from agent.generic.agent_loop import _truncate_tool_content, _truncate_dict_result
                if isinstance(result, dict):
                    result = _truncate_dict_result(result, real_tool_name)
                elif isinstance(result, str):
                    result = _truncate_tool_content(result, real_tool_name)
                _ = yield from try_call_generator(
                    self.tool_after_callback, real_tool_name,
                    args, response, result
                )
```

读实际代码确认原有的 `real_tool_name` 计算块（L1062-1069）删除（已提前到截断前），后续逻辑不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py::test_disk_large_result_gets_truncated -v`
Expected: PASS

如果测试因 `_dispatch_tool_name` 是私有方法或签名不匹配而失败，读 `agent/handler.py` 确认 disk 分支的实际方法名和调用方式，调整测试。

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/handler.py').read())"`
Expected: 无输出

- [ ] **Step 6: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py tests/test_compress_quality.py -v 2>&1 | tail -15`
Expected: 无新增 FAIL

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/handler.py tests/test_tool_truncation.py
git commit -m "fix(handler): disk 路径补保底截断，防止超大 tool 结果进 messages

disk 工具走 handler.py 独立路径，绕过了 agent_loop 的 _truncate_tool_content
保底截断（30000 字符）。lightrag_get_graph --depth 3 --limit 100 返回 50 万
字符直接进 tool 消息，触发火山方舟'max message tokens'超限。

修复：disk EXECUTE 分支返回前，对 dict/str 结果调 _truncate_dict_result /
_truncate_tool_content。real_tool_name 计算提前到截断前。"
```

---

## Task 3: lightrag_get_graph 工具层截断（20K 字符）

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:563-655`（`explore_node` 返回前截断）
- Test: `tests/test_tool_truncation.py`

- [ ] **Step 1: 写失败测试 — explore_node 大结果被截断到 20K**

在 `tests/test_tool_truncation.py` 追加：

```python
def test_explore_node_large_result_truncated():
    """explore_node 返回超大图时，被截断到 LIGHTRAG_GRAPH_MAX_CHARS (20000) 字符。"""
    from niu_api.internal.lightrag_adapter import LightRAGAdapter, LIGHTRAG_GRAPH_MAX_CHARS

    # mock _get_rag 返回一个含大量 nodes/edges 的 KnowledgeGraph
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
    # mock _get_rag 返回 FakeKG
    adapter._get_rag = lambda: object()  # 非 None 即可
    # mock call_async 返回 FakeKG
    import niu_api.internal.lightrag_adapter as la_module
    original_call_async = la_module.call_async if hasattr(la_module, 'call_async') else None
    try:
        la_module.call_async = lambda coro, timeout=120: FakeKG()
        result = adapter.explore_node(entity_name="test", depth=3)
    finally:
        if original_call_async:
            la_module.call_async = original_call_async

    import json
    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized) <= LIGHTRAG_GRAPH_MAX_CHARS, f"explore_node 结果应截断到 {LIGHTRAG_GRAPH_MAX_CHARS}，实际 {len(serialized)}"
    # 验证含截断标记
    assert result.get("status") == "truncated" or "截断" in json.dumps(result, ensure_ascii=False)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py::test_explore_node_large_result_truncated -v`
Expected: FAIL（`explore_node` 无截断，返回超大 dict；或 `LIGHTRAG_GRAPH_MAX_CHARS` 未定义）

- [ ] **Step 3: 在 lightrag_adapter.py 加截断常量 + explore_node 截断**

读 `niu_api/internal/lightrag_adapter.py:1-30` 确认顶部 import 段，在合适位置加常量：

```python
LIGHTRAG_GRAPH_MAX_CHARS = 20000  # 图查询结果最大字符数（参考 Claude Code Grep 工具）
```

读 `niu_api/internal/lightrag_adapter.py:563-655` 确认 `explore_node` 的 return 语句（约 L641-655）。

在 `return {...}` 之前加截断逻辑：

```python
            # 保底截断（图查询结果可能超大，如 depth=3 limit=100 返回 50 万字符）
            import json
            result = {
                "center": center,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }
            serialized = json.dumps(result, ensure_ascii=False)
            if len(serialized) > LIGHTRAG_GRAPH_MAX_CHARS:
                logger.warning(f"explore_node result {len(serialized)} chars > {LIGHTRAG_GRAPH_MAX_CHARS}, truncating")
                return {
                    "status": "truncated",
                    "message": f"[截断] lightrag_get_graph 原始输出 {len(serialized)} 字符，已截断至 {LIGHTRAG_GRAPH_MAX_CHARS} 字符。请缩小 depth/limit 参数后重新查询。",
                    "center": center,
                    "nodes": nodes[:max(1, int(len(nodes) * LIGHTRAG_GRAPH_MAX_CHARS / max(len(serialized), 1)))],  # 按比例保留部分 nodes
                    "edges": [],
                    "stats": {
                        "nodes": len(nodes),
                        "edges": len(edges),
                        "max_depth": depth,
                        "truncated": True,
                        "original_chars": len(serialized),
                    },
                }
            return result
```

注意：保留 `center` 和部分 `nodes` 让 LLM 知道查询方向，`edges` 清空（占字符最多且可重新查询）。`stats` 保留原始计数让 LLM 知道截断了多少。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py::test_explore_node_large_result_truncated -v`
Expected: PASS

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/internal/lightrag_adapter.py').read())"`
Expected: 无输出

- [ ] **Step 6: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py -v 2>&1 | tail -15`
Expected: 无新增 FAIL

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/lightrag_adapter.py tests/test_tool_truncation.py
git commit -m "feat(lightrag): explore_node 工具层截断到 20K 字符

lightrag_get_graph --depth 3 --limit 100 返回 50 万字符（1000 节点 + 1778 边），
触发单消息超限。新增 LIGHTRAG_GRAPH_MAX_CHARS=20000（参考 Claude Code Grep）：
- 序列化后超限返回 {status:truncated, center, 部分 nodes, stats}
- 保留 center + 部分 nodes 让 LLM 知道查询方向
- edges 清空（占字符最多，可重新查询）
- stats 保留原始计数让 LLM 知道截断比例

与 Task 2 的 disk 保底截断（30K）形成双层防护。"
```

---

## Task 4: 单消息聚合上限检查（200K 字符）

**Files:**
- Modify: `agent/generic/agent_loop.py`（新增 `_enforce_message_budget` + 在 LLM 调用前检查）
- Test: `tests/test_tool_truncation.py`

- [ ] **Step 1: 写失败测试 — 单消息聚合超限被截断**

在 `tests/test_tool_truncation.py` 追加：

```python
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
    """单消息 tool 内容合计 > 200K，最大的 tool 结果被截断。"""
    from agent.generic.agent_loop import _enforce_message_budget, MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
    # 3 个 tool 结果，合计 240K > 200K
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 50000},
        {"role": "tool", "tool_call_id": "call_2", "content": "y" * 90000},  # 最大
        {"role": "tool", "tool_call_id": "call_3", "content": "z" * 100000},  # 更大
    ]
    result = _enforce_message_budget(messages)
    # 最大的两个被截断
    total = sum(len(m.get("content", "")) for m in result if m.get("role") == "tool")
    assert total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS, f"聚合后应 <= {MAX_TOOL_RESULTS_PER_MESSAGE_CHARS}，实际 {total}"
    # 应有 [截断] 标记
    assert any("[截断]" in m.get("content", "") for m in result if m.get("role") == "tool")


def test_enforce_message_budget_no_tool_messages():
    """无 tool 消息时原样返回。"""
    from agent.generic.agent_loop import _enforce_message_budget
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = _enforce_message_budget(messages)
    assert result == messages
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py -v -k enforce_message_budget`
Expected: FAIL（`_enforce_message_budget` 未定义）

- [ ] **Step 3: 在 agent_loop.py 新增常量 + `_enforce_message_budget` 函数**

在 `MAX_TOOL_RESULT_CHARS = 30000` 之后（约 L181）新增常量：

```python
MAX_TOOL_RESULT_CHARS = 30000  # 单个工具结果最大字符数（约 15K-30K token）
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200000  # 单消息内 tool 结果合计上限（参考 Claude Code）
```

在 `_truncate_dict_result` 函数之后新增 `_enforce_message_budget`：

```python
def _enforce_message_budget(messages: list) -> list:
    """单消息内 tool 结果合计超 MAX_TOOL_RESULTS_PER_MESSAGE_CHARS 时，截断最大的几个。

    参考 Claude Code enforceToolResultBudget：防止一轮内多个并行工具结果
    合计爆掉单消息上限（火山方舟 'max message tokens'）。

    策略：按 tool content 大小降序，依次截断最大的，直到合计 <= 上限。
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    if not tool_indices:
        return messages

    total = sum(len(messages[i].get("content", "")) for i in tool_indices)
    if total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
        return messages  # 未超限

    # 按大小降序排列 tool 消息索引
    tool_indices_sorted = sorted(tool_indices, key=lambda i: len(messages[i].get("content", "")), reverse=True)

    # 依次截断最大的，直到合计 <= 上限
    current_total = total
    for idx in tool_indices_sorted:
        if current_total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
            break
        content = messages[idx].get("content", "")
        # 截断到 MAX_TOOL_RESULT_CHARS（保底值），释放 (len(content) - MAX_TOOL_RESULT_CHARS) 字符
        if len(content) > MAX_TOOL_RESULT_CHARS:
            messages[idx] = {
                **messages[idx],
                "content": _truncate_tool_content(content, "aggregated"),
            }
            current_total -= (len(content) - MAX_TOOL_RESULT_CHARS)

    logger.warning(f"[MessageBudget] tool results total {total} > {MAX_TOOL_RESULTS_PER_MESSAGE_CHARS}, truncated largest to {current_total}")
    return messages
```

- [ ] **Step 4: 在 LLM 调用前调用 `_enforce_message_budget`**

读 `agent/generic/agent_loop.py` 确认 LLM 调用位置（约 L331 `response = yield from response_gen` 或 L336 `response = exhaust(response_gen)`）。

在 `client.chat(messages, tools=tools_schema)` 调用前（约 L330 附近）加：

```python
        # 单消息聚合上限检查（防多个 tool 结果合计爆掉单消息上限）
        messages = _enforce_message_budget(messages)
        response_gen = client.chat(messages, tools=tools_schema, response_format=response_format)
```

读实际代码确认 `client.chat` 的调用方式和位置，把 `_enforce_message_budget(messages)` 放在调用前。注意 `messages` 可能是局部变量或需要 reassign。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py -v -k enforce_message_budget`
Expected: 3 个测试 PASS

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read())"`
Expected: 无输出

- [ ] **Step 7: 验证 import 不报错**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.generic.agent_loop import _enforce_message_budget, MAX_TOOL_RESULTS_PER_MESSAGE_CHARS; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 8: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tool_truncation.py tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -20`
Expected: 无新增 FAIL

- [ ] **Step 9: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/agent_loop.py tests/test_tool_truncation.py
git commit -m "feat(agent_loop): add _enforce_message_budget 单消息聚合上限 200K

参考 Claude Code MAX_TOOL_RESULTS_PER_MESSAGE_CHARS=200000，防止一轮内
多个并行 tool 结果合计爆掉单消息上限（火山方舟报错）。

策略：按 tool content 大小降序，依次截断最大的到 MAX_TOOL_RESULT_CHARS，
直到合计 <= 200K。在 LLM 调用前检查。

与 Task 2/3 形成三层防护：
- Task 3: lightrag 工具层 20K
- Task 2: disk 保底 30K
- Task 4: 单消息聚合 200K（最后一道闸门）"
```

---

## Task 5: 端到端验证（手动）

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动程序，触发大图查询**

用户执行：
1. `./niu` 启动程序
2. 对话中调用 `disk /lightrag/lightrag_get_graph explore --entity 李磊 --depth 3 --limit 100`
3. 观察日志

- [ ] **Step 2: 验证 lightrag 工具层截断生效**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "explore_node result.*truncating\|截断" logs/api_stderr.log 2>/dev/null | tail -5`
Expected: 看到 `explore_node result N chars > 20000, truncating`

- [ ] **Step 3: 验证无单消息超限错误**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "Total tokens of image and text exceed max message tokens" logs/api_stderr.log 2>/dev/null | tail -5 || echo "无超限错误"`
Expected: 不再出现 `exceed max message tokens` 错误

- [ ] **Step 4: 验证压缩后下一轮正常**

继续对话，触发压缩，观察压缩后下一轮 LLM 调用是否正常（不再报超限）。

- [ ] **Step 5: 最终提交（清理调试代码，如有）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
# 如有调试代码清理后
git add -A
git commit -m "feat(truncation): 工具输出截断修复完成

三层截断防护：
- lightrag_get_graph 工具层 20K（参考 Claude Code Grep）
- disk 保底 30K（补 handler.py 截断，原绕过 agent_loop）
- 单消息聚合 200K（参考 Claude Code，最后一道闸门）

修复 lightrag_get_graph --depth 3 --limit 100 返回 50 万字符触发
火山方舟'max message tokens'超限的 bug。"
```

---

## 自审检查

### 1. Spec 覆盖

- disk 路径补保底截断 → Task 2 ✅
- lightrag_get_graph 工具层截断 → Task 3 ✅
- 单消息聚合上限 → Task 4 ✅
- dict 结果截断辅助 → Task 1 ✅
- 端到端验证 → Task 5 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `_truncate_tool_content(content: str, tool_name: str) -> str`：已有，Task 1 使用
- `_truncate_dict_result(result, tool_name: str)`：Task 1 定义，Task 2 使用 ✅
- `_enforce_message_budget(messages: list) -> list`：Task 4 定义 + 使用 ✅
- `MAX_TOOL_RESULT_CHARS = 30000`：已有，Task 1/4 使用 ✅
- `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200000`：Task 4 定义 ✅
- `LIGHTRAG_GRAPH_MAX_CHARS = 20000`：Task 3 定义 ✅

### 4. 风险点

- Task 2 的 `real_tool_name` 计算提前可能影响后续 `tool_after_callback` 调用——读实际代码确认 `real_tool_name` 在 callback 里用的就是提前计算后的值
- Task 3 的 `explore_node` 截断保留 `center` + 部分 `nodes`，可能让 LLM 误以为查询成功——`status:truncated` + `message` 提示让 LLM 知道被截断
- Task 4 的 `_enforce_message_budget` 修改 messages 列表——注意是否影响其他地方的 messages 引用（读 agent_loop 确认 messages 是局部变量）
