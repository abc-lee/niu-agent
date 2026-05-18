"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

import asyncio
import threading

from loguru import logger

from .base import UnifiedMessage, ChannelAdapter


class FeishuChannelAdapter(ChannelAdapter):
    """飞书通道 — WebSocket 长连接，消息收发，Agent 无感知"""

    def __init__(self, app_id: str, app_secret: str, channel_router):
        from lark_oapi.channel import FeishuChannel

        self.channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
        self.router = channel_router
        self._user_p2p_chat_id = None

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)

    def _on_message(self, msg):
        """处理飞书消息事件（同步 handler，不阻塞 SDK 事件循环）"""
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

            if not self._user_p2p_chat_id:
                self._user_p2p_chat_id = msg.chat_id

            logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

            # 在 SDK bg loop 上下文中捕获 loop 引用
            # _on_message 由 SDK _invoke 在 bg loop 线程中调用，
            # 此时 get_running_loop() 返回 SDK bg loop
            try:
                sdk_loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("[FeishuChannel] No running event loop, cannot capture SDK loop")
                return
            chat_id = msg.chat_id

            def _process_and_reply():
                """在独立线程中执行阻塞调用，完成后通过 run_coroutine_threadsafe 发送回复"""
                try:
                    reply = self.router.route_in_sync(unified)
                    if reply:
                        asyncio.run_coroutine_threadsafe(
                            self.channel.send(chat_id, {"markdown": reply}),
                            sdk_loop,
                        )
                        logger.info(f"[FeishuChannel] Replied: {reply[:50]}...")
                except Exception as e:
                    logger.error(f"[FeishuChannel] Process/reply error: {e}")

            threading.Thread(target=_process_and_reply, daemon=True).start()

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    async def _on_card_action(self, action):
        """处理卡片交互事件（Phase 4 实现）"""
        logger.debug("[FeishuChannel] Card action received (not implemented yet)")

    def _on_reconnecting(self):
        """WebSocket 重连中（SDK 调用 h() 无参数）"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    def _on_reconnected(self):
        """WebSocket 重连成功（SDK 调用 h() 无参数）"""
        logger.info("[FeishuChannel] WebSocket reconnected")

    async def start(self) -> None:
        """启动 WebSocket 长连接"""
        await self.channel.connect_until_ready(timeout=30)
        logger.info("[FeishuChannel] WebSocket connected")

    async def disconnect(self) -> None:
        """断开 WebSocket 长连接"""
        try:
            await self.channel.disconnect()
            logger.info("[FeishuChannel] WebSocket disconnected")
        except Exception as e:
            logger.warning(f"[FeishuChannel] Disconnect error: {e}")

    async def send(self, channel_id: str, content: str) -> None:
        """发送消息到飞书"""
        try:
            await self.channel.send(channel_id, {"markdown": content})
        except Exception as e:
            logger.error(f"[FeishuChannel] Send failed: {e}")

    async def push(self, channel_id: str, content: str) -> None:
        """主动推送（定时提醒等）"""
        target = channel_id or self._user_p2p_chat_id
        if target:
            try:
                await self.channel.send(target, {"markdown": content})
            except Exception as e:
                logger.error(f"[FeishuChannel] Push failed: {e}")
        else:
            logger.warning("[FeishuChannel] No chat_id for push, skipping")

    @property
    def user_p2p_chat_id(self) -> str | None:
        """获取用户 P2P 会话 ID"""
        return self._user_p2p_chat_id