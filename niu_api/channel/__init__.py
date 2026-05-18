"""通道抽象层 — ChannelRouter + 全局单例"""

import asyncio
from typing import Dict, Optional
from loguru import logger

from .base import UnifiedMessage, ChannelAdapter

__all__ = ["ChannelRouter", "get_channel_router", "UnifiedMessage", "ChannelAdapter"]


class ChannelRouter:
    """统一消息路由器 — 所有通道的消息统一交给 Agent 处理"""

    def __init__(self):
        self.channels: Dict[str, ChannelAdapter] = {}
        self._agent_runner = None

    def set_agent_runner(self, runner):
        """由 niu_api 启动时注入 NiuRunner 实例"""
        self._agent_runner = runner

    async def route_in(self, message: UnifiedMessage) -> str:
        """所有通道的消息统一交给 Agent 处理"""
        if self._agent_runner is None:
            raise RuntimeError("Agent runner not initialized")
        reply = await asyncio.to_thread(self._chat_sync, message.content)
        return reply

    def _chat_sync(self, message: str) -> str:
        """同步调用 Agent（在线程池中执行，与 _chat_lock 共享同一把锁）"""
        import requests

        main_url = "http://127.0.0.1:9876"
        try:
            resp = requests.post(
                f"{main_url}/chat/sync",
                json={"session_id": "default", "message": message},
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