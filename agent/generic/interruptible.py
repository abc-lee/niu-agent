"""统一可中断执行器 — 主 Agent 停止立即返回的核心机制。

run_interruptibly(fn, stop_check, ...)：后台 daemon 线程执行 fn，前台轮询 stop_check。

设计语义（用户拍板）：
- stop 置位 → 放弃等待，立即返回 (False, None)——后台线程继续跑完，结果丢弃（"后台去运行好了，无所谓"）。
- fn 正常完成 → (True, result)。
- fn 抛异常 → 原样上抛（fn 已结束，completed=True 语义）。
- 与 _interruptible_iter（litellm_adapter.py）同构：后台执行 + 前台轮询，daemon 线程兜底。

线程模型：每次调用一个 daemon 线程（同 _interruptible_iter 模式）。线程创建开销 ~50-100µs，
每轮注入 2-3 次 + 每次工具调用，可接受。daemon 线程在解释器退出时不阻塞（对比
ThreadPoolExecutor 非 daemon 线程会在 atexit join 卡住——卡住的 future 会阻塞进程退出）。
"""
from __future__ import annotations

import queue as _queue
import threading as _threading
from typing import Any, Callable

from loguru import logger


def run_interruptibly(
    fn: Callable[[], Any],
    stop_check: Callable[[], bool],
    timeout: float = 0.2,
    args: tuple = (),
    kwargs: dict | None = None,
) -> tuple[bool, Any]:
    """后台线程执行 fn，前台轮询 stop_check。

    Args:
        fn: 要执行的同步函数（可阻塞）。
        stop_check: 停止谓词（主 Agent=全局 is_stop_requested；子 Agent=terminate 谓词）。
        timeout: 前台轮询间隔（秒）。默认 0.2——停止响应上限。
        args/kwargs: 透传给 fn。

    Returns:
        (True, result): fn 正常完成。
        (False, None): stop_check 置位，放弃等待（后台 daemon 线程继续执行，结果丢弃）。

    Raises:
        fn 的异常原样上抛。
    """
    q: _queue.Queue = _queue.Queue(maxsize=1)
    kw = kwargs or {}

    def _runner() -> None:
        try:
            q.put(("done", fn(*args, **kw)))
        except BaseException as e:  # noqa: BLE001 - 原样上抛（含 KeyboardInterrupt 转队列错误，可接受）
            q.put(("error", e))

    # R4-P1：启动前 stop 预检——stop 已在入口置位时不起后台线程（零开销跳过），
    # 覆盖所有 wrapper 入口的 stop 前置到达（注入 4 wrapper + T4 工具段全局受益），
    # 端到端停止收敛到单轮询周期 ~0.2s（无"起线程后 0.2s 才见 stop"的间隙浪费）。
    if stop_check():
        logger.info("[Interruptible] stop already set, skipping execution")
        return (False, None)
    t = _threading.Thread(target=_runner, daemon=True, name="interruptible-call")
    t.start()
    while True:
        try:
            kind, payload = q.get(timeout=timeout)
        except _queue.Empty:
            if stop_check():
                logger.info("[Interruptible] stop detected, abandoning wait (background thread continues)")
                return (False, None)
            continue
        if kind == "error":
            raise payload
        return (True, payload)
