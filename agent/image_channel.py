"""图片直通通道（plan v0.5.2 §4-V3）：markdown 图标记 → 多模态 content list + 出站日志 data URI 打码。

设计（R1）：DB 存文本、出口展开——transform_history / 当前 user 消息组装调用
expand_image_markers；本模块为纯函数 + 只读文件访问，独立模块防循环 import
（http_logger / litellm_adapter 均从本模块 import mask_image_data_uris，不得反向依赖）。

判定规则（§4-V3）：
- 主会话 = user-config.json llm 段 capabilities.input 含 "image" 且 capabilities.model
  与当前模型一致（T1 探测写入；get_llm_config 整段小写化透传；无/不匹配/读失败 → False）
- 子会话 = 该子 Agent frontmatter llmPreset 指向段 model 非空 → True
  （第三方模型不探测 D-F，档案无其条目——手工测通为准，不查档案）
"""

from __future__ import annotations

import base64
import json
import os
import re
from io import BytesIO
from pathlib import Path

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 单图上限；超限 → PIL 降采样，仍超限/解码失败 → 留文本+警示

# markdown 图标记 ![名称](目标)——目标为不含 ')' 的串（[^)]+）：内嵌空格接受
# （含空格路径如 CJK 目录名正常展开），仅 strip() 首尾空白；不支持 <...>/百分号解码
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# 出站日志打码：image data URI（base64 载荷段单独捕获用于计长）
_DATA_URI_RE = re.compile(r"data:image/[A-Za-z0-9+.\-]+;base64,([A-Za-z0-9+/=]+)")

# HEIF/HEIC ISO BMFF ftyp brand 集（魔数第 8-12 字节）——.heic/.heif 家族
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}


def _detect_image_mime(data: bytes) -> str | None:
    """按**实际载荷**魔数探测 media type（审计 #8——扩展名猜测会把非图片标成 image/png）。

    未知/非图片 → None（调用方降级留文本+警示）。只读头部字节，不解码全文件。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    # ISO BMFF（HEIF/HEIC）：第 4-8 字节 "ftyp" + brand 在 HEIF 集内
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def _downsample_to_data_uri(p: Path) -> str | None:
    """>4MB 图片降采样（PIL 半尺寸 ×≤3，JPEG q85）→ data URI；仍超限/解码失败 → None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(p) as img:
            for _ in range(3):
                w, h = img.size
                if min(w, h) <= 16:
                    break
                img = img.resize((max(1, w // 2), max(1, h // 2)))
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=85)
                data = buf.getvalue()
                if len(data) <= MAX_IMAGE_BYTES:
                    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        return None
    except Exception:
        return None


def _image_to_data_uri(path: str) -> str | None:
    """读图片文件 → data URI；media type 按**实际载荷**魔数探测（审计 #8——扩展名不可信）；
    缺文件/非图片/未知类型/超限且降采样失败 → None（调用方降级留文本+警示）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        data = p.read_bytes()
        mime = _detect_image_mime(data[:16])
        if mime is None:
            # 非图片载荷（假扩展名/损坏文件）——不猜 media type，直接降级
            return None
        if len(data) <= MAX_IMAGE_BYTES:
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        return _downsample_to_data_uri(p)
    except Exception:
        return None


def expand_image_markers(content, has_vision: bool = False):
    """消息文本中 markdown 图标记 `![名称](绝对路径)` → 多模态 content list（纯函数）。

    - has_vision=False / 非 str / 无标记 → 原样返回（默认 False 既有调用零影响）
    - 文件存在且 ≤4MB → text 段 + image_url data URI 段（alt 文本非空时前置为 text 段）
    - 文件 >4MB → PIL 降采样；仍超限/解码失败 → 留标记原文 + [图片不可读] 警示
    - 缺文件/读失败 → 留标记原文 + [图片不可读] 警示（R5：tmp 24h 清理语义）
    - 非本地路径（http(s)://、data:、相对路径）→ 留标记原文，不展开不警示

    Returns: str（未展开）或 list[dict]（OpenAI 多模态 content；Claude list 兼容先例）。
    """
    if not has_vision or not isinstance(content, str):
        return content
    matches = list(_MD_IMAGE_RE.finditer(content))
    if not matches:
        return content
    parts: list[dict] = []
    pos = 0
    for m in matches:
        before = content[pos:m.start()]
        if before:
            parts.append({"type": "text", "text": before})
        name, target = m.group(1), m.group(2).strip()
        if not os.path.isabs(target) or target.startswith(("http://", "https://", "data:")):
            # 非本地绝对路径 → 原样保留（web 图/data URI 不是截图落盘产物）
            parts.append({"type": "text", "text": content[m.start():m.end()]})
        else:
            data_uri = _image_to_data_uri(target)
            if data_uri is not None:
                if name:
                    parts.append({"type": "text", "text": name})
                parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            else:
                parts.append({
                    "type": "text",
                    "text": f"{content[m.start():m.end()]} [图片不可读，无法显示: {target}]",
                })
        pos = m.end()
    tail = content[pos:]
    if tail:
        parts.append({"type": "text", "text": tail})
    return parts


def mask_image_data_uris(obj):
    """递归替换 dict/list/str 中 image data URI 为 `[image data, N bytes]`（出站日志打码，plan R2）。

    N = base64 载荷长度。纯函数返回新对象；其他类型原样返回。
    三写函数共用：http_logger._write_log_entry / litellm_adapter._write_raw_log /
    _write_interaction_log（§3 P2：_format_request_log 是 formatter，打码在写函数入口）。
    """
    if isinstance(obj, str):
        return _DATA_URI_RE.sub(lambda m: f"[image data, {len(m.group(1))} bytes]", obj)
    if isinstance(obj, dict):
        return {k: mask_image_data_uris(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_image_data_uris(v) for v in obj]
    return obj


def main_has_vision(llm_config: dict | None) -> bool:
    """主会话图片直通判定：llm 段 capabilities.input 含 "image" 且 capabilities.model
    与当前模型一致（T1 探测写入 user-config.json；get_llm_config 整段小写化透传）。

    无 capabilities / model 不匹配（换模型后旧能力不采信）/ 读失败 → False（fail-closed，不展开图片）。
    """
    try:
        cfg = llm_config or {}
        caps = cfg.get("capabilities")
        if not isinstance(caps, dict):
            return False
        if caps.get("model") != cfg.get("model"):
            return False
        return "image" in (caps.get("input") or [])
    except Exception:
        return False


def preset_section_has_vision(preset_name: str, config_data: dict | None = None) -> bool:
    """子会话图片直通判定（正向规则）：llmPreset 指向的 user-config.json 顶层段 model 非空 → True。

    第三方模型不探测（D-F），档案无其条目——手工测通为准，不查档案。读失败 → False。
    config_data=None → 自读 CONFIG_PATH（独立调用）；传入预读 dict → 直接消费
    （子 Agent 派发链覆盖侧/判定侧共用同一读盘结果，避免重复读文件）。
    """
    try:
        if config_data is None:
            from niu_api.config import CONFIG_PATH
            config_data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        section = config_data.get(preset_name) or {}
        return bool(section.get("model"))
    except Exception:
        return False
