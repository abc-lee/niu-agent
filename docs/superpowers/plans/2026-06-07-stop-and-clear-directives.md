# /stop 和 /clear 指令实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `/stop` 指令（停止当前 Agent 工作）和 `/clear` 指令（先停止再清空对话），支持 Electron 和 IM 通用。

**Architecture:** 使用 `threading.Event` 作为全局停止标志。`/stop` 指令通过正常的消息通道发送（不是单独 API），在 `chat_session` 和 `ChatQueue` 入口拦截并设置停止标志。Agent 主循环、handler dispatch、子Agent 循环在关键点检查停止标志并退出。停止标志在每次新对话开始时自动清除，定时任务和 auto-tidy 不受影响。

**Tech Stack:** Python threading.Event, FastAPI, Electron IPC

---

## 设计决策

### 1. `/stop` 是指令而非 API

`/stop` 作为文本指令通过正常消息通道发送，这样所有 IM 接入（Electron、飞书等）都能使用。前端的"停止"按钮只是自动发送 `/stop` 文本。

### 2. 停止标志生命周期

**核心原则：Agent 循环退出时自动清除标志，不留残留。**

```
用户发送消息 → Agent 循环运行 → /stop 设置标志 → 循环检测到标志 → 退出循环 + clear_stop()
                                                                    ↑
                                                          标志已清除，后续定时任务不受影响
```

**清除时机**（按优先级）：
1. **Agent 循环退出时**（无论 STOPPED/EXITED/CONTEXT_OVERFLOW/MAX_TURNS_EXCEEDED/CURRENT_TASK_DONE）— 最关键，防止残留
2. `chat_session()` 用户发新消息时 — 防御性清除，确保干净开始
3. `ChatQueue._process_single()` 用户消息时 — 同上

**不需要特殊保护定时任务**：因为标志在 Agent 循环退出时就已清除，定时任务执行时标志一定是干净的。如果定时任务正在运行时用户发了 `/stop`，那标志确实会被置位——但这是合理的：用户明确要求停止，定时任务的 Agent 循环也会在下一轮检查到标志后退出，并自动清除标志。

### 4. 子Agent 传播

子Agent 运行在同一个 Python 进程中，共享全局 `_stop_requested`。主Agent 被 stop 后，正在执行的子Agent 也会在下一轮循环中检测到标志并退出。子Agent 退出后，主Agent 的循环也会在下一轮检测到标志并退出，最终所有循环退出时都执行 `clear_stop()`。

### 5. `/clear` 语义

`/clear` = `/stop` + 清空对话。先确保 Agent 停止，再清空记录。当前 `/new` 已实现清空，`/clear` 复用同一逻辑。

### 6. 标志设计

```python
# agent/runner.py
_stop_requested = threading.Event()

def request_stop():
    """Set the stop flag — Agent loops will check and exit."""
    _stop_requested.set()

def clear_stop():
    """Clear the stop flag — called when Agent loop exits and at conversation start."""
    _stop_requested.clear()

def is_stop_requested() -> bool:
    """Check if stop has been requested."""
    return _stop_requested.is_set()
```

### 7. /stop 指令的响应流程

```
前端发送 /stop → chat_session() 或 ChatQueue.enqueue_and_wait() 拦截
    → request_stop() → 立即返回 "已停止"
    → Agent 循环检测到标志 → clear_stop() + yield chat_idle
    → SSE 推送 chat_idle → 前端重置 isProcessing
```

前端收到 `/stop` 的 HTTP 响应后，Agent 可能仍在运行。前端通过 SSE `chat_idle` 事件得知 Agent 已真正停止。

### 8. `clear_stop()` 的幂等性

`threading.Event.clear()` 是幂等的——多次调用不会出错。Agent 循环退出时调用 `clear_stop()`，`chat_session` 入口也调用 `clear_stop()`，两者不会冲突。

---

## File Structure

| 文件 | 职责 | 修改类型 |
|------|------|----------|
| `agent/runner.py` | 停止标志定义和管理 | 修改 |
| `agent/generic/agent_loop.py` | 主循环检查停止标志 | 修改 |
| `agent/handler.py` | dispatch 前检查停止标志 | 修改 |
| `agent/subagent.py` | 传递停止标志给子Agent | 修改 |
| `niu_api/compat.py` | `/stop` 指令拦截 + `chat_session` 清除标志 | 修改 |
| `niu_api/chat_queue.py` | `ChatQueue` 清除标志 + 跳过系统任务 | 修改 |
| `ui/assistant/chat.html` | 前端 `/stop` 指令处理 + 停止按钮 | 修改 |

---

### Task 1: 停止标志核心机制

**Files:**
- Modify: `agent/runner.py`（顶部区域，import 之后）
- Create: `tests/test_stop_flag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stop_flag.py
"""Tests for stop flag mechanism."""
import threading
import time
from agent.runner import request_stop, clear_stop, is_stop_requested


def test_initial_state_is_not_stopped():
    """Stop flag should be clear initially."""
    clear_stop()
    assert is_stop_requested() is False


def test_request_stop_sets_flag():
    """request_stop() should set the flag."""
    clear_stop()
    request_stop()
    assert is_stop_requested() is True


def test_clear_stop_resets_flag():
    """clear_stop() should reset the flag."""
    request_stop()
    clear_stop()
    assert is_stop_requested() is False


def test_stop_flag_is_thread_safe():
    """Stop flag should be thread-safe."""
    clear_stop()
    results = []

    def set_flag():
        time.sleep(0.01)
        request_stop()
        results.append("set")

    t = threading.Thread(target=set_flag)
    t.start()
    # Spin until flag is set
    while not is_stop_requested():
        time.sleep(0.001)
    results.append("seen")
    t.join()
    assert results == ["set", "seen"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_stop_flag.py -v`
Expected: FAIL — `ImportError: cannot import name 'request_stop' from 'agent.runner'`

- [ ] **Step 3: Write minimal implementation**

在 `agent/runner.py` 的 import 区域之后、类定义之前添加：

```python
# --- Stop flag mechanism ---
_stop_requested = threading.Event()


def request_stop():
    """Set the stop flag — Agent loops will check and exit."""
    _stop_requested.set()


def clear_stop():
    """Clear the stop flag — called at the start of each user conversation."""
    _stop_requested.clear()


def is_stop_requested() -> bool:
    """Check if stop has been requested."""
    return _stop_requested.is_set()
```

确保 `threading` 已在文件顶部 import（搜索 `import threading`，如果没有则添加）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_stop_flag.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py tests/test_stop_flag.py
git commit -m "feat: add stop flag mechanism (request_stop/clear_stop/is_stop_requested)"
```

---

### Task 2: Agent 主循环检查停止标志

**Files:**
- Modify: `agent/generic/agent_loop.py:119-414`（agent_runner_loop 函数）
- Create: `tests/test_stop_flag.py`（追加测试）

- [ ] **Step 1: Write the failing test**

在 `tests/test_stop_flag.py` 末尾追加：

```python
def test_stop_flag_checked_in_loop():
    """agent_runner_loop should exit when stop flag is set."""
    from agent.runner import request_stop, clear_stop
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    clear_stop()

    # Create a mock client that returns a response with no tool calls
    client = MagicMock()
    response = MagicMock()
    response.tool_calls = None
    response.content = "Hello"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)

    # Make the first call return normally, then set stop flag before second call
    call_count = 0
    original_chat = client.chat

    def chat_with_stop_check(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: return a tool call to keep the loop going
            resp = MagicMock()
            resp.tool_calls = [{"function": {"name": "test_tool", "arguments": "{}"}}]
            resp.content = ""
            resp.usage = MagicMock(input_tokens=10, output_tokens=5)
            return iter([resp])
        else:
            # Should not reach here if stop flag works
            return iter([response])

    client.chat = chat_with_stop_check

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []

    # Mock dispatch to set stop flag
    def mock_dispatch(tool_name, args, resp, index=0):
        request_stop()  # Set stop flag during tool execution
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome  # 生成器必须 return outcome，否则 StopIteration.value 为 None

    handler.dispatch = mock_dispatch

    # Run the loop
    events = list(agent_runner_loop(
        client=client,
        system_prompt="test",
        user_input="hello",
        handler=handler,
        tools_schema=[],
        max_turns=5,
    ))

    # Check that we got chat_idle event (loop exited)
    # Note: StreamEvent fields are `type` and `content`, not `event_type` and `data`
    idle_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "system" and e.content == "chat_idle"]
    assert len(idle_events) >= 1, "Loop should have exited with chat_idle"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_stop_flag.py::test_stop_flag_checked_in_loop -v`
Expected: FAIL or TIMEOUT — loop doesn't check stop flag, may run all turns

- [ ] **Step 3: Write minimal implementation**

在 `agent/generic/agent_loop.py` 的 `agent_runner_loop()` 函数中添加停止检查：

**首先**：在函数开头（第 119 行 `def agent_runner_loop(...)` 之后、`messages = [...]` 之前）添加一次性 import：

```python
    from agent.runner import is_stop_requested, clear_stop
```

**检查点 1**：主循环 while 条件之后，上下文溢出检查之前（第 171 行 `turn += 1` 之后）：

```python
        # --- Stop flag check ---
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested, exiting loop")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}
```

**检查点 2**：工具调用分发前，`for ii, tc in enumerate(tool_calls):` 循环内部（第 265 行之后），dispatch 调用之前：

```python
            # --- Stop flag check before tool dispatch ---
            if is_stop_requested():
                logger.info("[AgentLoop] Stop requested, skipping remaining tools")
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {"result": "STOPPED", "messages": messages}
```

**检查点 3**：所有现有退出点的 `yield StreamEvent("system", "chat_idle")` 之前，都添加 `clear_stop()`。精确位置：

- 上下文溢出退出（第 181 行 `yield StreamEvent("system", "chat_idle")` 之前）
- should_exit 退出（第 310 行 `yield StreamEvent("system", "chat_idle")` 之前）
- CURRENT_TASK_DONE 退出（第 361 行和第 374 行，两处 `yield StreamEvent("system", "chat_idle")` 之前）
- MAX_TURNS_EXCEEDED 退出（第 413 行 `yield StreamEvent("system", "chat_idle")` 之前）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_stop_flag.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_stop_flag.py
git commit -m "feat: agent_loop checks stop flag at loop start and before tool dispatch"
```

---

### Task 3: 后端拦截 /stop 指令 + chat_session 清除标志

**Files:**
- Modify: `niu_api/compat.py:498-585`（chat_session 函数）

- [ ] **Step 1: 在 chat_session 中拦截 /stop 指令**

在 `chat_session()` 函数中，LLM 配置检查之后、获取 `_chat_lock` 之前（第 510 行之后、第 512 行之前），添加 `/stop` 指令拦截：

```python
    # --- /stop directive: stop current Agent work ---
    if request.message.strip() == "/stop":
        from agent.runner import request_stop
        request_stop()
        logger.info("[ChatSession] /stop requested")
        return ChatResponse(reply="已停止")
```

**注意**：
1. 使用 `ChatResponse` 而非 `JSONResponse`，与函数签名一致
2. 使用 `request.message`（函数参数名），不是 `user_input`
3. 放在 LLM 配置检查之后——LLM 未配置时无 Agent 运行，`/stop` 不可达也无需可达
4. `ChatResponse` 的 `session_id` 字段可选，不需要传

同时在 `chat_session()` 中，`sync_chat()` 函数定义之前（第 548 行之前），添加防御性 `clear_stop()`：

```python
    # 每次用户发起新对话时，清除停止标志
    from agent.runner import clear_stop
    clear_stop()
```

- [ ] **Step 2: 在 /api/chat/clear 中先执行 stop 并增加超时**

修改 `clear_chat()` 函数，在获取锁之前先请求停止，并增加锁等待超时（5秒不够，Agent 退出需要时间）：

```python
@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """Clear all messages (for /new and /clear commands)"""
    # 先请求停止当前 Agent 工作
    from agent.runner import request_stop
    request_stop()

    # 获取锁，防止与正在进行的 chat 冲突
    # 超时增加到 30 秒，等待 Agent 循环检测 stop 标志并退出
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=30.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 30s timeout, clear rejected")
        return {"success": False, "error": "系统正忙，请稍后再试"}
```

- [ ] **Step 3: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import niu_api.compat; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: intercept /stop directive in chat_session, clear_stop on new chat, stop before clear"
```

---

### Task 4: ChatQueue /stop 拦截和标志清除

**Files:**
- Modify: `niu_api/chat_queue.py`（`enqueue_and_wait` 和 `_process_single` 方法）

- [ ] **Step 1: 在 enqueue_and_wait() 中优先处理 /stop 指令**

`/stop` 指令不能入队等待——如果 Agent 正在运行，入队的 `/stop` 要等当前消息处理完才被取出，此时已无意义。应在入队前直接执行 `request_stop()`。

在 `enqueue_and_wait()` 方法开头添加：

```python
    async def enqueue_and_wait(self, content: str, source: str = "user",
                                session_id: str = "default", **kwargs):
        """入队消息并等待 Agent 回复"""
        # --- /stop directive: immediate stop, no queueing ---
        if content.strip() == "/stop":
            from agent.runner import request_stop
            request_stop()
            logger.info("[ChatQueue] /stop requested (immediate)")
            return "已停止"

        # ... 原有入队逻辑 ...
```

**注意**：`/stop` 在 `enqueue_and_wait()` 中拦截后直接返回，不走 `_process_single()`，因此 `_process_single()` 中的 `clear_stop()` 不会为 `/stop` 消息执行。标志清除依赖 Agent 循环退出时的 `clear_stop()`（Task 2）和 `chat_session()` 入口的防御性 `clear_stop()`（Task 3）。

- [ ] **Step 2: 在 _process_single() 中添加防御性标志清除**

```python
    async def _process_single(self, content: str, session_id: str = "default",
                               user_contents: list[str] | None = None, channel: str = "electron") -> str:
        """处理单条消息"""
        from agent.runner import clear_stop

        # 防御性清除：确保新对话开始时标志干净
        # （正常情况下 Agent 退出时已清除，这里处理异常退出残留）
        clear_stop()

        # ... 原有逻辑 ...
```

无需 `is_system` 判断——如果定时任务执行时用户发了 `/stop`，定时任务的 Agent 循环也会在下一轮检测到并退出（这是合理行为：用户明确要求停止），退出时自动 `clear_stop()`。

- [ ] **Step 3: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import niu_api.chat_queue; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/chat_queue.py
git commit -m "feat: ChatQueue intercept /stop in enqueue_and_wait, clear_stop defensively in _process_single"
```

---

### Task 5: 前端 /stop 和 /clear 指令处理

**Files:**
- Modify: `ui/assistant/chat.html:716-786`（sendMessage 函数中的指令处理区域）

- [ ] **Step 1: 在前端添加 /stop 和 /clear 指令处理**

**关键**：`/stop` 和 `/clear` 必须放在 `isProcessing` 检查**之前**（第 717 行之前），否则 Agent 运行时（`isProcessing=true`）用户无法输入这些指令。

修改 `sendMessage()` 函数，将 `/stop` 和 `/clear` 移到 `isProcessing` 检查之前，`/new` 保持在 `isProcessing` 检查之后：

```javascript
async function sendMessage() {
  const text = userInput.value.trim();
  if (!text) { userInput.style.height = 'auto'; return; }

  // /stop 指令 - 停止当前 Agent 工作（允许在 isProcessing=true 时执行）
  if (text === '/stop') {
    userInput.value = '';
    userInput.style.height = 'auto';
    try {
      await window.electronAPI.sendMessage('/stop');
    } catch (e) {
      console.error('停止失败:', e);
      addSystemMessage('停止失败: ' + (e.message || e));
    }
    return;
  }

  // /clear 指令 - 先停止再清空（允许在 isProcessing=true 时执行）
  if (text === '/clear') {
    userInput.value = '';
    userInput.style.height = 'auto';
    try {
      await window.electronAPI.sendMessage('/stop');
    } catch (e) {
      console.error('停止失败:', e);
      addSystemMessage('停止失败: ' + (e.message || e));
    }
    // 等 SSE chat_idle 事件重置 isProcessing 后，再清空聊天
    // 不直接 await clearChat()，因为锁被 Agent 占用会阻塞 UI
    _pendingClear = true;
    return;
  }

  if (isProcessing) { addSystemMessage('请等待当前回复完成'); return; }
  isProcessing = true;

  // /new 指令 - 清空聊天记录（只能在 isProcessing=false 时使用）
  if (text === '/new') {
    userInput.value = '';
    userInput.style.height = 'auto';
    isProcessing = false;
    await clearChat();
    return;
  }

  // ... 原有 sendMessage 逻辑（发送正常消息）...
}
```

**关键设计**：
1. `/stop` 和 `/clear` 在 `isProcessing` 检查之前，Agent 运行时也能执行
2. `/new` 在 `isProcessing` 检查之后，Agent 运行时不能清空（没有意义）
3. `/clear` 使用 `_pendingClear` 标志，等 `chat_idle` 事件后再执行 `clearChat()`，避免锁等待阻塞 UI
4. `/stop` 和 `/clear` 直接 `return`，不经过 `finally` 块（因为它们在 `isProcessing = true` 和 `try/finally` 之前），所以**不需要 `_skipFinallyReset`**

在 JavaScript 中添加变量声明（第 526 行 `const sendBtn = ...` 之后）：
```javascript
const stopBtn = document.getElementById('stop-btn');
let _pendingClear = false;
```

在 SSE `chat_idle` 事件处理中添加 `_pendingClear` 检查。需要将事件回调改为 `async` 以支持 `await clearChat()`：

```javascript
window.electronAPI.onNewMessage(async (data) => {  // ← 添加 async
  if (data && data.role === 'chat_idle') {
    hideTyping();
    if (busyTimeout) { clearTimeout(busyTimeout); busyTimeout = null; }
    isProcessing = false;
    sendBtn.disabled = false;
    stopBtn.style.display = 'none';
    sendBtn.style.display = 'flex';
    window.electronAPI.notifyBusy(false, 'chat');
    // 处理 /clear 的延迟清空
    // 注意：chat_idle 在 Agent 循环退出时推送，此时 chat_session 的锁可能还未释放
    // clearChat() 会等待锁（最多 30 秒），chat_session 的 finally 会很快释放锁
    if (_pendingClear) {
      _pendingClear = false;
      try {
        await clearChat();
      } catch (e) {
        console.error('清空失败:', e);
        addSystemMessage('清空失败: ' + (e.message || e));
      }
    }
  }
  // ... 其余 onNewMessage 逻辑不变 ...
});
```

- [ ] **Step 2: 添加停止按钮（处理中可见）**

在 HTML 中找到发送按钮（第 509 行 `<button id="send-btn">发送</button>`），在其后添加停止按钮：

```html
<button class="stop-btn" id="stop-btn" style="display:none;" title="停止生成 (/stop)">■</button>
```

在 CSS 区域添加样式：

```css
.stop-btn {
  background: #e74c3c;
  color: white;
  border: none;
  border-radius: 50%;
  width: 32px;
  height: 32px;
  cursor: pointer;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.stop-btn:hover {
  background: #c0392b;
}
```

在 JavaScript 中添加停止按钮的显示/隐藏逻辑：

搜索 `isProcessing = true` 的位置（通常在 sendMessage 函数中），在设置 `isProcessing = true` 之后添加：
```javascript
stopBtn.style.display = 'flex';
sendBtn.style.display = 'none';
```

搜索 `isProcessing = false` 的位置，在设置 `isProcessing = false` 之后添加：
```javascript
stopBtn.style.display = 'none';
sendBtn.style.display = 'flex';
```

添加停止按钮点击事件（在 DOMContentLoaded 或初始化区域）：
```javascript
stopBtn.addEventListener('click', async () => {
  try {
    await window.electronAPI.sendMessage('/stop');
  } catch (e) {
    console.error('停止失败:', e);
    addSystemMessage('停止失败: ' + (e.message || e));
  }
});
```

**注意**：停止按钮直接调用 IPC `sendMessage('/stop')`，不经过 `sendMessage()` 函数，因此不涉及 `isProcessing` 检查和 `finally` 块。

**重要**：还需要在 SSE `chat_idle` 事件处理中重置停止按钮。搜索 `data.role === 'chat_idle'` 的处理位置（第 1217 行），在 `isProcessing = false;` 和 `sendBtn.disabled = false;` 之后添加：
```javascript
stopBtn.style.display = 'none';
sendBtn.style.display = 'flex';
```

- [ ] **Step 3: 验证前端文件语法**

手动检查 HTML 文件是否闭合正确，CSS 和 JS 无语法错误。

- [ ] **Step 4: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "feat: add /stop and /clear directives, stop button in chat UI"
```

---

### Task 6: 子Agent 停止标志传播

**Files:**
- Modify: `agent/subagent.py:300-413`（call_subagent 函数）
- Modify: `agent/generic/agent_loop.py`（_run_agent_loop 或 agent_runner_loop 调用点）

- [ ] **Step 1: 在 call_subagent 中传递停止标志**

子Agent 运行在同一个 Python 进程中，`_stop_requested` 是全局的 `threading.Event`，子Agent 的 `agent_runner_loop` 已经在 Task 2 中添加了 `is_stop_requested()` 检查，所以子Agent 自动继承停止标志，**无需额外修改 `call_subagent()`**。

验证：`call_subagent()` 调用 `_run_agent_loop()` → `_run_agent_loop()` 调用 `agent_runner_loop()` → `agent_runner_loop()` 检查 `is_stop_requested()`。

子Agent 如果被 stop 中断，`call_subagent()` 返回的部分结果会被主Agent 的 `handler.dispatch()` 中的 `StepOutcome` 正常处理，主Agent 的循环会在下一轮检查 stop 标志并退出。

**结论：无需修改 `agent/subagent.py`**，Task 2 的改动已经覆盖子Agent。

- [ ] **Step 2: Commit (no-op, documentation only)**

无需代码修改。在 git log 中记录此决策。

---

### Task 7: handler.dispatch 中的停止检查

**Files:**
- Modify: `agent/handler.py:982-1217`（dispatch 函数）

- [ ] **Step 1: 在 dispatch 函数开头添加停止检查**

在 `handler.py` 的 `dispatch()` 函数开头（约第 982 行），添加：

```python
    def dispatch(self, tool_name: str, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器"""

        # --- Stop flag check ---
        from agent.runner import is_stop_requested
        if is_stop_requested():
            return StepOutcome(
                {"status": "stopped", "message": "用户已停止"},
                next_prompt="",
                should_exit=True,
            )
```

- [ ] **Step 2: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import agent.handler; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add agent/handler.py
git commit -m "feat: handler.dispatch checks stop flag before executing tools"
```

---

### Task 8: 端到端验证和文档更新

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 在 SYSTEM_MANUAL.md 中添加指令说明**

在"功能列表"表格中添加 `/stop` 和 `/clear` 指令说明。

在"常见问题"部分添加停止相关的说明。

- [ ] **Step 2: 运行完整测试套件**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_stop_flag.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add /stop and /clear directive documentation"
```

---

## 自查清单

### 1. Spec 覆盖度

| 需求 | 对应 Task |
|------|-----------|
| `/stop` 作为指令通过消息通道发送 | Task 3, Task 4 |
| 前端停止按钮发送 `/stop` | Task 5 |
| `/clear` 先执行 stop 再清空 | Task 5 |
| `/clear` 在 IM 中也有效 | Task 4（ChatQueue enqueue_and_wait 拦截） |
| 停止标志在 Agent 循环退出时自动清除 | Task 2（所有退出点 clear_stop） |
| 停止标志在用户发消息时防御性清除 | Task 3, Task 4 |
| 标志不残留影响后续定时任务 | Task 2（循环退出时 clear_stop） |
| 子Agent 自动继承停止标志 | Task 6（无需修改，共享全局 Event） |
| 主循环检查停止标志 | Task 2 |
| handler.dispatch 检查停止标志 | Task 7 |
| `/stop` 和 `/clear` 在 isProcessing 检查之前 | Task 5（致命修正） |
| `/clear` 用 _pendingClear 延迟到 chat_idle 后执行 | Task 5（避免锁阻塞 UI） |
| chat_idle 事件重置停止按钮 + 处理 _pendingClear | Task 5 |

### 2. Placeholder 扫描

无 TBD、TODO、implement later 等占位符。

### 3. 类型一致性

- `request_stop()` → `_stop_requested.set()` — 无返回值，全文件一致
- `clear_stop()` → `_stop_requested.clear()` — 无返回值，全文件一致
- `is_stop_requested()` → `_stop_requested.is_set()` → bool，全文件一致
- `StepOutcome(data, next_prompt, should_exit)` — 与 `agent_loop.py` 定义一致
