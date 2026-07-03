"""子 Agent 内存上下文 — 进度数据来源。

子 Agent 跑的时候，每轮 LLM 调用前后更新这个对象。
主 Agent 调 check_subagent_progress 时通过 snapshot() 一次性拷贝读一致状态。
内存对象不进 db，子 Agent 结束后随注册表移除而消失。
"""
import threading
from typing import Optional


class SubagentMemoryContext:
    """子 Agent 最近一轮 LLM 对话的内存对象。

    用普通类而非 @dataclass——threading.Lock 与 dataclass 的 __eq__/__hash__ 语义冲突，
    且 _lock 不应是 dataclass 字段（避免 asdict/astuple 包含锁）。

    Fields:
        last_llm_request: 最近一轮送给 LLM 的内容摘要（最后一条 user content 或 messages 拼接摘要）
        last_llm_response: LLM 最近一轮的回复文本（不含工具调用）
        current_turn: 当前第几轮（从 1 开始）
        last_tool_name: 最近一次调的工具名（可选辅助信息）
    """

    def __init__(self):
        self.last_llm_request: Optional[str] = None
        self.last_llm_response: Optional[str] = None
        self.current_turn: int = 0
        self.last_tool_name: Optional[str] = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        """一次性拷贝所有字段，保证主 Agent 读到一致状态。"""
        with self._lock:
            return {
                "last_llm_request": self.last_llm_request,
                "last_llm_response": self.last_llm_response,
                "current_turn": self.current_turn,
                "last_tool_name": self.last_tool_name,
            }

    def update(self, **kwargs) -> None:
        """子 Agent 线程更新字段，加锁保证一致性。"""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k) and not k.startswith("_"):
                    setattr(self, k, v)
