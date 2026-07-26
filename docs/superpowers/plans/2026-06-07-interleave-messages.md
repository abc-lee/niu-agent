# 见缝插针 — 用户消息永不阻塞 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-step. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户发送消息永远不阻塞，永远不等回复。Agent 循环每轮检查是否有用户补充消息，将其插入到 messages 中（当前任务之前作为参考），然后发给 LLM。

**Architecture:** 后端维护一个全局补充消息队列（`threading.Queue`）。所有入口（Electron chat_session、飞书 ChatQueue、调度器）收到消息时一律入队并立即返回。`agent_runner_loop` 每轮在 `next_prompt` 注入前，从队列取出所有补充消息，拼接到 `next_prompt` 前面——补充信息作为参考，当前任务（next_prompt）作为最后一条 user 消息，LLM 优先处理当前任务。前端发送消息永远不阻塞，状态灯（chat_busy/chat_idle）独立运作，仅做展示。

**Tech Stack:** Python threading.Queue, FastAPI

---

## 设计决策

### 1. 发送消息永远不阻塞

前端发消息就走，永远能发。`chat_session` 收到消息后：如果 `_chat_lock` 空闲，正常获取锁处理；如果锁被占用（Agent 在忙），将消息放入补充队列并立即返回。前端永远不等 Agent 处理完才返回 HTTP 响应。

### 2. 补充消息插入位置

在 `agent_runner_loop` 的 while 循环内，`next_prompt` 注入前（第 405-407 行之间）。补充消息拼接到 `next_prompt` 前面：

```
messages 末尾结构：
  ...tool_result
  user(补充消息1 + 补充消息2 + next_prompt)   ← 合并为一条 user 消息
```

LLM 看到的最后一条 user 消息包含：补充信息（参考）+ 当前任务。补充信息在前，当前任务在后，LLM 将补充信息作为上下文参考来执行当前任务。

### 3. 补充消息读取时机

补充消息在 `agent_runner_loop` 的每轮 while 循环中、`next_prompt` 注入前读取。具体时序：
- 用户在**工具执行期间**发补充消息 → 消息在当前轮的 `next_prompt` 注入点被读取
- 用户在**LLM 流式响应期间**发补充消息 → 消息在下一轮的 `next_prompt` 注入点被读取
- 用户在**Agent 已生成最终回复后**发补充消息 → Agent 循环已退出，消息残留在队列中，由 `drain_supplements()` 清理（Task 5），消息已持久化到 DB，下一轮对话通过历史加载重新进入上下文

### 4. 纯后端机制，前端零改动

前端不需要任何修改。状态灯（chat_busy/chat_idle）继续通过 SSE 独立运作。前端发消息走 `/api/chat/session`，后端在锁被占用时将消息入队并立即返回。Agent 的回复通过已有的 SSE → refreshFromDB() 推送，前端自然看到。

### 4. 统一入口

只有 Electron `chat_session` 走 `enqueue_supplement()` 路径：
- Electron `chat_session`：锁被占用 → 持久化 + SSE 推送 + `enqueue_supplement()` + 立即返回
- 飞书/调度器 ChatQueue：消息自然入队排队，由 `_process_with_merge` 的补充消息合并机制处理（已有逻辑，不改动）

`_supplement_queue` 和 ChatQueue 的合并机制是两套独立机制，作用域不同：
- `_supplement_queue`：在 Agent 运行期间，实时注入补充消息到当前轮
- ChatQueue 合并：在 `_process_single` 调用前，合并排队的补充消息到 merged_content

### 5. 子 Agent 不读取补充队列

子 Agent 调用 `agent_runner_loop` 时传递 `enable_supplement=False`，防止窃取主 Agent 的补充消息。

### 6. 持久化

补充的 user 消息在 `chat_session` 注入路径中持久化到 DB（调用 `store.add_message`），与正常路径一致。Agent 的回复通过已有的 persist 事件持久化。前端通过 refreshFromDB() 自然看到。

### 7. 竞态窗口

`_chat_lock.locked()` 检查与 `enqueue_supplement()` 之间存在竞态窗口：Agent 可能在检查后刚好释放锁。此时补充消息已入队但不会被当前 Agent 读取。这是可接受的行为——消息已持久化到 DB，残留消息由 `drain_supplements()` 在下一次对话开始时清理，消息不会丢失，只是延迟到下一轮对话。

### 8. runner.py 无需修改

`runner.py` 中的 `agent_runner_loop` 调用使用默认值 `enable_supplement=True`，无需修改。

### 9. 前端修改：状态由 SSE 驱动，不由 HTTP 响应驱动

前端修改的核心原则：**UI 状态（isProcessing、停止按钮、忙碌指示器）由后端的状态灯（chat_busy/chat_idle SSE 事件）驱动，不由 HTTP 请求的返回驱动。**

具体修改：
- 移除 `sendMessage` 中的 `isProcessing` 守卫——用户永远能发消息
- 移除 `busyTimeout` 120 秒超时强制恢复逻辑——不再需要前端自己判断超时
- 移除"请等待当前回复完成"和"回复超时，已自动恢复"提示
- 移除"系统正忙"提示
- `finally` 块不再重置 `isProcessing`、不再隐藏停止按钮——这些由 `chat_idle` SSE 事件处理

这些修改只涉及前端的 UI 显示逻辑，不涉及与后端的交互协议。

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `agent/runner.py` | 新增补充队列（`_supplement_queue`、`enqueue_supplement`、`drain_supplements`） | 修改 |
| `agent/generic/agent_loop.py` | 每轮 `next_prompt` 注入前读取补充消息并拼接到前面 | 修改 |
| `agent/subagent.py` | 传递 `enable_supplement=False` | 修改 |
| `niu_api/compat.py` | `chat_session` 锁被占用时入队并立即返回；`clear_chat` 清理残留补充消息 | 修改 |
| `ui/assistant/chat.html` | 移除 isProcessing 守卫、busyTimeout 超时、错误提示；UI 状态由 SSE 驱动 | 修改 |
| `tests/test_supplement_queue.py` | 测试补充队列核心机制 | 新建 |

**不修改的文件**：飞书通道（`feishu_channel.py`）、SSE（`chat.py`）、持久化（`persist_agent_reply`）、ChatQueue（`chat_queue.py`）、主进程（`main.js`）

---

### Task 1: 补充队列核心机制

**Files:**
- Modify: `agent/runner.py:40`（stop flag 机制之后）
- Create: `tests/test_supplement_queue.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the supplement queue mechanism (见缝插针)."""
import threading


def test_supplement_queue_initially_empty():
    """Supplement queue starts empty."""
    from agent.runner import drain_supplements
    assert drain_supplements() == []


def test_enqueue_supplement_adds_message():
    """enqueue_supplement adds a message to the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("用户补充的信息")
    result = drain_supplements()
    assert len(result) == 1
    assert result[0] == "用户补充的信息"


def test_drain_supplements_empties_queue():
    """drain_supplements removes all messages from the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("消息1")
    enqueue_supplement("消息2")
    assert len(drain_supplements()) == 2
    assert drain_supplements() == []


def test_supplement_queue_thread_safe():
    """enqueue_supplement and drain_supplements are thread-safe."""
    from agent.runner import enqueue_supplement, drain_supplements
    errors = []

    def enqueue_many():
        try:
            for i in range(100):
                enqueue_supplement(f"msg-{i}")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=enqueue_many)
    t2 = threading.Thread(target=enqueue_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    all_msgs = []
    while True:
        batch = drain_supplements()
        if not batch:
            break
        all_msgs.extend(batch)

    assert len(all_msgs) == 200
    assert len(errors) == 0


def test_drain_supplements_no_race_condition():
    """drain_supplements uses get_nowait() without empty() check — no race."""
    from agent.runner import enqueue_supplement, drain_supplements
    for i in range(50):
        enqueue_supplement(f"race-{i}")
    result = drain_supplements()
    assert len(result) == 50
    assert drain_supplements() == []


def test_drain_supplement_empty_returns_none():
    """No pending messages returns None."""
    from agent.runner import drain_supplement
    assert drain_supplement() is None


def test_drain_supplement_single_returns_raw():
    """Single pending message returned as-is."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("只有一条补充")
    assert drain_supplement() == "只有一条补充"


def test_drain_supplement_multiple_joins_with_prefix():
    """Multiple pending messages joined with [补充] prefix."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("第一个补充")
    enqueue_supplement("第二个补充")
    result = drain_supplement()
    assert "[补充] 第一个补充" in result
    assert "[补充] 第二个补充" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`
Expected: FAIL — `ImportError: cannot import name 'enqueue_supplement' from 'agent.runner'`

- [ ] **Step 3: Write minimal implementation**

在 `agent/runner.py` 顶部 import 区域添加：
```python
import queue as _queue_module
```

在 stop flag 机制之后（第 40 行之后）添加：

```python
# --- Supplement queue (见缝插针) ---
_supplement_queue = _queue_module.Queue()  # 无限长度，永不阻塞


def enqueue_supplement(content: str):
    """将用户在 Agent 运行期间发送的补充消息放入队列。"""
    _supplement_queue.put(content)


def drain_supplements() -> list[str]:
    """取出所有补充消息（非阻塞，无竞态）。"""
    msgs = []
    while True:
        try:
            msgs.append(_supplement_queue.get_nowait())
        except _queue_module.Empty:
            break
    return msgs


def drain_supplement() -> str | None:
    """取出所有补充消息，格式化为单条字符串。

    - 无消息返回 None
    - 单条返回原文
    - 多条合并为 "[补充] 消息1\\n[补充] 消息2"
    """
    msgs = drain_supplements()
    if not msgs:
        return None
    if len(msgs) == 1:
        return msgs[0]
    return "\n".join(f"[补充] {m}" for m in msgs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`
Expected: PASS（8 tests）

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py tests/test_supplement_queue.py
git commit -m "feat: add supplement queue mechanism (enqueue_supplement/drain_supplement)"
```

---

### Task 2: agent_runner_loop 读取补充消息 + 子 Agent 禁用

**Files:**
- Modify: `agent/generic/agent_loop.py:133`（import）、`:` 参数签名、`:405-407`（next_prompt 注入前）
- Modify: `agent/subagent.py:90-102`（`_run_agent_loop` 中 `enable_supplement=False`）
- Modify: `tests/test_supplement_queue.py`（追加测试）

- [ ] **Step 1: Write the failing test**

在 `tests/test_supplement_queue.py` 末尾追加：

```python


def test_supplement_inserted_before_next_prompt():
    """Supplement messages appear before next_prompt in messages sent to LLM."""
    from agent.runner import enqueue_supplement, drain_supplements
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    drain_supplements()

    turn = 0
    captured_messages_list = []

    def mock_chat(**kwargs):
        nonlocal turn
        turn += 1
        captured_messages_list.append(list(kwargs.get("messages", [])))

        resp = MagicMock()
        if turn == 1:
            resp.tool_calls = [MagicMock(
                id="tc1",
                function=MagicMock(name="test_tool", arguments="{}")
            )]
            resp.content = ""
            # 用户在工具执行期间发送补充消息
            enqueue_supplement("中途补充的信息")
        else:
            resp.tool_calls = None
            resp.content = "好的，已处理"
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        yield resp
        return resp

    client = MagicMock()
    client.chat = mock_chat

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler.next_prompt_patcher = lambda np, _ctx, tn: np

    def mock_dispatch(tool_name, args, resp, index=0):
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome

    handler.dispatch = mock_dispatch

    list(agent_runner_loop(
        client=client, system_prompt="test", user_input="hello",
        handler=handler, tools_schema=[], max_turns=5,
    ))

    # 第二轮的 messages 中应该包含补充消息
    assert len(captured_messages_list) >= 2
    second_call_messages = captured_messages_list[1]
    user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
    # 补充消息应该出现在最后一条 user 消息中
    last_user = user_msgs[-1] if user_msgs else None
    assert last_user is not None, f"No user messages found in: {user_msgs}"
    content = last_user["content"]
    assert "中途补充的信息" in content, f"Supplement not found in last user message: {content}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py::test_supplement_inserted_before_next_prompt -v`
Expected: FAIL — supplement message not found

- [ ] **Step 3: Write minimal implementation**

修改 `agent/generic/agent_loop.py`：

**1. 修改 import**（第 133 行附近，搜索 `from agent.runner import is_stop_requested, clear_stop`）：

```python
from agent.runner import is_stop_requested, clear_stop, drain_supplement
```

**2. 在函数签名中添加 `enable_supplement=True` 参数**（在参数列表末尾，`history=None` 之后）：

搜索 `history=None,  # Optional: list of`，在其后添加参数：
```python
    enable_supplement=True,  # False for sub-agents to prevent stealing main agent's supplements
```

**3. 在 `next_prompt` 注入前读取补充消息**（第 405-407 行之间）：

当前代码：
```python
        # 警告注入：只在有工具调用时才有意义（LLM 还在工作，可能需要调整策略）
        if next_prompt and next_prompt.strip():
            messages.append({"role": "user", "content": next_prompt})
```

修改为：
```python
        # --- 见缝插针：读取用户在 Agent 运行期间发送的补充消息 ---
        supplement = drain_supplement() if enable_supplement else None

        # 警告注入：只在有工具调用时才有意义（LLM 还在工作，可能需要调整策略）
        # 补充消息插在 next_prompt 前面，当前任务作为最后一条，LLM 优先处理
        if supplement or (next_prompt and next_prompt.strip()):
            combined = ""
            if supplement:
                combined = supplement
            if next_prompt and next_prompt.strip():
                combined = combined + "\n" + next_prompt if combined else next_prompt
            messages.append({"role": "user", "content": combined})
            if supplement:
                logger.info(f"[AgentLoop] Supplement inserted before next_prompt: {supplement[:80]}...")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`
Expected: PASS（9 tests）

- [ ] **Step 5: 在 `_run_agent_loop` 中传递 `enable_supplement=False`**

先 Read 文件确认 `agent_runner_loop` 调用的当前代码（约第 90-102 行）。

当前代码：
```python
    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
        history=history,
    )
```

修改为：
```python
    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
        history=history,
        enable_supplement=False,
    )
```

- [ ] **Step 6: 验证语法**

Run: `cd <repo_root> && python -c "from agent.generic.agent_loop import agent_runner_loop; print('agent_loop OK')" && python -c "from agent.subagent import call_subagent; print('subagent OK')"`

- [ ] **Step 7: Run all tests**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`
Expected: PASS（9 tests）

- [ ] **Step 8: Commit**

```bash
git add agent/generic/agent_loop.py agent/subagent.py tests/test_supplement_queue.py
git commit -m "feat: agent_runner_loop reads supplement messages before next_prompt; sub-agent disables supplement"
```

---

### Task 3: chat_session 锁被占用时入队并立即返回

**Files:**
- Modify: `niu_api/compat.py:519-526`（`_chat_lock` 获取逻辑）

- [ ] **Step 1: 在 chat_session 中添加补充快速路径**

先 Read 文件确认 `chat_session` 函数的当前结构。

当前代码（约第 519-526 行）：

```python
    # 排队等待锁：最多等 60 秒，而非直接拒绝
    # 之前 timeout=0.01 导致文件拖入等请求被直接丢弃
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
    except TimeoutError:
        logger.warning("[chat_session] _chat_lock 60s timeout, request rejected")
        return ChatResponse(reply="系统正忙，请稍后再试", session_id="default")
```

修改为：
```python
    # --- 见缝插针：Agent 运行期间，将补充消息入队并立即返回 ---
    if _chat_lock.locked():
        from agent.runner import enqueue_supplement

        # 持久化 user 消息（与正常路径一致）
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=request.message)

        # SSE 推送 user 消息给前端
        from niu_api.chat import notify_new_message
        await notify_new_message(user_msg_id, "user", request.message, source="electron")

        # 入队补充消息，立即返回
        enqueue_supplement(request.message)
        logger.info(f"[chat_session] Supplement enqueued: {request.message[:50]}...")
        return ChatResponse(reply="已收到", session_id="default", message_id=user_msg_id)

    # 锁未被占用：正常获取锁并处理
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
    except TimeoutError:
        logger.warning("[chat_session] _chat_lock 60s timeout, request rejected")
        return ChatResponse(reply="系统正忙，请稍后再试", session_id="default")
```

同时在正常路径中（获取锁后、调用 `runner.chat()` 之前），清理残留的补充消息：

在 `clear_stop()` 之后（搜索 `from agent.runner import clear_stop` 在 `chat_session` 中的位置），添加：
```python
        # 清理残留的补充消息（这些消息已被持久化，会通过历史加载重新进入上下文）
        from agent.runner import drain_supplements
        drain_supplements()
```

- [ ] **Step 2: 验证语法**

Run: `cd <repo_root> && python -c "import niu_api.compat; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: chat_session enqueues supplement when lock is held and returns immediately"
```

---

### Task 4: 前端 UI 状态由 SSE 驱动

**Files:**
- Modify: `ui/assistant/chat.html:784-834`

核心原则：UI 状态（isProcessing、停止按钮、忙碌指示器）由后端的状态灯（chat_busy/chat_idle SSE 事件）驱动，不由 HTTP 请求的返回驱动。

- [ ] **Step 1: 修改 sendMessage 函数**

先 Read 文件确认 `sendMessage` 函数的完整代码（约第 745-835 行）。

**修改 1：移除 isProcessing 守卫**

当前代码（第 784 行）：
```javascript
      if (isProcessing) { addSystemMessage('请等待当前回复完成'); return; }
```

删除这一行。

**修改 2：移除 busyTimeout 超时强制恢复逻辑**

当前代码（第 806-815 行）：
```javascript
      if (busyTimeout) clearTimeout(busyTimeout);
      busyTimeout = setTimeout(() => {
        isProcessing = false;
        sendBtn.disabled = false;
        stopBtn.style.display = 'none';
        hideTyping();
        window.electronAPI.notifyBusy(false, 'chat');
        addSystemMessage('回复超时，已自动恢复');
        busyTimeout = null;
      }, 120000);
```

删除整个 `if (busyTimeout)` 和 `busyTimeout = setTimeout(...)` 块。

**修改 3：简化 finally 块**

当前代码（第 825-834 行）：
```javascript
      } finally {
        if (busyTimeout) { clearTimeout(busyTimeout); busyTimeout = null; }
        hideTyping();
        window.electronAPI.notifyBusy(false, 'chat');
        sendBtn.disabled = false;
        stopBtn.style.display = 'none';
        isProcessing = false;
        loadStats();
        userInput.focus();
      }
```

修改为（只保留不涉及状态重置的逻辑，状态由 SSE chat_idle 事件处理）：
```javascript
      } finally {
        sendBtn.disabled = false;
        loadStats();
        userInput.focus();
      }
```

**修改 4：移除"系统正忙"提示**

当前代码（第 822-824 行）：
```javascript
        if (result && result.reply && result.reply.includes('系统正忙')) {
          addMessage('system', '系统正忙，请稍后再试');
        }
```

删除这个 if 块。

**修改 5：清理 isProcessing 的无效赋值**

移除第 784-785 行（isProcessing 守卫 + isProcessing = true）后，以下赋值变成无效：
- 第 792 行：`isProcessing = false;`（在 `/new` 指令路径中）→ 删除
- 第 800 行：`if (!userMsg) { isProcessing = false; return; }` → 改为 `if (!userMsg) { return; }`

**修改 6：对 handleDroppedImage 和 handleDroppedFile 做同样的修改**

这两个函数与 sendMessage 有相同的 isProcessing 守卫和 busyTimeout 模式。必须做相同的修改：

`handleDroppedImage`（搜索 `function handleDroppedImage`）：
1. 移除 `if (isProcessing) { addSystemMessage('请等待当前回复完成'); return; }`
2. 移除 `isProcessing = true;`
3. 移除 busyTimeout 块（与 sendMessage 相同的模式）
4. 简化 finally 块（只保留 `sendBtn.disabled = false;` 等非状态重置逻辑）
5. 移除"系统正忙"提示
6. 清理无效的 `isProcessing = false;` 赋值

`handleDroppedFile`（搜索 `function handleDroppedFile`）：
1-6：与 handleDroppedImage 完全相同的修改

**修改 7：清理 chat_idle SSE 处理程序中的 busyTimeout 引用**

搜索 `chat_idle` SSE 处理代码（在 onNewMessage 回调中），只移除 `if (busyTimeout) { clearTimeout(busyTimeout); busyTimeout = null; }`。**保留 `isProcessing = false;`**——这是 SSE 驱动状态重置的核心，Agent 完成后通过 chat_idle 事件重置 isProcessing，允许用户发送新消息。保留其他状态重置（`hideTyping()`、`sendBtn.disabled = false`、`stopBtn.style.display = 'none'`、`notifyBusy(false, 'chat')`）。

**修改 8：清理 handleDroppedImage 和 handleDroppedFile 中 filePath 检查失败时的 `isProcessing = false`**

handleDroppedImage 中搜索 `if (!filePath)` 块，删除其中的 `isProcessing = false;`。
handleDroppedFile 中搜索 `if (!filePath)` 块，删除其中的 `isProcessing = false;`。

**修改 9：更新 /new 指令的注释**

当前注释（第 788 行）：`// /new 指令 - 清空聊天记录（只能在 isProcessing=false 时使用）`
修改为：`// /new 指令 - 清空聊天记录`
        if (result && result.reply && result.reply.includes('系统正忙')) {
          addMessage('system', '系统正忙，请稍后再试');
        }
```

删除这个 if 块。

- [ ] **Step 2: 确认 SSE chat_busy/chat_idle 事件处理正确**

搜索 `onNewMessage` 或 `chat_idle` 或 `chat_busy` 的处理代码，确认：
- `chat_idle` 事件会重置 `isProcessing=false`、`hideTyping()`、`stopBtn.style.display='none'`
- `chat_busy` 事件会设置 `stopBtn.style.display='flex'`

这些已由 /stop 功能实现，不需要额外修改。

- [ ] **Step 3: 测试**

启动应用，测试以下场景：
1. 发送普通消息 → 正常工作
2. Agent 运行时发送补充消息 → 不被阻止，消息发送成功，前端显示用户消息
3. Agent 完成后 → chat_idle SSE 事件重置 UI 状态
4. /stop → 正常工作

- [ ] **Step 4: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "fix: remove isProcessing guard and busyTimeout, UI state driven by SSE chat_busy/chat_idle"
```

---

### Task 5: 残留补充消息清理

**Files:**
- Modify: `niu_api/compat.py`（`clear_chat` 中 `clear_stop()` 之后）

- [ ] **Step 1: 在 `_process_single` 中不清理补充消息**

**重要**：`_process_single` 是 ChatQueue 的处理路径（飞书/调度器），不应该触碰 `_supplement_queue`（只有 Electron chat_session 使用）。补充消息的清理只在 `chat_session` 和 `clear_chat` 中进行。

因此，Task 4 的 `_process_single` 部分被移除。`drain_supplements()` 只在 `chat_session` 正常路径和 `clear_chat` 中调用。

- [ ] **Step 2: 在 `clear_chat` 的 try 块内清理残留补充消息**

先 Read 文件确认 `clear_chat` 的当前代码。

`clear_chat` 中有两个 `clear_stop()`：一个在 `except TimeoutError:` 块内，一个在 `try` 块内（获取锁之后，约第 733 行）。`drain_supplements()` 应放在 **try 块内** 的 `clear_stop()` 之后（约第 733 行），不是 `except` 块内的那个。

```python
        clear_stop()  # 防御性清除：确保清空时标志干净
        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "import niu_api.compat; print('compat OK')"`

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: clean residual supplement messages in clear_chat"
```

---

### Task 6: 集成测试和文档更新

**Files:**
- Modify: `tests/test_supplement_queue.py`（追加集成测试）
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 编写集成测试**

在 `tests/test_supplement_queue.py` 末尾追加：

```python


def test_supplement_order_before_next_prompt():
    """Supplement message appears BEFORE next_prompt in the combined user message."""
    from agent.runner import enqueue_supplement, drain_supplements
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    drain_supplements()

    turn = 0
    captured_messages_list = []

    def mock_chat(**kwargs):
        nonlocal turn
        turn += 1
        captured_messages_list.append(list(kwargs.get("messages", [])))

        resp = MagicMock()
        if turn == 1:
            resp.tool_calls = [MagicMock(
                id="tc1",
                function=MagicMock(name="test_tool", arguments="{}")
            )]
            resp.content = ""
            enqueue_supplement("用户补充")
        else:
            resp.tool_calls = None
            resp.content = "完成"
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        yield resp
        return resp

    client = MagicMock()
    client.chat = mock_chat

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    # next_prompt_patcher 返回一个非空的 next_prompt
    handler.next_prompt_patcher = lambda np, _ctx, tn: "继续执行" if np == "" else np

    def mock_dispatch(tool_name, args, resp, index=0):
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome

    handler.dispatch = mock_dispatch

    list(agent_runner_loop(
        client=client, system_prompt="test", user_input="hello",
        handler=handler, tools_schema=[], max_turns=5,
    ))

    # 验证：最后一条 user 消息中，补充信息在 next_prompt 前面
    assert len(captured_messages_list) >= 2
    second_call_messages = captured_messages_list[1]
    user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
    last_user = user_msgs[-1] if user_msgs else None
    assert last_user is not None
    content = last_user["content"]
    supplement_pos = content.find("用户补充")
    next_prompt_pos = content.find("继续执行")
    assert supplement_pos >= 0, f"Supplement not found in: {content}"
    assert next_prompt_pos >= 0, f"Next prompt not found in: {content}"
    assert supplement_pos < next_prompt_pos, f"Supplement should be before next_prompt: {content}"


def test_supplement_without_next_prompt():
    """Supplement message is injected even when next_prompt is empty."""
    from agent.runner import enqueue_supplement, drain_supplements
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    drain_supplements()

    turn = 0
    captured_messages_list = []

    def mock_chat(**kwargs):
        nonlocal turn
        turn += 1
        captured_messages_list.append(list(kwargs.get("messages", [])))

        resp = MagicMock()
        if turn == 1:
            resp.tool_calls = [MagicMock(
                id="tc1",
                function=MagicMock(name="test_tool", arguments="{}")
            )]
            resp.content = ""
            enqueue_supplement("只有补充没有next_prompt")
        else:
            resp.tool_calls = None
            resp.content = "完成"
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        yield resp
        return resp

    client = MagicMock()
    client.chat = mock_chat

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler.next_prompt_patcher = lambda np, _ctx, tn: np  # 返回空 next_prompt

    def mock_dispatch(tool_name, args, resp, index=0):
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome

    handler.dispatch = mock_dispatch

    list(agent_runner_loop(
        client=client, system_prompt="test", user_input="hello",
        handler=handler, tools_schema=[], max_turns=5,
    ))

    # 补充消息即使没有 next_prompt 也应该被注入
    assert len(captured_messages_list) >= 2
    second_call_messages = captured_messages_list[1]
    user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
    last_user = user_msgs[-1] if user_msgs else None
    assert last_user is not None
    assert "只有补充没有next_prompt" in last_user["content"]


def test_subagent_does_not_drain_supplement():
    """Sub-agent with enable_supplement=False does not read supplement queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    drain_supplements()
    enqueue_supplement("主Agent的补充消息")

    captured_messages = []

    def mock_chat(**kwargs):
        captured_messages.append(list(kwargs.get("messages", [])))
        resp = MagicMock()
        resp.tool_calls = None
        resp.content = "子Agent回复"
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        yield resp
        return resp

    client = MagicMock()
    client.chat = mock_chat

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler.next_prompt_patcher = lambda np, _ctx, tn: np

    list(agent_runner_loop(
        client=client, system_prompt="test", user_input="sub task",
        handler=handler, tools_schema=[], max_turns=5,
        enable_supplement=False,
    ))

    # 子 Agent 不应该读取补充消息
    user_msgs = [m for m in captured_messages[0] if m.get("role") == "user"]
    supplement_found = any("主Agent的补充消息" in m.get("content", "") for m in user_msgs)
    assert not supplement_found, "Sub-agent should not see main agent's supplement"

    # 补充消息仍在队列中
    remaining = drain_supplements()
    assert len(remaining) == 1
    assert remaining[0] == "主Agent的补充消息"
```

- [ ] **Step 2: 运行全部测试**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`
Expected: PASS（12 tests）

- [ ] **Step 3: 更新 SYSTEM_MANUAL.md**

在功能列表表格中 `/clear 指令` 行之后添加：

```markdown
| 见缝插针 | Agent 运行期间发送的补充消息自动插入到当前对话上下文（补充在前，当前任务在后） |
```

在"指令机制"说明之后添加：

```markdown
**见缝插针机制**：
- Agent 运行期间，用户发送的补充消息通过 `enqueue_supplement()` 入队
- `agent_runner_loop` 每轮在 `next_prompt` 注入前读取队列（`drain_supplement()`），将补充消息拼接到 `next_prompt` 前面
- 补充信息作为参考在前，当前任务作为最后内容在后，LLM 优先处理当前任务
- 所有入口（Electron chat_session）统一使用 `enqueue_supplement()`
- 前端零改动，发送消息永远不阻塞
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_supplement_queue.py docs/SYSTEM_MANUAL.md
git commit -m "feat: add integration test and docs for supplement queue (见缝插针)"
```

---

## 自查清单

### 1. Spec 覆盖度

| 需求 | 对应 Task |
|------|-----------|
| 发送消息永远不阻塞 | Task 4（移除 isProcessing 守卫）+ Task 3（locked 时立即返回） |
| 每轮组装提示词时读取队列 | Task 2 |
| 补充消息插在当前任务前面 | Task 2（拼接到 next_prompt 前面） |
| 前端 UI 状态由 SSE 驱动 | Task 4（sendMessage + handleDroppedImage + handleDroppedFile + chat_idle SSE） |
| 前端永远允许发送 | Task 4（移除所有 isProcessing 守卫） |
| 前端不再有超时强制恢复 | Task 4（移除所有 busyTimeout） |
| 永不阻塞 | Task 1（Queue(maxsize=0)）+ Task 3（locked 时立即返回） |
| 不动 ChatQueue | 全部 Task 均不修改 ChatQueue |
| 不动飞书通道 | 全部 Task 均不修改飞书 |
| 子 Agent 不窃取补充消息 | Task 2（enable_supplement=False，与补充读取同一步提交） |
| 残留消息清理 | Task 5（只在 chat_session 和 clear_chat 中，不在 _process_single 中） |
| 竞态窗口可接受 | 消息已持久化，残留由 drain_supplements 清理 |
| TDD 开发模式 | 每个 Task 先写测试再实现 |

### 2. Placeholder 扫描

无 TBD、TODO、"add validation" 等占位符。所有步骤包含完整代码。

### 3. 类型一致性

- `enqueue_supplement(content: str)` — 参数 str
- `drain_supplements() -> list[str]` — 返回字符串列表
- `drain_supplement() -> str | None` — 返回可选字符串
- `agent_loop` 中 `drain_supplement()` 返回 `str | None`，`if supplement:` 正确处理 None 和空字符串
- `enable_supplement: bool = True` — agent_runner_loop 参数，subagent 传 False
