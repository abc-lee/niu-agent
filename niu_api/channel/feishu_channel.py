"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

import asyncio
import json
import threading
from pathlib import Path

from loguru import logger

from .base import UnifiedMessage, ChannelAdapter


class FeishuChannelAdapter(ChannelAdapter):
    """飞书通道 — WebSocket 长连接，消息收发，Agent 无感知"""

    def __init__(self, app_id: str, app_secret: str, channel_router):
        # 修补 lark_oapi.ws.client 模块级 loop — 防止捕获 uvicorn 的运行中 loop
        # ws/client.py 在 import 时通过 asyncio.get_event_loop() 捕获当前 loop，
        # 如果此时 uvicorn loop 已在运行，WSClient.start() 的 loop.run_until_complete()
        # 会抛出 RuntimeError: This event loop is already running
        import lark_oapi.ws.client as _ws_client
        if _ws_client.loop.is_running():
            _ws_client.loop = asyncio.new_event_loop()

        from lark_oapi.channel import FeishuChannel
        from lark_oapi.channel.config import OutboundConfig, MarkdownConverter

        self.channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            outbound=OutboundConfig(
                markdown_converter=MarkdownConverter(tag_md_mode="native")
            ),
        )
        self.router = channel_router
        self._user_p2p_chat_id = None
        self._user_open_id = None
        self._prefs_path = Path.home() / ".niu" / "preferences.json"
        self._feishu_prefs = self._load_prefs()

        # 从持久化数据恢复 chat_id / open_id
        self._apply_persisted_ids()

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self.channel.on("error", self._on_error)

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

            if not unified.content.strip() and not unified.resources:
                logger.debug("[FeishuChannel] Empty message with no resources, skipping")
                return

            if self._is_p2p_message(msg):
                self._user_p2p_chat_id = msg.chat_id

            # 仅 P2P 消息才持久化 chat_id 和 open_id
            if self._is_p2p_message(msg):
                self._update_persisted_ids(msg.chat_id, msg.sender_id)

            logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

            threading.Thread(target=self._process_and_reply, args=(unified,), daemon=True).start()

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    @staticmethod
    def _format_resources(resources: list) -> str:
        """将 resources 列表转为文本描述"""
        if not resources:
            return ""
        parts = []
        for r in resources:
            rtype = r.get("type", "")
            if rtype == "image":
                key = r.get("file_key", r.get("file_name", "未知图片"))
                parts.append(f"[图片: {key}]")
            elif rtype == "file":
                name = r.get("file_name", r.get("file_key", "未知文件"))
                parts.append(f"[文件: {name}]")
            else:
                name = r.get("file_name", r.get("file_key", "未知资源"))
                parts.append(f"[{rtype}: {name}]" if rtype else f"[资源: {name}]")
        return "\n".join(parts)

    def _process_and_reply(self, unified: UnifiedMessage):
        """在独立线程中执行阻塞调用，完成后通过 channel.schedule() 发送回复"""
        try:
            # P2P 用 sender_id，群聊用 chat_id
            if self._is_p2p_message(unified):
                session_id = f"feishu:{unified.sender_id}"
            else:
                session_id = f"feishu:group:{unified.channel_id}"

            # 将 resources 转为文本描述，追加到消息后面
            resource_text = self._format_resources(unified.resources)
            if resource_text:
                message_content = f"{unified.content}\n{resource_text}" if unified.content.strip() else resource_text
            else:
                message_content = unified.content

            reply = self.router.route_in_sync(unified, session_id=session_id, message_override=message_content)
            if reply:
                self.channel.schedule(
                    self.channel.send(unified.channel_id, {"markdown": reply}),
                )
                logger.info(f"[FeishuChannel] Replied: {reply[:50]}...")
            else:
                logger.warning("[FeishuChannel] Empty reply from agent")
                self.channel.schedule(
                    self.channel.send(unified.channel_id, {"text": "收到，但无法生成回复"}),
                )
        except Exception as e:
            logger.error(f"[FeishuChannel] Process/reply error: {e}")

    def _is_p2p_message(self, msg_or_unified) -> bool:
        """判断是否为 P2P 消息（非群聊）— 兼容 SDK msg 和 UnifiedMessage"""
        chat_type = getattr(msg_or_unified, 'chat_type', None)
        if chat_type:
            return chat_type == "p2p"
        raw = getattr(msg_or_unified, 'raw', None)
        if isinstance(raw, dict):
            return raw.get("chat_type") == "p2p"
        return False

    async def _on_card_action(self, action):
        """处理卡片交互事件（Phase 4 实现）"""
        logger.debug("[FeishuChannel] Card action received (not implemented yet)")

    def _on_reconnecting(self, _=None):
        """WebSocket 重连中（SDK 调用 h() 可能传一个参数）"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    def _on_reconnected(self, _=None):
        """WebSocket 重连成功（SDK 调用 h() 可能传一个参数）"""
        logger.info("[FeishuChannel] WebSocket reconnected")

    def _on_error(self, err):
        """SDK 内部错误集中处理"""
        logger.error(f"[FeishuChannel] SDK error: {err}")

    def _load_prefs(self) -> dict:
        """从 preferences.json 加载 feishu 配置段"""
        try:
            if self._prefs_path.exists():
                with open(self._prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                    return prefs.get("feishu", {})
        except Exception as e:
            logger.warning(f"[FeishuChannel] Failed to load preferences: {e}")
        return {}

    def _save_prefs(self):
        """将 feishu 配置段写回 preferences.json（原子写入）"""
        import os
        import tempfile
        try:
            prefs = {}
            if self._prefs_path.exists():
                with open(self._prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)

            feishu = prefs.setdefault("feishu", {})
            if self._user_p2p_chat_id:
                feishu["user_p2p_chat_id"] = self._user_p2p_chat_id
            if self._user_open_id:
                feishu["user_open_id"] = self._user_open_id

            dir_name = str(self._prefs_path.parent)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(prefs, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(self._prefs_path))
            except Exception:
                os.unlink(tmp_path)
                raise

            self._feishu_prefs = feishu
        except Exception as e:
            logger.warning(f"[FeishuChannel] Failed to save preferences: {e}")

    def _apply_persisted_ids(self):
        """从 _feishu_prefs 恢复 chat_id / open_id（不覆盖已有值）"""
        persisted_chat_id = self._feishu_prefs.get("user_p2p_chat_id")
        persisted_open_id = self._feishu_prefs.get("user_open_id")

        if persisted_chat_id and not self._user_p2p_chat_id:
            self._user_p2p_chat_id = persisted_chat_id
            logger.info(f"[FeishuChannel] Restored chat_id from preferences: {persisted_chat_id}")

        if persisted_open_id and not self._user_open_id:
            self._user_open_id = persisted_open_id
            logger.info(f"[FeishuChannel] Restored open_id from preferences: {persisted_open_id}")

    def _update_persisted_ids(self, chat_id: str, open_id: str):
        """更新 chat_id / open_id 并持久化（仅在变化时保存）"""
        changed = False

        if chat_id != self._user_p2p_chat_id:
            self._user_p2p_chat_id = chat_id
            changed = True

        if open_id != self._user_open_id:
            self._user_open_id = open_id
            changed = True

        if changed:
            self._save_prefs()

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
        target = channel_id or self._user_p2p_chat_id or self._user_open_id
        if target:
            try:
                await self.channel.send(target, {"markdown": content})
            except Exception as e:
                # 如果 chat_id 失效，尝试用 open_id 重发
                if self._user_open_id and target != self._user_open_id:
                    logger.warning(f"[FeishuChannel] Push to chat_id failed, retrying with open_id: {e}")
                    try:
                        await self.channel.send(self._user_open_id, {"markdown": content})
                        return
                    except Exception as e2:
                        logger.error(f"[FeishuChannel] Push to open_id also failed: {e2}")
                else:
                    logger.error(f"[FeishuChannel] Push failed: {e}")
        else:
            logger.warning("[FeishuChannel] No chat_id or open_id for push, skipping")

    @property
    def user_p2p_chat_id(self) -> str | None:
        """获取用户 P2P 会话 ID"""
        return self._user_p2p_chat_id