# 死代码清理 + 历史 tool 消息工具名注入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理 `_read/write_last_tidy_tokens` 死代码，修复历史 tool 消息截断标记缺少工具名的问题。

**Architecture:** 1) 移除 `_read_last_tidy_tokens`（零调用）和 `_write_last_tidy_tokens`（4处调用均写无人读的文件），清理所有相关调用点、import、游标列表和测试；2) 在 history 加载循环中增量构建 `tool_call_id → function.name` 映射，让截断标记显示具体工具名。

**Tech Stack:** Python, agent_loop.py, compat.py, runner.py

---

## File Structure

| File | Responsibility |
|------|---------------|
| `niu_api/compat.py` | 删除 `_read/write_last_tidy_tokens` 函数定义 + 清理 4 处调用 + 移除游标列表项 |
| `agent/runner.py` | 删除 import 和调用 |
| `tests/test_journal_agent_tidy.py` | 更新游标断言 |
| `agent/generic/agent_loop.py` | history 加载循环添加 tool_call_id→tool_name 映射 |

---

### Task 1: 清理 `_read/write_last_tidy_tokens` 死代码

**Files:**
- Modify: `niu_api/compat.py:307-317,320-331,345-355,1398-1408,1878-1888,850-859`
- Modify: `agent/runner.py:785-790,1175-1182`
- Modify: `tests/test_journal_agent_tidy.py:182-186`

**问题：** `_should_auto_tidy` 已简化为纯使用率判断，不再读取 `last_tidy_tokens`。但 `_write_last_tidy_tokens` 仍被 4 处调用，写入一个无人读取的文件。`_read_last_tidy_tokens` 零调用，完全是死代码。GitNexus 确认 `_read_last_tidy_tokens` 上游零依赖（LOW 风险），`_write_last_tidy_tokens` 的 3 个直接调用者（`_on_context_high_usage`、`_run_auto_tidy`、`_tidy_context_impl`）都是删除死写入，不影响活跃逻辑。

- [ ] **Step 1: 删除 `_read_last_tidy_tokens` 函数定义**

`niu_api/compat.py:307-317`，删除整个函数：

```python
# 删除以下整段代码（L307-317）：
def _read_last_tidy_tokens() -> int:
    ...
```

- [ ] **Step 2: 删除 `_write_last_tidy_tokens` 函数定义**

`niu_api/compat.py:320-331`（注意 Step 1 删除后行号会变化，按函数名定位），删除整个函数：

```python
# 删除以下整段代码：
def _write_last_tidy_tokens(tokens: int):
    ...
```

- [ ] **Step 3: 清理 `_run_auto_tidy` 中的调用**

`niu_api/compat.py:349-355`，将：

```python
            if result.get("status") == "error":
                # _tidy_context_impl 失败时可能没写 last_tidy_tokens，兜底写入当前值
                store = await get_message_store()
                messages = await store.get_messages()
                current_tokens = _estimate_total_tokens(messages)
                _write_last_tidy_tokens(current_tokens)
                logger.warning(f"[AutoTidy] tidy_context returned error: {result}, last_tidy_tokens updated to {current_tokens}")
```

改为：

```python
            if result.get("status") == "error":
                logger.warning(f"[AutoTidy] tidy_context returned error: {result}")
```

- [ ] **Step 4: 清理 `_tidy_context_impl` sleep 路径中的调用**

`niu_api/compat.py:1403-1408`，将：

```python
            # 更新 last_tidy_tokens
            try:
                post_tidy_msgs = await store.get_messages()
                _write_last_tidy_tokens(_estimate_total_tokens(post_tidy_msgs))
            except Exception as e:
                logger.warning(f"[Tidy] Failed to update last_tidy_tokens: {e}")
```

整段删除（4 行代码 + 1 行注释 + 1 行空行，共删除 6 行）。

- [ ] **Step 5: 清理 `_tidy_context_impl` force 路径中的调用**

`niu_api/compat.py:1883-1888`，将：

```python
            # 整理完成后更新 last_tidy_tokens，防止自动整理阈值失效
            try:
                post_tidy_msgs = await store.get_messages()
                _write_last_tidy_tokens(_estimate_total_tokens(post_tidy_msgs))
            except Exception as e:
                logger.warning(f"[Tidy] Force: Failed to update last_tidy_tokens: {e}")
```

整段删除。

- [ ] **Step 6: 从 `clear_chat` 游标列表移除 `last_tidy_tokens.json`**

`niu_api/compat.py:855`，将：

```python
        for cursor_name in ["last_entity_extract.json", "last_dream_evolve.json", "last_compress.json", "last_tidy_tokens.json", "last_journal.json"]:
```

改为：

```python
        for cursor_name in ["last_entity_extract.json", "last_dream_evolve.json", "last_compress.json", "last_journal.json"]:
```

- [ ] **Step 7: 清理 `agent/runner.py` 中的 import 和调用**

L785-790，将：

```python
        from niu_api.compat import (
            _build_incremental_msg_text,
            _truncate_task_for_subagent,
            _estimate_total_tokens,
            _write_last_tidy_tokens,
        )
```

改为：

```python
        from niu_api.compat import (
            _build_incremental_msg_text,
            _truncate_task_for_subagent,
            _estimate_total_tokens,
        )
```

L1177-1182，将：

```python
            # 更新 last_tidy_tokens
            try:
                post_tidy_msgs = self._sync_get_messages()
                _write_last_tidy_tokens(_estimate_total_tokens(post_tidy_msgs))
            except Exception as e:
                logger.warning(f"[Runner] Force: Failed to update last_tidy_tokens: {e}")
```

整段删除。

- [ ] **Step 8: 更新 `test_journal_agent_tidy.py` 中的断言**

`tests/test_journal_agent_tidy.py:182-186`，将：

```python
            if "last_entity_extract.json" in line and "last_journal.json" in line:
                # 所有游标文件都应在同一行
                assert "last_dream_evolve.json" in line
                assert "last_compress.json" in line
                assert "last_tidy_tokens.json" in line
```

改为：

```python
            if "last_entity_extract.json" in line and "last_journal.json" in line:
                # 所有游标文件都应在同一行
                assert "last_dream_evolve.json" in line
                assert "last_compress.json" in line
```

- [ ] **Step 9: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); py_compile.compile('agent/runner.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 10: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && PYTHONPATH=. python -m pytest tests/test_journal_agent_tidy.py tests/test_subagent_overflow.py -v -k "tidy or auto_tidy"`

Expected: all passed

- [ ] **Step 11: Commit**

```bash
git add niu_api/compat.py agent/runner.py tests/test_journal_agent_tidy.py
git commit -m "refactor: remove dead _read/write_last_tidy_tokens code"
```

---

### Task 2: 历史 tool 消息工具名注入

**Files:**
- Modify: `agent/generic/agent_loop.py:165-192`

**问题：** history 加载路径中，tool 消息的截断标记只显示"工具原始输出..."，LLM 无法知道是哪个工具的输出。DB 中 assistant 消息的 `tool_calls` 字段使用 OpenAI 格式（`tc["function"]["name"]` 存工具名），可以在 history 加载循环中增量构建 `tool_call_id → function.name` 映射。

- [ ] **Step 1: 修改 history 加载循环，添加 tool_call_id → tool_name 映射**

`agent/generic/agent_loop.py:165-192`，将：

```python
    if history:
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and (content or msg.get("tool_calls")):
                entry = {"role": role, "content": content}
                # 还原 tool_calls（assistant 消息可能携带工具调用）
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                messages.append(entry)
            elif role == "tool" and msg.get("tool_call_id") and content is not None:
                # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                # 截断超长的 tool 内容（DB 中保存了完整内容，但 LLM 上下文需要保护）
                entry = {"role": role, "content": _truncate_tool_content(content), "tool_call_id": msg["tool_call_id"]}
                messages.append(entry)
```

改为：

```python
    if history:
        # 从 assistant 消息的 tool_calls 构建 tool_call_id → tool_name 映射
        # 用于截断标记中显示工具名（DB 不存 tool_name，需从关联的 assistant 消息提取）
        _tc_id_to_name: dict[str, str] = {}
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("function", {}).get("name", "")
                    if tc_id and tc_name:
                        _tc_id_to_name[tc_id] = tc_name
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and (content or msg.get("tool_calls")):
                entry = {"role": role, "content": content}
                # 还原 tool_calls（assistant 消息可能携带工具调用）
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                messages.append(entry)
            elif role == "tool" and msg.get("tool_call_id") and content is not None:
                # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                # 截断超长的 tool 内容（DB 中保存了完整内容，但 LLM 上下文需要保护）
                tool_name = _tc_id_to_name.get(msg["tool_call_id"], "")
                entry = {"role": role, "content": _truncate_tool_content(content, tool_name), "tool_call_id": msg["tool_call_id"]}
                messages.append(entry)
```

**设计说明：**
- 两遍遍历：第一遍构建映射，第二遍加载消息。因为 assistant 消息中提取映射的逻辑和消息追加逻辑混在一起会导致一次遍历中处理 assistant 消息时需要同时做两件事（提取映射 + 构建entry），两遍遍历更清晰。
- 映射使用 OpenAI 格式 `tc["function"]["name"]`，与 DB 存储格式一致（`session.py:193` 确认）。
- 优雅降级：如果 `tool_call_id` 不在映射中（如 assistant 消息在分页范围外），`_tc_id_to_name.get()` 返回 `""`，截断标记回退到通用"工具"标签。

- [ ] **Step 2: 添加 `_truncate_tool_content` 工具名测试**

在 `tests/test_proactive_fifo.py` 末尾添加：

```python
def test_truncate_tool_content_with_name():
    """截断标记应包含工具名"""
    from agent.generic.agent_loop import _truncate_tool_content, MAX_TOOL_RESULT_CHARS
    long_content = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(long_content, "memory-server/remember")
    assert "memory-server/remember" in result
    assert "[截断]" in result
    assert len(result) <= MAX_TOOL_RESULT_CHARS


def test_truncate_tool_content_without_name():
    """无工具名时截断标记显示通用标签"""
    from agent.generic.agent_loop import _truncate_tool_content, MAX_TOOL_RESULT_CHARS
    long_content = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(long_content)
    assert "工具" in result
    assert "memory-server" not in result
    assert len(result) <= MAX_TOOL_RESULT_CHARS
```

- [ ] **Step 3: 运行新测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && PYTHONPATH=. python -m pytest tests/test_proactive_fifo.py -v -k "truncate_tool_content"`

Expected: 2 passed

- [ ] **Step 4: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('agent/generic/agent_loop.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 5: 运行全部测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && PYTHONPATH=. python -m pytest tests/test_proactive_fifo.py -v`

Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_proactive_fifo.py
git commit -m "fix: inject tool name into history tool message truncation markers"
```
