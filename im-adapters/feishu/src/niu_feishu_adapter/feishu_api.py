"""飞书 API 封装 — 上传/下载/发消息/卡片操作

所有飞书 REST API 调用集中在此文件，adapter.py 不直接调飞书 API。
"""
import asyncio
import json
import re
import time
from pathlib import Path

import requests as _requests
from loguru import logger

TEMP_DIR = Path.home() / ".niu" / "tmp"


# ── tenant token ──

_token_cache: dict = {"token": "", "expires_at": 0.0, "app_id": "", "app_secret": ""}


def _get_tenant_token(app_id: str, app_secret: str) -> str | None:
    """获取 tenant_access_token（带缓存，提前5分钟刷新）"""
    now = time.monotonic()
    if (_token_cache["token"] and _token_cache["app_id"] == app_id
            and _token_cache["expires_at"] > now + 300):
        return _token_cache["token"]
    try:
        resp = _requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Token failed: {result.get('msg', '')}")
            return None
        token = result["tenant_access_token"]
        expire = result.get("expire", 7200)
        _token_cache.update(token=token, expires_at=now + expire, app_id=app_id, app_secret=app_secret)
        return token
    except Exception as e:
        logger.error(f"[FeishuAPI] Token error: {e}")
        return None


# ── receive_id_type 推断 ──

def infer_receive_id_type(receive_id: str) -> str:
    """根据 ID 前缀推断 receive_id_type"""
    if not receive_id:
        return "chat_id"
    if "@" in receive_id:
        return "email"
    if receive_id.startswith("oc_"):
        return "chat_id"
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "user_id"


def extract_md_refs(content: str) -> list[tuple[str, str, str, bool, int]]:
    """括号平衡解析 Markdown 图片和链接引用，支持文件名中的括号"""
    results = []
    i = 0
    while i < len(content):
        # 检测 ![ (图片) 或 [ (链接)
        is_image = False
        if content[i] == '!' and i + 1 < len(content) and content[i + 1] == '[':
            is_image = True
            start = i
            i += 2  # 跳过 ![
        elif content[i] == '[':
            start = i
            i += 1
        else:
            i += 1
            continue

        # 找 alt_text（到第一个 ]）
        alt_start = i
        while i < len(content) and content[i] != ']':
            i += 1
        if i >= len(content):
            continue
        alt_text = content[alt_start:i]
        i += 1  # 跳过 ]

        # 必须紧跟 (
        if i >= len(content) or content[i] != '(':
            continue
        i += 1  # 跳过 (

        # 括号平衡找 path
        path_start = i
        depth = 1
        while i < len(content) and depth > 0:
            if content[i] == '(':
                depth += 1
            elif content[i] == ')':
                depth -= 1
            i += 1
        if depth != 0:
            continue
        raw_path = content[path_start:i - 1]
        full_match = content[start:i]
        results.append((alt_text, raw_path, full_match, is_image, start))
    return results


# ── 上传 ──

def upload_image(app_id: str, app_secret: str, local_path: str) -> str | None:
    """上传图片到飞书，返回 image_key"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    p = Path(local_path)
    if not p.exists():
        return None
    is_compressed = False
    actual_path = p
    try:
        actual_path = compress_image(p) or p
        is_compressed = actual_path != p
        with open(str(actual_path), "rb") as f:
            resp = _requests.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (p.name, f)},
                timeout=30,
            )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Upload image failed: {result.get('msg', '')}")
            return None
        return result.get("data", {}).get("image_key", "") or None
    except Exception as e:
        logger.error(f"[FeishuAPI] Upload image error: {e}")
        return None
    finally:
        if is_compressed and actual_path.exists():
            try:
                actual_path.unlink()
            except Exception:
                pass


def upload_file(app_id: str, app_secret: str, local_path: str, filename: str) -> str | None:
    """上传文件到飞书，返回 file_key"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    p = Path(local_path)
    if not p.exists():
        return None
    clean_name = re.sub(r'[\x00-\x1f\x7f"\\]', '_', filename or p.name)[:200]
    try:
        with open(str(p), "rb") as f:
            resp = _requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": "stream", "file_name": clean_name},
                files={"file": (clean_name, f, "application/octet-stream")},
                timeout=60,
            )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Upload file failed: {result.get('msg', '')}")
            return None
        return result.get("data", {}).get("file_key", "") or None
    except Exception as e:
        logger.error(f"[FeishuAPI] Upload file error: {e}")
        return None


# ── 下载 ──

def download_resource(app_id: str, app_secret: str, file_key: str,
                      rtype: str, file_name: str = "",
                      message_id: str = "") -> str | None:
    """下载飞书资源到本地，返回本地路径"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    name = file_name or f"{rtype}_{file_key}"
    if rtype == "image":
        name = name if '.' in name else f"{name}.jpg"
    local_path = TEMP_DIR / f"feishu_in_{file_key[:20]}_{name}"
    if local_path.exists():
        return str(local_path)
    try:
        # 主路径：message_resource（需要 message_id）
        if message_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            params = {"type": rtype}
        elif rtype == "image":
            url = f"https://open.feishu.cn/open-apis/im/v1/images/{file_key}"
            params = {}
        else:
            url = f"https://open.feishu.cn/open-apis/im/v1/files/{file_key}"
            params = {}
        resp = _requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=30,
        )
        if resp.status_code == 200:
            tmp = local_path.with_suffix(local_path.suffix + ".dl")
            tmp.write_bytes(resp.content)
            tmp.replace(local_path)
            return str(local_path)
        logger.error(f"[FeishuAPI] Download failed: {resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"[FeishuAPI] Download error: {e}")
        return None


async def send_file_message(client, receive_id: str, file_key: str, filename: str) -> bool:
    """发送飞书文件消息（独立于卡片）"""
    receive_id_type = infer_receive_id_type(receive_id)
    content = json.dumps({"file_key": file_key})
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("file")
                .content(content)
                .build()) \
            .build()
        resp = await asyncio.to_thread(client.im.v1.message.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Send file message failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Send file message error: {e}")
        return False


async def send_image_message(client, receive_id: str, image_key: str) -> bool:
    """发送飞书图片消息（独立于卡片，用于图片无法嵌入卡片时的 fallback）"""
    receive_id_type = infer_receive_id_type(receive_id)
    content = json.dumps({"image_key": image_key})
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("image")
                .content(content)
                .build()) \
            .build()
        resp = await asyncio.to_thread(client.im.v1.message.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Send image message failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Send image message error: {e}")
        return False


def compress_image(img_path: Path) -> Path | None:
    """压缩超过10MB的图片为JPEG，返回原路径或临时压缩路径。无法压缩时返回None"""
    if img_path.stat().st_size <= 10 * 1024 * 1024:
        return img_path
    try:
        from PIL import Image
        img = Image.open(str(img_path))
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        tmp = TEMP_DIR / f"compressed_{img_path.stem}.jpg"
        for quality in (85, 70, 55, 40, 25):
            rgb.save(str(tmp), "JPEG", quality=quality)
            if tmp.stat().st_size <= 10 * 1024 * 1024:
                img.close()
                return tmp
        img.close()
        logger.warning(f"[FeishuAPI] Image still >10MB after compression: {img_path.name}")
        return None
    except Exception as e:
        logger.error(f"[FeishuAPI] compress_image error: {e}")
        return None


# ── 发送消息 ──

async def send_markdown(client, target: str, content: str) -> bool:
    """发送 Markdown 消息（包装为流式卡片格式，确保正确渲染）"""
    receive_id_type = infer_receive_id_type(target)
    card = json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }, ensure_ascii=False)
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(target)
                .msg_type("interactive")
                .content(card)
                .build()) \
            .build()
        resp = await asyncio.to_thread(client.im.v1.message.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Send markdown failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Send markdown error: {e}")
        return False


async def send_markdown_reply(client, message_id: str, content: str) -> bool:
    """回复消息（Markdown 卡片格式），用于群聊回复"""
    card = json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }, ensure_ascii=False)
    try:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type("interactive").content(card).build()) \
            .build()
        resp = await asyncio.to_thread(client.im.v1.message.reply, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Send markdown reply failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Send markdown reply error: {e}")
        return False


# ── 流式卡片操作 ──

async def create_card(client, receive_id: str, content: str,
                       reply_to_id: str | None = None) -> tuple[str, str | None]:
    """创建流式卡片，返回 (card_id, message_id)"""
    card_json = json.dumps({
        "schema": "2.0",
        "header": {"title": {"content": "Niu助手", "tag": "plain_text"},
                   "subtitle": {"content": "思考中...", "tag": "plain_text"}},
        "config": {"streaming_mode": True, "update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": content, "element_id": "md1"}]},
    }, ensure_ascii=False)
    try:
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest, CreateCardRequestBody,
        )
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        # 创建卡片实体
        body = CreateCardRequestBody.builder().type("card_json").data(card_json).build()
        req = CreateCardRequest.builder().request_body(body).build()
        resp = await asyncio.to_thread(client.cardkit.v1.card.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Create card entity failed: {resp.code} {resp.msg}")
            return "", None
        card_id = resp.data.card_id

        # 发送卡片消息
        card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
        msg_id = None
        if reply_to_id:
            # 群聊：回复消息
            send_req = ReplyMessageRequest.builder() \
                .message_id(reply_to_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .msg_type("interactive").content(card_ref).build()) \
                .build()
            send_resp = await asyncio.to_thread(client.im.v1.message.reply, send_req)
            if send_resp.success():
                msg_id = send_resp.data.message_id
        else:
            # 单聊：新建消息
            receive_id_type = infer_receive_id_type(receive_id)
            send_req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive").content(card_ref).build()) \
                .build()
            send_resp = await asyncio.to_thread(client.im.v1.message.create, send_req)
            if send_resp.success():
                msg_id = send_resp.data.message_id

        if not (send_resp.success()):
            # 发送失败，无法清理孤立卡片（CardKit 没有 delete card API）
            logger.error(f"[FeishuAPI] Send card message failed: {send_resp.code}, orphaned card_id={card_id}")
            return "", None

        return card_id, msg_id
    except Exception as e:
        logger.error(f"[FeishuAPI] Create card error: {e}")
        return "", None


async def update_card_element(client, card_id: str, content: str, seq: int):
    """更新卡片的 markdown 元素"""
    try:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )
        req = ContentCardElementRequest.builder() \
            .card_id(card_id).element_id("md1") \
            .request_body(ContentCardElementRequestBody.builder()
                .content(content).sequence(seq).uuid(f"niu-{card_id[-6:]}-stream-{seq}").build()) \
            .build()
        resp = await asyncio.to_thread(client.cardkit.v1.card_element.content, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Update element failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Update element error: {e}")
        return False


async def finalize_card(client, card_id: str, final_json: str, seq: int):
    """终结卡片：Settings API 关闭 streaming_mode + UpdateCard 写完整内容"""
    try:
        from lark_oapi.api.cardkit.v1 import (
            SettingsCardRequest, SettingsCardRequestBody,
            UpdateCardRequest, UpdateCardRequestBody, Card,
        )
        # 1. 关闭 streaming_mode
        settings_json = json.dumps({"config": {"streaming_mode": False}})
        settings_req = SettingsCardRequest.builder() \
            .card_id(card_id) \
            .request_body(SettingsCardRequestBody.builder()
                .settings(settings_json).sequence(seq)
                .uuid(f"niu-{card_id[-6:]}-finalize-settings").build()) \
            .build()
        settings_resp = await asyncio.to_thread(client.cardkit.v1.card.settings, settings_req)
        if not settings_resp.success():
            logger.error(f"[FeishuAPI] Finalize settings failed: {settings_resp.code} {settings_resp.msg}")
            return False

        # 2. 更新完整内容
        new_seq = seq + 1
        update_req = UpdateCardRequest.builder() \
            .card_id(card_id) \
            .request_body(UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(final_json).build())
                .sequence(new_seq).uuid(f"niu-{card_id[-6:]}-finalize-update").build()) \
            .build()
        resp = await asyncio.to_thread(client.cardkit.v1.card.update, update_req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Finalize card failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Finalize card error: {e}")
        return False
