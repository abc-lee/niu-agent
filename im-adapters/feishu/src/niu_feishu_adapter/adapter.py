"""飞书 Adapter — TCP Client + 飞书 SDK，纯中转

入方向：飞书消息 → 下载附件 → 构造 MSG → 发给 Gateway
出方向：Gateway 指令 → 调飞书 API → 结果留在飞书侧
"""
import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

MAX_MSG_SIZE = 10 * 1024 * 1024  # 10MB
_LOCAL_PATH_PREFIX = str(Path.home() / ".niu" / "tmp")


class CardState:
    """单个 channel 的流式卡片状态"""
    __slots__ = ("card_id", "seq", "message_id", "receive_id", "reply_to_id",
                 "pending_images", "pending_files", "last_content")

    def __init__(self, card_id: str, receive_id: str, reply_to_id: str | None = None):
        self.card_id = card_id
        self.seq = 0
        self.message_id: str | None = None
        self.receive_id = receive_id
        self.reply_to_id = reply_to_id
        self.pending_images: list[dict] = []
        self.pending_files: list[dict] = []
        self.last_content = ""


class FeishuAdapter:
    """飞书 IM Adapter — TCP Client"""

    def __init__(self, gateway_port: int, app_id: str, app_secret: str,
                 push_chat_id: str = "", push_open_id: str = ""):
        self._gateway_port = gateway_port
        self._app_id = app_id
        self._app_secret = app_secret
        self._push_chat_id = push_chat_id
        self._push_open_id = push_open_id
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._card_states: dict[str, CardState] = {}

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._init_sdk()
        await self._connect_gateway()
        await self._send_ready()
        self._start_listener()
        await self._read_loop()

    # ── 飞书 SDK ──

    def _init_sdk(self):
        import lark_oapi as lark
        self._client = lark.Client.builder() \
            .app_id(self._app_id).app_secret(self._app_secret) \
            .log_level(lark.LogLevel.DEBUG).build()
        # 获取 bot open_id（群聊 @bot 检测需要）
        self._bot_open_id = self._fetch_bot_open_id()
        logger.info("[FeishuAdapter] SDK initialized")

    def _fetch_bot_open_id(self) -> str:
        """调用飞书 API 获取 bot 的 open_id"""
        from niu_feishu_adapter.feishu_api import _get_tenant_token
        import requests
        token = _get_tenant_token(self._app_id, self._app_secret)
        if not token:
            return ""
        try:
            resp = requests.get(
                "https://open.feishu.cn/open-apis/bot/v3/info/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            result = resp.json()
            if result.get("code") == 0:
                # 飞书 API 可能返回 data 信封格式或顶层格式
                data = result.get("data") or {k: v for k, v in result.items() if k not in ("code", "msg")}
                open_id = data.get("bot", {}).get("open_id", "")
                if open_id:
                    logger.info(f"[FeishuAdapter] Bot open_id: {open_id}")
                    return open_id
            logger.warning(f"[FeishuAdapter] Failed to get bot open_id: {result.get('msg', '')}")
        except Exception as e:
            logger.warning(f"[FeishuAdapter] Get bot open_id error: {e}")
        return ""

    def _start_listener(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        # 修补 lark_oapi.ws.client 模块级 loop — 防止捕获运行中的 loop
        # ws/client.py 在 import 时通过 asyncio.get_event_loop() 捕获当前 loop，
        # 如果此时 loop 已在运行，WSClient.start() 的 loop.run_until_complete()
        # 会抛出 RuntimeError: This event loop is already running
        import lark_oapi.ws.client as _ws_client
        if _ws_client.loop.is_running():
            _ws_client.loop = asyncio.new_event_loop()

        def on_message(data: P2ImMessageReceiveV1) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._on_feishu_msg(data), self._loop)
            except Exception as e:
                logger.error(f"[FeishuAdapter] Event error: {e}")

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(on_message).build()
        ws = lark.ws.Client(self._app_id, self._app_secret,
                            event_handler=handler, log_level=lark.LogLevel.DEBUG)
        threading.Thread(target=ws.start, daemon=True).start()
        logger.info("[FeishuAdapter] Listener started")

    async def _on_feishu_msg(self, data):
        """飞书消息回调"""
        try:
            msg = data.event.message
            sender = data.event.sender
        except AttributeError:
            return

        content_str = msg.content or "{}"
        chat_id = msg.chat_id or ""
        sender_id = sender.sender_id.open_id if sender.sender_id else ""
        msg_type = msg.message_type or "text"
        is_group = msg.chat_type == "group"
        message_id = getattr(msg, 'message_id', '') or ''

        # 群聊 @bot 过滤
        if is_group:
            mentions = getattr(msg, 'mentions', None) or []
            bot_mentioned = any(
                getattr(m.id, 'open_id', '') == self._bot_open_id
                for m in mentions if m and m.id
            ) if self._bot_open_id else False
            if not bot_mentioned:
                return

        # 文本内容
        if msg_type == "text":
            try:
                text = json.loads(content_str).get("text", "")
            except Exception:
                text = content_str
        else:
            text = content_str

        # 资源下载
        from niu_feishu_adapter.feishu_api import download_resource
        try:
            if msg_type == "image":
                image_key = json.loads(content_str).get("image_key", "")
                if image_key:
                    local = await asyncio.to_thread(
                        download_resource, self._app_id, self._app_secret,
                        image_key, "image", message_id=message_id)
                    if local:
                        text = f"入库照片：![图片]({local})"
                    else:
                        text = f"[图片下载失败: {image_key}]"
            elif msg_type == "file":
                cj = json.loads(content_str)
                file_key = cj.get("file_key", "")
                file_name = cj.get("file_name", "unknown")
                if file_key:
                    local = await asyncio.to_thread(
                        download_resource, self._app_id, self._app_secret,
                        file_key, "file", file_name, message_id=message_id)
                    if local:
                        text = f"入库文件：[{file_name}]({local})"
                    else:
                        text = f"[文件下载失败: {file_name}]"
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"[FeishuAdapter] Resource parse error: {e}, raw: {content_str[:200]}")

        # 群聊处理
        reply_to_id = None
        if is_group:
            # 清理 @bot 文本
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            # 发送者前缀（EventSender 没有 name，用 sender_id 前6位作标识）
            sender_name = f"用户{sender_id[:6]}" if sender_id else "未知"
            text = f"[群聊] {sender_name}: {text}"
            reply_to_id = message_id

        # P2P 消息：更新推送目标并写回配置
        if not is_group:
            if chat_id and chat_id != self._push_chat_id:
                self._push_chat_id = chat_id
            if sender_id and sender_id != self._push_open_id:
                self._push_open_id = sender_id
            await asyncio.to_thread(self._update_push_target, chat_id, sender_id)

        await self._send({
            "type": "MSG",
            "content": text,
            "channel_id": chat_id,
            "sender_id": sender_id,
            "session_id": f"feishu:{sender_id}" if not is_group else f"feishu:group:{chat_id}",
            "is_group": is_group,
            "reply_to_id": reply_to_id,
        })

    # ── Gateway TCP ──

    async def _connect_gateway(self):
        """连接 Gateway，最多重试 30 次（等待 Gateway 启动）"""
        for attempt in range(30):
            try:
                self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self._gateway_port)
                self._card_states.clear()  # 重连后清空旧卡片状态，避免用过时状态更新
                logger.info(f"[FeishuAdapter] Connected to Gateway :{self._gateway_port}")
                return
            except ConnectionRefusedError:
                if attempt == 0:
                    logger.info("[FeishuAdapter] Waiting for Gateway...")
                await asyncio.sleep(1)
        raise RuntimeError(f"Gateway not available after 30s on port {self._gateway_port}")

    async def _send_ready(self):
        await self._send({"type": "READY", "adapter": "feishu", "push_target": self._push_chat_id})

    async def _read_loop(self):
        try:
            while True:
                header = await self._reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length > MAX_MSG_SIZE:
                    logger.error(f"[FeishuAdapter] Message too large: {length}")
                    break
                data = await self._reader.readexactly(length)
                try:
                    cmd = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                try:
                    await self._dispatch(cmd)
                except Exception as e:
                    logger.error(f"[FeishuAdapter] Error dispatching command: {e}")
        except Exception as e:
            logger.error(f"[FeishuAdapter] Connection error: {e}")
        finally:
            if self._writer:
                self._writer.close()

    async def _dispatch(self, cmd: dict):
        t = cmd.get("type")
        if t == "STREAM":
            await self._on_stream(cmd)
        elif t == "SEND":
            await self._on_send(cmd)
        elif t == "PUSH":
            await self._on_push(cmd)

    async def _on_stream(self, cmd: dict):
        """STREAM = 完整内容 → 创建/更新卡片

        注意：当前生产环境 verbose=False，每次 STREAM 的 content 是该轮 LLM 的完整输出，
        不是增量文本。所以不累积，直接用 content 替换卡片显示。

        如果 content 为空（仅信号通知），只递增 seq 保持卡片活跃，不更新显示内容。
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        is_final = cmd.get("is_final", False)
        reply_to_id = cmd.get("reply_to_id")

        from niu_feishu_adapter.feishu_api import (
            create_card, update_card_element, finalize_card,
        )

        state = self._card_states.get(receive_id)

        # 空内容 = 信号通知（保持卡片活跃），只递增 seq
        if not content:
            if not state:
                if is_final:
                    logger.debug(f"[FeishuAdapter] STREAM(is_final) with no content and no card for {receive_id}")
                return
            state.seq += 1
            if is_final:
                # 不 pop state — 终结由后续 SEND 指令完成（SEND 携带正确的累积内容）
                logger.debug(f"[FeishuAdapter] STREAM(is_final) for {receive_id}, card will be finalized by SEND")
            return

        # 有内容 = 更新卡片显示
        filtered, images, files = await asyncio.to_thread(self._filter_media, content)
        display = filtered.replace("[PHOTO_SEP]", "")
        if len(display) > 18000:
            display = display[:17900] + "\n\n...[内容已截断]"

        if not state:
            card_id, msg_id = await create_card(self._client, receive_id, display, reply_to_id)
            if not card_id:
                logger.error(f"[FeishuAdapter] Card creation failed for {receive_id}")
                return
            state = CardState(card_id, receive_id, reply_to_id)
            state.message_id = msg_id
            state.seq = 1
            self._card_states[receive_id] = state
        else:
            state.seq += 1
            await update_card_element(self._client, state.card_id, display, state.seq)

        state.pending_images = images
        state.pending_files = files
        state.last_content = content

        if is_final:
            # 不 pop state — 终结由后续 SEND 指令完成（SEND 携带正确的累积内容）
            logger.debug(f"[FeishuAdapter] STREAM(is_final) with content for {receive_id}, card will be finalized by SEND")

    async def _on_send(self, cmd: dict):
        """SEND = 最终回复 → 终结卡片（无卡片时发 Markdown 文本）

        终结失败不重发：超时可能已成功（重发=重复），业务错误重试也失败。
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        state = self._card_states.pop(receive_id, None)
        if state:
            # 保存 pending_files 副本（_do_finalize 不改变文件列表）
            saved_files = list(state.pending_files)
            try:
                await self._do_finalize(state, content)
            except Exception as e:
                logger.error(f"[FeishuAdapter] Finalize failed for {receive_id}: {e}, card content already visible, not resending")
            # 不管终结成功失败，都发 pending_files（文件不在卡片中，不受终结影响）
            if saved_files:
                from niu_feishu_adapter.feishu_api import send_file_message
                for file_info in saved_files:
                    try:
                        await send_file_message(self._client, receive_id, file_info["file_key"], file_info["filename"])
                    except Exception as e:
                        logger.error(f"[FeishuAdapter] Send file failed: {e}")
            # 对终结后仍然失败的图片，重新上传并发独立图片消息
            # 使用 state.pending_images（终结阶段 _filter_media 的结果，而非流式阶段快照）
            failed_images = [img for img in state.pending_images if img.get("failed")]
            if failed_images:
                from niu_feishu_adapter.feishu_api import upload_image, send_image_message
                for img_info in failed_images:
                    try:
                        img_key = await asyncio.to_thread(upload_image, self._app_id, self._app_secret, img_info["path"])
                        if img_key:
                            await send_image_message(self._client, receive_id, img_key)
                        else:
                            logger.warning(f"[FeishuAdapter] Image re-upload failed: {img_info.get('path', '')}")
                    except Exception as e:
                        logger.error(f"[FeishuAdapter] Send image fallback failed: {e}")
        else:
            if not receive_id:
                logger.warning("[FeishuAdapter] SEND without receive_id and no card, dropping")
                return
            from niu_feishu_adapter.feishu_api import send_markdown, send_markdown_reply, extract_md_refs
            # 清理 Markdown 图片标记（文件链接保留，用户可看到 ↑ 文件名）
            img_removals = []
            for _, _, full_match, is_image, start_idx in extract_md_refs(content):
                if is_image:
                    img_removals.append((start_idx, start_idx + len(full_match)))
            for start, end in sorted(img_removals, reverse=True):
                content = content[:start] + content[end:]
            if content.strip():
                reply_to_id = cmd.get("reply_to_id")
                if reply_to_id:
                    await send_markdown_reply(self._client, reply_to_id, content)
                else:
                    await send_markdown(self._client, receive_id, content)

    async def _on_push(self, cmd: dict):
        """PUSH = 主动推送 → 先推 open_id（个人），失败后回退 chat_id（群聊）"""
        override_id = cmd.get("channel_id", "")
        # 优先使用 open_id（个人消息），回退 chat_id（群聊）
        receive_id = override_id or self._push_open_id or self._push_chat_id
        fallback_id = self._push_chat_id if receive_id == self._push_open_id else self._push_open_id
        content = cmd.get("content", "")
        if not receive_id:
            logger.warning("[FeishuAdapter] PUSH without target, dropping")
            return
        from niu_feishu_adapter.feishu_api import send_markdown
        ok = await send_markdown(self._client, receive_id, content)
        if not ok and fallback_id and fallback_id != receive_id:
            logger.warning(f"[FeishuAdapter] Push to {receive_id} failed, retrying with {fallback_id}")
            ok = await send_markdown(self._client, fallback_id, content)
        if not ok:
            logger.error(f"[FeishuAdapter] Push failed for all targets")

    def _update_push_target(self, chat_id: str, open_id: str):
        """更新推送目标并写回 preferences.json（原子写入）"""
        import os
        import tempfile
        try:
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if not prefs_path.exists():
                return
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            feishu = prefs.get("feishu", {})
            updated = False
            if chat_id and chat_id != feishu.get("user_p2p_chat_id"):
                feishu["user_p2p_chat_id"] = chat_id
                updated = True
            if open_id and open_id != feishu.get("user_open_id"):
                feishu["user_open_id"] = open_id
                updated = True
            if updated:
                prefs["feishu"] = feishu
                dir_name = str(prefs_path.parent)
                fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(prefs, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, str(prefs_path))
                except Exception:
                    os.unlink(tmp_path)
                    raise
                logger.info(f"[FeishuAdapter] Push target updated: chat_id={chat_id}, open_id={open_id}")
        except Exception as e:
            logger.warning(f"[FeishuAdapter] Update push target failed: {e}")

    async def _do_finalize(self, state: CardState, final_content: str):
        """终结卡片：图片嵌入卡片 body"""
        from niu_feishu_adapter.feishu_api import finalize_card

        # 终结时重新过滤图片和文件（确保所有图片都已上传）
        filtered, images, files = await asyncio.to_thread(self._filter_media, final_content)
        state.pending_images = images
        state.pending_files = files

        # 构建终结卡片 body（只用成功上传的图片，失败的留给 SEND fallback）
        success_images = [img for img in state.pending_images if not img.get("failed")]
        if success_images and "[PHOTO_SEP]" in filtered:
            elements = self._build_final_body(filtered, success_images)
        else:
            display = filtered.replace("[PHOTO_SEP]", "")
            if len(display) > 18000:
                display = display[:17900] + "\n\n...[内容已截断]"
            elements = [{"tag": "markdown", "content": display, "element_id": "md1"}]

        final_card = {
            "schema": "2.0",
            "header": {"title": {"content": "Niu助手", "tag": "plain_text"},
                       "subtitle": {"content": "", "tag": "plain_text"}},
            "config": {"streaming_mode": False, "update_multi": True},
            "body": {"elements": elements},
        }
        final_json = json.dumps(final_card, ensure_ascii=False)
        state.seq += 1
        ok = await finalize_card(self._client, state.card_id, final_json, state.seq)
        if not ok:
            logger.error(f"[FeishuAdapter] Finalize failed for card {state.card_id}")

    # ── 图片处理 ──

    def _filter_media(self, content: str) -> tuple[str, list[dict], list[dict]]:
        """过滤 Markdown 图片和文件链接：上传 → 替换标记，返回 (filtered, images, files)

        图片：上传获取 img_key → 替换为 [PHOTO_SEP] → 记录到 images
        文件：上传获取 file_key → 替换为 ↑ 文件名 → 记录到 files
        """
        from niu_feishu_adapter.feishu_api import upload_image, upload_file, extract_md_refs
        images: list[dict] = []
        files: list[dict] = []
        replacements: list[tuple[int, int, str]] = []

        for alt_text, raw_path, full_match, is_image, start_idx in extract_md_refs(content):
            end_idx = start_idx + len(full_match)
            path = raw_path

            if is_image:
                if not path.startswith(_LOCAL_PATH_PREFIX) or not Path(path).exists():
                    replacements.append((start_idx, end_idx, ""))
                    continue
                img_key = upload_image(self._app_id, self._app_secret, path)
                if img_key:
                    images.append({"img_key": img_key, "alt": alt_text or "照片"})
                    replacements.append((start_idx, end_idx, "[PHOTO_SEP]"))
                else:
                    # 上传失败，记录为 failed_image，终结后发独立图片消息重试
                    images.append({"img_key": None, "alt": alt_text or "照片", "path": path, "failed": True})
                    replacements.append((start_idx, end_idx, ""))
            else:
                # 文件链接
                if not path.startswith(_LOCAL_PATH_PREFIX) or not Path(path).exists():
                    # 不是本地 tmp 文件，保留原样（可能是 URL 或其他路径）
                    continue
                file_key = upload_file(self._app_id, self._app_secret, path, alt_text or Path(path).name)
                if file_key:
                    display_name = alt_text or Path(path).name
                    files.append({"file_key": file_key, "filename": display_name})
                    replacements.append((start_idx, end_idx, f"↑ {display_name}"))
                else:
                    replacements.append((start_idx, end_idx, f"[文件上传失败: {alt_text or Path(path).name}]"))

        for start, end, repl in sorted(replacements, key=lambda x: x[0], reverse=True):
            content = content[:start] + repl + content[end:]
        return content, images, files

    @staticmethod
    def _build_final_body(filtered_content: str, pending_images: list) -> list:
        """构建终结卡片 body：markdown + img 交替"""
        elements = []
        parts = filtered_content.split("[PHOTO_SEP]")
        md_idx, img_idx = 1, 0
        for i, part in enumerate(parts):
            part = part.strip()
            if part:
                if len(part) > 18000:
                    part = part[:17900] + "\n\n...[内容已截断]"
                elements.append({"tag": "markdown", "content": part, "element_id": f"md{md_idx}"})
                md_idx += 1
            if i < len(pending_images):
                info = pending_images[i]
                elements.append({
                    "tag": "img",
                    "img_key": info["img_key"],
                    "alt": {"tag": "plain_text", "content": info.get("alt", "照片")},
                    "element_id": f"img_{img_idx}",
                })
                img_idx += 1
        if not elements:
            elements.append({"tag": "markdown", "content": filtered_content.replace("[PHOTO_SEP]", ""),
                             "element_id": "md1"})
        return elements

    # ── 通用 TCP 发送 ──

    async def _send(self, cmd: dict):
        if not self._writer:
            return
        async with self._write_lock:
            payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
            self._writer.write(len(payload).to_bytes(4, "big") + payload)
            await self._writer.drain()
