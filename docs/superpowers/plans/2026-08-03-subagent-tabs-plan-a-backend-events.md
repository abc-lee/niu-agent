# 动态子 Agent 标签页 — 计划 A：后端事件通道

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让子 Agent 的工作过程事件（工具调用、thinking chain、文本输出）通过独立通道推送到前端，与主 Agent SSE 流完全隔离。

**Architecture:** 新建 SubagentEventBus 类（per-unique_name 队列路由 + ring buffer），在 handler 的 _is_subagent 跳过逻辑后调用独立推送函数，_run_agent_loop 的 StreamEvent 消费循环不再丢弃非 reply 类型。thinking chain 从 MockResponse.thinking 提取。subagent_started 事件通过主 Agent SSE 流推送（直接调 call_soon_threadsafe(_sync_broadcast, event)，不走 notify_new_message_sync）。

**Tech Stack:** Python, FastAPI, asyncio, loguru, threading

**设计文档:** `docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md` §4.1-4.2, §4.5

---

### Task 1: SubagentEventBus 类 + 独立 SSE 端点

**Files:**
- Create: `niu_api/internal/subagent_event_bus.py`
- Modify: `niu_api/chat.py` (新增 SSE 端点)
- Modify: `agent/subagent_registry.py` (unregister 中注入 close 回调)

**参考代码位置:**
- `niu_api/chat.py` L26: `_event_subscribers` 全局列表
- `niu_api/chat.py` L27: `_main_loop` 全局变量
- `niu_api/chat.py` L143-163: `notify_tool_status_sync` — call_soon_threadsafe(_sync_broadcast, event) 模式参考
- `niu_api/chat.py` L205-213: `_sync_broadcast(event)` 函数
- `niu_api/chat.py` L680-727: `/api/events/stream` 端点（含 headers: Cache-Control, Connection, X-Accel-Buffering）

- [ ] **Step 1: 创建 SubagentEventBus 类**

`niu_api/internal/subagent_event_bus.py`:

```python
"""子 Agent 独立事件总线。

per-unique_name 事件队列路由，与主 Agent SSE 流（_event_subscribers 全局广播）隔离。
复用 niu_api.chat._main_loop 引用做 call_soon_threadsafe 跨线程注入。

线程安全设计：
- _subscribers / _ring_buffers 只在主 asyncio loop 线程中操作（_subagent_broadcast 由
  call_soon_threadsafe 调度到主 loop 执行，subscribe/unsubscribe 是 async 函数也在主 loop 执行）。
- 不需要 asyncio.Lock（主 loop 单线程不会并发）。
- close() 的清理通过 call_soon_threadsafe 调度到主 loop 执行，避免跨线程竞争。
"""
import threading
from collections import deque
from loguru import logger

# 每个 unique_name → list[asyncio.Queue]（订阅者队列列表）
_subscribers: dict[str, list] = {}  # asyncio.Queue 运行时动态创建
# 每个 unique_name → deque(maxlen=100) 环形缓冲区（断线重连补发）
_ring_buffers: dict[str, deque] = {}
# 每个 unique_name → close epoch（防止 Timer 误删重新启动的同名子 Agent）
_close_epochs: dict[str, int] = {}
_epoch_lock = threading.Lock()
_epoch_counter = 0

_MAX_RING_BUFFER = 100


def _get_main_loop():
    """复用 niu_api.chat._main_loop 全局引用。"""
    from niu_api.chat import _main_loop
    return _main_loop


def notify_subagent_event_sync(unique_name: str, event_type: str, data: dict | None = None):
    """从同步线程推送子 Agent 事件到独立通道。

    与 notify_tool_status_sync (chat.py:143) 相同的 call_soon_threadsafe 模式。
    """
    loop = _get_main_loop()
    if loop is None or loop.is_closed():
        logger.debug(f"[SubagentEventBus] main loop not available, dropping {event_type} for {unique_name}")
        return
    event = {"type": event_type, "subagent_id": unique_name}
    if data:
        event.update(data)
    loop.call_soon_threadsafe(_subagent_broadcast, unique_name, event)


def _subagent_broadcast(unique_name: str, event: dict):
    """在 FastAPI 主循环中执行广播到该 unique_name 的所有订阅者。

    此函数由 call_soon_threadsafe 调度，在主 loop 中同步执行，
    与 subscribe/unsubscribe 不会并发（asyncio 单线程模型）。
    """
    # 写入 ring buffer
    if unique_name in _ring_buffers:
        _ring_buffers[unique_name].append(event)
    # 广播到所有订阅者队列
    subs = _subscribers.get(unique_name, [])
    for q in subs[:]:
        try:
            q.put_nowait(event)
        except Exception:
            pass


def pre_register(unique_name: str):
    """预注册 unique_name（在子 Agent 注册到 SubagentRegistry 时调用）。

    确保 has_subagent() 在 subagent_started 事件推送后立即返回 True，
    避免 SSE 端点 404 竞态。
    """
    if unique_name not in _ring_buffers:
        _ring_buffers[unique_name] = deque(maxlen=_MAX_RING_BUFFER)


async def subscribe(unique_name: str):
    """SSE 端点调用，返回该子 Agent 的事件队列。"""
    import asyncio
    if unique_name not in _subscribers:
        _subscribers[unique_name] = []
    if unique_name not in _ring_buffers:
        _ring_buffers[unique_name] = deque(maxlen=_MAX_RING_BUFFER)
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers[unique_name].append(q)
    # 补发 ring buffer 中的历史事件
    rb = _ring_buffers.get(unique_name, deque(maxlen=0))
    for evt in rb:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            break
    return q


async def unsubscribe(unique_name: str, q):
    """SSE 端点断开时调用。保留 ring buffer 供重连恢复（close() 负责最终清理）。"""
    subs = _subscribers.get(unique_name)
    if subs and q in subs:
        subs.remove(q)


def close(unique_name: str):
    """子 Agent 结束时调用，推送关闭事件并延迟清理。

    清理通过 call_soon_threadsafe 调度到主 loop 执行，避免跨线程竞争。
    用 epoch 防止 5 分钟内同名子 Agent 重新启动时误删新数据。
    _do_cleanup 接收 epoch 参数，在主 loop 中再次检查，消除 TOCTOU 竞态。
    """
    global _epoch_counter
    import threading
    with _epoch_lock:
        _epoch_counter += 1
        _close_epochs[unique_name] = _epoch_counter
        my_epoch = _epoch_counter

    # 推送关闭事件
    notify_subagent_event_sync(unique_name, "subagent_closed", {"unique_name": unique_name})

    # 延迟清理（5 分钟后，等窗口重开恢复）
    def _cleanup():
        # 检查 epoch 是否变化（同名子 Agent 重新启动会更新 epoch）
        if _close_epochs.get(unique_name) != my_epoch:
            return  # 已被新子 Agent 接管，不清理
        # 调度到主 loop 执行清理（避免跨线程操作 dict）
        loop = _get_main_loop()
        if loop is None or loop.is_closed():
            # loop 已关闭，没有 async 操作在进行，直接清理安全
            _subscribers.pop(unique_name, None)
            _ring_buffers.pop(unique_name, None)
            _close_epochs.pop(unique_name, None)
            return
        loop.call_soon_threadsafe(_do_cleanup, unique_name, my_epoch)

    timer = threading.Timer(300.0, _cleanup)
    timer.daemon = True
    timer.start()


def _do_cleanup(unique_name: str, my_epoch: int):
    """在主 loop 中执行清理。再次检查 epoch 防止 TOCTOU 竞态。"""
    if _close_epochs.get(unique_name) != my_epoch:
        return  # 在 Timer 检查和主 loop 执行之间，同名子 Agent 已重新启动
    _subscribers.pop(unique_name, None)
    _ring_buffers.pop(unique_name, None)
    _close_epochs.pop(unique_name, None)


def has_subagent(unique_name: str) -> bool:
    """检查 unique_name 是否在 EventBus 中（有订阅者或有 ring buffer）。"""
    return unique_name in _subscribers or unique_name in _ring_buffers
```

- [ ] **Step 2: 新增独立 SSE 端点（含 headers）**

在 `niu_api/chat.py` 中新增端点（参考 L680-727 的 `/api/events/stream` 模式，**必须包含相同 headers**）:

```python
from niu_api.internal.subagent_event_bus import subscribe, unsubscribe, has_subagent

@router.get("/api/subagents/{unique_name}/stream")
async def subagent_event_stream(unique_name: str):
    """子 Agent 独立 SSE 端点。"""
    if not has_subagent(unique_name):
        raise HTTPException(status_code=404, detail=f"Subagent {unique_name} not found")

    async def generate():
        q = await subscribe(unique_name)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await unsubscribe(unique_name, q)

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

- [ ] **Step 3: 在 SubagentRegistry 中注入 close 回调 + pre_register**

修改 `agent/subagent_registry.py`：

在 `register` 方法中（子 Agent 注册成功后）调 `pre_register`:
```python
# 在 register 方法末尾（return unique_name 之前）：
try:
    from niu_api.internal.subagent_event_bus import pre_register
    pre_register(unique_name)
except ImportError:
    pass  # niu_api 未启动时跳过
```

在 `unregister` 方法中注入 `close` 回调:
```python
@classmethod
def unregister(cls, unique_name: str):
    with cls._lock:
        cls._instances.pop(unique_name, None)
    # 通知 SubagentEventBus 子 Agent 结束
    try:
        from niu_api.internal.subagent_event_bus import close
        close(unique_name)
    except ImportError:
        pass  # niu_api 未启动时跳过
```

**注意 INTERCEPTED_SYNC 路径**：同步子 Agent `@niu-agent` 挂起时不调 `unregister`（state=waiting_for_answer），`close()` 不会被触发。需在 `_maybe_suspend_session`（`agent/subagent.py` L313-362）中推送 `subagent_suspended` 事件:

```python
# 在 _maybe_suspend_session 中设置 state=waiting_for_answer 之后：
try:
    from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
    notify_subagent_event_sync(unique_name, 'subagent_suspended', {})
except ImportError:
    pass
```

- [ ] **Step 4: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('niu_api/internal/subagent_event_bus.py').read()); print('OK')"
```

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/subagent_event_bus.py niu_api/chat.py agent/subagent_registry.py agent/subagent.py
git commit -m "feat: SubagentEventBus + independent SSE endpoint + pre_register + INTERCEPTED_SYNC suspend event"
```

---

### Task 2: handler 改造 — _subagent_unique_name 统一 + StreamEvent 转发 + notify_subagent_event

**Files:**
- Modify: `agent/handler.py` L469-490 (tool_before_callback / tool_after_callback)
- Modify: `agent/subagent.py` L280-294 (_run_agent_loop StreamEvent 消费循环), L841-878 (回复路径), L881-907 (异步路径), L909-952 (同步路径)

**参考代码位置:**
- `agent/handler.py` L457: `_is_subagent = False` 初始化
- `agent/handler.py` L469-479: `tool_before_callback` — `if getattr(self, '_is_subagent', False): return`
- `agent/handler.py` L481-490: `tool_after_callback`
- `agent/handler.py` L535: `_auto_generate_summary` 方法（确实存在，NiuHandler 的实例方法）
- `agent/subagent.py` L280-294: StreamEvent 消费循环
- `agent/subagent.py` L841-878: 回复路径（answer is not None），handler 是 `instance.suspended_handler`
- `agent/subagent.py` L884: 异步路径 `handler._subagent_unique_name = unique_name`
- `agent/subagent.py` L919: 同步路径 `handler._subagent_unique_name = unique_name`

- [ ] **Step 1: 回复路径补充 _subagent_unique_name**

在 `agent/subagent.py` 回复路径（L841-878 区域），**注意 handler 是 `instance.suspended_handler`**，在 _run_agent_loop 调用之前设置:
```python
instance.suspended_handler._subagent_unique_name = answer_unique_name
```
确保三路径都设置 `_subagent_unique_name`。

- [ ] **Step 2: 改造 tool_before_callback — 子 Agent 走独立推送**

`agent/handler.py` L469-479，将:
```python
if getattr(self, '_is_subagent', False):
    return
```
改为:
```python
if getattr(self, '_is_subagent', False):
    unique_name = getattr(self, '_subagent_unique_name', None)
    if unique_name:
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            short_name = tool_name.replace('chat-with-', '') if tool_name.startswith('chat-with-') else tool_name
            notify_subagent_event_sync(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'start'})
        except ImportError:
            pass  # niu_api 未启动
    return
```

- [ ] **Step 3: 改造 tool_after_callback — 子 Agent 走独立推送**

`agent/handler.py` L481-490，在 `_is_subagent` 分支加入子 Agent 推送:
```python
if getattr(self, '_is_subagent', False):
    unique_name = getattr(self, '_subagent_unique_name', None)
    if unique_name:
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            short_name = tool_name.replace('chat-with-', '') if tool_name.startswith('chat-with-') else tool_name
            summary = self._auto_generate_summary(tool_name, args, ret)
            notify_subagent_event_sync(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'end', 'summary': summary})
        except ImportError:
            pass
    return
```
注意：`_auto_generate_summary`（handler.py L535）是 NiuHandler 的实例方法，直接调 `self._auto_generate_summary(...)`，不需要 hasattr 检查。

- [ ] **Step 4: _run_agent_loop StreamEvent 消费循环 — 转发非 reply 类型**

`agent/subagent.py` L280-294，在 `if chunk.type == 'reply':` 分支之后、else 之前插入 elif:
```python
elif chunk.type in ('persist', 'system', 'tool_marker'):
    unique_name = getattr(handler, '_subagent_unique_name', None)
    if unique_name:
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            notify_subagent_event_sync(unique_name, chunk.type, {'content': chunk.content})
        except ImportError:
            pass
```
注意：用 `getattr(handler, '_subagent_unique_name', None)` 做防御性检查。

- [ ] **Step 5: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/handler.py').read()); ast.parse(open('agent/subagent.py').read()); print('OK')"
```

- [ ] **Step 6: 提交**

```bash
git add agent/handler.py agent/subagent.py
git commit -m "feat: handler subagent event push via SubagentEventBus + StreamEvent forwarding"
```

---

### Task 3: thinking chain 提取与推送

**Files:**
- Modify: `agent/generic/agent_loop.py` L771-773 (else/非 verbose 分支内，yield reply 之后)

**参考代码位置:**
- `agent/generic/agent_loop.py` L534: `def agent_runner_loop(client, ..., handler=None, ...)` — **模块级函数，没有 self，用 handler 参数**
- `agent/generic/agent_loop.py` L714-716: `verbose` 分支（不经过 reply yield，不适用）
- `agent/generic/agent_loop.py` L717-771: `else`（非 verbose）分支，`response = exhaust(response_gen)`，L771 `yield StreamEvent("reply", content)`
- `agent/generic/llmcore.py` L26-38: MockResponse 有 `self.thinking` 属性
- `agent/generic/litellm_adapter.py` L658-663: thinking=reasoning_content

- [ ] **Step 1: 在 agent_loop 中提取 thinking 并推送**

`agent/generic/agent_loop.py` L771 之后（**在 else/非 verbose 分支内**，`yield StreamEvent("reply", content)` 之后，`memory_context` 更新之前），新增:

```python
# 子 Agent thinking chain 推送（仅在非 verbose 分支内，verbose 分支不经过 reply yield）
if getattr(handler, '_is_subagent', False):
    unique_name = getattr(handler, '_subagent_unique_name', None)
    if unique_name and hasattr(response, 'thinking') and response.thinking:
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            notify_subagent_event_sync(unique_name, 'thinking_chain', {'content': response.thinking})
        except ImportError:
            pass
```

**关键**：用 `handler` 而不是 `self`（agent_runner_loop 是模块级函数，没有 self）。代码放在 else 分支内（L771 之后、L773 之前），verbose 分支不覆盖（verbose=False 时子 Agent 和主 Agent 都走 else 分支，subagent.py L271 和 runner.py 都用 verbose=False）。

- [ ] **Step 2: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "feat: extract and push thinking chain for subagents via handler param"
```

---

### Task 4: subagent_started 事件 + _dispatch_async_subagent 返回值改造

**Files:**
- Modify: `agent/subagent.py` L1165-1234 (_dispatch_async_subagent 所有 return 路径)
- Modify: `agent/handler.py` L1008-1016 (_call_subagent_gen 异步路径) + L1019 (同步路径)
- Modify: `tests/test_async_subagent_dispatch.py` L55 (适配元组返回值)
- Modify: `tests/test_integration_async_complete.py` L56 (适配元组返回值)
- Note: `tests/test_ask_main_agent_stop_deadlock.py` L68 也调了 _dispatch_async_subagent 但不接收返回值（丢弃），无需修改。

**参考代码位置:**
- `agent/subagent.py` L1206: `return '[错误] 主 asyncio loop 不可用...'`（错误路径 1）
- `agent/subagent.py` L1227: `return f'[错误] 派发异步子 Agent 失败：{e}'`（错误路径 2）
- `agent/subagent.py` L1228-1234: `return confirmation`（成功路径）
- `niu_api/chat.py` L143-163: `notify_tool_status_sync` — 直接 `loop.call_soon_threadsafe(_sync_broadcast, event)` 模式
- `niu_api/chat.py` L205-213: `_sync_broadcast(event)` 函数

- [ ] **Step 1: _dispatch_async_subagent 所有 return 路径统一返回元组**

同时更新函数签名类型注解：`-> str` 改为 `-> tuple[str | None, str]`。
`agent/subagent.py` 三个 return 路径改为:
```python
# L1206 错误路径 1：
return (None, '[错误] 主 asyncio loop 不可用，无法派发异步子 Agent')

# L1227 错误路径 2：
return (None, f'[错误] 派发异步子 Agent 失败：{e}')

# L1228-1234 成功路径：
return (unique_name, confirmation)
```

- [ ] **Step 2: _call_subagent_gen 异步路径解包 + 推送 subagent_started**

`agent/handler.py` L1008 附近，改为:
```python
unique_name, confirmation = _dispatch_async_subagent(
    agent_name=agent_name,
    task=task,
    llm_config=llm_config,
    mcp_client=self.mcp_client,
)
if unique_name is not None:
    # 推送 subagent_started 事件到主 Agent SSE 流
    # 不走 notify_new_message_sync（它需要 message_id），直接调 _sync_broadcast
    try:
        from niu_api.chat import _main_loop, _sync_broadcast
        if _main_loop and not _main_loop.is_closed():
            event = {
                'type': 'subagent_started',
                'unique_name': unique_name,
                'agent_name': agent_name,
                'is_sync': False,
            }
            _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except ImportError:
        pass
yield StreamEvent("tool_marker", f"[SubAgent] 异步派出：{confirmation[:100]}\n")
return StepOutcome({"status": "success", "result": confirmation}, next_prompt="")
```
如果 `unique_name is None`（错误），yield system 事件并返回 error:
```python
else:
    yield StreamEvent("system", confirmation)
    return StepOutcome({"status": "error", "msg": confirmation}, next_prompt="")
```

- [ ] **Step 3: 同步路径推送 subagent_started**

`agent/handler.py` L1019 附近，在 `call_subagent(...)` 之前:
```python
# 同步子 Agent：unique_name = agent_name
try:
    from niu_api.chat import _main_loop, _sync_broadcast
    if _main_loop and not _main_loop.is_closed():
        event = {
            'type': 'subagent_started',
            'unique_name': agent_name,
            'agent_name': agent_name,
            'is_sync': True,
        }
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
except ImportError:
    pass
```

- [ ] **Step 4: 适配现有测试**

`tests/test_async_subagent_dispatch.py` L55:
```python
# 原: result = _dispatch_async_subagent(...)
# 改: _, result = _dispatch_async_subagent(...)
```

`tests/test_integration_async_complete.py` L56:
```python
# 原: confirmation = _dispatch_async_subagent(...)
# 改: _, confirmation = _dispatch_async_subagent(...)
```

- [ ] **Step 5: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); ast.parse(open('agent/handler.py').read()); print('OK')"
```

- [ ] **Step 6: 提交**

```bash
git add agent/subagent.py agent/handler.py tests/test_async_subagent_dispatch.py tests/test_integration_async_complete.py
git commit -m "feat: subagent_started event via _sync_broadcast + tuple return + test adaptation"
```
