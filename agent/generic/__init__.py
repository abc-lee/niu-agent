r"""
GenericAgent Core - 完整移植自 E:\tools\GenericAgent

这是原始代码，未做任何修改，保持原汁原味。

核心文件：
- agent_loop.py (99行) - 核心循环
- handler.py (526行) - 工具实现
- llmcore.py (835行) - LLM 抽象层

Assets:
- assets/tools_schema.json - 工具描述
- assets/sys_prompt.txt - 系统提示词
- assets/global_mem_insight_template.txt - 记忆索引模板
- assets/insight_fixed_structure.txt - 记忆结构

Memory (L0/L1/L2/L3):
- memory/memory_management_sop.md - 记忆管理 SOP

总计约 1500 行核心代码。
"""

from .agent_loop import StepOutcome, BaseHandler, agent_runner_loop, exhaust
from .handler import GenericAgentHandler
from .llmcore import (
    ToolClient,
    MockResponse,
    MockToolCall,
    BaseSession,
)

__all__ = [
    # agent_loop
    "StepOutcome",
    "BaseHandler",
    "agent_runner_loop",
    "exhaust",
    # handler
    "GenericAgentHandler",
    # llmcore
    "ToolClient",
    "MockResponse",
    "MockToolCall",
    "BaseSession",
]
