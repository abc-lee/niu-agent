"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path

from loguru import logger

from .base import UnifiedMessage, ChannelAdapter, ResolvedMessage, LocalResource


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

            # 新增：下载远端资源到本地（需要 message_id 以访问消息附件）
            feishu_message_id = str(getattr(msg, 'id', '') or getattr(msg, 'message_id', '') or '')
            logger.info(f"[FeishuChannel] msg.id={getattr(msg, 'id', 'N/A')}, msg.message_id={getattr(msg, 'message_id', 'N/A')}, resolved message_id={feishu_message_id}")
            logger.info(f"[FeishuChannel] msg type: {type(msg).__name__}, resources: {len(unified.resources) if unified.resources else 0}")
            local_resources = self.resolve_inbound_resources(unified.resources, message_id=feishu_message_id)

            # 将 resources 转为文本描述（现有逻辑不变）
            resource_text = self._format_resources(unified.resources)
            if resource_text:
                message_content = f"{unified.content}\n{resource_text}" if unified.content.strip() else resource_text
            else:
                message_content = unified.content

            # 新增：替换占位符为本地路径
            for lr in local_resources:
                if lr.resource_type == 'image':
                    old = f"[图片: {lr.original_key}]"
                else:
                    old = f"[文件: {lr.filename or lr.original_key}]"
                new = f"[{lr.resource_type}: {lr.local_path}]"
                message_content = message_content.replace(old, new)

            # 对未下载成功的资源，标记为下载失败
            downloaded_keys = {lr.original_key for lr in local_resources}
            for r in unified.resources:
                rtype = getattr(r, 'type', '') if not isinstance(r, dict) else r.get('type', '')
                file_key = getattr(r, 'file_key', '') if not isinstance(r, dict) else r.get('file_key', '')
                if not file_key or file_key in downloaded_keys:
                    continue
                if rtype == 'image':
                    placeholder = f"[图片: {file_key}]"
                    if placeholder in message_content:
                        message_content = message_content.replace(placeholder, f"[图片下载失败: {file_key}]")
                elif rtype == 'file':
                    file_name_r = getattr(r, 'file_name', '') if not isinstance(r, dict) else r.get('file_name', '')
                    placeholder = f"[文件: {file_name_r or file_key}]"
                    if placeholder in message_content:
                        message_content = message_content.replace(placeholder, f"[文件下载失败: {file_name_r or file_key}]")

            # P2P 用 sender_id，群聊用 chat_id — 区分 session 避免上下文混淆
            if is_p2p:
                session_id = f"feishu:{unified.sender_id}"
            else:
                session_id = f"feishu:group:{unified.channel_id}"

            # 直接入队（不再启动新线程，入队操作几乎不耗时）
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
    def _is_path_allowed(path: str) -> bool:
        """路径白名单校验 — 只允许 ~/.niu/ 和临时目录"""
        p = Path(path).resolve()
        allowed_dirs = [
            (Path.home() / ".niu").resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        return any(str(p).startswith(str(d)) for d in allowed_dirs)

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

    async def resolve_outbound_content(self, content: str) -> list[ResolvedMessage]:
        """解析出方向消息中的本地文件标记"""
        media_messages = []
        cleaned_content = content

        for marker in ("person_photo", "file"):
            pattern = f"::{marker}::"
            while pattern in cleaned_content:
                start = cleaned_content.index(pattern)
                after_marker = start + len(pattern)
                json_end = cleaned_content.find("::", after_marker)
                if json_end == -1:
                    cleaned_content = cleaned_content[:start] + f"[{marker}标记格式错误]" + cleaned_content[after_marker:]
                    break
                json_str = cleaned_content[after_marker:json_end]
                remaining = cleaned_content[json_end + 2:]
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    cleaned_content = cleaned_content[:start] + f"[{marker}标记格式错误]" + remaining
                    break
                path = data.get("path", "")
                name = data.get("name", "")
                if not path:
                    replacement = "[文件信息缺失]"
                elif not self._is_path_allowed(path):
                    replacement = "[文件无法发送: 安全限制]"
                elif not Path(path).exists():
                    replacement = f"[文件不存在: {name}]" if name else "[文件不存在]"
                else:
                    if marker == "person_photo":
                        media_messages.append(ResolvedMessage(kind="image", local_path=path, caption=name))
                        replacement = f"↑ {name}的照片" if name else "↑ 照片"
                    else:
                        media_messages.append(ResolvedMessage(kind="file", local_path=path, filename=name))
                        replacement = f"↑ {name}" if name else "↑ 文件"
                cleaned_content = cleaned_content[:start] + replacement + remaining

        messages = []
        if cleaned_content.strip():
            messages.append(ResolvedMessage(kind="text", content=cleaned_content))
        messages.extend(media_messages)
        return messages

    def resolve_inbound_resources(self, resources: list, *, message_id: str = "") -> list[LocalResource]:
        """同步方法 — 在 SDK 线程中调用，通过 schedule 提交异步下载"""
        local_resources = []
        for r in resources:
            rtype = getattr(r, 'type', '') if not isinstance(r, dict) else r.get('type', '')
            file_key = getattr(r, 'file_key', '') if not isinstance(r, dict) else r.get('file_key', '')
            file_name = getattr(r, 'file_name', '') if not isinstance(r, dict) else r.get('file_name', '')

            if rtype not in ('image', 'file') or not file_key:
                continue

            logger.info(f"[FeishuChannel] resolve_inbound: type={rtype}, file_key={file_key}, file_name={file_name}, message_id={message_id}")

            # 用 schedule 提交到 SDK bg_loop，但超时设 30s（bg_loop 可能排队）
            future = self.channel.schedule(
                self._download_from_feishu(file_key, rtype, file_name or "", message_id=message_id)
            )
            try:
                local_path = future.result(timeout=30)
                if local_path:
                    local_resources.append(LocalResource(
                        original_key=file_key,
                        resource_type=rtype,
                        local_path=local_path,
                        filename=file_name or f"{file_key}.bin"
                    ))
                else:
                    logger.warning(f"[FeishuChannel] _download_from_feishu returned None for {file_key}")
            except TimeoutError:
                logger.warning(f"[FeishuChannel] Download timeout (30s) for {file_key} (message_id={message_id}), skipping")
            except Exception as e:
                logger.warning(f"[FeishuChannel] Download failed for {file_key} (message_id={message_id}): {type(e).__name__}: {e}")

        return local_resources

    async def _download_from_feishu(self, file_key: str, rtype: str, file_name: str, *, message_id: str = "") -> str | None:
        """下载飞书资源到本地，返回本地路径"""
        local_dir = Path.home() / ".niu" / "tmp"
        local_dir.mkdir(parents=True, exist_ok=True)
        safe_name = file_name or f"{file_key}.bin"
        local_path = local_dir / f"feishu_in_{file_key}_{safe_name}"

        if local_path.exists():
            logger.info(f"[FeishuChannel] Reusing cached download: {local_path}")
            return str(local_path)

        # message_id 是下载用户发送图片的关键：有 message_id → message_resource 端点
        # 没有 message_id → im/v1/images 端点（只能下载应用上传的图片）
        mid_for_sdk = message_id or None
        logger.info(f"[FeishuChannel] _download_from_feishu: file_key={file_key}, rtype={rtype}, message_id={message_id!r} (→ SDK as {mid_for_sdk})")

        try:
            result_path = await self.channel.download_resource_to_file(
                file_key,
                resource_type=rtype,
                message_id=mid_for_sdk,
                dest_dir=local_dir,
                file_name=local_path.name,
            )
            if result_path:
                logger.info(f"[FeishuChannel] Downloaded {rtype} to {result_path}")
                return str(result_path)
            else:
                logger.warning(f"[FeishuChannel] download_resource_to_file returned None for {file_key}")
        except Exception as e:
            logger.warning(f"[FeishuChannel] Download resource failed for {file_key} (message_id={message_id!r}): {type(e).__name__}: {e}")
            # 尝试回退到不带 message_id 的下载（可能是应用上传的图片）
            if mid_for_sdk:
                logger.info(f"[FeishuChannel] Retrying download without message_id for {file_key}")
                try:
                    result_path = await self.channel.download_resource_to_file(
                        file_key,
                        resource_type=rtype,
                        message_id=None,
                        dest_dir=local_dir,
                        file_name=local_path.name,
                    )
                    if result_path:
                        logger.info(f"[FeishuChannel] Fallback download succeeded: {result_path}")
                        return str(result_path)
                except Exception as e2:
                    logger.warning(f"[FeishuChannel] Fallback download also failed: {type(e2).__name__}: {e2}")
        return None

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

    async def send_media(self, channel_id: str, msg: ResolvedMessage) -> None:
        """发送飞书图片/文件消息"""
        target = channel_id or self._user_open_id or self._user_p2p_chat_id
        if not target:
            logger.warning("[FeishuChannel] send_media() no target, skipping")
            return

        from lark_oapi.channel.types import MediaSource, OutboundImage, OutboundFile

        result = None
        try:
            source = MediaSource(kind="file", path=msg.local_path)
            if msg.kind == "image":
                result = await self.channel.send(target, OutboundImage(
                    source=source,
                    caption=msg.caption or "",
                ))
            elif msg.kind == "file":
                result = await self.channel.send(target, OutboundFile(
                    source=source,
                    file_name=msg.filename or "",
                ))
            if result is not None and not result.success:
                logger.error(f"[FeishuChannel] send_media failed: {result.error}")
        except Exception as e:
            logger.error(f"[FeishuChannel] send_media exception: {e}")

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