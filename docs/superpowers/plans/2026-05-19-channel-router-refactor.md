# Chat Queue 架构 — 消息队列替代 _chat_lock

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用消息队列 + 串行处理 + 上下文合并替代 `_chat_lock`，使用户在 Agent 处理期间可以发送补充消息，飞书消息不被阻塞，路由层与内部锁机制完全解耦。

**Architecture:** 所有消息来源（前端、飞书、Scheduler）统一通过 ChatQueue 入队。前端端点即返（入队后立即返回），回复通过 SSE 推送。ChatWorker 后台协程串行处理队列中的消息，调用 runner.chat()。处理期间到达的补充消息在下一轮处理时合并到上下文中（每条独立持久化，合并仅发生在传给 runner.chat() 的参数中）。飞书消息通过 enqueue_sync 线程安全入队。ChannelRouter 直接入队，不再 HTTP 自调用。

**Tech Stack:** Python 3.11+, asyncio.Queue, threading (飞书 SDK 约束), FastAPI SSE

---

## 核心设计决策

### D1: ChatQueue 替代 _chat_lock

| 维度 | _chat_lock | ChatQueue |
|------|-----------|-----------|
| 用户体验 | 等待60s后拒绝 | 立即确认，后台处理 |
| 补充消息 | 被锁阻塞 | 入队等待，合并处理 |
| 飞书消息 | HTTP自调用受锁影响 | 直接入队，无锁 |
| 前端 | isProcessing 禁用输入 | 随时发送 |

### D2: 端点模式划分

**即返模式（enqueue）**：/api/chat/session, 飞书 ChannelRouter
- 消息入队后立即返回，回复通过 SSE 推送或飞书 push
- /api/chat/session 即返（前端 fire-and-forget，回复通过 SSE new_message 事件 → refreshFromDB 到达）
- 飞书消息即返（回复通过 ChatQueue._push_to_feishu 推送）

**等待模式（enqueue_and_wait）**：/chat/sync
- 消息入队后等待 ChatWorker 处理完成，返回回复
- /chat/sync 供 Scheduler 使用（需要同步结果用于飞书推送）

### D3: 消息合并策略

当 ChatWorker 处理完一条消息后，检查队列中是否还有待处理消息：

- **有补充消息**：将队列中所有待处理消息合并为一条，格式为：
  ```
  [原始消息]

  [补充1] 用户补充：第二条消息内容
  [补充2] 用户补充：第三条消息内容
  ```
  合并后的消息作为下一次 runner.chat() 的输入。

- **持久化策略**：user 消息在 _process_single 中持久化，但**先加载历史上下文再持久化**，避免刚持久化的 user 消息出现在历史中导致重复。具体做法：先调用 get_context_for_chat 加载历史（此时不包含当前 user 消息），再调用 store.add_message 持久化 user 消息，最后将合并后的消息传给 runner.chat()。runner.chat() 的 content 参数是合并后的消息文本（当前轮次输入），history_for_runner 中不包含当前轮次的 user 消息，不会重复。

- **无补充消息**：正常处理下一条。

### D4: ChannelRouter 直接入队

飞书消息 → `ChannelRouter.route_in_sync()` → `ChatQueue.enqueue_sync()` → ChatWorker → Agent

不再通过 HTTP 自调用 /chat/sync。

### D5: 前端解锁

去掉 `isProcessing`，消息入队后立即返回。前端通过 SSE 接收回复。用引用计数追踪活跃请求数，控制 typing 气泡显示。

### D6: 飞书 ID 管理

- **没有 ID 就不发** — push() 静默跳过
- **飞书消息来了自动记住** — _update_persisted_ids
- **重连后重新加载** — _on_reconnected 调用 _load_prefs + _apply_persisted_ids
- **push 优先 open_id** — 更稳定，不会过期

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/chat_queue.py` | ChatQueue + ChatWorker 核心实现 | **新建** |
| `niu_api/chat.py` | 提取 persist_agent_reply 共享函数；端点改造 | 修改 |
| `niu_api/compat.py` | 去掉 _chat_lock，/api/chat/session 改用 ChatQueue | 修改 |
| `niu_api/__main__.py` | FastAPI lifespan 集成 ChatQueue 启停 | 修改 |
| `niu_api/channel/__init__.py` | ChannelRouter 改用 ChatQueue 入队 | 修改 |
| `niu_api/channel/feishu_channel.py` | ID 管理、重连回调、push 优先 open_id | 修改 |
| `niu_api/internal/scheduler/service.py` | Scheduler 改用 ChatQueue 入队 | 修改 |
| `ui/assistant/chat.html` | 前端去掉 isProcessing，改用引用计数 | 修改 |
| `tests/test_chat_queue.py` | ChatQueue 单元测试 | **新建** |
| `tests/test_feishu_channel_robustness.py` | 飞书通道健壮性测试 | **新建** |

---

## Task 1: 提取 persist_agent_reply 共享函数

**Files:**
- Modify: `niu_api/chat.py` (提取共享函数)
- Test: `tests/test_chat_queue.py`

**前置**：ChatQueue 需要复用 chat.py 中的 Agent 回复持久化逻辑（working_memory 过滤、tool_call_id 关联等）。先提取为共享函数，避免逻辑分叉。

- [ ] **Step 1: 写失败测试 — persist_agent_reply 共享函数**

```python
# tests/test_chat_queue.py
"""ChatQueue 单元测试 — 消息队列 + 串行处理 + 上下文合并"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def reset_globals():
    """每个测试前后清理全局单例"""
    import niu_api.chat_queue as mod
    old = mod._queue
    mod._queue = None
    yield
    mod._queue = None


@pytest.fixture
def mock_runner():
    runner = MagicMock()
    runner.chat.return_value = iter(["回复内容"])
    runner.last_return_value = {"result": "ok", "messages": []}
    return runner


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.add_message.return_value = "msg-id-1"
    return store


@pytest.mark.asyncio
async def test_persist_agent_reply_simple(mock_store):
    """简单回复应持久化为 assistant 消息"""
    from niu_api.chat import persist_agent_reply
    runner = MagicMock()
    runner.last_return_value = {"result": "ok", "messages": []}

    msg_id = await persist_agent_reply(mock_store, runner, "你好世界", 0)
    assert msg_id is not None
    mock_store.add_message.assert_called_with(role="assistant", content="你好世界")


@pytest.mark.asyncio
async def test_persist_agent_reply_with_tool_messages(mock_store):
    """包含 tool_calls 的回复应正确持久化 tool 和 assistant 消息"""
    from niu_api.chat import persist_agent_reply
    runner = MagicMock()
    runner.last_return_value = {
        "result": "ok",
        "messages": [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc_1", "function": {"name": "search", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "搜索结果", "tool_call_id": "tc_1"},
            {"role": "assistant", "content": "最终回复"},
        ]
    }

    msg_id = await persist_agent_reply(mock_store, runner, "最终回复", 1)
    assert msg_id is not None
    # 应持久化 tool 消息和 assistant 消息
    assert mock_store.add_message.call_count >= 2


@pytest.mark.asyncio
async def test_persist_agent_reply_filters_working_memory(mock_store):
    """working_memory 工具调用应被过滤，不持久化"""
    from niu_api.chat import persist_agent_reply
    runner = MagicMock()
    runner.last_return_value = {
        "result": "ok",
        "messages": [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc_wm", "function": {"name": "working_memory", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "wm result", "tool_call_id": "tc_wm"},
            {"role": "assistant", "content": "最终回复"},
        ]
    }

    msg_id = await persist_agent_reply(mock_store, runner, "最终回复", 1)
    assert msg_id is not None
    # working_memory 的 tool_calls 和 tool 结果不应被持久化
    for call in mock_store.add_message.call_args_list:
        kwargs = call[1] if call[1] else call[0][0] if call[0] else {}
        # 不应有 working_memory 相关的 tool_call_id
        if isinstance(kwargs, dict) and kwargs.get("tool_call_id") == "tc_wm":
            pytest.fail("working_memory tool result should not be persisted")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_persist_agent_reply_simple -v`
Expected: FAIL — `ImportError: cannot import name 'persist_agent_reply' from 'niu_api.chat'`

- [ ] **Step 3: 从 chat.py 中提取 persist_agent_reply**

在 `niu_api/chat.py` 中，将现有的 Agent 回复持久化逻辑提取为独立函数：

```python
# niu_api/chat.py — 添加共享函数

async def persist_agent_reply(store, runner, full_reply: str, history_len: int) -> str | None:
    """
    持久化 Agent 回复到数据库（共享函数）

    处理 working_memory 过滤、tool_call_id 关联等逻辑。
    供 /chat/sync、/api/chat/session、ChatQueue 共同使用。
    """
    message_id = None
    rv = getattr(runner, "last_return_value", None)

    if rv and isinstance(rv, dict) and rv.get("messages"):
        _wm_tool_call_ids = set()
        for msg in rv["messages"][history_len + 1:]:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "working_memory":
                        _wm_tool_call_ids.add(tc.get("id", ""))

        persisted_ids = []
        last_assistant_content = ""
        for msg in rv["messages"][history_len + 1:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id", "")

            if role in ("system", "user"):
                continue
            if role == "assistant" and tool_calls:
                if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
                    continue
            if role == "tool" and tool_call_id in _wm_tool_call_ids:
                continue

            if role == "tool" and tool_call_id:
                pid = await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
                persisted_ids.append(pid)
            elif role == "assistant":
                pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
                persisted_ids.append(pid)
                last_assistant_content = content or ""

        # 找最后一条 assistant 消息 ID
        last_assistant_id = None
        if persisted_ids:
            persisted_idx = 0
            for msg in rv["messages"][history_len + 1:]:
                role = msg.get("role", "")
                if role in ("system", "user"):
                    continue
                tool_calls = msg.get("tool_calls")
                tool_call_id = msg.get("tool_call_id", "")
                if role == "assistant" and tool_calls:
                    if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
                        continue
                if role == "tool" and tool_call_id in _wm_tool_call_ids:
                    continue
                if persisted_idx < len(persisted_ids):
                    if role == "assistant":
                        last_assistant_id = persisted_ids[persisted_idx]
                    persisted_idx += 1

        if full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
            message_id = await store.add_message(role="assistant", content=full_reply)
        elif last_assistant_id:
            message_id = last_assistant_id
        elif full_reply.strip():
            message_id = await store.add_message(role="assistant", content=full_reply)
    elif full_reply.strip():
        message_id = await store.add_message(role="assistant", content=full_reply)

    return message_id
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/chat.py tests/test_chat_queue.py
git commit -m "feat: 提取 persist_agent_reply 共享函数 — ChatQueue 复用"
```

---

## Task 2: ChatQueue 核心实现

**Files:**
- Create: `niu_api/chat_queue.py`
- Test: `tests/test_chat_queue.py` (追加)

- [ ] **Step 1: 写失败测试 — ChatQueue 入队和串行处理**

```python
# tests/test_chat_queue.py (追加)

@pytest.mark.asyncio
async def test_enqueue_and_process(mock_runner, mock_store):
    """单条消息入队后应被 ChatWorker 串行处理"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        original = q._process_single
        async def tracked(*a, **kw):
            r = await original(*a, **kw)
            processed.set()
            return r
        q._process_single = tracked

        await q.start()
        try:
            result = await q.enqueue("你好")
            assert result.queued is True
            await asyncio.wait_for(processed.wait(), timeout=5.0)
            mock_runner.chat.assert_called_once()
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_message_merging(mock_runner, mock_store):
    """多条待处理消息应合并为一条传给 runner.chat()"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        original = q._process_single
        async def tracked(*a, **kw):
            r = await original(*a, **kw)
            processed.set()
            return r
        q._process_single = tracked

        await q.start()
        try:
            await q.enqueue("第一条消息")
            await q.enqueue("补充信息")
            await q.enqueue("再补充")

            await asyncio.wait_for(processed.wait(), timeout=5.0)

            call_args = mock_runner.chat.call_args
            user_input = call_args[1].get("user_input") or call_args[0][1]
            assert "第一条消息" in user_input
            assert "补充信息" in user_input
            assert "再补充" in user_input
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_each_message_persisted_separately(mock_runner, mock_store):
    """合并消息应独立持久化每条 user 消息，合并仅发生在传给 runner.chat() 的参数中"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        original = q._process_single
        async def tracked(*a, **kw):
            r = await original(*a, **kw)
            processed.set()
            return r
        q._process_single = tracked

        await q.start()
        try:
            await q.enqueue("第一条")
            await q.enqueue("补充")

            await asyncio.wait_for(processed.wait(), timeout=5.0)

            # 每条消息应独立持久化
            user_calls = [c for c in mock_store.add_message.call_args_list
                          if c[1].get("role") == "user" or (c[0] and len(c[0]) > 0 and c[0][0] == "user")]
            assert len(user_calls) >= 2  # 至少两条 user 消息
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_returns_immediately(mock_runner, mock_store):
    """enqueue 应立即返回，不等待处理完成"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        # 让 chat() 阻塞
        slow_chat = MagicMock(return_value=iter(["慢回复"]))
        mock_runner.chat = slow_chat

        await q.start()
        try:
            import time
            start = time.monotonic()
            result = await q.enqueue("测试立即返回")
            elapsed = time.monotonic() - start
            assert result.queued is True
            assert elapsed < 1.0
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_and_wait(mock_runner, mock_store):
    """enqueue_and_wait 应等待处理完成后返回回复"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        await q.start()
        try:
            reply = await q.enqueue_and_wait("测试等待", timeout=10.0)
            assert reply == "回复内容"
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_sync_from_other_thread(mock_runner, mock_store):
    """enqueue_sync 应从其他线程安全入队"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        # 模拟主事件循环
        import niu_api.chat as chat_mod
        original_loop = chat_mod._main_loop
        chat_mod._main_loop = asyncio.get_running_loop()

        await q.start()
        try:
            result = q.enqueue_sync("飞书消息", source="feishu", channel_id="chat_123")
            assert result.queued is True
            # 给 worker 时间处理
            await asyncio.sleep(0.3)
        finally:
            chat_mod._main_loop = original_loop
            await q.stop()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py -v -k "enqueue or merging"`
Expected: FAIL — `ModuleNotFoundError: No module named 'niu_api.chat_queue'`

- [ ] **Step 3: 实现 ChatQueue 核心代码**

```python
# niu_api/chat_queue.py
"""
ChatQueue — 消息队列 + 串行处理 + 上下文合并

替代 _chat_lock，所有消息来源（前端、飞书、Scheduler）统一入队，
ChatWorker 串行处理，补充消息在下一轮合并到上下文中。
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
from loguru import logger

from agent.runner import NiuRunner
from agent.session import get_message_store
from niu_api.chat import notify_new_message, persist_agent_reply


@dataclass
class ChatRequest:
    """入队消息"""
    content: str
    source: str = "frontend"  # "frontend" | "feishu" | "scheduler"
    channel_id: str = ""
    sender_id: str = ""
    session_id: str = "default"
    reply_future: Optional[asyncio.Future] = field(default=None, init=True, repr=False)


@dataclass
class EnqueueResult:
    """入队结果"""
    queued: bool = True
    request_id: str = ""
    message: str = "已入队"


class ChatQueue:
    """
    消息队列 — 替代 _chat_lock

    所有消息来源统一入队，ChatWorker 串行处理。
    处理期间到达的补充消息在下一轮合并到上下文中。
    """

    def __init__(self, runner: NiuRunner):
        self._queue: asyncio.Queue[ChatRequest] = asyncio.Queue()
        self._runner = runner
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        self._processing = False
        self._processing_done = asyncio.Event()
        self._processing_done.set()  # 初始状态：未在处理
        self._request_counter = 0

    @property
    def is_processing(self) -> bool:
        """当前是否正在处理消息"""
        return self._processing

    async def start(self):
        """启动 ChatWorker 后台协程"""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("[ChatQueue] Worker started")

    async def stop(self):
        """停止 ChatWorker"""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("[ChatQueue] Worker stopped")

    async def enqueue(self, content: str, source: str = "frontend",
                      channel_id: str = "", sender_id: str = "",
                      session_id: str = "default") -> EnqueueResult:
        """消息入队 — 立即返回"""
        self._request_counter += 1
        req = ChatRequest(
            content=content,
            source=source,
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued: source={source}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=str(self._request_counter))

    def enqueue_sync(self, content: str, source: str = "frontend",
                     channel_id: str = "", sender_id: str = "",
                     session_id: str = "default") -> EnqueueResult:
        """同步入队 — 供飞书线程调用（通过 call_soon_threadsafe）"""
        from niu_api.chat import _main_loop
        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.error("[ChatQueue] Main loop not available, cannot enqueue")
            return EnqueueResult(queued=False, message="主事件循环不可用")

        self._request_counter += 1
        req = ChatRequest(
            content=content,
            source=source,
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        loop.call_soon_threadsafe(self._queue.put_nowait, req)
        logger.info(f"[ChatQueue] Enqueued (sync): source={source}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=str(self._request_counter))

    async def enqueue_and_wait(self, content: str, source: str = "scheduler",
                               session_id: str = "default",
                               timeout: float = 120.0) -> str:
        """入队并等待回复 — 供 Scheduler 等需要同步结果的场景"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        req = ChatRequest(
            content=content,
            source=source,
            session_id=session_id,
            reply_future=future,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued (wait): source={source}, content={content[:50]}...")

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[ChatQueue] Wait timeout for: {content[:50]}...")
            return ""

    async def drain(self, timeout: float = 30.0) -> bool:
        """等待当前处理完成并清空队列"""
        # 清空队列中的待处理消息
        # 为被清空消息的 reply_future 设置降级结果，避免调用方永远等待
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if req.reply_future and not req.reply_future.done():
                    req.reply_future.set_result("[会话已清空]")
            except asyncio.QueueEmpty:
                break

        # 等待当前处理完成
        if self._processing:
            try:
                await asyncio.wait_for(self._processing_done.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                logger.warning("[ChatQueue] Drain timeout")
                return False
        return True

    async def _worker_loop(self):
        """ChatWorker 主循环 — 串行处理队列中的消息"""
        while self._running:
            try:
                req = await self._queue.get()
                await self._process_with_merge(req)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ChatQueue] Worker error: {e}")

    async def _process_with_merge(self, first_req: ChatRequest):
        """处理消息，合并队列中的补充消息"""
        self._processing = True
        self._processing_done.clear()

        try:
            # 收集补充消息（保留第一条消息的 source 和 channel_id）
            source = first_req.source
            channel_id = first_req.channel_id
            reply_future = first_req.reply_future
            supplements = []

            while not self._queue.empty():
                try:
                    extra = self._queue.get_nowait()
                    supplements.append(extra)
                    # 不覆盖 source/channel_id — 回复推送给第一条消息的来源
                except asyncio.QueueEmpty:
                    break

            # 合并补充消息（仅用于传给 runner.chat() 的参数）
            # user 消息持久化在 _process_single 中统一完成
            all_contents = [first_req.content] + [s.content for s in supplements]
            if supplements:
                supplement_parts = []
                for i, s in enumerate(supplements, 1):
                    supplement_parts.append(f"[补充{i}] {s.content}")
                merged_content = f"{first_req.content}\n\n" + "\n".join(supplement_parts)
                logger.info(
                    f"[ChatQueue] Merged {len(supplements)} supplement(s): "
                    f"{merged_content[:80]}..."
                )
            else:
                merged_content = first_req.content

            # 处理合并后的消息（传入每条消息内容列表，用于独立持久化）
            reply = await self._process_single(merged_content, first_req.session_id, all_contents)

            # 推送回复到飞书
            if source == "feishu" and channel_id:
                await self._push_to_feishu(channel_id, reply)

            # 设置 reply_future
            if reply_future and not reply_future.done():
                reply_future.set_result(reply)
            for s in supplements:
                if s.reply_future and not s.reply_future.done():
                    s.reply_future.set_result(reply)

        finally:
            self._processing = False
            self._processing_done.set()

    async def _process_single(self, content: str, session_id: str = "default",
                             user_contents: list[str] | None = None) -> str:
        """处理单条消息 — 加载历史，持久化 user 消息，调用 runner.chat()，持久化回复，SSE推送"""
        store = await get_message_store()

        # 先加载历史上下文（此时不包含当前 user 消息，避免重复）
        from agent.context_manager import get_context_manager
        context_manager = await get_context_manager(store)
        history_for_runner = await context_manager.get_context_for_chat()
        history_len = len(history_for_runner)

        # 持久化 user 消息（每条独立持久化，在历史加载之后）
        # 这些消息不在 history_for_runner 中，不会重复
        # runner.chat() 的 content 参数包含合并后的消息文本，作为当前轮次输入
        if user_contents:
            for uc in user_contents:
                await store.add_message(role="user", content=uc)
        else:
            await store.add_message(role="user", content=content)

        # 调用 runner.chat()（在 executor 中运行，不阻塞事件循环）
        def sync_chat():
            chunks = []
            for chunk in self._runner.chat(session_id, content, stream=False, history=history_for_runner):
                chunks.append(chunk)
            return "".join(chunks)

        try:
            full_reply = await asyncio.get_running_loop().run_in_executor(None, sync_chat)
        except Exception as e:
            logger.error(f"[ChatQueue] Chat error: {e}")
            full_reply = f"处理消息时出错：{str(e)}"

        # 持久化回复消息（使用共享函数）
        message_id = await persist_agent_reply(store, self._runner, full_reply, history_len)

        # SSE 推送
        if message_id:
            await notify_new_message(message_id, "assistant", full_reply)

        # 上下文溢出检测
        await self._check_overflow(session_id, store, full_reply)

        return full_reply

    async def _check_overflow(self, session_id: str, store, full_reply: str):
        """检测上下文溢出，触发压缩"""
        rv = getattr(self._runner, "last_return_value", None)
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[ChatQueue] CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens"
            )
            from niu_api.compat import _tidy_context_impl, _tidy_lock
            async with _tidy_lock:
                await _tidy_context_impl(request={"session_id": session_id, "mode": "force"})
        elif full_reply.strip():
            from niu_api.compat import _check_and_trigger_auto_tidy
            await _check_and_trigger_auto_tidy(store)

    async def _push_to_feishu(self, channel_id: str, reply: str):
        """推送回复到飞书 — 让 push() 按 open_id > chat_id 优先级选择目标"""
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            if router.has_channel("feishu"):
                adapter = router.channels["feishu"]
                # 传空 channel_id，让 push() 内部按 open_id > chat_id 优先级选择
                adapter.channel.schedule(
                    adapter.push("", reply)
                )
        except Exception as e:
            logger.warning(f"[ChatQueue] Feishu push failed: {e}")


# ============== 全局单例 ==============

_queue: ChatQueue | None = None


def get_chat_queue() -> ChatQueue:
    """获取全局 ChatQueue 实例"""
    global _queue
    if _queue is None:
        from niu_api.chat import get_or_create_runner
        runner = get_or_create_runner()
        _queue = ChatQueue(runner)
    return _queue


async def start_chat_queue():
    """启动 ChatQueue（在 FastAPI startup 中调用）"""
    q = get_chat_queue()
    await q.start()


async def stop_chat_queue():
    """停止 ChatQueue（在 FastAPI shutdown 中调用）"""
    global _queue
    if _queue:
        await _queue.stop()
        _queue = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/chat_queue.py tests/test_chat_queue.py
git commit -m "feat: ChatQueue 消息队列核心实现 — 替代 _chat_lock"
```

---

## Task 3: 端点改造 — 去掉 _chat_lock，改用 ChatQueue

**Files:**
- Modify: `niu_api/compat.py` (chat_session 端点)
- Modify: `niu_api/chat.py` (/chat SSE 端点, /chat/sync 端点)

**关键决策**：
- `/api/chat/session` — 即返模式（enqueue），前端通过 SSE 接收回复
- `/chat/sync` — 等待模式（enqueue_and_wait），Scheduler 需要
- `/chat` SSE — 即返模式（enqueue），SSE 流保持连接直到客户端断开

- [ ] **Step 1: 写失败测试 — 端点不再阻塞**

```python
# tests/test_chat_queue.py (追加)

@pytest.mark.asyncio
async def test_session_endpoint_returns_immediately(mock_runner, mock_store):
    """/api/chat/session 端点应即返，不等待处理完成"""
    from niu_api.chat_queue import ChatQueue, get_chat_queue
    import niu_api.chat_queue as mod
    q = ChatQueue(mock_runner)
    mod._queue = q

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        # 让 chat() 阻塞
        import time
        def slow_chat(*a, **kw):
            time.sleep(2)
            return iter(["慢回复"])
        mock_runner.chat = slow_chat

        await q.start()
        try:
            import time as t
            start = t.monotonic()
            result = await q.enqueue("测试消息", source="frontend")
            elapsed = t.monotonic() - start
            assert result.queued is True
            assert elapsed < 1.0  # 即返，不等处理完
        finally:
            await q.stop()
```

- [ ] **Step 2: 改造 /api/chat/session 端点 — 即返模式（enqueue）**

```python
# niu_api/compat.py — chat_session 端点改造

@router.post("/api/chat/session")
async def chat_session(request: ChatRequest) -> ChatResponse:
    """
    Chat endpoint — 消息入队后立即返回

    回复通过 SSE new_message 事件 → refreshFromDB 到达前端。
    前端 sendMessage 改为 fire-and-forget，不依赖此端点返回的 reply。
    """
    from niu_api.config import get_config

    config = get_config()
    if not config.llm or not config.llm.api_key:
        return ChatResponse(reply="Error: LLM not configured, please set API Key first")

    from niu_api.chat_queue import get_chat_queue
    q = get_chat_queue()

    result = await q.enqueue(
        content=request.message,
        source="frontend",
        session_id="default",
    )

    if result.queued:
        return ChatResponse(reply="", session_id="default")
    else:
        return ChatResponse(reply=result.message, session_id="default")
```

**注意**：main.js 的 `send-message` IPC handler 也调用此端点，返回空 reply 不影响功能——chat.html 不再依赖 IPC 返回的 reply，回复通过 SSE 到达。

- [ ] **Step 3: 改造 /chat/sync 端点 — 等待模式**

```python
# niu_api/chat.py — chat_sync 端点改造

@router.post("/chat/sync")
async def chat_sync(request: ChatRequest) -> ChatResponse:
    """Synchronous chat — 入队并等待回复（Scheduler 用）"""
    llm_cfg = _load_llm_config()
    if not llm_cfg["apikey"]:
        raise HTTPException(status_code=400, detail="LLM not configured.")

    from niu_api.chat_queue import get_chat_queue
    q = get_chat_queue()

    reply = await q.enqueue_and_wait(
        content=request.message,
        source="scheduler",
        session_id=request.session_id or "default",
        timeout=120.0,
    )

    return ChatResponse(session_id=request.session_id or "default", reply=reply)
```

- [ ] **Step 4: 改造 /chat SSE 端点 — 保持连接**

```python
# niu_api/chat.py — /chat SSE 端点改造

@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """
    Main chat endpoint — 消息入队，SSE 保持连接

    SSE 流保持连接直到客户端断开。
    回复通过 notify_new_message → SSE new_message 事件到达前端。
    """
    llm_cfg = _load_llm_config()
    if not llm_cfg["apikey"]:
        raise HTTPException(status_code=400, detail="LLM not configured.")

    from niu_api.chat_queue import get_chat_queue
    q = get_chat_queue()

    result = await q.enqueue(
        content=request.message,
        source="frontend",
        session_id=request.session_id or "default",
    )

    async def generate():
        # 入队确认
        yield f"data: {json.dumps({'status': 'queued', 'request_id': result.request_id})}\n\n"
        # 保持连接，让前端继续接收 SSE new_message 事件
        # 连接会在客户端断开时自动关闭
        try:
            while True:
                await asyncio.sleep(30)  # 心跳保活
                yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 5: 删除 _chat_lock**

从 `niu_api/compat.py` 和 `niu_api/chat.py` 中删除 `_chat_lock` 定义和所有使用。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add niu_api/compat.py niu_api/chat.py
git commit -m "feat: 端点改造 — 去掉 _chat_lock，改用 ChatQueue"
```

---

## Task 4: ChannelRouter 改造 — 直接入队

**Files:**
- Modify: `niu_api/channel/__init__.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_chat_queue.py (追加)

@pytest.mark.asyncio
async def test_channel_router_enqueues_directly(mock_runner, mock_store):
    """ChannelRouter 应直接将消息放入 ChatQueue"""
    from niu_api.channel import ChannelRouter
    from niu_api.channel.base import UnifiedMessage
    from niu_api.chat_queue import ChatQueue, get_chat_queue
    import niu_api.chat_queue as mod

    q = ChatQueue(mock_runner)
    mod._queue = q

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        await q.start()
        try:
            router = ChannelRouter()
            msg = UnifiedMessage(
                content="飞书消息", channel="feishu",
                channel_id="chat_123", sender_id="user_456",
                message_type="text",
            )
            result = router.route_in_sync(msg, session_id="default")
            assert result.queued is True
        finally:
            await q.stop()
```

- [ ] **Step 2: 改造 ChannelRouter**

```python
# niu_api/channel/__init__.py

"""通道抽象层 — ChannelRouter + 全局单例"""

from typing import Dict, Optional
from loguru import logger

from .base import UnifiedMessage, ChannelAdapter

__all__ = ["ChannelRouter", "get_channel_router", "UnifiedMessage", "ChannelAdapter"]


class ChannelRouter:
    """统一消息路由器 — 所有通道的消息统一入队到 ChatQueue"""

    def __init__(self):
        self.channels: Dict[str, ChannelAdapter] = {}

    def route_in_sync(self, message: UnifiedMessage, session_id: str = "default",
                      message_override: str | None = None) -> "EnqueueResult":
        """同步路由消息 — 直接放入 ChatQueue"""
        from niu_api.chat_queue import get_chat_queue

        content = message_override if message_override is not None else message.content
        q = get_chat_queue()

        return q.enqueue_sync(
            content=content,
            source="feishu",
            channel_id=message.channel_id,
            sender_id=message.sender_id,
            session_id=session_id,
        )

    async def route_in(self, message: UnifiedMessage) -> str:
        """异步路由消息"""
        from niu_api.chat_queue import get_chat_queue

        q = get_chat_queue()
        result = await q.enqueue(
            content=message.content,
            source="feishu",
            channel_id=message.channel_id,
            sender_id=message.sender_id,
        )
        return "queued" if result.queued else "rejected"

    async def route_out(self, reply: str, channel: str, channel_id: str) -> None:
        """回复投递到指定通道"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.send(channel_id, reply)

    async def push(self, content: str, channel: str, channel_id: str) -> None:
        """主动推送"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.push(channel_id, content)

    def register(self, name: str, adapter: ChannelAdapter) -> None:
        self.channels[name] = adapter
        logger.info(f"[ChannelRouter] Registered channel: {name}")

    def has_channel(self, name: str) -> bool:
        return name in self.channels


_router: Optional[ChannelRouter] = None


def get_channel_router() -> ChannelRouter:
    global _router
    if _router is None:
        _router = ChannelRouter()
    return _router
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add niu_api/channel/__init__.py
git commit -m "feat: ChannelRouter 直接入队 ChatQueue"
```

---

## Task 5: Scheduler 改造

**Files:**
- Modify: `niu_api/internal/scheduler/service.py`
- Test: `tests/test_chat_queue.py` (追加)

- [ ] **Step 1: 写失败测试 — Scheduler 通过 ChatQueue 入队**

```python
# tests/test_chat_queue.py (追加)

def test_scheduler_uses_chat_queue(mock_runner, mock_store):
    """trigger_callback 应通过 ChatQueue.enqueue_and_wait 入队"""
    from niu_api.internal.scheduler.service import trigger_callback
    import niu_api.chat_queue as mod
    q = ChatQueue(mock_runner)
    mod._queue = q

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value="msg-id"), \
         patch("niu_api.chat._main_loop", asyncio.new_event_loop()) as mock_loop, \
         patch("niu_api.alerts.add_pending_alert"):

        mock_cm_instance = AsyncMock()
        mock_cm_instance.get_context_for_chat.return_value = []
        mock_cm.return_value = mock_cm_instance

        task = {"content": "提醒我喝水"}
        # trigger_callback 在 scheduler 线程中运行，测试其入队逻辑
        result = trigger_callback(task)
        # 应返回非空结果（可能是 agent 回复或 fallback）
        assert result is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_scheduler_uses_chat_queue -v`
Expected: FAIL — trigger_callback 当前通过 HTTP 调用 /chat/sync，未使用 ChatQueue

- [ ] **Step 3: 改造 trigger_callback — 使用 ChatQueue.enqueue_and_wait 替代 HTTP 调用**

```python
# niu_api/internal/scheduler/service.py — trigger_callback 改造

def trigger_callback(task: dict) -> str:
    """任务触发回调：通过 ChatQueue 入队，不再 HTTP 自调用 /chat/sync"""
    from niu_api.alerts import add_pending_alert

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")
    prompt = f"[定时任务] {task['content']}"

    try:
        from niu_api.chat_queue import get_chat_queue
        q = get_chat_queue()

        import asyncio
        from niu_api.chat import _main_loop

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.error("[INTERNAL SCHEDULER] Main loop not available")
            fallback_msg = f"定时提醒：{task['content']}"
            _persist_fallback_message(prompt, fallback_msg)
            add_pending_alert("⏰")
            return fallback_msg

        future = asyncio.run_coroutine_threadsafe(
            q.enqueue_and_wait(content=prompt, source="scheduler", session_id="default", timeout=90.0),
            loop,
        )
        agent_reply = future.result(timeout=120)

        logger.info(f"[INTERNAL SCHEDULER] Agent replied: {agent_reply[:100] if agent_reply else '(empty)'}")
        add_pending_alert("⏰")

        # 飞书推送（检查 has_push_target，传空 channel_id 让 push() 按 open_id > chat_id 优先）
        try:
            from niu_api.channel import get_channel_router
            channel_router = get_channel_router()
            if channel_router.has_channel("feishu"):
                feishu_adapter = channel_router.channels["feishu"]
                if feishu_adapter.has_push_target:
                    feishu_adapter.channel.schedule(
                        feishu_adapter.push("", agent_reply)
                    )
                else:
                    logger.info("[SCHEDULER] No feishu push target yet, skipping")
        except Exception as e:
            logger.warning(f"[SCHEDULER] Feishu push failed: {e}")

        return agent_reply if agent_reply else f"定时提醒：{task['content']}"

    except Exception as e:
        logger.error(f"[INTERNAL SCHEDULER] Failed to enqueue: {e}")
        fallback_msg = f"定时提醒：{task['content']}"
        _persist_fallback_message(prompt, fallback_msg)
        add_pending_alert("⏰")
        return fallback_msg
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_scheduler_uses_chat_queue -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/scheduler/service.py
git commit -m "feat: Scheduler 使用 ChatQueue 入队"
```

---

## Task 6: 飞书连接健壮性 — ID 管理 + 重连 + push 优先 open_id

**Files:**
- Modify: `niu_api/channel/feishu_channel.py`
- Test: `tests/test_feishu_channel_robustness.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_feishu_channel_robustness.py
"""飞书通道健壮性测试 — ID 管理、推送逻辑、重连"""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from niu_api.channel.feishu_channel import FeishuChannelAdapter


@pytest.fixture
def mock_channel():
    ch = MagicMock()
    ch.on = MagicMock()
    ch.is_ready = True
    ch.schedule = MagicMock()
    send_result = MagicMock(success=True)
    ch.send = AsyncMock(return_value=send_result)
    return ch


@pytest.fixture
def adapter(mock_channel, tmp_path):
    with patch("niu_api.channel.feishu_channel.FeishuChannel", return_value=mock_channel), \
         patch("lark_oapi.ws.client.loop", MagicMock(is_running=MagicMock(return_value=False))):
        adapter = FeishuChannelAdapter(
            app_id="test_app_id", app_secret="test_app_secret",
            channel_router=MagicMock(),
        )
        adapter._prefs_path = tmp_path / "preferences.json"
        adapter._feishu_prefs = {}
        return adapter


@pytest.mark.asyncio
async def test_push_no_ids_skips(adapter, mock_channel):
    """没有 ID 时 push() 静默跳过"""
    adapter._user_p2p_chat_id = None
    adapter._user_open_id = None
    await adapter.push("", "测试")
    mock_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_push_prefers_open_id(adapter, mock_channel):
    """同时有 chat_id 和 open_id 时优先用 open_id"""
    adapter._user_p2p_chat_id = "oc_chat"
    adapter._user_open_id = "ou_open"
    await adapter.push("", "测试")
    mock_channel.send.assert_called_once_with("ou_open", {"markdown": "测试"})


@pytest.mark.asyncio
async def test_push_fallback_on_failure(adapter, mock_channel):
    """open_id 失败时 fallback 到 chat_id"""
    adapter._user_p2p_chat_id = "oc_chat"
    adapter._user_open_id = "ou_dead"
    fail = MagicMock(success=False, error="blocked")
    ok = MagicMock(success=True)
    mock_channel.send = AsyncMock(side_effect=[fail, ok])
    await adapter.push("", "测试")
    assert mock_channel.send.call_count == 2
    mock_channel.send.assert_called_with("oc_chat", {"markdown": "测试"})


def test_reconnected_reloads_ids(adapter, mock_channel, tmp_path):
    """重连后应重新加载 preferences.json 中已保存的 ID"""
    adapter._user_p2p_chat_id = "oc_saved"
    adapter._user_open_id = "ou_saved"
    adapter._save_prefs()
    adapter._user_p2p_chat_id = None
    adapter._user_open_id = None
    adapter._on_reconnected()
    assert adapter._user_p2p_chat_id == "oc_saved"
    assert adapter._user_open_id == "ou_saved"


def test_has_push_target(adapter):
    """has_push_target 应正确反映是否有推送目标"""
    adapter._user_p2p_chat_id = None
    adapter._user_open_id = None
    assert adapter.has_push_target is False
    adapter._user_open_id = "ou_open"
    assert adapter.has_push_target is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_channel_robustness.py -v`
Expected: FAIL — push() 当前优先用 chat_id，has_push_target 属性不存在

- [ ] **Step 3: 改造 feishu_channel.py**

关键改动：
1. `push()` — 没有 ID 就不发，优先 open_id，chat_id fallback
2. `_on_reconnected` — 重新加载 preferences
3. 添加 `user_open_id`、`has_push_target`、`is_connected` 属性

```python
# niu_api/channel/feishu_channel.py — push() 改造

async def push(self, channel_id: str, content: str) -> None:
    """主动推送 — 没有 ID 就不发，优先 open_id"""
    target = channel_id or self._user_open_id or self._user_p2p_chat_id
    if not target:
        logger.warning("[FeishuChannel] No chat_id or open_id, skipping push")
        return
    try:
        result = await self.channel.send(target, {"markdown": content})
        if not result.success:
            fallback = None
            if target == self._user_open_id and self._user_p2p_chat_id:
                fallback = self._user_p2p_chat_id
            elif target == self._user_p2p_chat_id and self._user_open_id:
                fallback = self._user_open_id
            if fallback:
                logger.warning(f"[FeishuChannel] Push to {target} failed, retrying with {fallback}")
                try:
                    r2 = await self.channel.send(fallback, {"markdown": content})
                    if not r2.success:
                        logger.error(f"[FeishuChannel] Push to {fallback} also failed: {r2.error}")
                except Exception as e2:
                    logger.error(f"[FeishuChannel] Push to {fallback} exception: {e2}")
            else:
                logger.error(f"[FeishuChannel] Push failed: {result.error}")
    except Exception as e:
        logger.error(f"[FeishuChannel] Push exception: {e}")


# _on_reconnected 改造
def _on_reconnected(self, _=None):
    """WebSocket 重连成功 — 重新加载已保存的 ID"""
    logger.info("[FeishuChannel] WebSocket reconnected")
    self._feishu_prefs = self._load_prefs()
    self._apply_persisted_ids()


# 新增属性
@property
def user_open_id(self) -> str | None:
    return self._user_open_id

@property
def is_connected(self) -> bool:
    return self.channel.is_ready

@property
def has_push_target(self) -> bool:
    return bool(self._user_p2p_chat_id or self._user_open_id)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_channel_robustness.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/channel/feishu_channel.py tests/test_feishu_channel_robustness.py
git commit -m "feat: 飞书 ID 管理 — 没有 ID 不发、优先 open_id、重连加载、has_push_target"
```

---

## Task 7: 飞书消息处理简化 — 与 ChatQueue 集成

**Files:**
- Modify: `niu_api/channel/feishu_channel.py` (_on_message, _process_and_reply)

- [ ] **Step 1: 简化 _on_message，删除 _process_and_reply**

由于 `route_in_sync()` 现在只是入队（几乎不耗时），不再需要 `threading.Thread`。
同时删除 `_process_and_reply` 方法（新架构中不再被调用）。

```python
# niu_api/channel/feishu_channel.py — _on_message 简化

def _on_message(self, msg):
    """处理飞书消息事件（直接入队，不阻塞 SDK 线程）"""
    try:
        raw = msg.raw or {}
        if msg.chat_type and "chat_type" not in raw:
            raw = {**raw, "chat_type": msg.chat_type}

        unified = UnifiedMessage(
            content=msg.content_text or "",
            channel="feishu",
            channel_id=msg.chat_id,
            sender_id=msg.sender_id,
            message_type=msg.raw_content_type or "text",
            resources=msg.resources or [],
            raw=raw,
        )

        if not unified.content.strip() and not unified.resources:
            logger.debug("[FeishuChannel] Empty message with no resources, skipping")
            return

        is_p2p = self._is_p2p_message(msg)
        log_preview = unified.content[:50] if unified.content.strip() else f"[resources: {len(unified.resources)}]"
        logger.info(f"[FeishuChannel] Received: {log_preview}...")

        # P2P 消息：更新推送目标并持久化
        if is_p2p:
            self._update_persisted_ids(unified.channel_id, unified.sender_id)

        # 将 resources 转为文本描述
        resource_text = self._format_resources(unified.resources)
        if resource_text:
            message_content = f"{unified.content}\n{resource_text}" if unified.content.strip() else resource_text
        else:
            message_content = unified.content

        # P2P 用 sender_id，群聊用 chat_id — 区分 session 避免上下文混淆
        if is_p2p:
            session_id = f"feishu:{unified.sender_id}"
        else:
            session_id = f"feishu:group:{unified.channel_id}"

        # 直接入队（不再启动新线程，入队操作几乎不耗时）
        result = self.router.route_in_sync(unified, session_id=session_id, message_override=message_content)
        if result.queued:
            logger.info(f"[FeishuChannel] Message queued: {message_content[:50]}...")
        else:
            logger.warning(f"[FeishuChannel] Failed to queue: {result.message}")

    except Exception as e:
        logger.error(f"[FeishuChannel] Message handler error: {e}")

# 删除 _process_and_reply 方法 — 新架构中不再需要（入队后 ChatWorker 处理）
```

- [ ] **Step 2: 写测试 — _on_message 直接入队，P2P 消息更新 ID**

```python
# tests/test_chat_queue.py (追加)

from niu_api.chat_queue import EnqueueResult

def test_feishu_on_message_enqueues_directly():
    """_on_message 应直接调用 route_in_sync 入队，不启动新线程"""
    with patch("niu_api.channel.feishu_channel.FeishuChannelAdapter") as MockAdapter:
        adapter = MockAdapter.return_value
        adapter.router = MagicMock()
        adapter.router.route_in_sync.return_value = EnqueueResult(queued=True, message="")

        msg = MagicMock()
        msg.content_text = "hello"
        msg.chat_id = "chat_123"
        msg.sender_id = "user_456"
        msg.chat_type = "p2p"
        msg.raw = {}
        msg.raw_content_type = "text"
        msg.resources = []

        adapter._on_message(msg)

        # 验证 route_in_sync 被调用（入队），session_id 区分 P2P
        adapter.router.route_in_sync.assert_called_once()
        call_kwargs = adapter.router.route_in_sync.call_args
        assert "feishu:user_456" in str(call_kwargs)

def test_feishu_on_message_p2p_updates_ids():
    """P2P 消息应调用 _update_persisted_ids 更新推送目标"""
    with patch("niu_api.channel.feishu_channel.FeishuChannelAdapter") as MockAdapter:
        adapter = MockAdapter.return_value
        adapter.router = MagicMock()
        adapter.router.route_in_sync.return_value = EnqueueResult(queued=True, message="")
        adapter._update_persisted_ids = MagicMock()

        msg = MagicMock()
        msg.content_text = "hello"
        msg.chat_id = "chat_123"
        msg.sender_id = "user_456"
        msg.chat_type = "p2p"
        msg.raw = {}
        msg.raw_content_type = "text"
        msg.resources = []

        adapter._on_message(msg)

        # P2P 消息应更新 ID
        adapter._update_persisted_ids.assert_called_once_with("chat_123", "user_456")
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_feishu_on_message_enqueues_directly tests/test_chat_queue.py::test_feishu_on_message_p2p_updates_ids -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add niu_api/channel/feishu_channel.py tests/test_chat_queue.py
git commit -m "feat: 飞书消息处理简化 — 直接入队 ChatQueue，删除 _process_and_reply"
```

---

## Task 8: 前端解锁 — 去掉 isProcessing

**Files:**
- Modify: `ui/assistant/chat.html`

- [ ] **Step 1: 改造 sendMessage — 去掉 isProcessing，fire-and-forget 模式**

```javascript
// 替换 isProcessing 为引用计数
// let isProcessing = false;  // 旧
let activeRequestCount = 0;  // 新

// sendMessage 改造 — fire-and-forget
// /api/chat/session 是 enqueue 即返模式，前端不等待 HTTP 响应
// 回复通过 SSE new_message 事件 → refreshFromDB 到达
async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text) return;

    // 删除: if (isProcessing) return;

    if (text === '/new') { /* ... 保持不变 */ }

    addMessage('user', text);
    inputEl.value = '';
    inputEl.style.height = 'auto';

    activeRequestCount++;
    showTyping();

    // fire-and-forget：发送请求但不 await 结果
    // HTTP 响应中的 reply 不使用，回复通过 SSE 到达
    sendMessageWithRetry(text).catch(error => {
        addMessage('system', '连接失败，请检查服务是否启动');
    }).finally(() => {
        activeRequestCount = Math.max(0, activeRequestCount - 1);
        if (activeRequestCount === 0) {
            notifyBusy(false, 'chat');
        }
        loadStats();
        inputEl.focus();
    });
}
```

- [ ] **Step 2: 改造 refreshFromDB — typing 由 assistant 消息到达控制**

```javascript
// refreshFromDB 中检测到 assistant 消息时：
if (hasAssistantMessage) {
    hideTyping();
}
```

- [ ] **Step 3: 改造拖入文件/图片 — 去掉 isProcessing 检查**

删除所有 `if (isProcessing)` 检查和 `isProcessing = true/false` 赋值。

- [ ] **Step 4: 手动验证**

1. 发送消息 → Agent 回复正常
2. Agent 处理期间发送补充消息 → 不被拒绝
3. 飞书消息 → 回复推送到飞书

- [ ] **Step 5: 提交**

```bash
git add ui/assistant/chat.html
git commit -m "feat: 前端解锁 — 去掉 isProcessing，改用引用计数"
```

---

## Task 9: FastAPI 生命周期集成

**Files:**
- Modify: `niu_api/__main__.py` (在现有 lifespan 中添加 ChatQueue 启停)

**关键**：现有 `__main__.py` 已有完整的 lifespan 函数（包含 session store 初始化、embedding 预加载、scheduler 启动、MCP 工具加载、runner 初始化、channel router 注册、飞书通道启动等）。**不能替换**，只能在其中添加 ChatQueue 启停步骤。

- [ ] **Step 1: 在现有 lifespan 中添加 ChatQueue 启停**

```python
# niu_api/__main__.py — 在现有 lifespan 函数中添加 ChatQueue 启停

# 在 startup 部分，runner 初始化之后添加：
from niu_api.chat_queue import start_chat_queue, stop_chat_queue
await start_chat_queue()
logger.info("[Main] ChatQueue started")

# 在 shutdown 部分，scheduler 停止之前添加：
await stop_chat_queue()
logger.info("[Main] ChatQueue stopped")
```

- [ ] **Step 2: 写测试 — ChatQueue 随 lifespan 启停**

```python
# tests/test_chat_queue.py (追加)

@pytest.mark.asyncio
async def test_chat_queue_lifecycle():
    """ChatQueue 应随 FastAPI lifespan 启停"""
    from niu_api.chat_queue import get_chat_queue, _queue
    # start_chat_queue 应创建并启动 ChatQueue
    from niu_api.chat_queue import start_chat_queue, stop_chat_queue
    await start_chat_queue()
    q = get_chat_queue()
    assert q is not None

    await stop_chat_queue()
    # 停止后 get_chat_queue 应返回 None 或抛出异常
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_chat_queue_lifecycle -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add niu_api/__main__.py
git commit -m "feat: 在现有 lifespan 中集成 ChatQueue 启停"
```

---

## Task 10: /api/chat/clear 改造

**Files:**
- Modify: `niu_api/compat.py`
- Test: `tests/test_chat_queue.py` (追加)

- [ ] **Step 1: 写测试 — /api/chat/clear 使用 ChatQueue.drain()**

```python
# tests/test_chat_queue.py (追加)

@pytest.mark.asyncio
async def test_clear_uses_drain():
    """clear 端点应调用 ChatQueue.drain() 清空队列"""
    from niu_api.chat_queue import ChatQueue
    runner = MagicMock()
    q = ChatQueue(runner)

    # 入队一条消息
    result = await q.enqueue(content="test", source="frontend", session_id="default")
    assert result.queued

    # drain 应清空队列
    drained = await q.drain(timeout=5.0)
    assert drained
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_clear_uses_drain -v`
Expected: FAIL — clear 端点当前不使用 ChatQueue.drain()

- [ ] **Step 3: 改造 /api/chat/clear 端点**

```python
# niu_api/compat.py — /api/chat/clear 改造

@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """清空会话 — 等待当前 chat 完成后清空"""
    from niu_api.chat_queue import get_chat_queue
    q = get_chat_queue()

    drained = await q.drain(timeout=5.0)
    if not drained:
        return {"status": "error", "message": "当前有任务正在处理，请稍后再试"}

    store = await get_message_store()
    await store.clear_messages()

    runner = get_or_create_runner()
    runner.handler.reset_working_memory()

    return {"status": "ok"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_chat_queue.py::test_clear_uses_drain -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/compat.py tests/test_chat_queue.py
git commit -m "feat: /api/chat/clear 使用 ChatQueue.drain()"
```

---

## Task 11: 清理 — 删除废弃代码

**Files:**
- Modify: `niu_api/compat.py` (删除 _chat_lock)
- Modify: `niu_api/chat.py` (删除 _chat_lock 引用)

- [ ] **Step 1: 确认 _chat_lock 的所有引用已删除**

Run: `cd <repo_root> && grep -rn "_chat_lock" niu_api/`
Expected: 无结果

- [ ] **Step 2: 删除 ChannelRouter 中废弃的 _chat_sync 方法**

- [ ] **Step 3: 运行全量测试**

Run: `cd <repo_root> && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: 清理废弃代码 — 删除 _chat_lock 和 _chat_sync"
```

---

## 风险评估

### 高风险

1. **NiuRunner 共享状态**：ChatQueue 保证了串行调用 runner.chat()，与 _chat_lock 等价。`_processing` 标志 + `asyncio.Event` 用于 drain() 等待。

2. **前端 fire-and-forget + 即返端点**：前端 sendMessage 不等待 HTTP 响应，回复通过 SSE 到达。需要验证 main.js 的 IPC handler 不依赖 response.reply（当前依赖但新架构中不再需要——回复通过 SSE 到达 chat.html）。

3. **消息合并格式**：`[补充N] 内容` 格式需要 LLM 正确理解。这是常见的 prompt 补充格式。

4. **飞书 SDK 三层隔离**：WS loop / bg loop / FastAPI loop 完全隔离，runner.chat() 阻塞不影响飞书保活。

### 中风险

5. **Scheduler 超时**：`enqueue_and_wait` 超时 90s，与原有一致。drain() 期间 Scheduler 会收到 `[会话已清空]` 降级回复。

6. **SSE /chat 端点**：前端不使用此端点，改造为即返 + 心跳保活。如果未来需要使用，需验证 SSE 事件流。

7. **persist_agent_reply 提取**：从 chat.py 提取共享函数，需要确保现有端点也改用共享函数。

8. **session_id 区分**：飞书 P2P 用 `feishu:{sender_id}`，群聊用 `feishu:group:{channel_id}`，前端用 `default`。需要确认 runner.chat() 正确处理不同 session_id。

### 低风险

9. **引用计数**：前端 activeRequestCount 在异常情况下可能泄漏，但 typing 气泡最终由 refreshFromDB 中的 assistant 消息检测清除。

10. **飞书 ID 管理**：没有 ID 就不发，飞书消息来了自动记住，逻辑简单可靠。

11. **spirit.html**：spirit 界面依赖 result.reply，但计划未提及改造。spirit.html 不常用，可后续处理。

---

## 回滚方案

每个 Task 的提交独立，可单独 revert。关键回滚步骤：

1. 恢复 `_chat_lock` 定义和使用
2. 恢复 ChannelRouter 的 HTTP 自调用 /chat/sync
3. 恢复前端 `isProcessing` 逻辑
4. 恢复飞书通道的 `_on_message` 和 `_process_and_reply`
5. 删除 `chat_queue.py`
