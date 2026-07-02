"""子 Agent 注册表（阶段一简化版）。

维护当前在跑的子 Agent（含同步和异步）。阶段一只用同步子 Agent（memory_context=None）。
双击停止按钮遍历此注册表批量推 /stop。

线程安全：register/unregister 用 threading.Lock 保护（read-modify-write 非原子）。
"""
import threading
import secrets
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class RunningSubagent:
    unique_name: str
    agent_type: str
    supplement_queue: Any  # SubagentSupplementQueue
    memory_context: Optional[Any] = None  # 阶段一同步子 Agent 为 None
    is_sync: bool = True  # 阶段一都是同步


class SubagentRegistry:
    _instances: dict = {}
    _lock = threading.Lock()

    @classmethod
    def _gen_unique_name(cls, agent_type: str) -> str:
        """生成 <agent_type>-<4位hex> 唯一名，碰撞重试。"""
        while True:
            suffix = secrets.token_hex(2)  # 4 位 hex
            name = f"{agent_type}-{suffix}"
            if name not in cls._instances:
                return name

    @classmethod
    def register(cls, agent_type: str, supplement_queue: Any,
                 memory_context: Optional[Any] = None,
                 is_sync: bool = True) -> str:
        with cls._lock:
            name = cls._gen_unique_name(agent_type)
            cls._instances[name] = RunningSubagent(
                unique_name=name,
                agent_type=agent_type,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
                is_sync=is_sync,
            )
            return name

    @classmethod
    def unregister(cls, unique_name: str) -> None:
        with cls._lock:
            cls._instances.pop(unique_name, None)

    @classmethod
    def get(cls, unique_name: str) -> Optional[RunningSubagent]:
        with cls._lock:
            return cls._instances.get(unique_name)

    @classmethod
    def list_running(cls) -> list:
        """返回副本，外部修改不影响内部。"""
        with cls._lock:
            return list(cls._instances.values())
