"""子 Agent 独立事件总线。

per-unique_name 事件队列路由，与主 Agent SSE 流（_event_subscribers 全局广播）隔离。
复用 niu_api.chat._main_loop 引用做 call_soon_threadsafe 跨线程注入。

线程安全设计：
- _subscribers / _ring_buffers / _closed 的广播读写由 call_soon_threadsafe 调度到主 loop 串行执行；
  pre_register 在 loop 不可用时直接写入（GIL 保护 dict 单次操作安全）。
- 不需要 asyncio.Lock（主 loop 单线程不会并发）。
- close() 立即清理（不延迟）：推送 subagent_closed 事件后通过 call_soon_threadsafe 调度 _do_cleanup
  到主 loop 执行，避免跨线程竞争。_closed set 防止双重 close。
"""
import asyncio
from collections import deque

from loguru import logger

# 每个 unique_name → list[asyncio.Queue]（订阅者队列列表）
_subscribers: dict[str, list] = {}  # asyncio.Queue 运行时动态创建
# 每个 unique_name → deque(maxlen=100) 环形缓冲区（断线重连补发）
_ring_buffers: dict[str, deque] = {}
# 已 close 的 unique_name 集合（防止双重 close；pre_register 清除以允许第二轮复用）
_closed: set[str] = set()

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
        except asyncio.QueueFull:
            logger.warning(f"[SubagentEventBus] {unique_name} subscriber queue full, skipping")
        except Exception:
            logger.exception(f"[SubagentEventBus] {unique_name} broadcast error")


def pre_register(unique_name: str):
    """预注册 unique_name（在子 Agent 注册到 SubagentRegistry 时调用）。

    确保 has_subagent() 在 subagent_started 事件推送后立即返回 True，
    避免 SSE 端点 404 竞态。

    线程安全：与 notify_subagent_event_sync 一致，用 call_soon_threadsafe
    调度到主 loop 执行。loop 不可用时直接写入（GIL 保护 dict 单次操作安全）。
    """
    _closed.discard(unique_name)  # 清除上一轮残留的 close 标记
    loop = _get_main_loop()
    if loop is None or loop.is_closed():
        if unique_name not in _ring_buffers:
            _ring_buffers[unique_name] = deque(maxlen=_MAX_RING_BUFFER)
        return
    loop.call_soon_threadsafe(_do_pre_register, unique_name)


def _do_pre_register(unique_name: str):
    """在主 loop 中执行预注册。"""
    if unique_name not in _ring_buffers:
        _ring_buffers[unique_name] = deque(maxlen=_MAX_RING_BUFFER)


async def subscribe(unique_name: str):
    """SSE 端点调用，返回该子 Agent 的事件队列。"""
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
    """子 Agent 结束时调用，推送关闭事件并立即清理。

    用 _closed set 防止双重 close（场景 1/8 正常完成时 call_subagent 内部已 close）。
    清理通过 call_soon_threadsafe 调度到主 loop 执行，避免跨线程竞争。
    """
    if unique_name in _closed:
        return
    _closed.add(unique_name)
    notify_subagent_event_sync(unique_name, "subagent_closed", {"unique_name": unique_name})
    loop = _get_main_loop()
    if loop is None or loop.is_closed():
        _subscribers.pop(unique_name, None)
        _ring_buffers.pop(unique_name, None)
        _closed.discard(unique_name)
        return
    loop.call_soon_threadsafe(_do_cleanup, unique_name)


def _do_cleanup(unique_name: str):
    """在主 loop 中执行清理。"""
    _subscribers.pop(unique_name, None)
    _ring_buffers.pop(unique_name, None)
    _closed.discard(unique_name)


def has_subagent(unique_name: str) -> bool:
    """检查子 Agent 是否活跃（有订阅者或有 ring buffer 且未 close）。"""
    if unique_name in _closed:
        return False
    return unique_name in _subscribers or unique_name in _ring_buffers
