# IM 通道继承机制 Implementation Plan (v5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 主 Agent 回复始终推送到最近一次真实用户消息的来源通道——IM 用户发的消息，后续所有回复（包括子 Agent 完成后的汇报）都推 IM；只有用户从 Chat 页面发消息才切回 Electron 通道。定时任务天生触发 IM 通道，也应被继承。

**Architecture:** 在 NiuRunner 上增加 `_im_channel_id` 实例变量，记录最近一次真实用户消息或定时任务的 IM channel_id。三个入口负责设置/清除：ChatQueue（IM→设置，Electron→清除，Scheduler→不调让继承）、chat_session（根据 source 字段判断）、/chat/sync（Electron→清除）。子 Agent 注入（source 为空）继承但不改变 `_im_channel_id`。

**Tech Stack:** Python, FastAPI, asyncio, Electron

---

## File Structure

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `agent/runner.py` | NiuRunner — `_im_channel_id` 继承逻辑 + `set_im_channel`/`get_im_channel` | Modify |
| `niu_api/chat_queue.py` | _process_single — 调 set_im_channel（IM→设置，Electron→清除，Scheduler→不调） | Modify |
| `niu_api/compat.py` | ChatRequest 加 source；chat_session 通道判断 + IM 推送（锁释放后） | Modify |
| `niu_api/chat.py` | /chat/sync 调 set_im_channel("") | Modify |
| `niu_api/internal/scheduler/service.py` | 两处 push→route_out + push_chat_id 用 get_im_channel fallback | Modify |
| `ui/main/preload-chat.js` | sendMessage 支持可选 source | Modify |
| `ui/main/main.js` | 三个 IPC handler 传 source | Modify |
| `ui/main/windows/assistant/chat.html` | sendMessageWithRetry 支持 source；子 Agent 注入传 source="" | Modify |
| `tests/test_im_channel_inheritance.py` | 测试 | Create |

## 核心设计

### 通道判断规则（用户确认）

```
真实用户消息来自 IM → _im_channel_id = channel_id → 后续所有回复推 IM
真实用户消息来自 Chat（Electron）→ _im_channel_id = "" → 回复走 Electron
定时任务 → 不调 set_im_channel → 继承 _im_channel_id（定时任务天生走 IM）
子 Agent 注入 → 不调 set_im_channel → 继承 _im_channel_id
```

### IM 推送语义（已核实）

- **STREAM**（notify_stream）：创建/更新流式卡片
- **SEND**（route_out→adapter.send，需非空 channel_id）：终结卡片。无卡片时发独立 markdown
- **PUSH**（router.push→adapter.push）：发独立消息，不终结卡片
- route_out 空 channel_id 回退到 push → 不终结卡片

因此 scheduler 的 IM 推送必须：① 用 route_out（不是 push）；② channel_id 非空（用 `runner.get_im_channel()` fallback）。

---

## Task 1: NiuRunner _im_channel_id 继承逻辑

**Files:**
- Modify: `agent/runner.py:731` (init), `agent/runner.py:2792` (chat 开头)
- Test: `tests/test_im_channel_inheritance.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_im_channel_inheritance.py`：

```python
"""IM 通道继承机制测试。"""
import pytest
from unittest.mock import MagicMock, patch
from agent.runner import NiuRunner


def _make_runner():
    runner = NiuRunner.__new__(NiuRunner)
    runner._current_channel_id = ""
    runner._im_channel_id = ""
    runner._first_turn_extra_injection = ""
    runner.last_return_value = None
    runner._persisted_msgs = []
    runner.handler = MagicMock()
    runner.client = MagicMock()
    runner.disk_engine = MagicMock()
    runner.disk_engine.get_schema.return_value = {"type": "function", "function": {"name": "disk", "parameters": {"type": "object", "properties": {}}}}
    runner.base_tools_schema = []
    runner.default_model = "test"
    runner._refresh_base_tools_schema_if_dirty = MagicMock()
    runner._assemble_system_message = MagicMock()
    return runner


class TestSetGetIMChannel:
    def test_set_im_channel_records_channel_id(self):
        runner = _make_runner()
        runner.set_im_channel("im_chat_123")
        assert runner.get_im_channel() == "im_chat_123"

    def test_set_im_channel_clears_with_empty_string(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        runner.set_im_channel("")
        assert runner.get_im_channel() == ""

    def test_get_im_channel_returns_empty_by_default(self):
        runner = _make_runner()
        assert runner.get_im_channel() == ""


class TestChatInheritance:
    def test_chat_with_im_channel_id_sets_im_channel_id(self):
        runner = _make_runner()
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id="im_chat_123"))
        assert runner._im_channel_id == "im_chat_123"

    def test_chat_with_empty_channel_id_inherits_im_channel_id(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id=""))
        assert runner._im_channel_id == "im_chat_123"

    def test_chat_with_empty_channel_id_and_empty_im_channel_id_stays_empty(self):
        runner = _make_runner()
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id=""))
        assert runner._im_channel_id == ""

    def test_im_channel_id_survives_across_multiple_chat_calls(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "first", channel_id=""))
            list(runner.chat("default", "second", channel_id=""))
        assert runner._im_channel_id == "im_chat_123"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_im_channel_inheritance.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`agent/runner.py` L731 `self._current_channel_id = ""` 之后加：
```python
        self._im_channel_id = ""
```

约 L735 加方法：
```python
    def set_im_channel(self, channel_id: str) -> None:
        """设置/清除 IM 通道。必须在 _chat_lock 持有时调用。"""
        self._im_channel_id = channel_id

    def get_im_channel(self) -> str:
        return self._im_channel_id
```

L2792 将 `self._current_channel_id = channel_id` 替换为：
```python
        if not channel_id and self._im_channel_id:
            channel_id = self._im_channel_id
        self._current_channel_id = channel_id
        if channel_id:
            self._im_channel_id = channel_id
```

- [ ] **Step 4: 测试 + 语法检查 + 提交**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_im_channel_inheritance.py -v
python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"
git add agent/runner.py tests/test_im_channel_inheritance.py
git commit -m "feat: add _im_channel_id inheritance in NiuRunner"
```

---

## Task 2: ChatQueue 调用 set_im_channel

**Files:**
- Modify: `niu_api/chat_queue.py` (_process_single, lock 获取后)

- [ ] **Step 1: 实现**

`niu_api/chat_queue.py` `_process_single`，在 `if not acquired: raise TimeoutError(...)` 之后、`full_reply = await ...run_in_executor(None, sync_chat)` 之前（约 L333），加：
```python
                if channel == "electron":
                    self._runner.set_im_channel("")
                elif channel == "im":
                    self._runner.set_im_channel(channel_id)
                # scheduler 不调，继承 _im_channel_id
```

- [ ] **Step 2: 语法检查 + 提交**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('niu_api/chat_queue.py').read()); print('OK')"
git add niu_api/chat_queue.py
git commit -m "feat: ChatQueue sets/clears _im_channel_id"
```

---

## Task 3: ChatRequest source + chat_session 通道判断 + IM 推送（锁释放后）

**Files:**
- Modify: `niu_api/compat.py:1333` (ChatRequest), `niu_api/compat.py:2196` (set_im_channel), `niu_api/compat.py:2199` (runner.chat), `niu_api/compat.py:2242-2247` (IM 推送移到 finally 之后)

- [ ] **Step 1: ChatRequest 加 source**

L1333 `resources: list = []` 之后加：
```python
    source: str = ""
```

- [ ] **Step 2: chat_session set_im_channel + runner.chat**

L2196 `def sync_chat():` 之前加：
```python
        if request.source == "electron":
            runner.set_im_channel("")
```

L2199 改为：
```python
            for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner, resources=request.resources or None, channel_id=runner.get_im_channel()):
```

- [ ] **Step 3: IM 推送移到 finally 之后（锁释放后）**

当前结构：
```python
    try:
        ...
        return ChatResponse(reply=full_reply, ...)
    finally:
        clear_stop()
        drain_supplements()
        _chat_lock.release()
```

改为：
```python
    try:
        ...
    finally:
        clear_stop()
        drain_supplements()
        _chat_lock.release()

    # IM 推送在锁释放后执行（与 ChatQueue 模式一致，避免网络 I/O 阻塞锁）
    try:
        im_cid = runner.get_im_channel()
        if im_cid and chat_error is None:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            await router.route_out(full_reply, "im", im_cid)
    except Exception as e:
        logger.warning(f"[chat_session] IM push failed: {e}")

    return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
```

注意：`return ChatResponse` 从 try 块内移到 finally 之后。`full_reply`/`message_id`/`chat_error` 变量在 try 块中赋值，finally 之后可访问（Python 作用域）。如果 try 块中 return 了（原代码），finally 仍会执行但 return 值已确定——改为不 return，finally 后统一 return。

- [ ] **Step 4: 语法检查 + 提交**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"
git add niu_api/compat.py
git commit -m "feat: ChatRequest source field, chat_session IM push after lock release"
```

---

## Task 4: scheduler push→route_out + push_chat_id fallback

**Files:**
- Modify: `niu_api/internal/scheduler/service.py:152-154` (reminder) + `niu_api/internal/scheduler/service.py:251-253` (background_script)

- [ ] **Step 1: 两处修改**

**第一处** L152-154（trigger_callback reminder），将：
```python
            push_chat_id = task.get("chat_id") or ""
            push_future = asyncio.run_coroutine_threadsafe(
                router.push(agent_reply, "im", push_chat_id),
```
改为：
```python
            # 优先用 task chat_id，回退到继承的 _im_channel_id（确保 route_out 走 SEND 终结卡片）
            from niu_api.chat import get_or_create_runner
            _runner = get_or_create_runner()
            im_cid = _runner.get_im_channel() if _runner else ""
            push_chat_id = task.get("chat_id") or im_cid
            push_future = asyncio.run_coroutine_threadsafe(
                router.route_out(agent_reply, "im", push_chat_id),
```

**第二处** L251-253（_trigger_background_script），将：
```python
                push_chat_id = task.get("chat_id") or ""
                push_future = asyncio.run_coroutine_threadsafe(
                    router.push(agent_reply, "im", push_chat_id),
```
改为：
```python
                from niu_api.chat import get_or_create_runner
                _runner = get_or_create_runner()
                im_cid = _runner.get_im_channel() if _runner else ""
                push_chat_id = task.get("chat_id") or im_cid
                push_future = asyncio.run_coroutine_threadsafe(
                    router.route_out(agent_reply, "im", push_chat_id),
```

原因：scheduler 继承 _im_channel_id → runner.chat() STREAM 创建卡片 → route_out（SEND，非空 channel_id）终结卡片。task.chat_id 为空时用 get_im_channel() fallback 确保 channel_id 非空。无卡片时 route_out fallback 发独立 markdown。

- [ ] **Step 2: 语法检查 + 提交**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('niu_api/internal/scheduler/service.py').read()); print('OK')"
git add niu_api/internal/scheduler/service.py
git commit -m "fix: scheduler push→route_out + get_im_channel fallback for card finalization"
```

---

## Task 5: /chat/sync 清除 IM 通道

**Files:**
- Modify: `niu_api/chat.py:616`

- [ ] **Step 1: 实现**

L616 `runner = get_or_create_runner()` 之后加：
```python
        runner.set_im_channel("")
```

- [ ] **Step 2: 语法检查 + 提交**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('niu_api/chat.py').read()); print('OK')"
git add niu_api/chat.py
git commit -m "feat: /chat/sync clears IM channel"
```

---

## Task 6: 前端改造

**Files:**
- Modify: `ui/main/preload-chat.js:20`
- Modify: `ui/main/main.js:960,927,1006`
- Modify: `ui/main/windows/assistant/chat.html` (sendMessageWithRetry + L2427)

- [ ] **Step 1: preload-chat.js**

`ui/main/preload-chat.js` L20，将：
```javascript
        sendMessage: (message) => ipcRenderer.invoke('send-message', message),
```
改为：
```javascript
        sendMessage: (message, source) => ipcRenderer.invoke('send-message', message, source),
```

- [ ] **Step 2: main.js send-message**

L960，将：
```javascript
ipcMain.handle('send-message', async (event, message) => {
  return new Promise((resolve) => {
    const data = JSON.stringify({ message: message });
```
改为：
```javascript
ipcMain.handle('send-message', async (event, message, source) => {
  return new Promise((resolve) => {
    const data = JSON.stringify({ message: message, source: source !== undefined ? source : 'electron' });
```

- [ ] **Step 3: main.js process-image + send-to-agent**

L927 process-image 的 JSON.stringify 加 `source: 'electron'`。
L1006 send-to-agent 的 payload 改为 `{ message: message, source: 'electron' }`。

- [ ] **Step 4: chat.html sendMessageWithRetry**

函数签名改为 `async function sendMessageWithRetry(text, source, maxRetries = 10, delay = 2000)`。
内部 `sendMessage(text)` 改为 `sendMessage(text, source)`。

子 Agent 注入路径（约 L2427）`sendMessageWithRetry(msgContent)` 改为 `sendMessageWithRetry(msgContent, '')`。
用户消息路径不改（source 为 undefined → main.js 中 `source !== undefined ? source : 'electron'` → 'electron'）。

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/preload-chat.js ui/main/main.js ui/main/windows/assistant/chat.html
git commit -m "feat: frontend passes source to distinguish Electron vs subagent"
```

---

## Task 7: 端到端验证

- [ ] **Step 1: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['agent/runner.py', 'niu_api/chat_queue.py', 'niu_api/compat.py', 'niu_api/chat.py', 'niu_api/internal/scheduler/service.py']]; print('OK')"
```

- [ ] **Step 2: 测试**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_im_channel_inheritance.py tests/test_compress_degradation.py tests/test_compress_quality.py tests/test_truncation_marker.py tests/test_llm_error_handling.py tests/test_at_prefix_interception.py tests/test_sync_subagent_interaction.py -v 2>&1 | tail -20
```
Expected: All PASS（1 pre-existing NoneType 可接受）

- [ ] **Step 3: 验证表**

| 场景 | 入口 | source | set_im_channel | _im_channel_id | IM 推送 |
|---|---|---|---|---|---|
| IM 用户发消息 | ChatQueue | "im" | set("im_123") | "im_123" | ✓ STREAM + route_out(SEND) |
| 子 Agent 完成 | chat_session | "" | 不调 | "im_123" | ✓ STREAM + route_out(SEND) |
| Chat 用户发消息 | chat_session | "electron" | set("") | "" | ✗ Electron |
| 定时任务触发 | ChatQueue | "scheduler" | 不调 | "im_123" | ✓ STREAM + route_out(SEND, push_chat_id=get_im_channel fallback) |
| 定时任务→子Agent | chat_session | "" | 不调 | "im_123" | ✓ STREAM + route_out(SEND) |
| /chat/sync | /chat/sync | N/A | set("") | "" | ✗ Electron |

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add -A
git commit -m "test: e2e verification for IM channel inheritance" --allow-empty
```
