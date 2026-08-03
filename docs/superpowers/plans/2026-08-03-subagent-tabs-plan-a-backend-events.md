# 动态子 Agent 标签页 — 计划 A：后端事件通道

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让子 Agent 的工作过程事件（工具调用、thinking chain、文本输出）通过独立通道推送到前端，与主 Agent SSE 流完全隔离。

**Architecture:** 新建 SubagentEventBus 类（per-unique_name 队列路由 + ring buffer），在 handler 的 _is_subagent 跳过逻辑后调用独立推送函数，_run_agent_loop 的 StreamEvent 消费循环不再丢弃非 reply 类型。thinking chain 从 MockResponse.thinking 提取。subagent_started 事件通过主 Agent SSE 流推送（新增事件类型，不改动现有逻辑）。

**Tech Stack:** Python, FastAPI, asyncio, loguru, threading

**设计文档:** `docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md` §4.1-4.2, §4.5, §4.6(thinking chain 部分)

---

### Task 1: SubagentEventBus 类 + 独立 SSE 端点

**Files:**
- Create: `niu_api/internal/subagent_event_bus.py`
- Modify: `niu_api/chat.py` (新增 SSE 端点)
- Modify: `niu_api/__main__.py` (include router 如果新建的话，否则直接在 chat.py 加端点)

**参考代码位置:**
- `niu_api/chat.py` L26: `_event_subscribers` 全局列表
- `niu_api/chat.py` L27: `_main_loop` 全局变量
- `niu_api/chat.py` L44-47: `set_main_event_loop(loop)` 函数
- `niu_api/chat.py` L205-213: `_sync_broadcast(event)` 函数
- `niu_api/chat.py` L680-727: `/api/events/stream` 端点实现（StreamingResponse + async generator + 30s 心跳）

- [ ] **Step 1: 创建 SubagentEventBus 类**

`niu_api/internal/subagent_event_bus.py`:

```python
"""子 Agent 独立事件总线。

per-unique_name 事件队列路由，与主 Agent SSE 流（_event_subscribers 全局广播）隔离。
复用 niu_api.chat._main_loop 引用做 call_soon_threadsafe 跨线程注入。
"""
import asyncio
from collections import deque
from loguru import logger

# 每个 unique_name → (list[asyncio.Queue], deque(maxlen=100) ring buffer)
_subscribers: dict[str, list[asyncio.Queue]] = {}
_ring_buffers: dict[str, deque] = {}
_lock = asyncio.Lock()

_MAX_RING_BUFFER = 100


def _get_main_loop():
    """复用 niu_api.chat._main_loop 全局引用。"""
    from niu_api.chat import _main_loop
    return _main_loop


def notify_subagent_event_sync(unique_name: str, event_type: str, data: dict | None = None):
    """从同步线程推送子 Agent 事件到独立通道。

    与 notify_tool_status_sync 相同的 call_soon_threadsafe 模式。
    """
    loop = _get_main_loop()
    if loop is None:
        return
    event = {"type": event_type, "subagent_id": unique_name}
    if data:
        event.update(data)
    loop.call_soon_threadsafe(_subagent_broadcast, unique_name, event)


def _subagent_broadcast(unique_name: str, event: dict):
    """在 FastAPI 主循环中执行广播到该 unique_name 的所有订阅者。"""
    # 写入 ring buffer
    if unique_name in _ring_buffers:
        _ring_buffers[unique_name].append(event)
    # 广播到所有订阅者队列
    subs = _subscribers.get(unique_name, [])
    for q in subs[:]:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def subscribe(unique_name: str) -> asyncio.Queue:
    """SSE 端点调用，返回该子 Agent 的事件队列。"""
    async with _lock:
        if unique_name not in _subscribers:
            _subscribers[unique_name] = []
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


async def unsubscribe(unique_name: str, q: asyncio.Queue):
    """SSE 端点断开时调用。"""
    async with _lock:
        subs = _subscribers.get(unique_name)
        if subs and q in subs:
            subs.remove(q)
        if subs is not None and len(subs) == 0:
            # 无订阅者时保留 ring buffer 一段时间（窗口重开恢复）
            # 不立即删除，等 close() 调用
            pass


def close(unique_name: str):
    """子 Agent 结束时调用，推送 close 事件并清理。"""
    # 推送关闭事件
    notify_subagent_event_sync(unique_name, "subagent_closed", {"unique_name": unique_name})
    # 延迟清理 ring buffer（5 分钟后，等窗口重开恢复）
    import threading
    def _cleanup():
        _subscribers.pop(unique_name, None)
        _ring_buffers.pop(unique_name, None)
    timer = threading.Timer(300.0, _cleanup)
    timer.daemon = True
    timer.start()


def has_subagent(unique_name: str) -> bool:
    """检查 unique_name 是否在 EventBus 中（有订阅者或有 ring buffer）。"""
    return unique_name in _subscribers or unique_name in _ring_buffers
```

- [ ] **Step 2: 新增独立 SSE 端点**

在 `niu_api/chat.py` 中新增端点（参考 L680-727 的 `/api/events/stream` 模式）:

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

    return StreamingResponse(generate(), media_type="text/event-stream")
```

- [ ] **Step 3: 在 SubagentRegistry.unregister 中注入 close 回调**

修改 `agent/subagent_registry.py` L96-98:

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
        pass  # niu_api 未启动时（纯 agent 模式）跳过
```

- [ ] **Step 4: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('niu_api/internal/subagent_event_bus.py').read()); print('OK')"
```

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/subagent_event_bus.py niu_api/chat.py agent/subagent_registry.py
git commit -m "feat: SubagentEventBus + independent SSE endpoint for subagent events"
```

---

### Task 2: handler 改造 — _subagent_unique_name 统一 + StreamEvent 转发 + notify_subagent_event

**Files:**
- Modify: `agent/handler.py` L469-490 (tool_before_callback / tool_after_callback)
- Modify: `agent/subagent.py` L280-294 (_run_agent_loop StreamEvent 消费循环), L841-878 (回复路径 unique_name), L881-907 (异步路径), L909-952 (同步路径)

**参考代码位置:**
- `agent/handler.py` L457: `_is_subagent = False` 初始化
- `agent/handler.py` L469-479: `tool_before_callback` — `if getattr(self, '_is_subagent', False): return`
- `agent/handler.py` L481-490: `tool_after_callback` — `if not getattr(self, '_is_subagent', False): notify_tool_status_sync(...)`
- `agent/subagent.py` L280-294: StreamEvent 消费循环，非 reply 类型被忽略
- `agent/subagent.py` L884: 异步路径 `handler._subagent_unique_name = unique_name`
- `agent/subagent.py` L919: 同步路径 `handler._subagent_unique_name = unique_name`
- `agent/subagent.py` L841-878: 回复路径（answer is not None），**未设置** `_subagent_unique_name`

- [ ] **Step 1: 回复路径补充 _subagent_unique_name**

在 `agent/subagent.py` 回复路径（L841-878 区域），在 _run_agent_loop 调用之前设置:
```python
handler._subagent_unique_name = answer_unique_name  # 回复路径补设
```
确保三路径都设置 handler._subagent_unique_name。

- [ ] **Step 2: 改造 tool_before_callback — 子 Agent 走独立推送**

`agent/handler.py` L469-479，将:
```python
def tool_before_callback(self, tool_name, args, response):
    if getattr(self, '_is_subagent', False):
        return
    # ... 主 Agent 推送逻辑
```
改为:
```python
def tool_before_callback(self, tool_name, args, response):
    if getattr(self, '_is_subagent', False):
        # 子 Agent 走独立 EventBus 推送
        unique_name = getattr(self, '_subagent_unique_name', None)
        if unique_name:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            short_name = tool_name.replace('chat-with-', '') if tool_name.startswith('chat-with-') else tool_name
            notify_subagent_event_sync(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'start'})
        return
    # ... 主 Agent 推送逻辑（不变）
```

- [ ] **Step 3: 改造 tool_after_callback — 子 Agent 走独立推送**

`agent/handler.py` L481-490，将 `if not getattr(self, '_is_subagent', False):` 的 else 分支加入子 Agent 推送:
```python
def tool_after_callback(self, tool_name, args, response, ret):
    if getattr(self, '_is_subagent', False):
        unique_name = getattr(self, '_subagent_unique_name', None)
        if unique_name:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            short_name = tool_name.replace('chat-with-', '') if tool_name.startswith('chat-with-') else tool_name
            summary = self._auto_generate_summary(tool_name, args, ret) if hasattr(self, '_auto_generate_summary') else ''
            notify_subagent_event_sync(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'end', 'summary': summary})
        return
    # ... 主 Agent 推送逻辑（不变）
```

- [ ] **Step 4: _run_agent_loop StreamEvent 消费循环 — 转发非 reply 类型**

`agent/subagent.py` L280-294，在 `# 忽略 persist/system/tool_marker` 注释处改为转发:
```python
# 原: 忽略 persist/system/tool_marker
# 新: 转发到 SubagentEventBus
elif chunk.type in ('persist', 'system', 'tool_marker'):
    unique_name = handler._subagent_unique_name if hasattr(handler, '_subagent_unique_name') else None
    if unique_name:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, chunk.type, {'content': chunk.content})
```

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
- Modify: `agent/generic/agent_loop.py` L717-771 (LLM 响应处理段)

**参考代码位置:**
- `agent/generic/agent_loop.py` L713-718: `response = exhaust(response_gen)` 拿到 MockResponse
- `agent/generic/agent_loop.py` L717-771: 响应处理，只读 `.content` 和 `.tool_calls`
- `agent/generic/llmcore.py` L26-38: MockResponse 有 `self.thinking` 属性
- `agent/generic/litellm_adapter.py` L658-663: thinking=reasoning_content 从流式响应提取
- `agent/thinking_chain.py` L84-119: `extract_thinking_from_content_blocks` 函数（从未被调用）

- [ ] **Step 1: 在 agent_loop 中提取 thinking 并推送**

`agent/generic/agent_loop.py` L771 附近（yield reply 之后），新增:
```python
# 子 Agent thinking chain 推送
if hasattr(self, '_is_subagent') and self._is_subagent:
    unique_name = getattr(self, '_subagent_unique_name', None)
    if unique_name and hasattr(response, 'thinking') and response.thinking:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'thinking_chain', {'content': response.thinking})
```

注意：`self` 在 agent_loop 中是 handler 实例（通过闭包或参数传入），需确认 handler 引用可用。如果 agent_loop 中没有 handler 引用，则在 `_run_agent_loop` 的 StreamEvent 消费循环中（subagent.py L280-294）新增一个 thinking 检查点。

- [ ] **Step 2: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "feat: extract and push thinking chain for subagents"
```

---

### Task 4: subagent_started 事件 + _dispatch_async_subagent 返回值改造

**Files:**
- Modify: `agent/subagent.py` L1165-1234 (_dispatch_async_subagent 返回值)
- Modify: `agent/handler.py` L1008-1016 (_call_subagent_gen 异步路径)
- Modify: `agent/handler.py` L1019 (同步路径 subagent_started)

**参考代码位置:**
- `agent/subagent.py` L1228-1234: `_dispatch_async_subagent` 返回纯文本 str
- `agent/handler.py` L1008: `confirmation = _dispatch_async_subagent(...)` 拿到 str
- `agent/handler.py` L1019: 同步路径 `call_subagent(...)`
- `niu_api/chat.py` L84-116: `notify_new_message_sync` — 主 Agent SSE 推送模式参考

- [ ] **Step 1: _dispatch_async_subagent 返回 (unique_name, confirmation_text) 元组**

`agent/subagent.py` L1228 附近，将 `return confirmation` 改为 `return (unique_name, confirmation)`。

- [ ] **Step 2: _call_subagent_gen 异步路径解包并推送 subagent_started**

`agent/handler.py` L1008 附近，将:
```python
confirmation = _dispatch_async_subagent(...)
```
改为:
```python
unique_name, confirmation = _dispatch_async_subagent(...)
# 推送 subagent_started 事件到主 Agent SSE 流
from niu_api.chat import notify_new_message_sync
notify_new_message_sync(
    role='subagent_started',
    content=json.dumps({'unique_name': unique_name, 'agent_name': agent_name, 'is_sync': False}),
    source='electron'
)
```

- [ ] **Step 3: 同步路径推送 subagent_started**

`agent/handler.py` L1019 附近，在 `call_subagent(...)` 之前:
```python
# 同步子 Agent：unique_name = agent_name
from niu_api.chat import notify_new_message_sync
notify_new_message_sync(
    role='subagent_started',
    content=json.dumps({'unique_name': agent_name, 'agent_name': agent_name, 'is_sync': True}),
    source='electron'
)
```

- [ ] **Step 4: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); ast.parse(open('agent/handler.py').read()); print('OK')"
```

- [ ] **Step 5: 提交**

```bash
git add agent/subagent.py agent/handler.py
git commit -m "feat: subagent_started event + _dispatch_async_subagent returns tuple"
```
