# 截断关口统一化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把工具输出截断关口从"工具侧（adapter 内部 + handler 三处分散）"移到"距离 Agent 调用最近的统一位置"（`agent_loop.py:549` dispatch 调用之后），修复前端图谱被误截断的 bug，同时保证所有 Agent 工具路径都被截断覆盖。

**Architecture:** 统一关口放在 `agent_loop.py:549` 的 `handler.dispatch` 调用之后、`outcome.data` 进 messages 之前。这样：所有 Agent 工具（MCP server / disk 路径 / 内置工具 / chat-with-* 子 Agent）的返回值都走这一道；前端 API 和内部业务（region_detector/region_manager）不经过 dispatch，拿完整结果不被截断。改动顺序：先加统一关口（保证有截断保护）→ 再移除分散截断 → 最后更新测试。

注：`StepOutcome` 定义在 `agent/generic/agent_loop.py:64-68`（dataclass，字段 `data/next_prompt/should_exit`），`agent/handler.py:23` 仅 import。`handler.dispatch` 各路径返回值统一为 `StepOutcome` 对象——其中 chat-with-* 子 Agent 路径（handler.py:1018-1035）返回 `ret`，而 `ret` 是 `_call_subagent_gen` 产出的 `StepOutcome` 对象，`ret.data` 是子 Agent 的最终输出（通常是 dict）。

**Tech Stack:** Python 3.11 + pytest

---

## 文件结构

### 修改文件清单

| 文件 | 改动内容 | Task |
|------|---------|------|
| `agent/generic/agent_loop.py` | L549-555 dispatch 调用后加统一截断关口（含 dict/list/str 三分支，list 返回 truncated dict）；L574/L621 双重截断标注为冗余但保留 | Task 1, 4 |
| `agent/handler.py` | 移除 L1070-1076（disk）/ L1156-1160（MCP /）/ L1220-1224（MCP 裸名）三处分散截断；移除 L1186/L1240 的 `result_summary` 死代码 | Task 2 |
| `niu_api/internal/lightrag_adapter.py` | 移除 L721（explore_node）/ L1006（get_graph_snapshot）的 `_truncate_graph_result` 调用；L568 函数定义处加 DEPRECATED 注释 | Task 3 |
| `tests/test_tool_truncation.py` | 重写 4 个"断言 dispatch 内截断"的测试为"断言 agent_loop 统一关口截断"；新增 list 类型测试 | Task 5 |

### 不改动文件

- `niu_api/compat.py:978-1003` — 估算用截断（不是真截断 messages），保留不动
- `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` — MCP 函数 `lightrag_get_graph` 调 adapter 拿完整结果，截断由 agent_loop 统一关口负责
- `niu_api/kg_api.py` — 前端 API 直接调 adapter，不经过 dispatch，天然不被截断
- `niu_api/internal/region_detector.py` / `region_manager.py` — 内部业务直接调 adapter，不经过 dispatch
- `_truncate_graph_result` 函数（Task 3 Step 1.5 标 DEPRECATED，无调用方）/ `LIGHTRAG_GRAPH_MAX_CHARS` 常量 — 保留（测试仍引用）

---

## Task 1: agent_loop.py 加统一截断关口

**Files:**
- Modify: `agent/generic/agent_loop.py:549-556`（dispatch 调用后加截断）

- [ ] **Step 1: 在 dispatch 调用后加统一截断关口**

修改 `agent/generic/agent_loop.py`，在 L549-555 的 dispatch 调用块之后、L557 的 `if outcome.should_exit:` 之前，插入统一截断逻辑。

**当前代码**（L549-556）：
```python
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield StreamEvent("tool_marker", "`````\n")
                outcome = yield from gen
                yield StreamEvent("tool_marker", "`````\n")
            else:
                outcome = exhaust(gen)

            if outcome.should_exit:
```

**改为**（在 `outcome = exhaust(gen)` 之后、`if outcome.should_exit:` 之前插入统一截断关口）：
```python
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield StreamEvent("tool_marker", "`````\n")
                outcome = yield from gen
                yield StreamEvent("tool_marker", "`````\n")
            else:
                outcome = exhaust(gen)

            # === 统一截断关口 ===
            # 距离 Agent 调用最近，覆盖所有工具路径（MCP/disk/内置/chat-with-*）
            # 前端 API 和内部业务（region_detector/region_manager）不经过 dispatch，不被截断
            if outcome.data is not None:
                if isinstance(outcome.data, dict):
                    outcome.data = _truncate_dict_result(outcome.data, tool_name)
                elif isinstance(outcome.data, list):
                    # list 类型：序列化后截断，返回 truncated dict（与 _truncate_dict_result 一致）
                    _list_str = json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                    if len(_list_str) > MAX_TOOL_RESULT_CHARS:
                        _label = f"工具 {tool_name}" if tool_name else "工具"
                        _message = f"[截断] {_label}原始输出 {len(_list_str)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。"
                        _budget = MAX_TOOL_RESULT_CHARS - len(_message) - 200
                        outcome.data = {
                            "status": "truncated",
                            "message": _message,
                            "data": _list_str[:_budget],
                        }
                elif isinstance(outcome.data, str):
                    outcome.data = _truncate_tool_content(outcome.data, tool_name)

            if outcome.should_exit:
```

- [ ] **Step 2: 运行现有测试确认不破坏**

Run: `cd <repo_root> && python -m pytest tests/test_tool_truncation.py -v 2>&1 | tail -20`
Expected: 现有测试仍 PASS（统一关口在 dispatch 外，不影响 dispatch 内部截断测试——这些测试会在 Task 5 重写）

- [ ] **Step 3: 运行 import 检查**

Run: `cd <repo_root> && python -c "import agent.generic.agent_loop; print('IMPORT OK')"`
Expected: 输出 `IMPORT OK`

- [ ] **Step 4: Commit**

```bash
cd <repo_root>
git add agent/generic/agent_loop.py
git commit -m "feat(agent_loop): 加统一截断关口在 dispatch 调用后

距离 Agent 调用最近的关口，覆盖所有工具路径（MCP/disk/内置/
chat-with-*）。处理 dict/list/str 三种类型，None 安全跳过。
后续 Task 会移除 handler 和 adapter 内部的分散截断。"
```

---

## Task 2: 移除 handler.py 三处分散截断

**Files:**
- Modify: `agent/handler.py:1070-1076`（disk 路径）
- Modify: `agent/handler.py:1156-1160`（MCP / 路径）
- Modify: `agent/handler.py:1220-1224`（MCP 裸名路径）

- [ ] **Step 1: 移除 disk 路径的截断（L1070-1076）**

读 `agent/handler.py:1060-1080` 确认当前代码。

**当前代码**（L1060-1080 附近）：
```python
                result = disk_result.raw_result
                # Map /dir/tool → server-name/tool using DiskConfig
                real_tool_name = tool_name
                parts = disk_result.tool_path.strip("/").split("/", 1)
                if len(parts) == 2:
                    dir_name, tool = parts
                    server = self.disk_engine.config.get_server_by_dir(dir_name)
                    if server is not None:
                        real_tool_name = f"{server.server_name}/{tool}"
                # 保底截断（disk 路径绕过 agent_loop 的 _truncate_tool_content，需在此补）
                # 截断在 tool_after_callback 之前：callback 看到截断后结果，不再能看到原始 tool 结果
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

**改为**（移除 L1070-1076 的截断块，保留 result 和 real_tool_name 赋值）：
```python
                result = disk_result.raw_result
                # Map /dir/tool → server-name/tool using DiskConfig
                real_tool_name = tool_name
                parts = disk_result.tool_path.strip("/").split("/", 1)
                if len(parts) == 2:
                    dir_name, tool = parts
                    server = self.disk_engine.config.get_server_by_dir(dir_name)
                    if server is not None:
                        real_tool_name = f"{server.server_name}/{tool}"
                # 截断由 agent_loop 统一关口处理（dispatch 返回后）
                _ = yield from try_call_generator(
                    self.tool_after_callback, real_tool_name,
                    args, response, result
                )
```

- [ ] **Step 2: 移除 MCP / 路径的截断（L1156-1160）和 result_summary 死代码（L1186）**

读 `agent/handler.py:1148-1190` 确认当前代码。

**当前代码**（L1148-1162 截断块 + L1185-1187 result_summary 死代码，两段相隔几十行）：
```python
                # 直接调用工具函数
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 保底截断（与 disk 路径对称，防止超大 MCP 结果进 messages）
                # 截断在 tool_after_callback 之前：callback 看到截断后结果
                from agent.generic.agent_loop import _truncate_tool_content, _truncate_dict_result
                if isinstance(result, dict):
                    result = _truncate_dict_result(result, tool_name)
                elif isinstance(result, str):
                    result = _truncate_tool_content(result, tool_name)
```
以及 L1185-1187 附近的死代码（`result_summary` 计算后从未被读取）：
```python
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        result_summary = json.dumps(result, ensure_ascii=False)[:500]
                        return StepOutcome(result, next_prompt="")
```

**改为**（移除截断块 + 移除 `result_summary = ...` 死代码行，保留 return）：
```python
                # 直接调用工具函数
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 截断由 agent_loop 统一关口处理（dispatch 返回后）
```
以及 L1185-1187 附近改为（移除 `result_summary = ...` 这一行：Task 2 移除截断后 result 可能是 97 万字符的原始 dict，`json.dumps(result)` 完整序列化浪费约 50ms 性能且 result_summary 从未被使用）：
```python
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
```

- [ ] **Step 3: 移除 MCP 裸名路径的截断（L1220-1224）和 result_summary 死代码（L1240）**

读 `agent/handler.py:1213-1245` 确认当前代码。

**当前代码**（L1213-1224 截断块 + L1239-1241 result_summary 死代码，两段相隔约 15 行）：
```python
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 保底截断（与 disk 路径对称，防止超大 MCP 结果进 messages）
                from agent.generic.agent_loop import _truncate_tool_content, _truncate_dict_result
                if isinstance(result, dict):
                    result = _truncate_dict_result(result, tool_name)
                elif isinstance(result, str):
                    result = _truncate_tool_content(result, tool_name)
```
以及 L1239-1241 附近的死代码（`result_summary` 计算后从未被读取）：
```python
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        result_summary = json.dumps(result, ensure_ascii=False)[:500]
                        return StepOutcome(result, next_prompt="")
```

**改为**（移除截断块 + 移除 `result_summary = ...` 死代码行，保留 return）：
```python
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 截断由 agent_loop 统一关口处理（dispatch 返回后）
```
以及 L1239-1241 附近改为（移除 `result_summary = ...` 这一行：与 Step 2 同理，避免在超大 result 上做完整 json.dumps）：
```python
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
```

- [ ] **Step 4: 运行 import 检查**

Run: `cd <repo_root> && python -c "import agent.handler; print('IMPORT OK')"`
Expected: 输出 `IMPORT OK`

- [ ] **Step 5: 运行现有测试（部分会失败，Task 5 会重写）**

Run: `cd <repo_root> && python -m pytest tests/test_tool_truncation.py -v 2>&1 | tail -20`
Expected: `test_disk_large_dict_result_gets_truncated` 和 `test_direct_mcp_large_dict_result_gets_truncated` 会 FAIL（这两测试断言 dispatch 内截断，Task 5 重写）。其他测试 PASS。

- [ ] **Step 6: Commit**

```bash
cd <repo_root>
git add agent/handler.py
git commit -m "refactor(handler): 移除三处分散截断和 result_summary 死代码，统一由 agent_loop 关口处理

- disk 路径（L1070-1076）：移除 _truncate_dict_result/_truncate_tool_content 调用
- MCP / 路径（L1156-1160）：同上 + 移除 L1186 result_summary 死代码
- MCP 裸名路径（L1220-1224）：同上 + 移除 L1240 result_summary 死代码

result_summary 在 status ok/success 分支里 json.dumps(result)[:500]
但从未被读取，Task 2 移除截断后 result 可能是 97 万字符原始 dict，
完整序列化浪费约 50ms 性能，一并移除。

截断关口统一到 agent_loop.py:549 dispatch 调用后（Task 1 已加），
覆盖所有工具路径。tool_after_callback 现在看到原始结果（不截断），
但结果进 messages 前会被统一关口截断。"
```

---

## Task 3: 移除 lightrag_adapter 内部截断

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:721`（explore_node）
- Modify: `niu_api/internal/lightrag_adapter.py:1006`（get_graph_snapshot）

- [ ] **Step 1: 移除 explore_node 的截断（L721）**

读 `niu_api/internal/lightrag_adapter.py:705-725` 确认当前代码。

**当前代码**（L705-725 附近，注意 `return self._truncate_graph_result(...)` 在 try 块末尾，except 块返回简短错误结构）：
```python
            result = {
                "center": center_node,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }
            return self._truncate_graph_result(result, "lightrag_get_graph(explore)")

        except Exception as e:
            logger.error(f"LightRAG explore_node failed: {e}")
            return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}
```

**改为**（只把 L721 的 `return self._truncate_graph_result(result, "lightrag_get_graph(explore)")` 改为 `return result`，except 块不动）：
```python
            result = {
                "center": center_node,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API 和内部业务（region_detector/region_manager）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG explore_node failed: {e}")
            return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}
```

- [ ] **Step 1.5: 给 `_truncate_graph_result` 加 DEPRECATED 注释**

读 `niu_api/internal/lightrag_adapter.py:568` 确认函数定义位置。

**当前代码**（L568 附近）：
```python
    def _truncate_graph_result(self, result: Dict[str, Any], tool_name: str = "lightrag_get_graph") -> Dict[str, Any]:
```

**改为**（在 def 行上方加 DEPRECATED 注释）：
```python
    # DEPRECATED: 截断已移至 agent_loop 统一关口，本函数保留供参考但无调用方
    def _truncate_graph_result(self, result: Dict[str, Any], tool_name: str = "lightrag_get_graph") -> Dict[str, Any]:
```

- [ ] **Step 2: 移除 get_graph_snapshot 的截断（L1006）**

读 `niu_api/internal/lightrag_adapter.py:995-1010` 确认当前代码。

**当前代码**（L995-1010 附近，注意 `return self._truncate_graph_result(...)` 在 try 块末尾，except 块返回简短 `{"nodes": [], "edges": []}`）：
```python
            result = {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "limit": limit,
                },
            }
            return self._truncate_graph_result(result, "lightrag_get_graph(snapshot)")

        except Exception as e:
            logger.error(f"LightRAG get_graph_snapshot failed: {e}")
            return {"nodes": [], "edges": []}
```

**改为**（只把 L1006 的 `return self._truncate_graph_result(result, "lightrag_get_graph(snapshot)")` 改为 `return result`，except 块不动）：
```python
            result = {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "limit": limit,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API（kg_api.py graph_snapshot 端点）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG get_graph_snapshot failed: {e}")
            return {"nodes": [], "edges": []}
```

- [ ] **Step 3: 运行 import 检查**

Run: `cd <repo_root> && python -c "import niu_api.internal.lightrag_adapter; print('IMPORT OK')"`
Expected: 输出 `IMPORT OK`

- [ ] **Step 4: 运行测试（部分会失败，Task 5 会重写）**

Run: `cd <repo_root> && python -m pytest tests/test_tool_truncation.py -v 2>&1 | tail -20`
Expected: `test_explore_node_large_result_truncated` 和 `test_explore_node_center_huge_description_truncated` 会 FAIL（这两测试断言 adapter 内截断，Task 5 重写）。

- [ ] **Step 5: Commit**

```bash
cd <repo_root>
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix(lightrag): 移除 explore_node/get_graph_snapshot 内部截断

修复前端图谱被误截断的 bug：kg_api.py 的 graph_snapshot 端点
直接调 adapter.get_graph_snapshot，之前被内部截断导致图谱
边全没、节点只剩几个。

现在 adapter 返回完整结果，截断由 agent_loop 统一关口处理
（只有 Agent 工具调用路径经过 dispatch → 统一关口截断）。
前端 API 和内部业务（region_detector/region_manager）拿完整结果。

_truncate_graph_result 函数标记 DEPRECATED（无调用方，保留供参考），
LIGHTRAG_GRAPH_MAX_CHARS 常量保留（测试仍引用）。"
```

---

## Task 4: 标注 agent_loop 双重截断冗余

**Files:**
- Modify: `agent/generic/agent_loop.py:574`（should_exit 路径）
- Modify: `agent/generic/agent_loop.py:621`（normal 路径）

- [ ] **Step 1: 给 should_exit 路径的截断加注释（L574）**

读 `agent/generic/agent_loop.py:570-580` 确认当前代码。

**当前代码**（L570-575 附近）：
```python
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
```

**改为**（加注释说明冗余但保留作防御）：
```python
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
```

- [ ] **Step 2: 给 normal 路径的截断加注释（L621 附近）**

读 `agent/generic/agent_loop.py:615-625` 确认当前代码。

**当前代码**（L615-625 附近，找到 `_truncate_tool_content` 在 normal 路径的调用）：
```python
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
```

**改为**（加注释说明冗余但保留作防御）：
```python
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
```

注：如果 grep 发现 L621 附近实际行号有偏移，按真实位置加注释。

- [ ] **Step 3: 运行 import 检查**

Run: `cd <repo_root> && python -c "import agent.generic.agent_loop; print('IMPORT OK')"`
Expected: 输出 `IMPORT OK`

- [ ] **Step 4: Commit**

```bash
cd <repo_root>
git add agent/generic/agent_loop.py
git commit -m "docs(agent_loop): 标注 should_exit/normal 路径的冗余截断

统一关口已在 dispatch 返回后截断 outcome.data，should_exit 和
normal 路径的 _truncate_tool_content 调用变冗余。保留作防御性
编程，加注释说明。"
```

---

## Task 5: 重写测试覆盖统一关口

**Files:**
- Modify: `tests/test_tool_truncation.py`（重写 4 个测试 + 新增 list 测试）

- [ ] **Step 1: 重写 test_explore_node_large_result_truncated**

读 `tests/test_tool_truncation.py:141-173` 确认当前测试。

**当前测试逻辑**：断言 `adapter.explore_node` 返回的 result 序列化后 ≤ LIGHTRAG_GRAPH_MAX_CHARS（20K）。

**改为**：断言 `adapter.explore_node` 返回完整 result（不截断），然后构造 dispatch 调用验证统一关口截断。

新测试代码（替换原测试）：
```python
def test_explore_node_returns_full_result_no_internal_truncation():
    """explore_node 不再在 adapter 内部截断，返回完整结果。
    
    截断由 agent_loop 统一关口处理（见 test_dispatch_truncates_at_unified_gate）。
    前端 API 和内部业务调 explore_node 拿完整结果。
    """
    adapter = _make_adapter_with_large_graph()  # 复用现有 helper
    result = adapter.explore_node("large_entity", depth=2)
    serialized = json.dumps(result, ensure_ascii=False)
    # 现在不截断，返回完整结果（可能 > 20K）
    assert len(serialized) > LIGHTRAG_GRAPH_MAX_CHARS, \
        f"explore_node should return full result (>{LIGHTRAG_GRAPH_MAX_CHARS} chars), got {len(serialized)}"
    assert result.get("status") != "truncated", "explore_node should not truncate internally"
```

注：如果 `_make_adapter_with_large_graph` helper 不存在，复用原测试里构造大图的代码（原 L141-173 里的 setup 部分）。

- [ ] **Step 2: 重写 test_explore_node_center_huge_description_truncated**

读 `tests/test_tool_truncation.py:257-293` 确认当前测试。

**改为**：断言 `explore_node` 返回的 center.description 是完整的（不被截断到 5K）。
```python
def test_explore_node_center_huge_description_not_truncated_internally():
    """center.description 超大时，adapter 不再内部截断，返回完整 description。
    
    截断由 agent_loop 统一关口处理。
    """
    adapter = _make_adapter_with_huge_center_description()  # 复用现有 helper
    result = adapter.explore_node("entity_with_huge_desc", depth=0)
    center = result.get("center", {})
    desc = center.get("description", "")
    # 现在不截断，description 完整保留
    assert len(desc) > 5000, f"center.description should be full (>{5000} chars), got {len(desc)}"
```

- [ ] **Step 3: 重写 test_disk_large_dict_result_gets_truncated**

读 `tests/test_tool_truncation.py:58-102` 确认当前测试。

**改为**：构造完整 `agent_runner_loop` 调用，验证 dispatch 返回后统一关口截断 outcome.data。

**mock 关键点**（避免三个阻断级 bug）：
1. `agent_loop.py:413-418` 用 `exhaust(response_gen)` 或 `yield from response_gen`，要求 `client.chat` 返回**生成器**，不是普通对象。
2. `agent_loop.py:494-500` 用 `tc.function.name` / `tc.function.arguments` 属性访问，要求 `tool_calls` 是对象列表（用 `types.SimpleNamespace`），不是 dict list。
3. `agent_loop.py:489` `if not response.tool_calls:` 是退出条件，FakeClient 必须有状态：第一轮返回 tool_calls，第二轮返回空 tool_calls + content，否则跑满 40 轮 max_turns。

新测试代码（替换原测试）：
```python
def test_unified_gate_truncates_large_dict_from_dispatch(monkeypatch):
    """统一关口在 dispatch 返回后截断超大 dict 结果。
    
    构造一个返回 97 万字符 dict 的工具，通过 agent_runner_loop 调用，
    验证 messages 里的 tool 结果被截断到 ≤ MAX_TOOL_RESULT_CHARS。
    """
    from types import SimpleNamespace
    from agent.generic.agent_loop import agent_runner_loop, MAX_TOOL_RESULT_CHARS
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
    assert len(content) <= MAX_TOOL_RESULT_CHARS, \
        f"unified gate should truncate to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
```

注：FakeHandler 的 `tool_before_callback` / `tool_after_callback` 用 `return; yield` 模式让普通函数成为生成器（`try_call_generator` 检查 `__iter__`，生成器函数返回的 generator 满足条件，会 `yield from` 它，最终拿到 return 值 None）。如果执行者遇到 mock 兼容问题，可改用真实 `NiuHandler` + monkeypatch `dispatch`。

- [ ] **Step 4: 重写 test_direct_mcp_large_dict_result_gets_truncated**

读 `tests/test_tool_truncation.py:296-358` 确认当前测试。

**改为**：与 Step 3 同构，构造 dispatch 返回超大 dict，验证统一关口截断。tool_name 改为含 `/` 的 MCP 路径名，以覆盖 MCP / 路径分支。

```python
def test_unified_gate_truncates_large_dict_from_mcp_path(monkeypatch):
    """统一关口截断 MCP 工具路径的超大 dict 结果。
    
    与 test_unified_gate_truncates_large_dict_from_dispatch 同构，
    但 tool_name 含 '/'（MCP 路径），覆盖 MCP / 分支的统一关口。
    """
    from types import SimpleNamespace
    from agent.generic.agent_loop import agent_runner_loop, MAX_TOOL_RESULT_CHARS
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
    assert len(content) <= MAX_TOOL_RESULT_CHARS, \
        f"unified gate should truncate MCP path to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
```

- [ ] **Step 5: 新增 list 类型截断测试**

在 `tests/test_tool_truncation.py` 末尾追加。注意 Task 1 Step 1 修复后 list 截断返回 `{"status": "truncated", "message": ..., "data": 截断字符串}` dict（不是裸 str），断言要相应调整。
```python
def test_unified_gate_truncates_large_list_result(monkeypatch):
    """统一关口截断超大 list 结果（list 类型在 Task 1 Step 1 新增）。
    
    list 截断后返回 {"status": "truncated", "message": ..., "data": 截断字符串}
    dict（与 _truncate_dict_result 一致），LLM 看到的是结构化 dict 而非裸 str。
    """
    from types import SimpleNamespace
    from agent.generic.agent_loop import agent_runner_loop, MAX_TOOL_RESULT_CHARS
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
    assert len(content) <= MAX_TOOL_RESULT_CHARS, \
        f"unified gate should truncate list to {MAX_TOOL_RESULT_CHARS}, got {len(content)}"
    # list 截断后是 truncated dict（含 status/message/data 字段），不是裸 str
    assert "truncated" in content, "truncated list should have 'truncated' marker"
    assert "[截断]" in content, "truncated list should have [截断] marker"
```

- [ ] **Step 6: 新增小 dict 不截断测试**

在 `tests/test_tool_truncation.py` 末尾追加。覆盖"统一关口只截断超大数据，小数据原样通过"的路径。
```python
def test_unified_gate_preserves_small_dict(monkeypatch):
    """小 dict 不被截断，原样返回。"""
    from types import SimpleNamespace
    from agent.generic.agent_loop import agent_runner_loop, MAX_TOOL_RESULT_CHARS
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
```

- [ ] **Step 7: 新增 should_exit 路径截断测试**

在 `tests/test_tool_truncation.py` 末尾追加。覆盖"should_exit 路径的 outcome.data 也被统一关口截断"——L557-596 的 should_exit 分支会把 outcome.data 进 messages，统一关口在 L557 之前已截断，所以 should_exit 路径的 tool content 也应 ≤ 30K。
```python
def test_unified_gate_truncates_should_exit_path(monkeypatch):
    """should_exit 路径的 data 也被统一关口截断。"""
    from types import SimpleNamespace
    from agent.generic.agent_loop import agent_runner_loop, MAX_TOOL_RESULT_CHARS
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
    assert len(content) <= MAX_TOOL_RESULT_CHARS, \
        f"should_exit path should also be truncated, got {len(content)}"
```

- [ ] **Step 8: 运行全部测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tool_truncation.py -v 2>&1 | tail -25`
Expected: 所有测试 PASS（重写的 4 个 + 新增 list 测试 + 新增小 dict 测试 + 新增 should_exit 测试 + 其他原有测试）

如果 mock 构造失败，执行者可调整 mock 策略（如直接调用 `_truncate_dict_result` / `_truncate_tool_content` 函数测试截断逻辑，不构造完整 agent_runner_loop）。

- [ ] **Step 9: Commit**

```bash
cd <repo_root>
git add tests/test_tool_truncation.py
git commit -m "test(truncation): 重写测试覆盖统一关口 + 新增 list/小 dict/should_exit 测试

- 重写 test_explore_node_*：断言 adapter 返回完整结果（不内部截断）
- 重写 test_*_large_dict_result_gets_truncated：从'断言 dispatch
  内截断'改为'断言 agent_loop 统一关口截断'
- 新增 test_unified_gate_truncates_large_list_result：覆盖 list 类型
  （截断后返回 truncated dict，不是裸 str）
- 新增 test_unified_gate_preserves_small_dict：小 dict 不截断
- 新增 test_unified_gate_truncates_should_exit_path：should_exit 路径
  也被统一关口截断

mock 关键点：FakeClient.chat 是生成器（agent_loop 用 exhaust 消费），
FakeResponse.tool_calls 用 SimpleNamespace（代码用 tc.function.name
属性访问），FakeClient 有状态（第一轮 tool_calls，第二轮空 + content
退出循环）。"
```

---

## Task 6: 端到端验证

**Files:**
- Test: 手动启动程序验证

- [ ] **Step 1: 启动程序**

Run: `cd <repo_root> && ./niu &`
然后: `sleep 8 && ps aux | grep niu | grep -v grep | head -3`
Expected: niu 进程正常启动

- [ ] **Step 2: 验证前端图谱完整显示**

在前端打开知识图谱页面，确认：
- 节点数量正常（之前被截断到几个，现在应该有几十/上百个）
- 边正常显示（之前全没了）
- 不再有 `lightrag_get_graph(snapshot) result 976282 chars > 20000, truncating` 警告日志

Run: `grep "truncating" logs/api_stderr.log 2>/dev/null | tail -5`
Expected: 无新的 truncating 警告（或只在 Agent 工具调用路径出现，前端 API 路径不出现）

- [ ] **Step 3: 验证 Agent 工具调用仍被截断**

跟主 Agent 说："用 lightrag_get_graph explore 看一下知识图谱全貌，depth 3 limit 100"
Expected: Agent 调用工具后，messages 里的 tool 结果被截断（≤ 30K，含 `[截断]` 标记），不爆上下文

- [ ] **Step 4: 验证 region_detector/region_manager 正常工作**

触发一次脑区检测或脑区管理操作（如"刷新脑区状态"），确认：
- region_detector 调 `get_graph_snapshot(limit=0)` 拿完整图
- region_manager 调 `explore_node` 拿完整邻居
- 不报截断相关错误

- [ ] **Step 5: 杀进程清理**

Run: `pkill -9 -f "python.*niu" ; pkill -9 -f "./niu" ; pkill -9 -f "Electron.*niu"`
Expected: 所有 niu 进程被杀

- [ ] **Step 6: 最终 Commit（如有验证修复）**

```bash
cd <repo_root>
git add -A
git commit -m "test: 端到端验证截断关口统一化

前端图谱完整显示（节点+边），Agent 工具调用仍被截断（≤30K），
内部业务（region_detector/region_manager）拿完整结果。"
```

---

## 验证清单

完成所有 Task 后，确认：

- [ ] `python -m pytest tests/test_tool_truncation.py -v` 全部 PASS
- [ ] `python -c "import agent.generic.agent_loop, agent.handler, niu_api.internal.lightrag_adapter; print('OK')"` 输出 OK
- [ ] 前端知识图谱页面节点和边完整显示
- [ ] 不再有 `lightrag_get_graph(snapshot) result ... truncating` 警告（前端路径）
- [ ] Agent 调用 lightrag_get_graph 工具时 messages 里 tool 结果 ≤ 30K
- [ ] region_detector/region_manager 正常工作

## 风险与回滚

### 风险

1. **统一关口性能**：超大 dict（97 万字符）的 `json.dumps` + while 循环截断约 500ms，可接受（单工具调用场景）。如果一轮内多个超大工具结果串行截断，可能累计到秒级——加性能日志监控。
2. **list 类型截断**：Task 1 新增 list 分支用 `json.dumps` 序列化后截断，返回 `{"status": "truncated", "message": ..., "data": 截断字符串}` dict（与 `_truncate_dict_result` 一致），LLM 看到的是结构化 dict 而非裸 str——避免 LLM 看到不完整 JSON（如 `[{...}, {...`）无法解析。
3. **should_exit 路径**：统一关口改写 outcome.data 后，should_exit 路径的 `outcome.data` 也是截断后的——这是预期行为（should_exit 结果也要进 messages），但 `return {"data": outcome.data}` 给调用方的也是截断后的，需确认调用方不依赖原始 data 做决策。

### 回滚

1. **备份提交**：执行前 `git add -A && git commit -m "backup: 截断关口统一化前基线"`
2. **分步提交**：每个 Task 单独 commit，便于二分回退
3. **快速回滚**：`git revert <commit-sha>` 回滚到改造前
4. **功能开关**：如统一关口出问题，可临时在 Task 1 的截断块加 `if os.environ.get("NIU_SKIP_TRUNCATE"): pass` 跳过

## 不改动部分

- `niu_api/compat.py:978-1003` — 估算用截断（不是真截断 messages），保留
- `mcp-servers/lightrag-server/...__init__.py` — MCP 函数调 adapter 拿完整结果，截断由统一关口负责
- `niu_api/kg_api.py` — 前端 API 直接调 adapter
- `niu_api/internal/region_detector.py` / `region_manager.py` — 内部业务直接调 adapter
- `_truncate_graph_result` 函数（已标 DEPRECATED，无调用方）/ `LIGHTRAG_GRAPH_MAX_CHARS` 常量 — 保留（测试仍引用）
