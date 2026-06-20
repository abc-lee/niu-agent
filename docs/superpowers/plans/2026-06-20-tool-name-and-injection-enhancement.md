# Tool 消息添加 name 字段 + 向量检索增强工具调用信息 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 tool 消息缺少 name 字段的问题，并在动态注入向量检索 query 中加入工具调用名和参数关键词，提升检索精度。

**Architecture:** 两部分修复：(1) agent_loop.py 三处构建 tool 消息时添加 `name` 字段 + runner.py 压缩 reload 路径添加 `name`；(2) runner.py 两个 context 提取函数增加从 assistant 的 tool_calls 提取完整调用命令行（工具名+参数），而非仅工具名。不修改 persist 事件和 DB schema——`name` 不需要持久化，历史还原时通过 `_tc_id_to_name` 映射从 assistant 的 tool_calls 推导。

**Tech Stack:** Python, OpenAI API message format, LightRAG vector search

---

## 修改文件清单

| 文件 | 职责 |
|------|------|
| `agent/generic/agent_loop.py` | tool 消息添加 `name` 字段（3处构建） |
| `agent/runner.py` | `_extract_context_from_history` 和 `_extract_context_from_messages` 增加工具调用信息提取 + 压缩 reload 路径添加 `name` |

---

### Task 1: agent_loop.py — 历史还原路径 tool 消息添加 name 字段

**Files:**
- Modify: `agent/generic/agent_loop.py:234`

**原因:** 历史消息还原时，tool 消息只有 `role`、`content`、`tool_call_id`，缺少 `name`。`tool_name` 变量在第 233 行已通过 `_tc_id_to_name` 映射获取，直接加入即可。

- [ ] **Step 1: 修改历史还原路径的 tool 消息构建**

将第 234 行：
```python
                entry = {"role": role, "content": _truncate_tool_content(content, tool_name), "tool_call_id": msg["tool_call_id"]}
```

改为：
```python
                entry = {"role": role, "content": _truncate_tool_content(content, tool_name), "tool_call_id": msg["tool_call_id"], "name": tool_name}
```

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile agent/generic/agent_loop.py
```

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "fix: add name field to tool message in history restore path"
```

---

### Task 2: agent_loop.py — should_exit 路径 tool 消息添加 name 字段

**Files:**
- Modify: `agent/generic/agent_loop.py:456-460`

- [ ] **Step 1: 修改 should_exit 路径的 tool 消息构建**

将第 456-460 行：
```python
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", ""))
                    })
```

改为：
```python
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                        "name": tool_result.get("tool_name", ""),
                    })
```

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile agent/generic/agent_loop.py
```

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "fix: add name field to tool message in should_exit path"
```

---

### Task 3: agent_loop.py — 正常路径 tool 消息添加 name 字段

**Files:**
- Modify: `agent/generic/agent_loop.py:499-503`

- [ ] **Step 1: 修改正常路径的 tool 消息构建**

将第 499-503 行：
```python
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", ""))
            })
```

改为：
```python
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                "name": tool_result.get("tool_name", ""),
            })
```

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile agent/generic/agent_loop.py
```

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "fix: add name field to tool message in normal path"
```

---

### Task 4: runner.py — 压缩 reload 路径 tool 消息添加 name 字段

**Files:**
- Modify: `agent/runner.py:1136-1148`

**原因:** `_on_context_high_usage` 压缩回调从 DB 重新加载消息后，构建的 tool 消息只有 `role`、`content`、`tool_call_id`，缺少 `name`。这些消息直接替换到 `agent_loop` 的 `messages` 列表中，发给 LLM 时 tool 消息缺少 `name` 字段。需要与 `agent_loop.py` 的历史还原路径一样，构建 `_tc_id_to_name` 映射并添加 `name`。

**注意：** persist 事件不修改——`name` 不需要持久化到 DB，因为历史还原时 `agent_loop.py:212-220` 已通过 `_tc_id_to_name` 映射从 assistant 的 tool_calls 推导出 tool_name。persist 中的 `name` 无人消费，加了是死代码。

- [ ] **Step 1: 读取 `_on_context_high_usage` 中 reload 路径的代码**

读取 `agent/runner.py` 第 1136-1148 行，找到从 DB 加载消息后构建 dict 的代码。

- [ ] **Step 2: 在 reload 逻辑中添加 `_tc_id_to_name` 映射和 `name` 字段**

在 reload 路径中，遍历 assistant 消息构建 `_tc_id_to_name` 映射（与 `agent_loop.py:212-220` 相同逻辑），然后在构建 tool 消息时添加 `"name": _tc_id_to_name.get(msg["tool_call_id"], "")`。

- [ ] **Step 3: 语法检查**

```bash
python -m py_compile agent/runner.py
```

- [ ] **Step 4: 提交**

```bash
git add agent/runner.py
git commit -m "fix: add name field to tool message in context high usage reload path"
```

---

### Task 5: runner.py — _extract_context_from_history 增加工具调用信息

**Files:**
- Modify: `agent/runner.py:1272-1303`

**原因:** 当前只提取 user/assistant 的 content，完全忽略 tool_calls。需要从 assistant 消息的 tool_calls 中提取完整的工具调用命令行（工具名 + 参数），让向量检索更精准。例如 `ingest_photo(file_path="/photos/任飞.jpg", abstract="任飞在海边")` 比 `"ingest_photo"` + 短字符串参数信息量大得多。

- [ ] **Step 1: 修改 `_extract_context_from_history` 函数**

将第 1289-1298 行的循环体：
```python
        context_parts = []
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if content and role in ("user", "assistant"):
                # 截断过长的内容（80字符，保持向量匹配精度）
                if len(content) > 80:
                    content = content[:80] + "..."
                context_parts.append(f"{role}: {content}")
```

改为：
```python
        context_parts = []
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if content and role in ("user", "assistant"):
                if len(content) > 80:
                    content = content[:80] + "..."
                context_parts.append(f"{role}: {content}")

            # 从 assistant 的 tool_calls 提取完整调用命令行
            if role == "assistant":
                for tc in msg.get("tool_calls", [])[:3]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if name:
                        call_str = f"{name}({fn.get('arguments', '')})"
                        context_parts.append(call_str)
```

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile agent/runner.py
```

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "feat: extract full tool call string in _extract_context_from_history"
```

---

### Task 6: runner.py — _extract_context_from_messages 增加完整工具调用提取

**Files:**
- Modify: `agent/runner.py:496-504`

**原因:** 当前已提取工具名，但每条 assistant 消息只取 1 个（`break`），且不提取参数。需要改为提取完整的工具调用命令行（工具名 + 参数），放宽到最多 3 个工具调用。

- [ ] **Step 1: 修改 `_extract_context_from_messages` 中的 tool_calls 提取逻辑**

将第 496-504 行：
```python
            # 从 assistant 的 tool_calls 中提取工具名
            # 3条消息中assistant最多出现2次（第1、3条），每次最多1个工具名，共最多2个
            if role == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if name:
                        context_parts.append(name)
                        break  # 每条assistant消息只取1个工具名
```

改为：
```python
            # 从 assistant 的 tool_calls 中提取完整调用命令行
            if role == "assistant":
                for tc in msg.get("tool_calls", [])[:3]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if name:
                        call_str = f"{name}({fn.get('arguments', '')})"
                        context_parts.append(call_str)
```

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile agent/runner.py
```

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "feat: extract full tool call string in _extract_context_from_messages"
```

---

## 验证步骤

1. 启动程序 `./niu`
2. 正常对话，触发工具调用（如照片入库、文件处理等）
3. 检查日志中发给 LLM 的 tool 消息是否包含 `name` 字段
4. 检查动态注入的提示词是否包含完整的工具调用命令行（工具名+参数）
5. 对比修改前后，处理照片时动态注入的脑区/知识体系/SKILL 是否更精准（应包含"照片"相关内容）
6. 触发上下文压缩（长对话），确认压缩 reload 后 tool 消息仍包含 `name` 字段
7. 重启程序，确认历史消息还原时 tool 消息的 `name` 字段正确
