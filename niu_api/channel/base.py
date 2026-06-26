"""通道抽象层 — UnifiedMessage 数据类 + ChannelAdapter 接口"""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class ResolvedMessage:
    """通道解析后的出方向消息"""
    kind: str              # "text" | "image" | "file"
    content: str = ""      # text 类型的内容
    local_path: str = ""   # image/file 类型的本地文件路径
    caption: str = ""      # image 的描述文字
    filename: str = ""     # file 的显示文件名


@dataclass
class LocalResource:
    """已下载到本地的远端资源"""
    original_key: str      # IM file_key / image_key
    resource_type: str     # "image" | "file"
    local_path: str        # 本地文件路径
    filename: str          # 原始文件名


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

    async def resolve_outbound_content(self, content: str) -> list[ResolvedMessage]:
        """解析出方向消息中的本地文件标记，返回待发送的消息列表。

        默认实现：不转换，返回一条 ResolvedMessage(kind="text", content=content)
        IM 通道 (Gateway)：使用默认实现（Markdown 透传给 Adapter，由 Adapter 解释图片/文件标记）
        Electron 通道：使用默认实现（前端自行解析 Markdown 图片）
        """
        return [ResolvedMessage(kind="text", content=content)]

    def resolve_inbound_resources(self, resources: list) -> list[LocalResource]:
        """解析入方向消息中的远端资源引用，下载到本地。

        默认实现：不处理，返回空列表
        IM 通道重写：下载 IM 图片/文件 → 写入 ~/.niu/tmp/ → 返回 LocalResource 列表

        注意：此方法是同步的，因为 _on_message 在 SDK 线程中调用。
        """
        return []

    async def send_media(self, channel_id: str, msg: ResolvedMessage) -> None:
        """发送媒体消息（图片/文件）— 默认实现不做任何事"""
        pass