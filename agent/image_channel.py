"""图片工具共享 helper（plan 2026-09-11-vision-channel-refactor：图片直通通道退役后保留项）。

- MIME 魔数探测 / 超限降采样（预算 = max_image_bytes()，默认 4MB）→ data URI（analyze_image 读图复用）
- capture_caps / max_image_bytes：截图尺寸上限与单图字节上限，每次调用现读
  user-config.json vision 段（热生效、缺键按单键回默认）——vision-server 是 preload
  长驻进程，模块级常量 = 改配置需重启（先例 niu_vision_server._vision_chain）
- mask_image_data_uris：出站日志 data URI 打码（raw_http / 交互日志不落 base64 明文）
- main_has_vision：analyze_image 选模型守卫

设计：纯函数 + 只读文件访问，独立模块防循环 import
(http_logger / litellm_adapter 均从本模块 import mask_image_data_uris，不得反向依赖)。

main_has_vision 判定：user-config.json llm 段 capabilities.input 含 "image" 且 capabilities.model
与当前模型一致（探测写入；get_llm_config 整段小写化透传；无/不匹配/读失败 → False，fail-closed）。
"""

from __future__ import annotations

import base64
import json
import re
from io import BytesIO
from pathlib import Path

from loguru import logger

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 单图字节上限**默认值**（vision.max_image_bytes 缺键回退；运行时走 max_image_bytes() 现读）

# 截图尺寸上限**默认值**（vision.capture_max_width / capture_max_height 缺键各自回退；
# 运行时走 capture_caps() 现读——禁止把本组常量当 caps 来源直接传给 capture）
_DEFAULT_CAPTURE_W = 2560
_DEFAULT_CAPTURE_H = 1600


def _vision_section() -> dict:
    """现读 user-config.json 的 vision 段（每次调用都读 → 改配置免重启热生效）。

    CONFIG_PATH **函数级 import**——保持既有 `monkeypatch("niu_api.config.CONFIG_PATH")`
    注入点生效（先例 niu_vision_server._vision_chain）。
    读失败 / JSON 坏 / vision 段类型不对 → 空 dict（不抛异常，调用方按缺键回默认）。
    """
    from niu_api.config import CONFIG_PATH
    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        sec = data.get("vision") or {}
        return sec if isinstance(sec, dict) else {}
    except Exception:
        return {}


def _positive_int(value, default: int) -> int:
    """配置值 → 正整数；缺键 / 类型不对（含 bool）/ 非正数 → default。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def capture_caps() -> dict:
    """截图尺寸上限 `{"max_width": int, "max_height": int}`——**两维都生效**。

    来源 user-config.json vision 段（capture_max_width / capture_max_height），
    缺键按单键各自回默认；每次调用现读 → 热生效。W*H 超 Zhipu 单图像素限制
    （6000×6000）或原生合成上限（268_435_456）→ 仅 logger.warning，不拦截。
    """
    sec = _vision_section()
    w = _positive_int(sec.get("capture_max_width"), _DEFAULT_CAPTURE_W)
    h = _positive_int(sec.get("capture_max_height"), _DEFAULT_CAPTURE_H)
    pixels = w * h
    if pixels > 6000 * 6000:
        logger.warning(
            f"[image_channel] 截图上限 {w}×{h}（={pixels}px）超 Zhipu 单图像素限制 "
            "6000×6000，视觉链可能拒图")
    if pixels > 268_435_456:
        logger.warning(
            f"[image_channel] 截图上限 {w}×{h}（={pixels}px）超原生合成上限 "
            "268_435_456，截图可能失败")
    return {"max_width": w, "max_height": h}


def max_image_bytes() -> int:
    """单图字节上限（analyze_image 超限降采样预算）：vision.max_image_bytes；
    缺键 / 读失败 → 默认 MAX_IMAGE_BYTES。每次调用现读 → 热生效。"""
    return _positive_int(_vision_section().get("max_image_bytes"), MAX_IMAGE_BYTES)

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


def _downsample_to_data_uri(p: Path):
    """超 max_image_bytes() 图片降采样（PIL 半尺寸 ×≤3，JPEG q85）→ (data URI, meta)；仍超限/解码失败 → None。

    meta = {"w","h","format","bytes","downsampled"}——记**最终**（降采样后）尺寸/格式/字节数，
    downsampled=True（供 analyze_image 回执注记与日志）。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    budget = max_image_bytes()
    try:
        with Image.open(p) as img:
            for _ in range(3):
                w, h = img.size
                if min(w, h) <= 16:
                    break
                nw, nh = max(1, w // 2), max(1, h // 2)
                img = img.resize((nw, nh))
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=85)
                data = buf.getvalue()
                if len(data) <= budget:
                    uri = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                    meta = {"w": nw, "h": nh, "format": "jpeg",
                            "bytes": len(data), "downsampled": True}
                    return uri, meta
        return None
    except Exception:
        return None


def _image_to_data_uri(path: str):
    """读图片文件 → (data URI, meta)；media type 按**实际载荷**魔数探测（审计 #8——扩展名不可信）；
    缺文件/非图片/未知类型/超限且降采样失败 → None（调用方降级留文本+警示）。

    meta = {"w","h","format","bytes","downsampled"}：直读路径 w/h 用 PIL open 取
    （PIL 解不开的载荷如无 pillow-heif 的 HEIC → None，不影响直读成功），
    format 来自魔数探测、bytes = 文件字节数、downsampled=False；降采样路径透传
    _downsample_to_data_uri 的 meta（最终尺寸/格式/字节数，downsampled=True）。
    """
    try:
        p = Path(path)
        if not p.is_file():
            return None
        data = p.read_bytes()
        mime = _detect_image_mime(data[:16])
        if mime is None:
            # 非图片载荷（假扩展名/损坏文件）——不猜 media type，直接降级
            return None
        if len(data) <= max_image_bytes():
            uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            w = h = None
            try:
                from PIL import Image
                with Image.open(p) as img:
                    w, h = img.size
            except Exception:
                pass  # PIL 解不开（如无 pillow-heif 的 HEIC）：w/h 留 None
            meta = {"w": w, "h": h, "format": mime.split("/", 1)[1],
                    "bytes": len(data), "downsampled": False}
            return uri, meta
        return _downsample_to_data_uri(p)
    except Exception:
        return None


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
    """analyze_image 选模型守卫：llm 段 capabilities.input 含 "image" 且 capabilities.model
    与当前模型一致（探测写入 user-config.json；get_llm_config 整段小写化透传）。

    无 capabilities / model 不匹配（换模型后旧能力不采信）/ 读失败 → False（fail-closed，不采信旧能力）。
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
