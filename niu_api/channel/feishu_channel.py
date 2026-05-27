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
        self._app_id = app_id
        self._app_secret = app_secret
        self.router = channel_router
        self._user_p2p_chat_id = None
        self._user_open_id = None

        # 飞书流式推送状态
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._feishu_waiting: bool = False       # 飞书用户是否在等回复
        self._stream_card_id: str | None = None  # 流式卡片 card_id
        self._stream_message_id: str | None = None  # 流式卡片消息 message_id
        self._last_pushed_rowid: int = 0         # DB rowid 游标
        self._stream_seq: int = 0               # CardKit sequence 计数器
        self._stream_target: str | None = None   # 当前飞书请求的发送目标 (chat_id or open_id)
        self._stream_card_created: bool = False  # 流式卡片是否已创建
        self._stream_fallback_used: bool = False # 流式是否已回退到普通模式

        self._prefs_path = Path.home() / ".niu" / "preferences.json"
        self._feishu_prefs = self._load_prefs()
        self._prefs_lock = threading.Lock()
        self._tenant_token: str | None = None
        self._tenant_token_expires_at: float = 0.0

        # 从持久化数据恢复 chat_id / open_id
        self._apply_persisted_ids()

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self.channel.on("error", self._on_error)

    def _on_message(self, msg):
        """处理飞书消息事件 — 同步下载资源后入队"""
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
            logger.debug(f"[FeishuChannel] msg.id={getattr(msg, 'id', 'N/A')}, msg.message_id={getattr(msg, 'message_id', 'N/A')}, resolved message_id={feishu_message_id}")
            logger.debug(f"[FeishuChannel] msg type: {type(msg).__name__}, resources: {len(unified.resources) if unified.resources else 0}")
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
            self._feishu_waiting = True
            self._stream_target = unified.channel_id or self._user_open_id or self._user_p2p_chat_id
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
        for d in allowed_dirs:
            prefix = str(d)
            sp = str(p)
            if sp == prefix or sp.startswith(prefix + os.sep):
                return True
        return False

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
        """同步下载飞书资源 — 在当前线程直接调用 SDK 同步 API，下载完成后才入队

        使用 lark_oapi.Client 的同步 get() 方法（requests 库），无需 event loop。
        下载在 _on_message 回调线程中同步完成，保证 Agent 收到的是真实文件路径。
        """
        local_resources = []
        for r in resources:
            rtype = getattr(r, 'type', '') if not isinstance(r, dict) else r.get('type', '')
            file_key = getattr(r, 'file_key', '') if not isinstance(r, dict) else r.get('file_key', '')
            file_name = getattr(r, 'file_name', '') if not isinstance(r, dict) else r.get('file_name', '')

            if rtype not in ('image', 'file') or not file_key:
                continue

            logger.info(f"[FeishuChannel] resolve_inbound: type={rtype}, file_key={file_key}, message_id={message_id}")

            local_path = self._download_sync(file_key, rtype, file_name or "", message_id=message_id)
            if local_path:
                local_resources.append(LocalResource(
                    original_key=file_key,
                    resource_type=rtype,
                    local_path=local_path,
                    filename=file_name or f"{file_key}.bin"
                ))
            else:
                logger.warning(f"[FeishuChannel] Download failed for {file_key}")

        return local_resources

    def _download_sync(self, file_key: str, rtype: str, file_name: str, *, message_id: str = "") -> str | None:
        """同步下载飞书资源到本地 — 使用 SDK 同步 API（requests 库），无需 event loop

        在 _on_message 的 SDK WebSocket 线程中直接调用，下载完成后才入队。
        飞书资源下载通常 1-3 秒，不会长时间阻塞 SDK 线程。
        """
        local_dir = Path.home() / ".niu" / "tmp"
        local_dir.mkdir(parents=True, exist_ok=True)

        sdk_client = self.channel.client
        mid = message_id or None
        logger.info(f"[FeishuChannel] _download_sync: file_key={file_key}, rtype={rtype}, message_id={mid}")

        # 主路径：带 message_id → message_resource 端点（下载用户发送的图片/文件）
        if mid:
            try:
                result = self._sync_download_message_resource(sdk_client, file_key, rtype, mid, local_dir, file_key)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"[FeishuChannel] message_resource download failed: {type(e).__name__}: {e}")

        # 回退路径：不带 message_id → im/v1/images 或 im/v1/file 端点
        try:
            result = self._sync_download_standalone(sdk_client, file_key, rtype, local_dir, file_key)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[FeishuChannel] standalone download also failed: {type(e).__name__}: {e}")

        return None

    def _sync_download_message_resource(self, sdk_client, file_key: str, rtype: str, message_id: str, local_dir: Path, basename: str) -> str | None:
        """同步调用 message_resource.get() — 下载用户发送的消息附件"""
        from lark_oapi.api.im.v1.model.get_message_resource_request import GetMessageResourceRequest

        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(rtype)
            .build()
        )
        resp = sdk_client.im.v1.message_resource.get(req)

        if resp.file is None:
            logger.warning(f"[FeishuChannel] message_resource.get() returned no file: code={resp.code}, msg={resp.msg}")
            return None

        data = resp.file.read()
        if not data:
            logger.warning(f"[FeishuChannel] message_resource.get() returned empty file content")
            return None

        # 确定文件名：优先 resp.file_name（飞书API返回的原始文件名），其次推断
        final_name = self._resolve_filename(resp.file_name, rtype, basename, resp.raw)
        local_path = local_dir / f"feishu_in_{basename}_{final_name}"

        if local_path.exists():
            logger.info(f"[FeishuChannel] Reusing cached download: {local_path}")
            return str(local_path)

        # 原子写入
        tmp_path = local_dir / f".dl-{local_path.name}"
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(local_path))
        logger.info(f"[FeishuChannel] Downloaded {rtype} via message_resource: {local_path} ({len(data)} bytes)")
        return str(local_path)

    def _sync_download_standalone(self, sdk_client, file_key: str, rtype: str, local_dir: Path, basename: str) -> str | None:
        """同步调用独立端点 — im/v1/images 或 im/v1/file"""
        if rtype == "image":
            from lark_oapi.api.im.v1.model.get_image_request import GetImageRequest
            req = GetImageRequest.builder().image_key(file_key).build()
            resp = sdk_client.im.v1.image.get(req)
        else:
            from lark_oapi.api.im.v1.model.get_file_request import GetFileRequest
            req = GetFileRequest.builder().file_key(file_key).build()
            resp = sdk_client.im.v1.file.get(req)

        if resp.file is None:
            logger.warning(f"[FeishuChannel] standalone {rtype}.get() returned no file")
            return None

        data = resp.file.read()
        if not data:
            logger.warning(f"[FeishuChannel] standalone {rtype}.get() returned empty content")
            return None

        final_name = self._resolve_filename(resp.file_name, rtype, basename, resp.raw)
        local_path = local_dir / f"feishu_in_{basename}_{final_name}"

        if local_path.exists():
            logger.info(f"[FeishuChannel] Reusing cached download: {local_path}")
            return str(local_path)

        tmp_path = local_dir / f".dl-{local_path.name}"
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(local_path))
        logger.info(f"[FeishuChannel] Downloaded {rtype} via standalone endpoint: {local_path} ({len(data)} bytes)")
        return str(local_path)

    @staticmethod
    def _resolve_filename(api_file_name: str | None, rtype: str, basename: str, raw_resp) -> str:
        """确定最终文件名 — 优先飞书API返回的原始文件名，其次从Content-Type推断，最后兜底"""
        # 优先级1：飞书API的 Content-Disposition 头返回的原始文件名（含扩展名）
        if api_file_name:
            return api_file_name

        # 优先级2：从 Content-Type 推断扩展名
        content_type = ""
        if raw_resp and hasattr(raw_resp, 'headers'):
            content_type = raw_resp.headers.get("Content-Type", "") or ""
        ext_from_ct = FeishuChannelAdapter._content_type_to_ext(content_type)
        if ext_from_ct:
            return f"{basename}{ext_from_ct}"

        # 优先级3：兜底 — 图片默认.jpg，文件默认.bin
        if rtype == "image":
            return f"{basename}.jpg"
        return f"{basename}.bin"

    @staticmethod
    def _content_type_to_ext(content_type: str) -> str | None:
        """从 Content-Type 推断文件扩展名"""
        if not content_type:
            return None
        ct_lower = content_type.lower().split(";")[0].strip()
        mapping = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/msword": ".doc",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.ms-powerpoint": ".ppt",
            "text/plain": ".txt", "text/csv": ".csv",
            "application/zip": ".zip",
            "application/x-rar-compressed": ".rar",
            "application/json": ".json",
        }
        return mapping.get(ct_lower)

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
        self._main_loop = asyncio.get_running_loop()
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
            # 流式推送完成处理
            if self._stream_card_created and not self._stream_fallback_used:
                # 流式卡片已创建且未回退 — 终结卡片
                if self._stream_card_id:
                    await self._finalize_stream_card()
                logger.info("[FeishuStream] Stream card completed, skip normal send")
                return
            # 流式回退或未创建 — 走原有逻辑

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
            # 重置所有流式状态
            self._feishu_waiting = False
            self._stream_card_id = None
            self._stream_message_id = None
            self._last_pushed_rowid = 0
            self._stream_seq = 0
            self._stream_target = None
            self._stream_card_created = False
            self._stream_fallback_used = False

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
        """发送飞书图片/文件消息 — 上传+发消息全走 REST API"""
        target = channel_id or self._user_open_id or self._user_p2p_chat_id
        if not target:
            logger.warning("[FeishuChannel] send_media() no target, skipping")
            return

        try:
            if msg.kind == "image":
                ok = await self._send_image_via_rest(target, msg.local_path, msg.caption)  # noqa: caption unused — 飞书 image 消息不支持
                if not ok:
                    await self.send(channel_id, "[图片发送失败]")
            elif msg.kind == "file":
                ok = await self._send_file_via_rest(target, msg.local_path, msg.filename or Path(msg.local_path).name)
                if not ok:
                    await self.send(channel_id, "[文件发送失败]")
        except Exception as e:
            logger.error(f"[FeishuChannel] send_media exception: {e}")
            try:
                await self.send(channel_id, f"[媒体发送异常: {type(e).__name__}]")
            except Exception:
                pass

    async def _send_image_via_rest(self, receive_id: str, img_path: str, _caption: str | None = None) -> bool:
        """上传图片并发送消息 — 全走 REST API"""
        import requests as _requests

        token = await self._get_tenant_token()
        if not token:
            logger.error("[FeishuChannel] send_image: no tenant token")
            return False

        p = Path(img_path)
        if not p.exists():
            logger.error(f"[FeishuChannel] send_image: file not found: {img_path}")
            return False

        # 超过10MB时压缩
        actual_path = p
        compressed_path = None
        if p.stat().st_size > 10 * 1024 * 1024:
            compressed_path = await self._compress_image(p)
            if compressed_path:
                actual_path = compressed_path
            else:
                logger.warning("[FeishuChannel] send_image: compression failed, trying original")

        try:
            # Step 1: 上传图片
            with open(str(actual_path), "rb") as f:
                resp = await asyncio.to_thread(
                    _requests.post,
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (actual_path.name, f)},
                    timeout=30,
                )
            result = resp.json()
            code = result.get("code", -1)
            if code != 0:
                logger.error(f"[FeishuChannel] upload image failed: code={code}, msg={result.get('msg', '')}")
                return False
            image_key = result.get("data", {}).get("image_key", "")
            if not image_key:
                logger.error("[FeishuChannel] upload image: no image_key in response")
                return False
            logger.info(f"[FeishuChannel] upload image success: {image_key}")

            # Step 2: 发送图片消息
            receive_id_type = self._infer_receive_id_type(receive_id)
            content = json.dumps({"image_key": image_key})
            resp2 = await asyncio.to_thread(
                _requests.post,
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "image",
                    "content": content,
                },
                timeout=15,
            )
            result2 = resp2.json()
            code2 = result2.get("code", -1)
            if code2 != 0:
                logger.error(f"[FeishuChannel] send image msg failed: code={code2}, msg={result2.get('msg', '')}")
                return False
            logger.info(f"[FeishuChannel] send image msg success")
            return True

        except Exception as e:
            logger.error(f"[FeishuChannel] send_image_via_rest exception: {type(e).__name__}: {e}")
            return False
        finally:
            # 清理临时压缩文件
            if compressed_path and compressed_path != p and compressed_path.exists():
                try:
                    compressed_path.unlink()
                except Exception:
                    pass

    async def _send_file_via_rest(self, receive_id: str, file_path: str, file_name: str) -> bool:
        """上传文件并发送消息 — 全走 REST API"""
        import requests as _requests

        token = await self._get_tenant_token()
        if not token:
            logger.error("[FeishuChannel] send_file: no tenant token")
            return False

        p = Path(file_path)
        if not p.exists():
            logger.error(f"[FeishuChannel] send_file: file not found: {file_path}")
            return False

        try:
            # Step 1: 上传文件
            with open(str(p), "rb") as f:
                resp = await asyncio.to_thread(
                    _requests.post,
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream"},
                    files={"file": (file_name, f)},
                    timeout=60,
                )
            result = resp.json()
            code = result.get("code", -1)
            if code != 0:
                logger.error(f"[FeishuChannel] upload file failed: code={code}, msg={result.get('msg', '')}")
                return False
            file_key = result.get("data", {}).get("file_key", "")
            if not file_key:
                logger.error("[FeishuChannel] upload file: no file_key in response")
                return False
            logger.info(f"[FeishuChannel] upload file success: {file_key}")

            # Step 2: 发送文件消息
            receive_id_type = self._infer_receive_id_type(receive_id)
            content = json.dumps({"file_key": file_key})
            resp2 = await asyncio.to_thread(
                _requests.post,
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "file",
                    "content": content,
                },
                timeout=15,
            )
            result2 = resp2.json()
            code2 = result2.get("code", -1)
            if code2 != 0:
                logger.error(f"[FeishuChannel] send file msg failed: code={code2}, msg={result2.get('msg', '')}")
                return False
            logger.info(f"[FeishuChannel] send file msg success")
            return True

        except Exception as e:
            logger.error(f"[FeishuChannel] send_file_via_rest exception: {type(e).__name__}: {e}")
            return False

    async def _compress_image(self, img_path: Path) -> Path | None:
        """压缩超过10MB的图片为JPEG — 返回临时文件路径，失败返回None"""
        try:
            from PIL import Image

            img = Image.open(str(img_path))
            try:
                rgb_img = img.convert("RGB") if img.mode in ("RGBA", "P") else img

                tmp_dir = Path.home() / ".niu" / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = tmp_dir / f"compressed_{img_path.stem}.jpg"

                for quality in (85, 70, 55, 40, 25):
                    rgb_img.save(str(tmp_path), "JPEG", quality=quality)
                    if tmp_path.stat().st_size <= 10 * 1024 * 1024:
                        logger.info(f"[FeishuChannel] compressed {img_path.name} to {tmp_path.stat().st_size // 1024}KB (quality={quality})")
                        return tmp_path

                # 即使最低质量仍超过10MB
                logger.warning(f"[FeishuChannel] compressed image still >10MB at quality=25")
                return None
            finally:
                img.close()

        except Exception as e:
            logger.error(f"[FeishuChannel] compress image failed: {e}")
            return None

    @staticmethod
    def _infer_receive_id_type(receive_id: str) -> str:
        """根据 receive_id 前缀推断 receive_id_type"""
        if receive_id.startswith("oc_"):
            return "chat_id"
        elif receive_id.startswith("ou_"):
            return "open_id"
        elif receive_id.startswith("on_"):
            return "open_id"
        else:
            return "chat_id"  # 默认

    async def _get_tenant_token(self) -> str | None:
        """获取飞书 tenant_access_token（带缓存，提前5分钟刷新）"""
        import time

        if self._tenant_token and time.monotonic() < self._tenant_token_expires_at:
            return self._tenant_token

        import requests as _requests

        try:
            resp = await asyncio.to_thread(
                _requests.post,
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            result = resp.json()
            if result.get("code", -1) != 0:
                logger.error(f"[FeishuChannel] _get_tenant_token failed: {result}")
                return None
            token = result.get("tenant_access_token", "")
            expire = result.get("expire", 7200)  # 默认2小时
            self._tenant_token = token
            # 提前5分钟刷新
            self._tenant_token_expires_at = time.monotonic() + expire - 300
            return token
        except Exception as e:
            logger.error(f"[FeishuChannel] _get_tenant_token exception: {e}")
            return None

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

    # ─────────────────────────────────────────────
    # 飞书流式推送（CardKit streaming card）
    # ─────────────────────────────────────────────

    @classmethod
    def trigger_push_incremental(cls):
        """从 executor 线程触发飞书增量推送（非阻塞）"""
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            feishu_adapter = router.channels.get("feishu")
            if not feishu_adapter:
                logger.debug("[FeishuStream] trigger: no feishu adapter")
                return
            if not feishu_adapter._feishu_waiting:
                logger.debug("[FeishuStream] trigger: not waiting")
                return
            loop = feishu_adapter._main_loop
            if not loop or loop.is_closed():
                logger.debug(f"[FeishuStream] trigger: loop invalid, loop={loop}")
                return
            asyncio.run_coroutine_threadsafe(
                feishu_adapter._push_incremental(), loop
            )
            logger.debug("[FeishuStream] trigger: dispatched to main loop")
        except Exception as e:
            logger.error(f"[FeishuStream] trigger exception: {e}")

    async def _push_incremental(self):
        """增量推送 — 检查 _feishu_waiting → 游标初始化 → 读增量 → 创建/更新卡片"""
        if not self._feishu_waiting:
            return
        if not self._stream_target:
            return

        try:
            from agent.session import get_message_store
            store = await get_message_store()

            # 游标初始化
            if self._last_pushed_rowid == 0:
                self._last_pushed_rowid = await store.get_max_rowid()
                logger.info(f"[FeishuStream] Rowid cursor initialized: {self._last_pushed_rowid}")
                return

            # 读增量 assistant 文本消息
            rows = await store.get_assistant_text_after_rowid(self._last_pushed_rowid)
            if not rows:
                return

            # 取最后一条 assistant 消息（累积内容）
            latest_rowid, latest_content = rows[-1]

            # 内容截断（飞书卡片有字符限制）
            if len(latest_content) > 18000:
                latest_content = latest_content[:18000] + "\n\n... (内容过长，已截断)"

            if not self._stream_card_created:
                # 首次推送 — 创建卡片并发送消息
                await self._create_and_send_stream_card(latest_content)
            else:
                # 后续推送 — 更新卡片
                self._stream_seq += 1
                await self._update_stream_card(latest_content, self._stream_seq)

            self._last_pushed_rowid = latest_rowid

        except Exception as e:
            logger.error(f"[FeishuStream] push_incremental error: {e}")
            self._stream_fallback_used = True

    async def _create_and_send_stream_card(self, content: str):
        """创建流式卡片并通过 im.v1.message.create 发送到飞书"""
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        card_json = self._build_streaming_card_json(content)

        # 1. 创建卡片
        body = CreateCardRequestBody.builder() \
            .type("card_json") \
            .data(card_json) \
            .build()
        req = CreateCardRequest.builder().request_body(body).build()
        resp = await self.channel._client.cardkit.v1.card.acreate(req)

        if not resp.success():
            logger.error(f"[FeishuStream] Create card failed: {resp.code} {resp.msg}")
            self._stream_fallback_used = True
            return

        self._stream_card_id = resp.data.card_id
        self._stream_card_created = True
        logger.info(f"[FeishuStream] Card created: {self._stream_card_id}")

        # 2. 发送卡片消息（内联 card JSON，不用 card_id 引用）
        target = self._stream_target
        assert target is not None  # guarded by early return above
        receive_id_type = self._infer_receive_id_type(target)

        send_body = CreateMessageRequestBody.builder() \
            .receive_id(target) \
            .msg_type("interactive") \
            .content(card_json) \
            .build()
        send_req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(send_body) \
            .build()
        send_resp = await self.channel._client.im.v1.message.acreate(send_req)

        if not send_resp.success():
            logger.error(f"[FeishuStream] Send card message failed: {send_resp.code} {send_resp.msg}")
            self._stream_fallback_used = True
            return

        self._stream_message_id = send_resp.data.message_id
        logger.info(f"[FeishuStream] Card sent to {target}, message_id={self._stream_message_id}")

    async def _update_stream_card(self, content: str, seq: int):
        """更新流式卡片内容"""
        from lark_oapi.api.cardkit.v1 import UpdateCardRequest, UpdateCardRequestBody
        from lark_oapi.api.cardkit.v1 import Card

        if not self._stream_card_id:
            return

        card_json = self._build_streaming_card_json(content)

        body = UpdateCardRequestBody.builder() \
            .card(Card.builder().type("card_json").data(card_json).build()) \
            .uuid(f"niu-stream-{seq}") \
            .sequence(seq) \
            .build()
        req = UpdateCardRequest.builder() \
            .card_id(self._stream_card_id) \
            .request_body(body) \
            .build()
        resp = await self.channel._client.cardkit.v1.card.aupdate(req)

        if not resp.success():
            logger.error(f"[FeishuStream] Update card failed: {resp.code} {resp.msg}")
            self._stream_fallback_used = True
            return

        logger.info(f"[FeishuStream] Card updated, seq={seq}, content_len={len(content)}")

    async def _finalize_stream_card(self):
        """终结流式卡片（设置 streaming_mode=False）"""
        from lark_oapi.api.cardkit.v1 import BatchUpdateCardRequest, BatchUpdateCardRequestBody

        if not self._stream_card_id:
            return

        self._stream_seq += 1
        finalize_actions = json.dumps([{
            "action": "partial_update_setting",
            "params": {
                "settings": {
                    "config": {
                        "streaming_mode": False
                    }
                }
            }
        }])

        body = BatchUpdateCardRequestBody.builder() \
            .uuid("niu-finalize") \
            .sequence(self._stream_seq) \
            .actions(finalize_actions) \
            .build()
        req = BatchUpdateCardRequest.builder() \
            .card_id(self._stream_card_id) \
            .request_body(body) \
            .build()
        resp = await self.channel._client.cardkit.v1.card.abatch_update(req)

        if not resp.success():
            logger.error(f"[FeishuStream] Finalize card failed: {resp.code} {resp.msg}")
            # 不设 fallback — 卡片保持 streaming，飞书会超时自动终结
        else:
            logger.info("[FeishuStream] Card finalized")

    @staticmethod
    def _build_streaming_card_json(content: str) -> str:
        """构建 Card JSON 2.0 流式卡片"""
        card_dict = {
            "schema": "2.0",
            "header": {
                "title": {"content": "Niu Agent", "tag": "plain_text"},
                "subtitle": {"content": "思考中...", "tag": "plain_text"},
            },
            "config": {
                "streaming_mode": True,
                "summary": {"content": content[:50]},
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content, "element_id": "stream_md"}
                ]
            },
        }
        return json.dumps(card_dict, ensure_ascii=False)