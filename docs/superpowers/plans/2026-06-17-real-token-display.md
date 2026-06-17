# 前端上下文使用率显示真实 Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前端圆环显示真实的 LLM API prompt_tokens 而非 litellm 估算值

**Architecture:** agent_loop 把真实 prompt_tokens 写到 handler 实例属性，/api/stats 优先读真实值，无真实值时回退到估算。前端零改动——chat_idle 后已自动调 loadStats()。

**Tech Stack:** Python, asyncio, FastAPI

---

## File Structure

| File | Responsibility |
|------|---------------|
| `agent/generic/agent_loop.py` | 写入 handler._last_prompt_tokens |
| `niu_api/compat.py` | /api/stats 优先读真实值 + clear_chat 重置 |
| `niu_api/chat.py` | clear_session 重置 |
| `niu_api/session.py` | delete_session 重置 |

---

### Task 1: agent_loop 写入真实 prompt_tokens 到 handler

**Files:**
- Modify: `agent/generic/agent_loop.py:216`
- Modify: `agent/generic/agent_loop.py:293-294`

- [ ] **Step 1: 初始化 handler._last_prompt_tokens**

将第216行：
```python
    last_prompt_tokens = 0
```
改为：
```python
    last_prompt_tokens = 0
    handler._last_prompt_tokens = 0
```

- [ ] **Step 2: 每轮提取 prompt_tokens 后同步到 handler**

将第293-294行：
```python
                last_prompt_tokens = int(_pt)
                logger.info(f"[Context] prompt_tokens={last_prompt_tokens}, context_window={context_window_tokens}")
```
改为：
```python
                last_prompt_tokens = int(_pt)
                handler._last_prompt_tokens = last_prompt_tokens
                logger.info(f"[Context] prompt_tokens={last_prompt_tokens}, context_window={context_window_tokens}")
```

- [ ] **Step 3: 压缩后重置 handler 值**

将第304行：
```python
                            last_prompt_tokens = 0
```
改为：
```python
                            last_prompt_tokens = 0
                            handler._last_prompt_tokens = 0
```

- [ ] **Step 4: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('syntax ok')"`

- [ ] **Step 5: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "feat(stats): write real prompt_tokens to handler._last_prompt_tokens for /api/stats"
```

---

### Task 2: /api/stats 优先使用真实 prompt_tokens

**Files:**
- Modify: `niu_api/compat.py:499-507`

- [ ] **Step 1: 修改 /api/stats 的 context_usage 计算**

将第499-507行：
```python
    # 计算上下文使用率
    context_usage = 0.0
    try:
        all_msgs = await store.get_messages()
        total_tokens = _estimate_total_tokens(all_msgs)
        context_window = _read_context_window_tokens()
        context_usage = total_tokens / context_window if context_window > 0 else 0.0
    except Exception:
        context_usage = 0.0
```
改为：
```python
    # 计算上下文使用率（优先用 LLM API 返回的真实 prompt_tokens）
    context_usage = 0.0
    try:
        context_window = _read_context_window_tokens()
        real_tokens = 0
        try:
            from niu_api.chat import get_or_create_runner
            runner = get_or_create_runner()
            real_tokens = getattr(getattr(runner, 'handler', None), '_last_prompt_tokens', 0) or 0
        except Exception:
            pass
        if real_tokens > 0:
            context_usage = real_tokens / context_window if context_window > 0 else 0.0
        else:
            all_msgs = await store.get_messages()
            total_tokens = _estimate_total_tokens(all_msgs)
            context_usage = total_tokens / context_window if context_window > 0 else 0.0
    except Exception:
        context_usage = 0.0
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax ok')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat(stats): /api/stats uses real prompt_tokens from LLM API, fallback to estimate"
```

---

### Task 3: 所有会话清除端点重置 handler._last_prompt_tokens

**Files:**
- Modify: `niu_api/compat.py:809`
- Modify: `niu_api/chat.py:588`
- Modify: `niu_api/session.py:85`

- [ ] **Step 1: 在 compat.py clear_chat 中重置 _last_prompt_tokens**

将第808-809行：
```python
            if runner.handler:
                runner.handler.reset_working_memory()
```
改为：
```python
            if runner.handler:
                runner.handler.reset_working_memory()
                runner.handler._last_prompt_tokens = 0
```

- [ ] **Step 2: 在 chat.py clear_session 中重置 _last_prompt_tokens**

将第586-589行：
```python
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    return {"status": "ok", "session_id": session_id}
```
改为：
```python
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"status": "ok", "session_id": session_id}
```

- [ ] **Step 3: 在 session.py delete_session 中重置 _last_prompt_tokens**

将第82-86行：
```python
async def delete_session(session_id: str) -> dict:
    """Delete a session (deprecated - clears all messages)"""
    store = await get_message_store()
    await store.clear_messages()
    return {"deleted": True}
```
改为：
```python
async def delete_session(session_id: str) -> dict:
    """Delete a session (deprecated - clears all messages)"""
    store = await get_message_store()
    await store.clear_messages()
    from niu_api.chat import get_or_create_runner
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"deleted": True}
```

- [ ] **Step 4: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); ast.parse(open('niu_api/chat.py').read()); ast.parse(open('niu_api/session.py').read()); print('syntax ok')"`

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py niu_api/chat.py niu_api/session.py
git commit -m "fix(stats): reset handler._last_prompt_tokens on all session clear endpoints"
```

---

## Verification

1. 启动程序，正常对话几轮
2. 观察 /api/stats 返回的 context_usage 是否与 LLM 日志中的 prompt_tokens 一致
3. 压缩触发后，context_usage 应下降——如果循环继续（有工具调用），下一轮 LLM 调用会更新 _last_prompt_tokens 为压缩后的真实值；如果循环退出（纯文本回复），_last_prompt_tokens 被重置为 0，回退到估算值，两者都会下降
4. 下一轮对话后，context_usage 恢复为真实值
5. 首次启动（无对话历史），context_usage 应为 0 或估算值（无真实值时回退）
