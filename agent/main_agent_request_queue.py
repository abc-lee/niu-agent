"""主 Agent 请求内存队列。

存子 Agent 的 ask 请求和完成通知（FIFO），content 两种格式与 type 字段对应：
- ask（子 Agent 提问需主 Agent 回复）：【子Agent提问·需回复】[unique_name]\n问题\n收到请回复
  （unique_name 同步路径=纯 agent_name，异步路径=agent_name-4位hex；
   文本由 _compose_ask_main_agent_message 拼装，db_monitor 链路 A 直通不拼装）
- notify（完成通知/告知）：[子名] 内容（如 [file-processor] 已完成）

db_monitor 检测主 Agent 闲置时 pop 一条，推 SSE 触发前端调 /api/chat/session。

不写 db——只在主 Agent 闲置时由 db_monitor 推 SSE，前端触发后由后端 compat.py 写 user 消息到 db。
这样保证消息是 db 最后一条 user 消息，LLM 才会作为当前输入处理。

线程安全：queue.Queue 实现，多线程 push/pop 安全。

type 字段（T2 兼容设计）：内部存 (content, msg_type) 元组——
- `push(content, msg_type="ask"|"notify")`，默认 "notify"（向后兼容既有调用）
- `peek()/pop()` 解包返回 content（字符串——既有消费方零破坏）
- 新增 `peek_type()/pop_type()` 读取 msg_type（db_monitor 链路 A 日志标注 + 测试断言）
"""
import queue as _queue

_ASK_TYPE = "ask"
_NOTIFY_TYPE = "notify"


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
        self._q: _queue.Queue[tuple[str, str]] = _queue.Queue()

    def push(self, content: str, msg_type: str = _NOTIFY_TYPE) -> None:
        """推入一条请求（content + msg_type 元组）。线程安全（queue.Queue.put_nowait）。

        msg_type: "ask"（子 Agent 提问需主 Agent 回复）或 "notify"（完成通知/告知，默认）。
        """
        self._q.put_nowait((content, msg_type))

    def _pop_item(self) -> tuple[str, str] | None:
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None

    def _peek_item(self) -> tuple[str, str] | None:
        try:
            return self._q.queue[0]  # queue.Queue 内部 deque，访问 [0] 不移除
        except IndexError:
            return None

    def pop(self) -> str | None:
        """取出并移除队首，返回 content（解包）。空队列返回 None，不阻塞。"""
        item = self._pop_item()
        return item[0] if item is not None else None

    def peek(self) -> str | None:
        """查看队首但不移除，返回 content（解包）。空队列返回 None。

        db_monitor 检测主 Agent 闲时先 peek 决定是否推 SSE，
        推 SSE 成功后才 pop（避免推送失败丢消息）。
        """
        item = self._peek_item()
        return item[0] if item is not None else None

    def pop_type(self) -> str | None:
        """取出并移除队首，返回 msg_type（"ask"/"notify"）。空队列返回 None，不阻塞。

        注意：pop_type 会移除队首——调用前若需保留消息请用 peek_type。
        """
        item = self._pop_item()
        return item[1] if item is not None else None

    def peek_type(self) -> str | None:
        """查看队首但不移除，返回 type（"ask"/"notify"）。空队列返回 None。"""
        item = self._peek_item()
        return item[1] if item is not None else None

    def is_empty(self) -> bool:
        """队列是否为空。"""
        return self._q.empty()


# 全局单例
_main_agent_request_queue = MainAgentRequestQueue()


def get_main_agent_request_queue() -> MainAgentRequestQueue:
    """获取全局 MainAgentRequestQueue 单例。"""
    return _main_agent_request_queue
