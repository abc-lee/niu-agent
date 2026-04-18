# 上下文管理 KB→Token 统一实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将上下文管理的计量单位从"字符-KB"统一为"token"，使压缩目标（token 使用率）与传给子 Agent 的消息大小标注一致，确保强制压缩能精确达到 token 减少目标。

**Architecture:** 在 compat.py 中用 litellm.token_counter 为每条消息计算 token 数，传给 context-manager 时标注 token 而非 KB。session-manager MCP 的 get_messages 也改为返回 token 数。delete_messages 的 freed 指标改为 token。context-manager.md 的描述从 KB 改为 token。

**Tech Stack:** Python, litellm.token_counter (tiktoken cl100k_base), FastAPI

---

## 问题分析

当前代码同时使用两种计量单位：
- **字符-KB**：`len(content) / 1024`，传给子 Agent、显示在 prompt 中
- **token**：`litellm.token_counter(model="gpt-4o")`，用于计算使用率百分比

两者没有换算关系：中文 1字符-KB ≈ 700-1000 tokens，英文 1字符-KB ≈ 128 tokens。子 Agent 按 KB 累计删除，无法精确达到 token 压缩目标。

## 涉及文件

| 文件 | 操作 | 职责 |
|------|------|------|
| `niu_api/compat.py` | 修改 | tidy_context 端点：每条消息算 token，prompt 标 token |
| `mcp-servers/session-manager/src/niu_session_manager/__init__.py` | 修改 | get_messages 返回 token 数而非 KB |
| `config/agents/context-manager.md` | 修改 | 所有 KB 描述改为 token |
| `agent/context_manager.py` | 修改 | count_tokens_simple 已有，确认回退估算修正 |

---

### Task 1: compat.py — tidy_context 改用 token 标注

**Files:**
- Modify: `niu_api/compat.py:341-382`

当前代码：
```python
total_chars = sum(len(msg.content or "") for msg in messages)
total_kb = total_chars / 1024
# ...
kb = len(msg.content or "") / 1024
prompt += f"[idx:{idx}] {kb:.1f}KB {msg.role}: {msg.content[:100]}\n"
```

- [ ] **Step 1: 为每条消息计算 token 数**

在 tidy_context 中，逐条计算 token 数而非字符-KB：

```python
# 替换 total_chars / total_kb 计算
msg_tokens = []
try:
    from litellm import token_counter
    for msg in messages:
        t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
        msg_tokens.append(t)
    estimated_tokens = sum(msg_tokens)
except Exception:
    msg_tokens = [max(1, len(msg.content or "") // 2) for msg in messages]
    estimated_tokens = sum(msg_tokens)
```

- [ ] **Step 2: prompt 中标注 token 而非 KB**

睡眠模式 prompt：
```python
prompt = f"""系统进入睡眠状态。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

消息列表：
共 {message_count} 条消息（idx 从小到大 = 从旧到新）

"""
for idx, msg in enumerate(messages):
    tokens = msg_tokens[idx]
    prompt += f"[idx:{idx}] {tokens}tokens {msg.role}: {msg.content[:100]}\n"
prompt += "\n请按照【模式一：睡眠整理（非强制）】的规则处理。"
```

dream-evolver prompt 同样改：
```python
dream_prompt = f"""系统进入睡眠状态，触发梦境进化。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）
...
"""
for idx, msg in enumerate(messages):
    tokens = msg_tokens[idx]
    dream_prompt += f"[idx:{idx}] {tokens}tokens {msg.role}: {msg.content[:100]}\n"
```

- [ ] **Step 3: 删除 total_kb 变量，日志改用 token**

```python
logger.info(f"[Tidy] Current context: {message_count} messages, {estimated_tokens} tokens, {usage_percent:.1f}%")
```

- [ ] **Step 4: 验证**

启动 API，触发睡眠整理，检查日志中 prompt 是否标注 token 而非 KB。

---

### Task 2: compat.py — delete_messages 改用 token

**Files:**
- Modify: `niu_api/compat.py:254-272`

当前代码：
```python
freed_kb = 0.0
for idx in message_indices:
    if 0 <= idx < len(all_messages):
        msg = all_messages[idx]
        freed_kb += len(msg.content or "") / 1024
```

- [ ] **Step 1: freed_kb 改为 freed_tokens**

```python
freed_tokens = 0
message_ids = []
for idx in message_indices:
    if 0 <= idx < len(all_messages):
        msg = all_messages[idx]
        # 用 litellm 计算 token
        try:
            from litellm import token_counter
            t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
        except Exception:
            t = max(1, len(msg.content or "") // 2)
        freed_tokens += t
        message_ids.append(msg.id)

if message_ids:
    deleted_count = await store.delete_messages_by_ids(message_ids)
    logger.info(f"[Context] Deleted {deleted_count} messages, freed {freed_tokens} tokens")
    return {
        "deleted_count": deleted_count,
        "freed_tokens": freed_tokens,
    }

return {"deleted_count": 0, "freed_tokens": 0}
```

- [ ] **Step 2: 验证**

调用 delete_messages 后检查返回值是否包含 `freed_tokens`。

---

### Task 3: session-manager MCP — get_messages 返回 token 数

**Files:**
- Modify: `mcp-servers/session-manager/src/niu_session_manager/__init__.py:154-177`

当前代码：
```python
kb = max(1, len(content) // 1024)  # At least 1KB
total_kb += kb
# ...
{"idx": i, "kb": kb, "role": ...}
# ...
"total_kb": total_kb,
```

- [ ] **Step 1: 改为 token 计算**

```python
# Format messages with token counts
messages = result.get("messages", [])
formatted = []
total_tokens = 0

for i, msg in enumerate(messages):
    content = msg.get("content", "")
    try:
        from litellm import token_counter
        tokens = token_counter(model="gpt-4o", messages=[{"role": msg.get("role", "user"), "content": content}])
    except Exception:
        tokens = max(1, len(content) // 2)
    total_tokens += tokens

    formatted.append(
        {
            "idx": i,
            "tokens": tokens,
            "role": msg.get("role", "unknown"),
            "content": content,
        }
    )

output = {
    "total_messages": len(messages),
    "total_tokens": total_tokens,
    "messages": formatted,
}
```

- [ ] **Step 2: 更新 TOOL_SCHEMAS 描述**

将 get_messages 的 description 从 "KB sizes" 改为 "token counts"。

- [ ] **Step 3: 验证**

通过 MCP 工具调用 get_messages，检查返回值是否包含 `tokens` 和 `total_tokens`。

---

### Task 4: context-manager.md — KB 描述改为 token

**Files:**
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: 模式一（睡眠整理）描述改 token**

将消息列表示例从：
```
[idx:0] 1KB user: 今天天气不错
```
改为：
```
[idx:0] 15tokens user: 今天天气不错
```

- [ ] **Step 2: 模式二（强制压缩）描述改 token**

将：
```
当前上下文：150 KB（75%）
目标上下文：100 KB（50%）
需要减少：50 KB
```
改为：
```
当前上下文：150000 tokens（75%）
目标上下文：100000 tokens（50%）
需要减少：50000 tokens
```

将删除优先级中的"KB 大"改为"tokens 多"，累计 KB 改为累计 tokens。

- [ ] **Step 3: 约束和格式描述改 token**

所有涉及 KB 的描述统一改为 token。

---

### Task 5: context_manager.py — 修正回退估算

**Files:**
- Modify: `agent/context_manager.py:82-87`

当前回退估算 `len(content) // 2` 假设 2 字符/token，对中文基本准确（1中文字≈1token），对英文严重低估（8字符≈1token）。

- [ ] **Step 1: 修正回退估算**

中文为主时约 1.5 字符/token，英文约 4 字符/token。取中间值 3：
```python
except Exception:
    # 回退：混合文本约 3 字符/token（中文≈1.5，英文≈4）
    total_tokens = 0
    for msg in messages:
        content = msg.get("content", "")
        total_tokens += max(1, len(content) // 3) + 4
    return total_tokens
```

- [ ] **Step 2: 验证**

确认 litellm 正常时走 token_counter 路径，异常时走回退路径。

---

## 验证清单

1. 启动 API，触发睡眠整理，日志中 prompt 标注 token 而非 KB
2. context-manager 子 Agent 看到的是 token 数，能准确累计
3. delete_messages 返回 freed_tokens
4. session-manager get_messages 返回 tokens 和 total_tokens
5. 强制压缩（实现后）传给子 Agent 的是 token 目标，能精确达到
