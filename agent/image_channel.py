"""图片直通通道（plan v0.5.2 §4-V3）：markdown 图标记 → 多模态 content list + 出站日志 data URI 打码。

设计（R1）：DB 存文本、出口展开——transform_history / 当前 user 消息组装调用
expand_image_markers；本模块为纯函数 + 只读文件访问，独立模块防循环 import
（http_logger / litellm_adapter 均从本模块 import mask_image_data_uris，不得反向依赖）。

判定规则（§4-V3）：
- 主会话 = 主 llm 档案 |llm vision.supported==true（T1 探测写入；无档案/读失败 → False）
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

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _mime_for(path: str) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "image/png")


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
    """读图片文件 → data URI；缺文件/读失败/超限且降采样失败 → None（调用方降级留文本+警示）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        data = p.read_bytes()
        if len(data) <= MAX_IMAGE_BYTES:
            return f"data:{_mime_for(path)};base64," + base64.b64encode(data).decode("ascii")
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
    """主会话图片直通判定：主 llm 档案 |llm vision.supported==true（T1 探测写入）。

    无 apibase/model/无档案/读失败 → False（fail-closed，不展开图片）。
    """
    try:
        from niu_api.model_probe import read_profile
        api_base = (llm_config or {}).get("apibase") or ""
        model = (llm_config or {}).get("model") or ""
        if not api_base or not model:
            return False
        profile = read_profile(api_base, model)
        vision = (profile or {}).get("vision") or {}
        return vision.get("supported") is True
    except Exception:
        return False


def preset_section_has_vision(preset_name: str) -> bool:
    """子会话图片直通判定（正向规则）：llmPreset 指向的 user-config.json 顶层段 model 非空 → True。

    第三方模型不探测（D-F），档案无其条目——手工测通为准，不查档案。读失败 → False。
    """
    try:
        from niu_api.config import CONFIG_PATH
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        section = data.get(preset_name) or {}
        return bool(section.get("model"))
    except Exception:
        return False
