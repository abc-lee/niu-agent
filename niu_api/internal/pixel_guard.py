"""像素护栏（v3.0 新增，spec D-26）：编码发送前的**唯一关口**。

只读图头取 W×H（不解码全图），双阈值判定：
- W×H 超 ``GUARD_MAX_PIXELS`` → 拒收：超大图 base64 请求体数十 MB + 十分钟级编码窗口整段
  持锁堵死文本嵌入的最坏情形被挡在服务端之外（§1.6 事故形态出不了门）；
- 文件体积超 ``GUARD_MAX_BYTES`` → 拒收：data: URI 的 body = base64(原文件)，字节数直接
  决定传输与解码成本。
被拒图片**永不发给** llama-server（记录为降级：弃行 + 带原因的 WARNING，D-26）。

它不解决什么（如实披露，R1）：护栏**之内**的图片编码窗口内，文本嵌入仍排队、可能超时
失败——只能披露、不能在本工程消除（消除 = 双实例或锁重构，超范围）。
阈值是启发式（R18）：初值取保守侧，且**低于已知最坏件**——§1.6 事故的 8.7MP 照片
（编码 17 分钟未完成 + 机器进 swap + 文本嵌入全堵）；S6 标定步用真照片实测 W×H→编码耗时
曲线后覆盖为终值并写进手册（易腐数据，§12；按曲线可升可降）。
图头读不出（未知格式/文件缺失/头截断）⇒ **放行 + WARNING**（自述契约；全函数是 total：
任何异常同样放行）——服务端 ``--image-max-tokens`` 是最后防线（超限 500 时按 D-6 的失败
路径有界重试），宁可付一次长编码窗口，不可白丢向量。
"""
from __future__ import annotations

import os
import struct

from loguru import logger

#: 初始像素预算（保守侧）：**低于已知最坏件** 8.7MP 事故照片（2026-10-13 事故：编码 17 分钟未完成 + 进 swap + 文本嵌入全堵）；S6 标定步以实测覆盖（易腐数据，不写进条款）。
GUARD_MAX_PIXELS = 8_000_000
#: 初始文件体积预算（保守侧）：30MB（base64 体最多 ≈ 40MB）。
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
    → None，绝不抛异常向外——调用方是 tick 的循环线程，异常外抛会把队首行钉死（见 ``check``）。"""
    try:
        f = open(path, "rb")
    except (OSError, ValueError):  # ValueError = 路径含 NUL（open 抛它、不是 OSError）——让「绝不抛异常向外」对一切路径形态为真
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
                    if head64[22:24] == b"\x9d\x01\x2a":
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


def check(path: str) -> tuple[bool, str]:
    """护栏检查（同步、毫秒级）：返回 (是否放行, 原因)。**total 函数：绝不向外抛异常**——
    任何异常（getsize 的 OSError/ValueError、图头解析的 struct.error/IndexError）⇒ 放行 +
    WARNING（自述契约：图头读不出 ⇒ 放行）。调用方 = tick 的循环线程：异常外抛会崩掉本轮
    tick，同一队首行每 10s 一轮崩一次、行永不删除、状态永不更新 ⇒ 媒体队列永久冻结（R14 症状）。

    文件缺失 ⇒ 放行：让编码端按「文件缺失」失败（D-24 的显式失败原因，走有界重试、
    弃单原因可辨）——护栏不代替存在性判定。
    """
    try:
        size = os.path.getsize(path)
        if size > GUARD_MAX_BYTES:
            return False, f"文件 {size / 1048576:.0f}MB 超护栏预算 {GUARD_MAX_BYTES / 1048576:.0f}MB"
        dims = _image_size(path)
        if dims is None:
            logger.warning(f"[PixelGuard] 图头读不出，放行（服务端 --image-max-tokens 是最后防线）: {path}")
            return True, ""
        w, h = dims
        if w * h > GUARD_MAX_PIXELS:
            return False, f"{w}×{h} = {w * h / 1e6:.0f}MP 超像素预算 {GUARD_MAX_PIXELS / 1e6:.0f}MP"
        return True, ""
    except Exception as e:
        logger.warning(f"[PixelGuard] 护栏检查异常，放行（服务端 --image-max-tokens 是最后防线）: {path}: {type(e).__name__}: {e}")
        return True, ""
