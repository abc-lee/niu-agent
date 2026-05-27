"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path

from loguru import logger

from .base import UnifiedMessage, ChannelAdapter

from lark_oapi.api.cardkit.v1 import (
    CreateCardRequest, CreateCardRequestBody, Card,
    UpdateCardRequest, UpdateCardRequestBody,
    SettingsCardRequest, SettingsCardRequestBody,
    ContentCardElementRequest, ContentCardElementRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
)


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
        self._prefs_lock = threading.Lock()

        # 流式推送状态
        self._feishu_waiting: bool = False
        self._stream_card_id: str | None = None
        self._stream_message_id: str | None = None
        self._last_pushed_rowid: int = 0
        self._stream_seq: int = 0
        self._stream_target: str | None = None
        self._stream_card_created: bool = False
        self._stream_fallback_used: bool = False
        self._accumulated_text: str = ""

        # 从持久化数据恢复 chat_id / open_id
        self._apply_persisted_ids()

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self.channel.on("error", self._on_error)

    def _on_message(self, msg):
        """处理飞书消息事件（直接入队，不阻塞 SDK 线程）"""
        try:
            raw = msg.raw or {}
            if msg.chat_type and "chat_type" not in raw:
                raw = {**raw, "chat_type": msg.chat_type}

            unified = UnifiedMessage(
                content=msg.content_text or "",
                channel="feishu",
                channel_id=msg.chat_id,
                sender_id=msg.sender_id,
                message_type=msg.raw_content_type or "text",
                resources=msg.resources or [],
                raw=raw,
            )

            if not unified.content.strip() and not unified.resources:
                logger.debug("[FeishuChannel] Empty message with no resources, skipping")
                return

            is_p2p = self._is_p2p_message(msg)
            log_preview = unified.content[:50] if unified.content.strip() else f"[resources: {len(unified.resources)}]"
            logger.info(f"[FeishuChannel] Received: {log_preview}...")

            # P2P 消息：更新推送目标并持久化
            if is_p2p:
                self._update_persisted_ids(unified.channel_id, unified.sender_id)

            # 将 resources 转为文本描述
            resource_text = self._format_resources(unified.resources)
            if resource_text:
                message_content = f"{unified.content}\n{resource_text}" if unified.content.strip() else resource_text
            else:
                message_content = unified.content

            # P2P 用 sender_id，群聊用 chat_id — 区分 session 避免上下文混淆
            if is_p2p:
                session_id = f"feishu:{unified.sender_id}"
            else:
                session_id = f"feishu:group:{unified.channel_id}"

            # 直接入队（不再启动新线程，入队操作几乎不耗时）
            self._feishu_waiting = True
            self._stream_target = unified.channel_id or self._user_open_id or self._user_p2p_chat_id

            # 同步初始化游标：记录当前 DB 位置，后续 _persist_one_msg 的增量从此之后开始
            try:
                import sqlite3
                db_path = str(Path.home() / ".niu" / "messages.db")
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.execute("SELECT MAX(rowid) FROM messages")
                    row = cursor.fetchone()
                    self._last_pushed_rowid = row[0] if row and row[0] is not None else 0
                finally:
                    conn.close()
                logger.info(f"[FeishuStream] Waiting, cursor={self._last_pushed_rowid}")
            except Exception as e:
                logger.warning(f"[FeishuStream] Failed to init cursor: {e}")
                self._last_pushed_rowid = 0

            result = self.router.route_in_sync(unified, session_id=session_id, message_override=message_content)
            if result.queued:
                logger.info(f"[FeishuChannel] Message queued: {message_content[:50]}...")
            else:
                logger.warning(f"[FeishuChannel] Failed to queue: {result.message}")
                try:
                    self.channel.schedule(
                        self.channel.send(unified.channel_id, {"text": "消息入队失败，请稍后重试"}),
                    )
                except Exception as e:
                    logger.warning(f"[FeishuChannel] Failed to send error notification: {e}")

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    @staticmethod
    def _format_resources(resources: list | None) -> str:
        """将 resources 列表转为文本描述（兼容 ResourceDescriptor dataclass 和 dict）"""
        if not resources:
            return ""
        parts = []
        for r in resources:
            if isinstance(r, dict):
                rtype = r.get("type", "")
                file_key = r.get("file_key", "")
                file_name = r.get("file_name", "") or ""
            else:
                rtype = getattr(r, "type", "")
                file_key = getattr(r, "file_key", "")
                file_name = getattr(r, "file_name", "") or ""
            if rtype == "image":
                key = file_key or file_name or "未知图片"
                parts.append(f"[图片: {key}]")
            elif rtype == "file":
                name = file_name or file_key or "未知文件"
                parts.append(f"[文件: {name}]")
            else:
                name = file_name or file_key or "未知资源"
                parts.append(f"[{rtype}: {name}]" if rtype else f"[资源: {name}]")
        return "\n".join(parts)

    def _is_p2p_message(self, msg_or_unified) -> bool:
        """判断是否为 P2P 消息（非群聊）— 兼容 SDK msg 和 UnifiedMessage"""
        chat_type = getattr(msg_or_unified, 'chat_type', None)
        if chat_type:
            return chat_type == "p2p"
        raw = getattr(msg_or_unified, 'raw', None)
        if isinstance(raw, dict):
            return raw.get("chat_type") == "p2p"
        return False

    async def _on_card_action(self, _action):
        """处理卡片交互事件（Phase 4 实现）"""
        logger.debug("[FeishuChannel] Card action received (not implemented yet)")

    def _on_reconnecting(self, _=None):
        """WebSocket 重连中（SDK 调用 h() 可能传一个参数）"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    def _on_reconnected(self, _=None):
        """WebSocket 重连成功 — 重新加载已保存的 ID"""
        logger.info("[FeishuChannel] WebSocket reconnected")
        self._feishu_prefs = self._load_prefs()
        self._apply_persisted_ids()

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
        """将 feishu 配置段写回 preferences.json（原子写入 + 文件锁）"""
        with self._prefs_lock:
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
        """发送消息到飞书 — 回复到指定会话，空 channel_id 时 fallback 到 push()"""
        try:
            if self._stream_card_created and not self._stream_fallback_used:
                await self._finalize_stream_card(content)
                return  # 流式卡片已展示完整内容，不重复发
            target = channel_id or self._user_open_id or self._user_p2p_chat_id
            if not target:
                logger.warning("[FeishuChannel] send() no target, skipping")
                return
            try:
                result = await self.channel.send(target, {"markdown": content})
                if not result.success:
                    logger.error(f"[FeishuChannel] Send failed: {result.error}")
            except Exception as e:
                logger.error(f"[FeishuChannel] Send exception: {e}")
        finally:
            self._feishu_waiting = False
            self._stream_card_id = None
            self._stream_message_id = None
            self._stream_card_created = False
            self._stream_fallback_used = False
            self._stream_seq = 0
            self._last_pushed_rowid = 0
            self._stream_target = None
            self._accumulated_text = ""

    async def push(self, channel_id: str, content: str) -> None:
        """主动推送 — 没有 ID 就不发，优先 open_id"""
        target = channel_id or self._user_open_id or self._user_p2p_chat_id
        if not target:
            logger.warning("[FeishuChannel] No chat_id or open_id, skipping push")
            return
        try:
            result = await self.channel.send(target, {"markdown": content})
            if not result.success:
                fallback = None
                if target == self._user_open_id and self._user_p2p_chat_id:
                    fallback = self._user_p2p_chat_id
                elif target == self._user_p2p_chat_id and self._user_open_id:
                    fallback = self._user_open_id
                if fallback:
                    logger.warning(f"[FeishuChannel] Push to {target} failed, retrying with {fallback}")
                    try:
                        r2 = await self.channel.send(fallback, {"markdown": content})
                        if not r2.success:
                            logger.error(f"[FeishuChannel] Push to {fallback} also failed: {r2.error}")
                    except Exception as e2:
                        logger.error(f"[FeishuChannel] Push to {fallback} exception: {e2}")
                else:
                    logger.error(f"[FeishuChannel] Push failed: {result.error}")
        except Exception as e:
            logger.error(f"[FeishuChannel] Push exception: {e}")

    @property
    def user_p2p_chat_id(self) -> str | None:
        """获取用户 P2P 会话 ID"""
        return self._user_p2p_chat_id

    @property
    def user_open_id(self) -> str | None:
        """获取用户 open_id"""
        return self._user_open_id

    @property
    def is_connected(self) -> bool:
        """WebSocket 是否已连接"""
        return self.channel.is_ready

    @property
    def has_push_target(self) -> bool:
        """是否有可用的推送目标（chat_id 或 open_id）"""
        return bool(self._user_p2p_chat_id or self._user_open_id)

    # ── 流式推送 ──────────────────────────────────────────────

    @classmethod
    def trigger_feishu_stream_push(cls):
        """从 executor 线程触发流式推送（通过 run_coroutine_threadsafe 调度到主循环）"""
        try:
            from niu_api.chat import _main_loop
            loop = _main_loop
        except Exception:
            loop = None

        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(cls._do_feishu_stream_push(), loop)
        else:
            logger.debug("[FeishuStream] No running event loop, skipping stream push")

    @staticmethod
    async def _do_feishu_stream_push():
        """在主循环中执行流式推送"""
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            adapter = router.channels.get("feishu")
            if adapter and isinstance(adapter, FeishuChannelAdapter):
                await adapter._push_incremental()
        except Exception as e:
            logger.warning(f"[FeishuStream] Stream push error: {e}")

    async def _push_incremental(self):
        """读取 DB 增量内容，创建或更新流式卡片"""
        if not self._feishu_waiting:
            return

        try:
            from agent.session import get_message_store
            store = await get_message_store()

            # 读取增量 assistant 文本（游标在 _on_message 中已初始化）
            new_texts = await store.get_assistant_text_after_rowid(self._last_pushed_rowid)
            if not new_texts:
                return

            # 拼接内容
            parts = [text for _, text in new_texts]
            self._accumulated_text += "\n".join(parts)
            new_rowid = new_texts[-1][0]

            content = self._accumulated_text
            if len(content) > 18000:
                content = content[:17900] + "\n\n...[内容已截断]"

            if not self._stream_card_created:
                # 首次：创建流式卡片
                card_id = self._create_stream_card(content)
                if card_id:
                    self._stream_card_id = card_id
                    self._stream_card_created = True
                    self._last_pushed_rowid = new_rowid
                    self._stream_seq = 1
                    logger.info(f"[FeishuStream] Card created: card_id={card_id}")
                else:
                    self._stream_fallback_used = True
                    logger.warning("[FeishuStream] Card creation failed, will fallback to markdown")
            else:
                # 后续：元素级更新
                self._stream_seq += 1
                success = self._update_stream_element(content, self._stream_seq)
                if success:
                    self._last_pushed_rowid = new_rowid
                    logger.info(f"[FeishuStream] Element updated: seq={self._stream_seq}")
                else:
                    self._stream_fallback_used = True
                    logger.warning("[FeishuStream] Element update failed, will fallback to markdown")

        except Exception as e:
            logger.error(f"[FeishuStream] Push incremental error: {e}")
            self._stream_fallback_used = True

    def _create_stream_card(self, content: str) -> str | None:
        """创建流式卡片实体 + 用 card_id 引用发送消息"""
        try:
            card_json = self._build_streaming_card_dict(content)

            # 创建卡片实体
            body = CreateCardRequestBody.builder().type("card_json").data(card_json).build()
            req = CreateCardRequest.builder().request_body(body).build()
            resp = self.channel._client.cardkit.v1.card.create(req)
            if not resp.success():
                logger.error(f"[FeishuStream] CreateCard failed: {resp.code} {resp.msg}")
                return None
            card_id = resp.data.card_id

            # 用 card_id 引用发送消息（关键：只有引用方式，终结操作才能传导到飞书端）
            card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
            send_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(self._stream_target)
                    .msg_type("interactive")
                    .content(card_ref)
                    .build()) \
                .build()
            send_resp = self.channel._client.im.v1.message.create(send_req)
            if not send_resp.success():
                logger.error(f"[FeishuStream] SendMessage failed: {send_resp.code} {send_resp.msg}")
                return None
            self._stream_message_id = send_resp.data.message_id

            return card_id
        except Exception as e:
            logger.error(f"[FeishuStream] Create stream card exception: {e}")
            return None

    def _update_stream_element(self, content: str, seq: int) -> bool:
        """元素级内容更新（轻量级，不是全卡 UpdateCard）"""
        try:
            req = ContentCardElementRequest.builder() \
                .card_id(self._stream_card_id) \
                .element_id("md1") \
                .request_body(ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(seq)
                    .uuid(f"niu-stream-{seq}")
                    .build()) \
                .build()
            resp = self.channel._client.cardkit.v1.card_element.content(req)
            if not resp.success():
                logger.error(f"[FeishuStream] UpdateElement failed: {resp.code} {resp.msg}")
                return False
            return True
        except Exception as e:
            logger.error(f"[FeishuStream] UpdateElement exception: {e}")
            return False

    async def _finalize_stream_card(self, final_content: str):
        """终结流式卡片：flush 最后内容 → settings API → UpdateCard 完整内容"""
        try:
            # 1. 如果还有未推送的内容，先 flush
            if final_content and final_content.strip() != self._accumulated_text.strip():
                self._stream_seq += 1
                self._update_stream_element(final_content, self._stream_seq)
                self._accumulated_text = final_content

            # 2. Settings API 关闭 streaming_mode
            self._stream_seq += 1
            settings_json = json.dumps({"config": {"streaming_mode": False}})
            settings_req = SettingsCardRequest.builder() \
                .card_id(self._stream_card_id) \
                .request_body(SettingsCardRequestBody.builder()
                    .settings(settings_json)
                    .sequence(self._stream_seq)
                    .uuid(f"niu-finalize-settings")
                    .build()) \
                .build()
            settings_resp = self.channel._client.cardkit.v1.card.settings(settings_req)
            if not settings_resp.success():
                logger.error(f"[FeishuStream] Settings API failed: {settings_resp.code} {settings_resp.msg}")
                self._stream_fallback_used = True
                return

            # 3. UpdateCard 更新完整卡片内容（移除 subtitle）
            self._stream_seq += 1
            content = self._accumulated_text
            if len(content) > 18000:
                content = content[:17900] + "\n\n...[内容已截断]"
            final_card = {
                "schema": "2.0",
                "header": {
                    "title": {"content": "Niu助手", "tag": "plain_text"},
                    "subtitle": {"content": "", "tag": "plain_text"},
                },
                "config": {"streaming_mode": False, "summary": {"content": ""}},
                "body": {"elements": [{"tag": "markdown", "content": content, "element_id": "md1"}]},
            }
            final_json = json.dumps(final_card, ensure_ascii=False)
            update_req = UpdateCardRequest.builder() \
                .card_id(self._stream_card_id) \
                .request_body(UpdateCardRequestBody.builder()
                    .card(Card.builder().type("card_json").data(final_json).build())
                    .sequence(self._stream_seq)
                    .uuid(f"niu-finalize-update")
                    .build()) \
                .build()
            update_resp = self.channel._client.cardkit.v1.card.update(update_req)
            if not update_resp.success():
                logger.error(f"[FeishuStream] UpdateCard failed: {update_resp.code} {update_resp.msg}")
            else:
                logger.info("[FeishuStream] Card finalized successfully")

        except Exception as e:
            logger.error(f"[FeishuStream] Finalize exception: {e}")
            self._stream_fallback_used = True

    @staticmethod
    def _build_streaming_card_dict(content: str) -> str:
        """构建流式卡片 JSON 2.0"""
        card = {
            "schema": "2.0",
            "header": {
                "title": {"content": "Niu助手", "tag": "plain_text"},
                "subtitle": {"content": "思考中...", "tag": "plain_text"},
            },
            "config": {
                "streaming_mode": True,
                "update_multi": True,
                "summary": {"content": ""},
            },
            "body": {
                "elements": [{"tag": "markdown", "content": content, "element_id": "md1"}],
            },
        }
        return json.dumps(card, ensure_ascii=False)