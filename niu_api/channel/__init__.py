"""通道抽象层 — ChannelRouter + 全局单例"""

from typing import Dict, Optional
from loguru import logger

from .base import UnifiedMessage, ChannelAdapter, ResolvedMessage, LocalResource

__all__ = ["ChannelRouter", "get_channel_router", "UnifiedMessage", "ChannelAdapter", "ResolvedMessage", "LocalResource"]


class ChannelRouter:
    """统一消息路由器 — 所有通道的消息统一交给 ChatQueue 处理"""

    def __init__(self):
        self.channels: Dict[str, ChannelAdapter] = {}

    async def route_in(self, message: UnifiedMessage) -> str:
        """所有通道的消息统一交给 ChatQueue 处理（异步入队）

        飞书 SDK 在后台线程中调用 _on_message，此时 asyncio.to_thread
        会使用 SDK 的后台事件循环而非 FastAPI 主循环，导致上下文错误。
        因此飞书通道使用 route_in_sync（同步入队），而非此方法。
        """
        from niu_api.chat_queue import get_chat_queue
        q = get_chat_queue()
        channel = message.channel or "electron"
        session_id = "default" if channel == "electron" else f"{channel}:{message.channel_id}"
        result = await q.enqueue(
            content=message.content,
            source=channel,
            channel=channel,
            channel_id=message.channel_id,
            sender_id=message.sender_id,
            session_id=session_id,
        )
        return result.message

    def route_in_sync(self, message: UnifiedMessage, session_id: str | None = None,
                      message_override: str | None = None):
        """同步路由消息到 ChatQueue — 供飞书等通道线程中调用

        返回 EnqueueResult，不再返回回复文本。
        回复由 ChatQueue Worker 处理后自动推送到飞书。
        """
        from niu_api.chat_queue import get_chat_queue
        content = message_override if message_override is not None else message.content
        channel = message.channel or "feishu"
        if session_id is None:
            session_id = f"{channel}:{message.sender_id}"
        q = get_chat_queue()
        return q.enqueue_sync(
            content=content,
            source=channel,
            channel=channel,
            channel_id=message.channel_id,
            sender_id=message.sender_id,
            session_id=session_id,
        )

    async def route_out(self, reply: str, channel: str, channel_id: str) -> None:
        """回复投递到指定通道 — 先解析文件标记，再路由发送"""
        adapter = self.channels.get(channel)
        if not adapter:
            return

        messages = await adapter.resolve_outbound_content(reply)
        for msg in messages:
            if msg.kind == "text":
                if channel_id:
                    await adapter.send(channel_id, msg.content)
                else:
                    await adapter.push("", msg.content)
            elif msg.kind in ("image", "file"):
                await adapter.send_media(channel_id, msg)

    async def push(self, content: str, channel: str, channel_id: str) -> None:
        """主动推送（定时提醒等）— 先解析文件标记，再路由发送"""
        adapter = self.channels.get(channel)
        if not adapter:
            return

        messages = await adapter.resolve_outbound_content(content)
        for msg in messages:
            if msg.kind == "text":
                await adapter.push(channel_id, msg.content)
            elif msg.kind in ("image", "file"):
                await adapter.send_media(channel_id, msg)

    def register(self, name: str, adapter: ChannelAdapter) -> None:
        """注册通道适配器"""
        self.channels[name] = adapter
        logger.info(f"[ChannelRouter] Registered channel: {name}")

    def has_channel(self, name: str) -> bool:
        """检查通道是否已注册"""
        return name in self.channels


# 全局单例
_router: Optional[ChannelRouter] = None


def get_channel_router() -> ChannelRouter:
    """获取全局 ChannelRouter 实例"""
    global _router
    if _router is None:
        _router = ChannelRouter()
    return _router