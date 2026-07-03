"""子 Agent 注册表。

维护当前在跑的子 Agent（含同步和异步）。
- 同步子 Agent：is_sync=True，task=None，memory_context=None（阶段一已有）
- 异步子 Agent：is_sync=False，task=asyncio.Task 或 concurrent.futures.Future，memory_context=SubagentMemoryContext（阶段二新增）

双击停止按钮遍历此注册表批量推 /stop。
db_monitor 路由 @子名 消息时从此注册表拿 supplement_queue。

线程安全：register/unregister 用 threading.Lock 保护（read-modify-write 非原子）。
"""
import threading
import secrets
import asyncio
import time
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Optional, Any, Union


@dataclass
class RunningSubagent:
    unique_name: str
    agent_type: str
    supplement_queue: Any  # SubagentSupplementQueue
    memory_context: Optional[Any] = None  # 异步子 Agent 才有，同步为 None
    is_sync: bool = True
    # task 字段：异步子 Agent 的可取消句柄
    # 用 run_coroutine_threadsafe 跨线程调度时返回 concurrent.futures.Future（不是 asyncio.Task）
    # 两者都有 cancel() 方法，类型用 Union 兼容
    task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None  # 异步子 Agent 才有，同步为 None
    started_at: float = field(default_factory=time.time)  # 启动时间，用于动态注入区排序


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
    def register(
        cls,
        agent_type: str,
        supplement_queue: Any,
        memory_context: Optional[Any] = None,
        is_sync: bool = True,
        task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None,
    ) -> str:
        """注册一个子 Agent，返回唯一名。

        同步子 Agent：is_sync=True，task=None，memory_context=None
        异步子 Agent：is_sync=False，task=asyncio.Task 或 concurrent.futures.Future，memory_context=SubagentMemoryContext
        """
        with cls._lock:
            name = cls._gen_unique_name(agent_type)
            cls._instances[name] = RunningSubagent(
                unique_name=name,
                agent_type=agent_type,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
                is_sync=is_sync,
                task=task,
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
