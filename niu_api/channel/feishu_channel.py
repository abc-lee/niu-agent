"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

from loguru import logger

from .base import UnifiedMessage, ChannelAdapter, ChannelRouter


class FeishuChannelAdapter(ChannelAdapter):
    """飞书通道 — WebSocket 长连接，消息收发，Agent 无感知"""

    def __init__(self, app_id: str, app_secret: str, channel_router: ChannelRouter):
        from lark_oapi.channel import FeishuChannel

        self.channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
        self.router = channel_router
        self._user_p2p_chat_id = None

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)

    async def _on_message(self, msg):
        """处理飞书消息事件"""
        try:
            unified = UnifiedMessage(
                content=msg.content_text or "",
                channel="feishu",
                channel_id=msg.chat_id,
                sender_id=msg.sender_id,
                message_type=msg.raw_content_type or "text",
                resources=msg.resources or [],
                raw=msg.raw or {},
            )

            if not unified.content.strip():
                logger.debug("[FeishuChannel] Empty message, skipping")
                return

            # 记录 P2P chat_id 用于主动推送
            if not self._user_p2p_chat_id:
                self._user_p2p_chat_id = msg.chat_id

            logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

            # 交给 ChannelRouter → Agent 处理
            reply = await self.router.route_in(unified)
            if reply:
                await self.channel.send(msg.chat_id, {"markdown": reply})
                logger.info(f"[FeishuChannel] Replied: {reply[:50]}...")

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    async def _on_card_action(self, action):
        """处理卡片交互事件（Phase 4 实现）"""
        logger.debug("[FeishuChannel] Card action received (not implemented yet)")

    async def _on_reconnecting(self, _):
        """WebSocket 重连中"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    async def _on_reconnected(self, _):
        """WebSocket 重连成功"""
        logger.info("[FeishuChannel] WebSocket reconnected")

    async def start(self) -> None:
        """启动 WebSocket 长连接"""
        await self.channel.connect_until_ready(timeout=30)
        logger.info("[FeishuChannel] WebSocket connected")

    async def send(self, chat_id: str, content: str) -> None:
        """发送消息到飞书"""
        await self.channel.send(chat_id, {"markdown": content})

    async def push(self, chat_id: str, content: str) -> None:
        """主动推送（定时提醒等）"""
        target = chat_id or self._user_p2p_chat_id
        if target:
            await self.channel.send(target, {"markdown": content})
        else:
            logger.warning("[FeishuChannel] No chat_id for push, skipping")

    @property
    def user_p2p_chat_id(self) -> str | None:
        """获取用户 P2P 会话 ID"""
        return self._user_p2p_chat_id