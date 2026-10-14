"""像素护栏（v3.0 新增，spec D-26；2026-10-13 HANDOVER D 机制演进为「客户端门槛 + 缩放」）：
编码发送前的**唯一关口**。只读图头（不解码全图）取 W×H，返回**三动作**：

- `"pass"`（直送候选）：W×H ≤ 直送门槛 ⇒ 原图字节直送候选；图头读不出（未知格式/缺失/截断）⇒
  放行 + WARNING（护栏级 fail-open；随后由 dispatcher 做**白名单分判**：白名单内读不出 ⇒
  原图直送、HEIC 依 `pillow-heif` 可选插件在位与否、白名单外 ⇒ 拦截——不再一律直送）；
- `"downscale"`（缩放）：W×H 超直送门槛 ⇒ 客户端内存中缩放到目标长边后发送（原图照旧
  入库、不落临时文件）——超门槛从「拒收」变为「缩放」，正是本演进的核心（D 节口径）；
- `"reject"`（病态拒收）：**仅**文件体积超体积预算——把「数十 MB 请求体 + 十分钟级编码
  窗口整段持锁堵死文本嵌入」的最坏情形挡在服务端之外（§1.6 事故形态出不了门）；拒收
  永不发送，记录为降级（弃行 + 带原因的 WARNING）。

直送门槛是**配置项**（`media_ingest.max_pixels`，经唯一读取点现读、缺省保守初值、改配置
免重启生效）；体积预算仍是模块常量（保守侧，病态闸）。阈值是启发式（R18）：只追求把
最坏情形挡在门外，不追求精确；「损失能不能承担」由 Agent 跟用户按手册标定流程判定。
本模块全部函数是 **total**：任何异常 ⇒ `("pass", "", None)`，绝不外抛（调用方是 tick 的
循环线程，异常外抛会把队首行钉死、队列永久冻结，R14 症状）。
"""
from __future__ import annotations

import os
import struct

from loguru import logger

#: 直送门槛缺省（保守侧，同改动前初值；`media_ingest.max_pixels` 配置值优先，见
#: ``media_ingest_config.media_ingest_max_pixels``）。
DEFAULT_MAX_PIXELS = 8_000_000
#: 体积预算（保守侧，病态闸）：超限 ⇒ 无论两分支一律拒收。
GUARD_MAX_BYTES = 30 * 1024 * 1024


def _jpeg_size(f) -> tuple[int, int] | None:
    """JPEG：流式解析尺寸段（SOF0–SOF3），对大 EXIF 前缀段稳健（逐段跳，不吃固定窗口）。"""
    f.read(2)  # SOI
    while True:
        b = f.read(1)
        if not b or b != b"\xff":
            return None
        marker = f.read(1)
        if not marker:
            return None
        m = marker[0]
        if m in (0xC0, 0xC1, 0xC2, 0xC3):  # SOFn：尺寸段
            f.read(3)  # 段长(2) + 精度(1)
            h = struct.unpack(">H", f.read(2))[0]
            w = struct.unpack(">H", f.read(2))[0]
            return w, h
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            continue  # 无长度段的独立标记（SOI/TEM/RST）
        if m == 0xD9:
            return None  # 已到 EOI 仍未见尺寸段
        seglen = struct.unpack(">H", f.read(2))[0]
        f.seek(seglen - 2, os.SEEK_CUR)  # 跳过内容段（EXIF/APPn 等）


def _image_size(path: str) -> tuple[int, int] | None:
    """只读图头取 (宽, 高)；**total 函数**：读不出（未知格式/文件缺失/头截断/切片越界）
    → None，绝不抛异常向外（见 ``classify``）。识别面：JPEG / PNG / WEBP（VP8/VP8L/VP8X）/ GIF。"""
    try:
        f = open(path, "rb")
    except (OSError, ValueError):  # ValueError = 路径含 NUL（open 抛它、不是 OSError）
        return None
    with f:
        try:
            head = f.read(64)
            if head[:2] == b"\xff\xd8":  # JPEG
                f.seek(0)
                return _jpeg_size(f)
            if head[:8] == b"\x89PNG\r\n\x1a\n":  # PNG：IHDR 固定偏移
                return struct.unpack(">I", head[16:20])[0], struct.unpack(">I", head[20:24])[0]
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":  # WEBP：三种帧
                head64 = head
                if head64[12:16] == b"VP8 ":
                    if head64[23:26] == b"\x9d\x01\x2a":
                        return (
                            struct.unpack("<H", head64[26:28])[0] & 0x3FFF,
                            struct.unpack("<H", head64[28:30])[0] & 0x3FFF,
                        )
                    return None  # 关键帧形状未识别
                if head64[12:16] == b"VP8L":
                    b0, b1, b2, b3 = head64[21], head64[22], head64[23], head64[24]
                    return (
                        1 + (((b1 & 0x3F) << 8) | b0),
                        1 + (((b3 & 0x0F) << 10) | (b2 << 2) | (b1 >> 6)),
                    )
                if head64[12:16] == b"VP8X":
                    return (
                        1 + (head64[24] | head64[25] << 8 | head64[26] << 16),
                        1 + (head64[27] | head64[28] << 8 | head64[29] << 16),
                    )
                return None
            if head[:6] in (b"GIF87a", b"GIF89a"):  # GIF：逻辑屏幕描述符
                return struct.unpack("<H", head[6:8])[0], struct.unpack("<H", head[8:10])[0]
        except Exception:  # 头截断/切片越界（struct.error、IndexError、…）⇒ 视为读不出
            return None
    return None


def classify(path: str) -> tuple[str, str, tuple[int, int] | None]:
    """护栏判定（同步、毫秒级，**total 函数：绝不向外抛异常**）：
    返回 `(action, reason, dims)`，`action ∈ "pass" | "downscale" | "reject"`。

    判定次序（I-6）：体积预算**先判**（超 ⇒ reject，两分支同判的保守闸），再像素门槛
    （现读 `media_ingest.max_pixels`，配置缺省 `DEFAULT_MAX_PIXELS`）：读得出且超 ⇒
    `"downscale"`（不再拒收，HANDOVER D 机制）；读得出且在 ⇒ `"pass"`；图头读不出 ⇒
    `"pass"`（护栏级 fail-open：WARNING 自述 + 服务端 `--image-max-tokens` 最后防线 + 下游
    dispatcher 的白名单分判：白名单内 ⇒ 直送/HEIC 转换、白名单外 ⇒ 拦截）。任何异常（getsize 的 OSError/ValueError、图头解析的
    struct.error/IndexError）⇒ `("pass", "", None)`——调用方是 tick 的循环线程：异常外抛
    会崩本轮 tick、同一队首行每轮崩一次、队列永久冻结（R14 症状）。文件缺失 ⇒ 放行：
    让编码端按「文件缺失」失败（D-24 的显式失败原因），护栏不代替存在性判定。
    """
    from niu_api.internal.media_ingest_config import media_ingest_max_pixels  # 函数级 import（同开关配方）

    try:
        size = os.path.getsize(path)
        if size > GUARD_MAX_BYTES:
            return "reject", f"文件 {size / 1048576:.0f}MB 超体积预算 {GUARD_MAX_BYTES / 1048576:.0f}MB", None
        dims = _image_size(path)
        if dims is None:
            logger.warning(f"[PixelGuard] 图头读不出，放行（服务端 --image-max-tokens 是最后防线）: {path}")
            return "pass", "", None
        w, h = dims
        threshold = media_ingest_max_pixels()
        if w * h > threshold:
            return "downscale", f"{w}×{h} = {w * h / 1e6:.0f}MP 超直送门槛 {threshold / 1e6:.0f}MP", dims
        return "pass", "", dims
    except Exception as e:
        logger.warning(f"[PixelGuard] 护栏检查异常，放行（服务端 --image-max-tokens 是最后防线）: {path}: {type(e).__name__}: {e}")
        return "pass", "", None
