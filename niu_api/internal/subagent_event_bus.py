"""子 Agent 独立事件总线。

per-unique_name 事件队列路由，与主 Agent SSE 流（_event_subscribers 全局广播）隔离。
复用 niu_api.chat._main_loop 引用做 call_soon_threadsafe 跨线程注入。

线程安全设计：
- _subscribers / _ring_buffers 的广播读写由 call_soon_threadsafe 调度到主 loop 串行执行；
  pre_register 在 loop 不可用时直接写入（GIL 保护 dict 单次操作安全）。
- 不需要 asyncio.Lock（主 loop 单线程不会并发）。
- close() 的清理通过 call_soon_threadsafe 调度到主 loop 执行，避免跨线程竞争。
"""
import asyncio
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
    """子 Agent 结束时调用，推送关闭事件并延迟清理。

    清理通过 call_soon_threadsafe 调度到主 loop 执行，避免跨线程竞争。
    用 epoch 防止 5 分钟内同名子 Agent 重新启动时误删新数据。
    _do_cleanup 接收 epoch 参数，在主 loop 中再次检查，消除 TOCTOU 竞态。
    """
    global _epoch_counter
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
            # 用 _epoch_lock 保护 fallback 路径的 dict pop（跑在 Timer 线程）
            with _epoch_lock:
                if _close_epochs.get(unique_name) != my_epoch:
                    return
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


def is_closing(unique_name: str) -> bool:
    """检查 unique_name 是否已在 close 延迟清理窗口内（_close_epochs 有记录）。

    用于 handler finally 的 else 分支区分两种 instance is None 的场景：
    - 场景 2（register 前异常）：未 close 过，is_closing=False → 需要 close
    - 场景 1/8（call_subagent 完成后已 close）：is_closing=True → 不需要再 close
    """
    return unique_name in _close_epochs
