"""通道抽象层 — ChannelRouter + 全局单例"""

from typing import Dict, Optional
from loguru import logger

from .base import UnifiedMessage, ChannelAdapter

__all__ = ["ChannelRouter", "get_channel_router", "UnifiedMessage", "ChannelAdapter"]


class ChannelRouter:
    """统一消息路由器 — 所有通道的消息统一交给 Agent 处理"""

    def __init__(self):
        self.channels: Dict[str, ChannelAdapter] = {}

    async def route_in(self, message: UnifiedMessage) -> str:
        """所有通道的消息统一交给 Agent 处理

        飞书 SDK 在后台线程中调用 _on_message，此时 asyncio.to_thread
        会使用 SDK 的后台事件循环而非 FastAPI 主循环，导致上下文错误。
        因此直接同步调用 _chat_sync（它本身是同步函数，在任意线程中都可运行）。
        """
        return self._chat_sync(message.content)

    def route_in_sync(self, message: UnifiedMessage, session_id: str = "feishu", message_override: str | None = None) -> str:
        """同步路由消息 — 供飞书通道线程中调用"""
        content = message_override if message_override is not None else message.content
        return self._chat_sync(content, session_id=session_id)

    def _chat_sync(self, message: str, session_id: str = "feishu") -> str:
        """同步调用 Agent — 可在任意线程中运行"""
        import os
        import requests

        port = os.environ.get("NIU_API_PORT", "9876")
        try:
            resp = requests.post(
                f"http://127.0.0.1:{port}/chat/sync",
                json={"session_id": session_id, "message": message},
                timeout=120,
            )
            if resp.status_code == 200:
                return resp.json().get("reply", "")
            else:
                logger.error(f"[ChannelRouter] chat/sync returned {resp.status_code}")
                return ""
        except Exception as e:
            logger.error(f"[ChannelRouter] Failed to call chat/sync: {e}")
            return ""

    async def route_out(self, reply: str, channel: str, channel_id: str) -> None:
        """回复投递到指定通道"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.send(channel_id, reply)

    async def push(self, content: str, channel: str, channel_id: str) -> None:
        """主动推送（定时提醒等）"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.push(channel_id, content)

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