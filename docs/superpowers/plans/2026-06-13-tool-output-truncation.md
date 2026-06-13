# 工具输出截断 + 移除 50K 自动整理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在源头拦截超长工具输出，防止撑爆 LLM 上下文；移除不合理的 50K 增量自动整理触发。

**Architecture:** 1) 在 agent_loop.py 的 messages.append 处截断超长工具结果（不影响 DB 持久化）；2) 将 _should_auto_tidy 的增量阈值逻辑改为仅保留使用率兜底（≥80% 才触发），移除 50K 增量触发路径。

**Tech Stack:** Python, agent_loop.py, compat.py

---

## File Structure

| File | Responsibility |
|------|---------------|
| `agent/generic/agent_loop.py` | 截断函数 + messages.append 处截断 |
| `niu_api/compat.py` | _should_auto_tidy 移除增量阈值，仅保留使用率兜底 |

---

### Task 1: 移除 50K 增量自动整理触发

**Files:**
- Modify: `niu_api/compat.py:263-291`

**问题：** `_should_auto_tidy` 有三条触发路径：1) 从未整理且总量≥50K；2) 增量≥50K；3) 使用率≥80%。前两条路径导致 50% 使用率也会触发整理，不合理。正确的保护机制应该是工具输出截断（Task 2），而不是低阈值自动整理。

**修改方案：** 仅保留使用率兜底路径（≥80%），移除 50K 增量阈值路径。保留"从未整理"的特殊处理（首次也走使用率判断）。

- [ ] **Step 1: 修改 `_should_auto_tidy`**

将 `niu_api/compat.py:263-291` 的 `_should_auto_tidy` 替换为：

```python
def _should_auto_tidy(current_tokens: int, last_tidy_tokens: int, context_window_tokens: int = 0) -> bool:
    """
    判断是否应该触发自动增量整理。

    仅当上下文使用率 >= 80% 时触发。
    工具输出的超长问题由 agent_loop 中的截断机制处理，
    不再用 50K 增量阈值触发整理（该阈值在重启后容易误触发）。
    """
    if current_tokens <= 0:
        return False
    if context_window_tokens <= 0:
        return False
    usage_ratio = current_tokens / context_window_tokens
    return usage_ratio >= 0.80
```

- [ ] **Step 2: 更新 `_check_and_trigger_auto_tidy` 中的日志**

`niu_api/compat.py:314-316` 的日志引用了 `increment`，更新为只显示使用率：

```python
        usage_pct = f"{current_tokens/context_window_tokens:.1%}" if context_window_tokens > 0 else "N/A"
        logger.info(f"[AutoTidy] Triggering sleep tidy: tokens={current_tokens}, usage={usage_pct}")
```

删除 `increment = current_tokens - last_tidy_tokens` 行（不再需要）。

- [ ] **Step 3: 验证语法**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "fix: remove 50K incremental auto-tidy trigger, use usage-ratio only"
```

---

### Task 2: 工具输出截断

**Files:**
- Modify: `agent/generic/agent_loop.py:394-399,437-442`

**设计要点：**
- 截断阈值：`MAX_TOOL_RESULT_CHARS = 30000`（约 15000-30000 token）
- 截断位置：`messages.append` 时截断 `content`，**不影响 `tool_results`**（从而不影响 `persist` 事件和 DB 持久化）
- 截断格式：包含原始长度提示，让 LLM 知道结果不完整
- 两条路径都需要截断：正常路径（L437-442）和 should_exit 路径（L394-399）
- **JSON 格式注意：** 当 outcome.data 是 dict/list 时，json.dumps 后截断会破坏 JSON 格式。这是可接受的——LLM 将 content 读取为文本而非结构化数据，截断标记会传达其不完整性，完整数据仍保留在 DB 中。
- **历史加载截断：** 从 DB 加载的 tool 消息内容是完整的，下次对话时可能绕过截断。因此在 history 重建路径（L165-178）也需要对 tool 角色消息做截断。

- [ ] **Step 1: 在 agent_loop.py 顶部定义截断函数和常量**

在 `agent_runner_loop` 函数之前（约 line 121 `_fifo_prune` 之后）添加：

```python
MAX_TOOL_RESULT_CHARS = 30000  # 单个工具结果最大字符数（约 15K-30K token）


def _truncate_tool_content(content: str, tool_name: str = "") -> str:
    """截断超长工具输出，保留开头部分并添加截断标记。"""
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    truncated = content[:MAX_TOOL_RESULT_CHARS]
    label = f"工具 {tool_name}" if tool_name else "工具"
    marker = f"\n\n[截断] {label}原始输出 {len(content)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。如需完整内容，请调整查询参数或分页重新获取。"
    return truncated + marker
```

- [ ] **Step 2: 在 should_exit 路径的 messages.append 处加截断**

`agent_loop.py:394-399`，将：

```python
                for tool_result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": tool_result["content"]
                    })
```

改为：

```python
                for tool_result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", ""))
                    })
```

- [ ] **Step 3: 在正常路径的 messages.append 处加截断**

`agent_loop.py:437-442`，将：

```python
        for tool_result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            })
```

改为：

```python
        for tool_result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", ""))
            })
```

- [ ] **Step 4: 在 tool_results 构建时保存 tool_name**

为了在截断标记中显示工具名，需要在 `tool_results.append` 时额外保存 `tool_name` 字段。

**关键：** L359-360 处循环 `for ii, tc in enumerate(tool_calls):` 中，`tc` 是 dict（`{"tool_name": ..., "args": ..., "id": ...}`），不是对象。L360 已有 `tool_name = tc["tool_name"]`，直接使用这个变量。

具体修改：

L390: `tool_results.append({"tool_use_id": tid, "content": datastr, "tool_name": tool_name})`
L392: `tool_results.append({"tool_use_id": tid, "content": "", "tool_name": tool_name})`
L430: `tool_results.append({"tool_use_id": tid, "content": datastr, "tool_name": tool_name})`
L432: `tool_results.append({"tool_use_id": tid, "content": "", "tool_name": tool_name})`

- [ ] **Step 5: 在 history 加载路径对 tool 消息做截断**

`agent_loop.py:154-157` 处，从 history 重建 tool 消息时，也需要截断。将：

```python
            elif role == "tool" and msg.get("tool_call_id") and content is not None:
                # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                entry = {"role": "tool", "content": content, "tool_call_id": msg["tool_call_id"]}
                messages.append(entry)
```

改为：

```python
            elif role == "tool" and msg.get("tool_call_id") and content is not None:
                # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                # 截断超长的 tool 内容（DB 中保存了完整内容，但 LLM 上下文需要保护）
                entry = {"role": "tool", "content": _truncate_tool_content(content), "tool_call_id": msg["tool_call_id"]}
                messages.append(entry)
```

- [ ] **Step 6: 验证语法**

Run: `python3 -c "import py_compile; py_compile.compile('agent/generic/agent_loop.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 7: 运行现有测试**

Run: `PYTHONPATH=. python -m pytest tests/test_proactive_fifo.py -v`

Expected: 11 passed

- [ ] **Step 8: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "feat: truncate tool results exceeding 30K chars in LLM context"
```

---

### Task 2.5: 更新 _should_auto_tidy 相关测试

**Files:**
- Modify: `tests/test_subagent_overflow.py:606-624`

**问题：** 4 个测试直接调用 `_should_auto_tidy` 并传递 `threshold` 参数，移除 `threshold` 后会 TypeError。

- [ ] **Step 1: 更新测试**

将 `tests/test_subagent_overflow.py` 中引用 `_should_auto_tidy` 的测试替换为基于使用率的测试：

```python
def test_should_auto_tidy_usage_above_80pct():
    """使用率 >= 80% 时触发整理"""
    assert _should_auto_tidy(current_tokens=170000, last_tidy_tokens=0, context_window_tokens=200000) is True

def test_should_auto_tidy_usage_below_80pct():
    """使用率 < 80% 时不触发整理"""
    assert _should_auto_tidy(current_tokens=150000, last_tidy_tokens=0, context_window_tokens=200000) is False

def test_should_auto_tidy_zero_context_window():
    """context_window_tokens=0 时不触发整理（无法计算使用率）"""
    assert _should_auto_tidy(current_tokens=100000, last_tidy_tokens=0, context_window_tokens=0) is False

def test_should_auto_tidy_zero_current_tokens():
    """current_tokens=0 时不触发整理"""
    assert _should_auto_tidy(current_tokens=0, last_tidy_tokens=0, context_window_tokens=200000) is False
```

- [ ] **Step 2: 运行测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_subagent_overflow.py -v -k "should_auto_tidy"`

Expected: 4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_subagent_overflow.py
git commit -m "test: update _should_auto_tidy tests for usage-ratio-only logic"
```

---

### Task 3: 真实测试验证

**Files:** 无代码修改

- [ ] **Step 1: 清理缓存并重启 API**

```bash
pkill -9 -f "niu_api"
find . -name "__pycache__" -type d -exec rm -rf {} +
PYTHONPATH=agent:.. python -m niu_api > /tmp/niu_api_stdout.log 2> /tmp/niu_api_stderr.log &
```

- [ ] **Step 2: 发送消息触发工具调用（如查询知识图谱）**

```bash
curl -s http://localhost:9876/chat/sync -H "Content-Type: application/json" -d '{"message":"查一下知识图谱里有哪些实体"}'
```

- [ ] **Step 3: 检查 stderr 日志确认无报错**

```bash
tail -20 /tmp/niu_api_stderr.log
```

- [ ] **Step 4: 检查 DB 中的 tool 消息是否完整（未被截断）**

确认 persist 事件写入 DB 的内容是完整的，截断只影响 LLM 上下文。

- [ ] **Step 5: 停止 API**

```bash
pkill -9 -f "niu_api"
```
