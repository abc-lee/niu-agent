"""子 Agent 问主 Agent 的阻塞与回答路由机制。

子 Agent 调 ask_main_agent 工具时：
  1. 创建 AskMainAgentFuture（threading.Event + answer 共享变量）
  2. 注册到 PendingAskRegistry（key=unique_name）
  3. 推 "[unique_name] question" 到 MainAgentRequestQueue 内存队列（不写 db）
  4. future.wait() 阻塞，直到主 Agent 回答或被 cancel

db_monitor 链路 A 检测主 Agent 闲置时消费 MainAgentRequestQueue：
  - 推 SSE 触发前端调 /api/chat/session
  - 后端写 user 消息到 db（content="[子名] question"，作为最后一条 user 消息触发主 Agent 新一轮 LLM）
  - 主 Agent 回复 @子名 回答，persist_agent_reply 以 role=subagent_msg 写 db

db_monitor 链路 B 轮询到主 Agent 回答消息（@子名 role=subagent_msg）时：
  - PendingAskRegistry.set_answer(子名, 回答) — 解除 ask_main_agent 阻塞

主 Agent 发 /stop 给子 Agent 时（@子名 /stop）：
  - db_monitor 链路 B 推 /stop 到子 Agent supplement queue（is_terminate=True）
  - 同时 PendingAskRegistry.cancel_pending_ask(子名) — 解除 ask_main_agent 阻塞（避免死锁）
"""
import logging
import threading

logger = logging.getLogger(__name__)


# 终止信号 — cancel_pending_ask 设此值，ask_main_agent 工具识别后返回终止状态
TERMINATED_SIGNAL = "__TERMINATED__"


class AskMainAgentFuture:
    """子 Agent 问主 Agent 的一次阻塞等待。

    线程安全：Event + answer 共享变量。子 Agent 跑在 asyncio.to_thread 独立线程，
    db_monitor 跑在主 asyncio loop（route_message 是同步函数），跨线程用 Event.set() 安全。
    """

    def __init__(self):
        self._event = threading.Event()
        self._answer: str | None = None

    def set_answer(self, answer: str) -> None:
        """主 Agent 回答路由来时调，解除阻塞。"""
        self._answer = answer
        self._event.set()

    def wait(self, timeout: float | None = None) -> str | None:
        """阻塞等待回答。超时返回 None；被 cancel 返回 TERMINATED_SIGNAL。"""
        self._event.wait(timeout=timeout)
        return self._answer


class PendingAskRegistry:
    """按 unique_name 路由 ask_main_agent 回答的注册表。

    同一子 Agent 同时只有一个 Future 在等（ask_main_agent 阻塞子 Agent 循环），
    所以按 unique_name 路由唯一且简单。
    """

    def __init__(self):
        self._futures: dict[str, AskMainAgentFuture] = {}
        self._lock = threading.Lock()

    def register(self, unique_name: str) -> AskMainAgentFuture:
        """子 Agent 调 ask_main_agent 时注册一个 future。

        如果该 unique_name 已有 future（前一次 ask 未解除就再问，不应发生但容错），
        旧 future 设 TERMINATED_SIGNAL 解除阻塞，避免泄漏。
        """
        future = AskMainAgentFuture()
        with self._lock:
            old = self._futures.get(unique_name)
            if old is not None:
                old.set_answer(TERMINATED_SIGNAL)
            self._futures[unique_name] = future
        return future

    def set_answer(self, unique_name: str, answer: str) -> bool:
        """主 Agent 回答路由来时调。返回是否找到 future。

        找到 future → set_answer 解除阻塞，从注册表移除。
        找不到（孤儿回答：子 Agent 崩溃/超时后主 Agent 才回答）→ 返回 False + 日志，
        调用方（db_monitor）决定降级处理（推 supplement queue 让子 Agent 下一轮看到，
        或子 Agent 已退出则推回主 Agent）。
        """
        with self._lock:
            future = self._futures.pop(unique_name, None)
        if future is None:
            logger.warning(f"PendingAskRegistry.set_answer: 找不到 {unique_name} 的 future（可能被 cancel 或超时先 pop），回答降级处理")
            return False
        future.set_answer(answer)
        return True

    def cancel_pending_ask(self, unique_name: str) -> None:
        """主 Agent 发 /stop 时调，解除 ask_main_agent 阻塞避免死锁。

        找到 future → set_answer(TERMINATED_SIGNAL)，ask_main_agent 工具识别后返回终止状态。
        找不到（子 Agent 没在问主，或 /stop 在检查与 register 之间到达）→ 设置 instance._ask_terminated
        标记（如果 instance 存在），让后续任何 ask_main_agent 调用立即短路返回 terminated，
        避免 /stop 在 _ask_main_agent_impl 检查标记与 register 之间到达导致子 Agent 阻塞满 300s 超时。
        """
        with self._lock:
            future = self._futures.pop(unique_name, None)
        if future is not None:
            future.set_answer(TERMINATED_SIGNAL)
        else:
            # future 不存在（子 Agent 没在问主，或 /stop 在检查与 register 之间到达）
            # 设置 instance._ask_terminated 标记，让后续 ask_main_agent 立即短路
            try:
                from .subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(unique_name)
                if instance is not None:
                    instance._ask_terminated = True
            except Exception:
                pass  # 标记设置失败不影响主流程

    def unregister(self, unique_name: str) -> None:
        """子 Agent 结束时调（正常/异常/终止），清理未解除的 future。"""
        with self._lock:
            self._futures.pop(unique_name, None)


# 全局单例 — db_monitor 和 ask_main_agent 工具共用
_pending_ask_registry = PendingAskRegistry()


def get_pending_ask_registry() -> PendingAskRegistry:
    """获取全局 PendingAskRegistry 单例。"""
    return _pending_ask_registry
