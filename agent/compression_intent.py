# agent/compression_intent.py
"""主 Agent 压缩意图（统一压缩入口状态，spec 2026-09-06）。

所有压缩触发方（手工 /compact 拦截、发送前达线检查）只置位本意图；
唯一消费方 = agent_runner_loop 发送前检查（consume）。线程安全：
组装/HTTP 在事件循环线程、agent_loop 在 executor 线程，跨线程读写。

溢出（CONTEXT_OVERFLOW）不走本意图——模型已 400、无循环可消费，
保留 chat.fire_and_forget_compaction 独立纯机械压实（spec 触发口收敛）。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_requested: bool = False
_reason: str = ""


def request_compression(reason: str) -> None:
    """置位压缩意图。reason ∈ {"manual", "auto"}；可重复置位，后置覆盖 reason。"""
    global _requested, _reason
    with _lock:
        _requested = True
        _reason = reason


def consume_compression() -> tuple[bool, str]:
    """消费压缩意图：返回 (是否有意图, reason)，消费后清除。"""
    global _requested, _reason
    with _lock:
        r, reason = _requested, _reason
        _requested = False
        _reason = ""
        return r, reason


def peek_compression() -> bool:
    """只读是否有压缩意图（不消费）。"""
    with _lock:
        return _requested


def reset_compression_intent() -> None:
    """/new 清理面复位（clear_chat → reset_derived_state 调用）。"""
    global _requested, _reason
    with _lock:
        _requested = False
        _reason = ""
