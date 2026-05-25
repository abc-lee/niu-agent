"""Electron 通道适配器 — 包装现有 SSE 推送"""

from loguru import logger

from .base import ChannelAdapter


class ElectronChannelAdapter(ChannelAdapter):
    """Electron 通道 — 消息已通过 SSE 推送到前端，此适配器主要用于 push"""

    async def send(self, channel_id: str, content: str) -> None:
        """Electron 的消息回复已由 chat_sync/chat 端点自动推送到 SSE"""
        logger.debug("[ElectronChannel] send() called — reply already pushed via SSE")

    async def push(self, channel_id: str, content: str) -> None:
        """主动推送 — 通过 SSE 事件总线推送"""
        from niu_api.chat import notify_new_message_sync
        import uuid

        msg_id = str(uuid.uuid4())
        notify_new_message_sync(msg_id, "assistant", content, source="electron")
        logger.debug(f"[ElectronChannel] push() — sent via SSE (id={msg_id[:8]})")