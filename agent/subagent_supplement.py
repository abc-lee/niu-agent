"""子 Agent 独立 supplement queue。

每个子 Agent 实例一个，线程安全（queue.Queue）。db 监测程序按 unique_name 路由写入。
与主 Agent 的全局 _supplement_queue（agent/runner.py:44）隔离，避免主子串话。
"""
import queue as _queue
from dataclasses import dataclass


@dataclass
class SubagentSupplementItem:
    """supplement 队列里的一项。"""
    content: str
    is_terminate: bool  # /stop 标记
    sender: str          # 发送者名（如 "主Agent"）


class SubagentSupplementQueue:
    """每个子 Agent 实例一个的 supplement queue，线程安全。

    db 监测程序（主 loop）put_nowait，子 Agent（asyncio.to_thread 线程）drain。
    """

    def __init__(self, unique_name: str):
        self.unique_name = unique_name
        self._q = _queue.Queue()

    def push(self, content: str, is_terminate: bool = False, sender: str = "主Agent") -> None:
        """推入一项。线程安全（queue.Queue.put_nowait）。"""
        self._q.put_nowait(SubagentSupplementItem(content, is_terminate, sender))

    def drain(self) -> list:
        """取出全部并清空。线程安全。"""
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return items
