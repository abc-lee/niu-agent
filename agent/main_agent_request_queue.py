"""主 Agent 请求内存队列。

存子 Agent 的 ask 请求和完成通知（content 格式 `[子名] 内容`），FIFO。
db_monitor 检测主 Agent 闲置时 pop 一条，推 SSE 触发前端调 /api/chat/session。

不写 db——只在主 Agent 闲置时由 db_monitor 推 SSE，前端触发后由后端 compat.py 写 user 消息到 db。
这样保证消息是 db 最后一条 user 消息，LLM 才会作为当前输入处理。

线程安全：queue.Queue 实现，多线程 push/pop 安全。
"""
import queue as _queue


class MainAgentRequestQueue:
    """全局内存队列，存子 Agent → 主 Agent 的请求（ask 或完成通知）。

    db_monitor 链路 A 消费此队列：
    1. 检测 _chat_lock.locked() == False（主 Agent 闲置）
    2. peek 队首，如果有消息：
       - 调 notify_new_message 推 SSE
       - pop 移除（推 SSE 成功后才 pop，避免推送失败丢消息）
    3. 前端收到 SSE → 调 /api/chat/session → 后端写 user 消息 + 调 LLM
    """

    def __init__(self):
        self._q: _queue.Queue[str] = _queue.Queue()

    def push(self, content: str) -> None:
        """推入一条请求。线程安全（queue.Queue.put_nowait）。"""
        self._q.put_nowait(content)

    def pop(self) -> str | None:
        """取出并移除队首。空队列返回 None，不阻塞。"""
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None

    def peek(self) -> str | None:
        """查看队首但不移除。空队列返回 None。

        db_monitor 检测主 Agent 闲时先 peek 决定是否推 SSE，
        推 SSE 成功后才 pop（避免推送失败丢消息）。
        """
        try:
            return self._q.queue[0]  # queue.Queue 内部 deque，访问 [0] 不移除
        except IndexError:
            return None

    def is_empty(self) -> bool:
        """队列是否为空。"""
        return self._q.empty()


# 全局单例
_main_agent_request_queue = MainAgentRequestQueue()


def get_main_agent_request_queue() -> MainAgentRequestQueue:
    """获取全局 MainAgentRequestQueue 单例。"""
    return _main_agent_request_queue
