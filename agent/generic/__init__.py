r"""
GenericAgent Core - 完整移植自 E:\tools\GenericAgent

核心文件：
- agent_loop.py (99行) - 核心循环
- llmcore.py (835行) - LLM 抽象层

总计约 900 行核心代码。
"""

from .agent_loop import BaseHandler, StepOutcome, agent_runner_loop, exhaust
from .llmcore import (
    BaseSession,
    MockResponse,
    MockToolCall,
    ToolClient,
)

__all__ = [
    # agent_loop
    "StepOutcome",
    "BaseHandler",
    "agent_runner_loop",
    "exhaust",
    # llmcore
    "ToolClient",
    "MockResponse",
    "MockToolCall",
    "BaseSession",
]
