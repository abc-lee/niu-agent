"""
Niu Agent Package

简化的 Agent 架构，直接使用 GenericAgent 组件。
"""

from .handler import NiuHandler
from .runner import NiuRunner, chat, get_runner
from .session import MessageStore, get_message_store

__version__ = "0.2.0"

__all__ = [
    "NiuRunner",
    "get_runner",
    "chat",
    "NiuHandler",
    "MessageStore",
    "get_message_store",
]
