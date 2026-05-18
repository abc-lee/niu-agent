"""通道抽象层 — UnifiedMessage 数据类 + ChannelAdapter 接口"""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class UnifiedMessage:
    """统一消息格式，所有通道的消息都转换为此格式"""

    content: str
    channel: str
    channel_id: str
    sender_id: str
    message_type: str
    resources: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class ChannelAdapter(ABC):
    """通道适配器接口"""

    @abstractmethod
    async def send(self, channel_id: str, content: str) -> None:
        """发送消息到指定会话"""

    @abstractmethod
    async def push(self, channel_id: str, content: str) -> None:
        """主动推送（定时提醒等）"""