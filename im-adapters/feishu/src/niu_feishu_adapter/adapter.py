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


class CardState:
    """单个 channel 的流式卡片状态"""
    __slots__ = ("card_id", "seq", "message_id", "receive_id", "reply_to_id",
                 "pending_images", "pending_files", "last_content", "accumulated")

    def __init__(self, card_id: str, receive_id: str, reply_to_id: str | None = None):
        self.card_id = card_id
        self.seq = 0
        self.message_id: str | None = None
        self.receive_id = receive_id
        self.reply_to_id = reply_to_id
        self.pending_images: list[dict] = []
        self.pending_files: list[dict] = []
        self.last_content = ""
        self.accumulated = ""  # 累计全文（流式追加，飞书要求每次传累计，前缀一致→打字机续打）


def _truncate_card_text(text: str, max_bytes: int = 29500) -> str:
    """飞书卡片内容字节守卫：官方卡片 JSON ≤30KB（error 200860）、content ≤100000 字符。
    按 UTF-8 字节截（CJK 3B/字——17900 字×3≈53KB 超 30KB）。create/update/finalize 共用。
    默认 CUT_BYTES=29500——29500+22 后缀+~250 wrapper≈29772，十进制 30KB(30000) 与
    1024 口径(30720) 都安全（官方未定义 KB 单位，猜错代价=丢回复）。
    max_bytes：多段总预算分摊时传按占比计算的段预算（见 _build_final_body）。"""
    SUFFIX = "\n\n...[内容已截断]"
    if len(text.encode('utf-8')) <= max_bytes:
        return text
    cut = text.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
    return cut + SUFFIX


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
        # v11/v12：channel 处于「ask_user 终结后待新卡/重复 SEND」态——ask_user 专用
        # SEND（ask_finalize=True）记标记；route_out 重复 SEND（无 ask_finalize）被跳过时清标记
        self._ask_finalized: set[str] = set()
        # ImplReview-P2-2：ask_user 终结时的 final_content 记录——无 state 判重改「内容双重判定」：
        # 仅当 route_out SEND content == 终结内容（真重复：2b 无新 chunk 整轮已显示）才跳过；
        # runner return_value 兜底文本（CONTEXT_OVERFLOW/STOPPED/错误）≠ 终结内容 → 正常 send_markdown
        # （无条件跳过会把兜底吞掉——pre-patch 会发，新回归）
        self._ask_finalized_content: dict[str, str] = {}

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
                self._ask_finalized.clear()  # R11-B-P3：标记随卡片状态一起失效（防重连后 turn2 无流式 SEND 被误跳）
                self._ask_finalized_content.clear()  # P2-2：内容记录与标记同步失效
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
        """STREAM = 增量内容 → 创建/更新卡片（纯文本显示，不处理图片）

        图片上传和嵌入只在 SEND 终结阶段做，原因：
        1. STREAM 发的是增量 chunk，图片引用可能不完整
        2. 流式阶段上传图片后 [PHOTO_SEP] 被删掉，图片信息丢失
        3. 同一张图片被上传两次（STREAM + SEND），浪费且 img_key 不一致

        飞书契约：每次 PUT card-element/content 传「元素累计全文」（非增量），
        平台自动算增量做打字机——前缀一致→续打，前缀不同→整体替换。
        因此这里把每个 chunk 追加到 state.accumulated，update/create 都用累计全文。
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        reply_to_id = cmd.get("reply_to_id")

        from niu_feishu_adapter.feishu_api import create_card, update_card_element

        state = self._card_states.get(receive_id)

        # 空内容 = 信号通知（保持卡片活跃），只递增 seq
        if not content:
            if not state:
                return
            state.seq += 1
            return

        # 累积全文（飞书要求每次传累计，前缀一致→打字机续打）
        if not state:
            # 建卡：内容=首 chunk 的字节截断版（超大首 chunk 防 30KB 建卡失败 200860）
            init_display = _truncate_card_text(content)
            card_id, msg_id = await create_card(self._client, receive_id, init_display, reply_to_id)
            if not card_id:
                logger.error(f"[FeishuAdapter] Card creation failed for {receive_id}")
                return
            state = CardState(card_id, receive_id, reply_to_id)
            state.message_id = msg_id
            state.seq = 1
            # R3-P3：accumulated 存「原始首 chunk」作种子（非截断 init_display）——
            # 截断只作用于显示值，raw 全文留给后续累积与 finalize（防首 chunk 尾部永久丢失）
            state.accumulated = content
            # v11（R7-A-P3 + R8 修订）：建新卡清除 ask_finalized 标记（新卡出现，后续 SEND 走 state 分支）
            self._ask_finalized.discard(receive_id)
            self._ask_finalized_content.pop(receive_id, None)  # P2-2：内容记录同步清除
            self._card_states[receive_id] = state
        else:
            state.accumulated = state.accumulated + content          # 追加增量到累计全文
            state.seq += 1
            # 字节守卫（官方卡片 JSON ≤30KB error 200860 / content ≤100000 字符）：
            display = _truncate_card_text(state.accumulated)
            await update_card_element(self._client, state.card_id, display, state.seq)
        state.last_content = state.accumulated

    async def _on_send(self, cmd: dict):
        """SEND = 最终回复 → 终结卡片（无卡片时发 Markdown 文本）

        终结失败不重发：超时可能已成功（重发=重复），业务错误重试也失败。
        v11 权威实现：F3 三重条件（best-effort）+ try/except + 媒体回退保留
        （pending_files/failed_images——_do_finalize 只 populate 不发，此处是唯一投递路径）
        + ask_finalized 状态判重（ask_user 终结后 route_out 重复 SEND 跳过）。
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        state = self._card_states.pop(receive_id, None)
        if state:
            # F3 三重条件（R5-P1）：cmd.content 非空 且 startswith(accumulated) 且 len 比 <0.9 →
            # 用 cmd.content 补全（流式中断语义）；否则用 accumulated（卡 A 空串跳过、卡 B 后缀不触发、正常流相等）
            # R9-P2 注记：F3 为 best-effort——startswith 比较受归一化失配影响（chunk 边界空白），
            # 未命中时终结用 accumulated（卡片已显示内容，无功能回归）；命中时补全尾部。
            # P3-3（ImplReviewFix）：route_out SEND content 正常路径已去 @ 段（persist_agent_reply
            # strip_at_messages；rv=None 兜底且 @ 未匹配时可能保留字面 @，非子 Agent 指令，无重注入）——
            # F3 send_content 无子 Agent @ 指令，重注入风险已缓解（无逻辑改动）。
            send_content = cmd.get("content", "")
            if (send_content and send_content.startswith(state.accumulated)
                    and len(state.accumulated) < len(send_content) * 0.9):
                final_content = send_content
            else:
                final_content = state.accumulated
            try:
                await self._do_finalize(state, final_content)
            except Exception as e:
                logger.error(f"[FeishuAdapter] Finalize failed for {receive_id}: {e}")
            # 【媒体回退保留——R8-P1/A+B】终结后发送独立文件消息（文件不在卡片中）
            if state.pending_files:
                from niu_feishu_adapter.feishu_api import send_file_message
                for file_info in state.pending_files:
                    try:
                        await send_file_message(self._client, receive_id, file_info["file_key"], file_info["filename"])
                    except Exception as e:
                        logger.error(f"[FeishuAdapter] Send file failed: {e}")
            # 终结后对上传失败的图片，重新上传并发独立图片消息
            failed_images = [img for img in state.pending_images if img.get("failed")]
            if failed_images:
                from niu_feishu_adapter.feishu_api import upload_image, send_image_message
                for img_info in failed_images:
                    try:
                        img_key = await asyncio.to_thread(upload_image, self._app_id, self._app_secret, img_info["path"])
                        if img_key:
                            await send_image_message(self._client, receive_id, img_key)
                    except Exception as e:
                        logger.error(f"[FeishuAdapter] Image fallback failed: {e}")
            # ask_user 终结（ask_finalize=True）→ 记标记 + 拼接记录终结内容（ImplReviewFix-P2-2：
            # 多轮 ask_user 各轮终结内容拼接——2c 场景 route_out 整轮 a1+a2 与拼接记录相等才判重跳过；
            # 单轮 dict 覆盖会丢 a1 → 判重 miss 整轮重复）
            if cmd.get("ask_finalize"):
                self._ask_finalized.add(receive_id)
                self._ask_finalized_content[receive_id] = (self._ask_finalized_content.get(receive_id, "") + final_content)
        else:
            if not content:
                return
            if receive_id in self._ask_finalized and not cmd.get("ask_finalize"):
                # route_out 重复 SEND 判重（ImplReview-P2-2：内容双重判定替代无条件跳过）：
                # 仅当 content == ask_user 终结内容（真重复：2b 无新 chunk，整轮已显示）→ 跳过 + 清标记；
                # 否则（runner return_value 兜底文本——CONTEXT_OVERFLOW/STOPPED/错误，pre-patch 会
                # send_markdown，无条件跳过会吞掉用户回答后该看到的兜底）→ 落 send_markdown 正常发。
                # 注记：2b 场景 content 与终结内容因归一化失配可能 miss → 低频重复（方案已注明接受）。
                if content == self._ask_finalized_content.get(receive_id):
                    self._ask_finalized.discard(receive_id)
                    self._ask_finalized_content.pop(receive_id, None)
                    return
                # 非重复（兜底文本）→ 清标记防跨轮残留（R6-P2），落 send_markdown
                self._ask_finalized.discard(receive_id)
                self._ask_finalized_content.pop(receive_id, None)
            # ask_user 问题（ask_finalize=True，无 state）→ 正常 send_markdown，【不清标记】（保留供 route_out 判重）
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
            display = _truncate_card_text(filtered.replace("[PHOTO_SEP]", ""))
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
                # 本地路径检查：只要不是 URL/data URI 且文件存在就上传（与旧代码一致）
                if not path or path.startswith(("http://", "https://", "ftp://", "data:")):
                    continue
                if not Path(path).exists():
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
                if not path or path.startswith(("http://", "https://", "ftp://", "data:", "mailto:")):
                    continue
                if not Path(path).exists():
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
        """构建终结卡片 body：markdown + img 交替。

        R4-P1：每段截断改字节守卫 _truncate_card_text（17900 字符对 CJK 超 30KB，error 200860）。
        R5-P2 + ImplReview-P2-1：总预算分摊计元素开销——wrapper 220B（含 subtitle）/ md 元素
        55B/个 / img 元素 135B/个（含 img_key）/ 截断后缀 26B/段（转义后）/ json.dumps 转义
        1.08×（实测：LLM markdown 转义主导源 \n→\\n 2×，~4.6% 扩展；CJK/英文不转义）。
        最终卡片 JSON 总字节 ≤ 30000（escape ≤8% 时两种 30KB 口径安全）。截断放分段后
        （防切坏 [PHOTO_SEP] 标记）。注记：病态内容（如 40KB 纯引号/反斜杠，转义 ~2×）
        可能超限——超限时终结 200860 失败，属极低概率边界（正常回复为散文），接受并注明。
        """
        TOTAL_BUDGET = 30000
        WRAPPER_BUDGET = 220      # schema/header(含 subtitle)/config/body 骨架（ImplReviewFix 实测）
        MD_ELEMENT_BUDGET = 55    # 每个 markdown 元素键开销（tag/content/element_id）
        IMG_ELEMENT_BUDGET = 135  # 每个 img 元素键开销（含 img_key/alt 结构）
        ESCAPE_FACTOR = 1.08      # json.dumps 转义预留（主导源 \n→\\n 2×，LLM markdown ~4.6% 扩展）
        TRUNC_SUFFIX_BYTES = 26   # 截断后缀 "\n\n...[内容已截断]" 转义后字节（每段 +26B）

        parts = filtered_content.split("[PHOTO_SEP]")
        md_parts = [p.strip() for p in parts if p.strip()]
        n_md = len(md_parts)

        # 总预算分摊：text 原始字节预算 = (总预算 - 骨架 - 元素开销 - 截断后缀预留×段数) / 转义系数
        text_budget = (TOTAL_BUDGET - WRAPPER_BUDGET
                       - MD_ELEMENT_BUDGET * n_md - IMG_ELEMENT_BUDGET * len(pending_images)
                       - TRUNC_SUFFIX_BYTES * n_md)
        if md_parts and text_budget > 0:
            text_budget = int(text_budget / ESCAPE_FACTOR)
            total_bytes = sum(len(p.encode('utf-8')) for p in md_parts)
            if total_bytes > text_budget:
                md_parts = [
                    _truncate_card_text(p, max_bytes=max(1, int(len(p.encode('utf-8')) / total_bytes * text_budget)))
                    for p in md_parts
                ]

        elements = []
        md_idx, img_idx = 1, 0
        md_iter = iter(md_parts)
        for i, part in enumerate(parts):
            part = part.strip()
            if part:
                md_content = next(md_iter, part)
                elements.append({"tag": "markdown", "content": md_content, "element_id": f"md{md_idx}"})
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
