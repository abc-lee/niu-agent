"""像素护栏行为套件（plan §5 S4a，spec D-26）：双阈值分判（含 VP8/VP8L 正向尺寸解码）+ total 函数回归 + 两种拒收原因可辨 + WARNING 契约。

全部用临时合成文件（图头按格式规范构造，只保证 64 字节窗口内可解析），不碰真实图片。
total 函数回归覆盖第一轮 P1-B 的四个复现输入（20B 截断 PNG 头 / 截断 VP8X / SOF0 后截断的
JPEG / 路径含 NUL）及其余形态（空文件 / 文件缺失 / 8 字节 PNG / 截断 GIF / HEIC 未知头）：
任何此类输入 ``check`` 零抛异常、按契约放行 + WARNING。
"""
import struct

import pytest

from niu_api.internal import pixel_guard


def _png_header(w: int, h: int) -> bytes:
    """合成 PNG 图头：签名 + IHDR 段（W/H 在固定偏移 16/20）+ 填充尾。"""
    return (
        b"\x89PNG\r\n\x1a\n"            # 签名（8B）
        + b"\x00\x00\x00\x0d"           # IHDR 段长 13
        + b"IHDR"
        + struct.pack(">II", w, h)      # 宽(16-19) 高(20-23)
        + b"\x08\x06\x00\x00\x00"       # 色深/颜色类型/压缩/滤波/隔行
        + b"\x00" * 32                  # CRC 与后续段（护栏不读）
    )


def _jpeg(w: int, h: int, exif_payload: bytes = b"") -> bytes:
    """合成 JPEG：SOI [+ APP1/EXIF 前缀段] + SOF0（宽/高）。"""
    out = b"\xff\xd8"  # SOI
    if exif_payload:
        payload = b"Exif\x00\x00" + exif_payload
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out += b"\xff\xc0" + struct.pack(">H", 0x0A) + b"\x08" + struct.pack(">HH", h, w) + b"\x01"
    return out


def _jpeg_truncated_after_sof0() -> bytes:
    """SOF0 后截断：SOI + 长 EXIF 前缀 + SOF0 段头（段长/精度）即止，宽高两字节缺失。"""
    exif = b"\xa5" * 512
    payload = b"Exif\x00\x00" + exif
    return (
        b"\xff\xd8"
        + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        + b"\xff\xc0" + struct.pack(">H", 0x0A) + b"\x08"
    )


def _webp_vp8x(w: int, h: int, truncate: bool = False) -> bytes:
    """合成 WEBP（VP8X 扩展块）；truncate=True 时画布宽高字节缺失。"""
    if truncate:
        return b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + b"\x00"
    return (
        b"RIFF" + struct.pack("<I", 30) + b"WEBP"
        + b"VP8X"
        + b"\x00"                              # 标志
        + struct.pack("<I", w - 1)[1:]         # 画布宽-1（3 字节小端）
        + struct.pack("<I", h - 1)[1:]         # 画布高-1（3 字节小端）
        + b"\x00" * 10                         # 配置字段
    )


def _webp_vp8(w: int, h: int) -> bytes:
    """合成 WEBP（VP8 有损关键帧）：起始码 0x9D012A 在偏移 23（实测真实 Pillow 有损 WebP 头），宽高低 14 位在偏移 26/28。"""
    out = bytearray(64)
    out[0:4] = b"RIFF"
    out[8:12] = b"WEBP"
    out[12:16] = b"VP8 "
    out[23:26] = b"\x9d\x01\x2a"                        # 关键帧起始码（3 字节，偏移 23；实测对齐）
    out[26:28] = struct.pack("<H", w & 0x3FFF)           # 宽（低 14 位）
    out[28:30] = struct.pack("<H", h & 0x3FFF)           # 高（低 14 位）
    return bytes(out)


def _webp_vp8l(w: int, h: int) -> bytes:
    """合成 WEBP（VP8L 无损）：签名 0x2F 在偏移 20，W-1/H-1 按 14 位位打包在偏移 21/22/23/24（WebP 规范）。"""
    out = bytearray(64)
    out[0:4] = b"RIFF"
    out[8:12] = b"WEBP"
    out[12:16] = b"VP8L"
    out[20] = 0x2F                                       # 无损签名
    wm1, hm1 = w - 1, h - 1
    out[21] = wm1 & 0xFF                                 # 宽-1 低 8 位
    out[22] = ((wm1 >> 8) & 0x3F) | ((hm1 & 0x3) << 6)   # 宽-1 高 6 位 + 高-1 低 2 位
    out[23] = (hm1 >> 2) & 0xFF                          # 高-1 中 8 位
    out[24] = (hm1 >> 10) & 0x0F                         # 高-1 高 4 位
    return bytes(out)


# ── ① 双阈值分判 ──────────────────────────────────────────────


def test_rejects_png_over_pixel_budget(tmp_path):
    """PNG 4000×4000（16MP，超 8MP 初值预算）⇒ 拒收，原因含「超像素预算」。"""
    p = tmp_path / "big.png"
    p.write_bytes(_png_header(4000, 4000))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "超像素预算" in reason


def test_allows_png_under_pixel_budget(tmp_path):
    """PNG 2828×2828（≈7.99MP，预算内）⇒ 放行。"""
    p = tmp_path / "ok.png"
    p.write_bytes(_png_header(2828, 2828))
    ok, reason = pixel_guard.check(str(p))
    assert ok is True
    assert reason == ""


def test_rejects_jpeg_incident_dims_with_exif_prefix(tmp_path):
    """JPEG 3861×2252（8.69MP，2026-10-13 事故件尺寸）+ 长 EXIF 前缀 ⇒ 拒收——
    尺寸段在 EXIF 段之后，护栏须逐段跳过 EXIF 仍读出 W×H（不吃固定窗口）。"""
    p = tmp_path / "incident.jpg"
    p.write_bytes(_jpeg(3861, 2252, exif_payload=b"\xa5" * 512))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "超像素预算" in reason


def test_allows_jpeg_under_budget(tmp_path):
    """JPEG 3000×2000（6MP，预算内，无 EXIF）⇒ 放行。"""
    p = tmp_path / "ok.jpg"
    p.write_bytes(_jpeg(3000, 2000))
    ok, reason = pixel_guard.check(str(p))
    assert ok is True
    assert reason == ""


def test_rejects_over_byte_budget_even_if_dims_within_budget(tmp_path):
    """文件体积 >30MB（W×H 在预算内）⇒ 拒收，原因含「超护栏预算」。"""
    p = tmp_path / "fat.png"
    p.write_bytes(_png_header(100, 100) + b"\x00" * (31 * 1024 * 1024))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "超护栏预算" in reason


def test_rejects_vp8_over_pixel_budget(tmp_path):
    """WEBP VP8（有损关键帧）4000×4000（16MP，超预算）⇒ 拒收——守护 VP8 正向尺寸解码路径（P3-2）。"""
    p = tmp_path / "big_vp8.webp"
    p.write_bytes(_webp_vp8(4000, 4000))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "4000×4000" in reason and "超像素预算" in reason


def test_rejects_vp8l_over_pixel_budget(tmp_path):
    """WEBP VP8L（无损）4000×4000（16MP，超预算）⇒ 拒收——VP8L 的 14 位位打包解码正确（P3-2）。"""
    p = tmp_path / "big_vp8l.webp"
    p.write_bytes(_webp_vp8l(4000, 4000))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "4000×4000" in reason and "超像素预算" in reason


def test_rejects_extreme_aspect_ratio_png(tmp_path):
    """PNG 100000×100（10MP，极端长宽比）⇒ 拒收——struct ">I" 支持 >65535 的 PNG 宽（P3-3）。"""
    p = tmp_path / "wide.png"
    p.write_bytes(_png_header(100000, 100))
    ok, reason = pixel_guard.check(str(p))
    assert ok is False
    assert "100000×100" in reason and "超像素预算" in reason


# ── ② total 函数回归（第一轮 P1-B 复现用例入库）────────────────


@pytest.mark.parametrize(
    "name,build",
    [
        # 四个复现输入（远端角探针形态）
        ("truncated_png_20b", lambda: _png_header(4000, 4000)[:20]),
        ("truncated_vp8x", lambda: _webp_vp8x(4000, 4000, truncate=True)),
        ("jpeg_truncated_after_sof0", _jpeg_truncated_after_sof0),
        ("nul_in_path", lambda: None),  # 非文件形态，单独用例
        # 其余形态
        ("empty_file", lambda: b""),
        ("missing_file", lambda: None),  # 非文件形态，单独用例
        ("png_8b_signature_only", lambda: b"\x89PNG\r\n\x1a\n"),
        ("truncated_gif", lambda: b"GIF89a\x21\x00"),
        ("heic_unknown_header", lambda: b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16),
    ],
    ids=[
        "20B截断PNG头", "截断VP8X", "SOF0后截断JPEG", "路径含NUL",
        "空文件", "文件不存在", "8字节PNG", "截断GIF", "HEIC未知头",
    ],
)
def test_total_function_never_raises(tmp_path, monkeypatch, name, build):
    """任何畸形/未知输入 ⇒ ``check`` 零抛异常、按契约放行（True, ""）——绝不崩 tick。"""
    if name in ("nul_in_path", "missing_file"):
        target = str(tmp_path / "nul\0path.png") if name == "nul_in_path" else str(tmp_path / "does_not_exist.png")
        ok, reason = pixel_guard.check(target)  # 不抛即通过
        assert ok is True and reason == ""
        return
    path = tmp_path / f"{name}.bin"
    path.write_bytes(build())
    ok, reason = pixel_guard.check(str(path))  # 不抛即通过
    assert ok is True
    assert reason == ""


# ── ③ 两种拒收原因可辨 ─────────────────────────────────────────


def test_two_rejection_reasons_are_distinguishable(tmp_path):
    """像素超限与体积超限的 reason 文案不同（拒收提示/SSE 事件原样透传 reason，
    用户看到的拒收原因与拒绝分支一一对应）。"""
    px = tmp_path / "big.png"
    px.write_bytes(_png_header(4000, 4000))
    by = tmp_path / "fat.png"
    by.write_bytes(_png_header(100, 100) + b"\x00" * (31 * 1024 * 1024))
    _, r_px = pixel_guard.check(str(px))
    _, r_by = pixel_guard.check(str(by))
    assert "超像素预算" in r_px and "超护栏预算" not in r_px
    assert "超护栏预算" in r_by and "超像素预算" not in r_by

# ── ④ WARNING 契约（P2-1）────────────────────────────────────────────


def test_warning_emitted_when_header_unreadable(tmp_path):
    """契约：图头读不出 ⇒ 放行 + WARNING。本用例把契约的「WARNING 半」钉住：两行 ``logger.warning``
    被删掉时套件照样全绿，须以一条断言 ``[PixelGuard]`` 告警前缀的用例作回归防线（P2-1）。"""
    from loguru import logger
    captured = []
    sink_id = logger.add(captured.append, level="WARNING")
    try:
        p = tmp_path / "trunc.png"
        p.write_bytes(_png_header(4000, 4000)[:20])  # 20B 截断头
        ok, reason = pixel_guard.check(str(p))
        assert ok is True and reason == ""
        assert any("[PixelGuard]" in m for m in captured), "未捕获 [PixelGuard] WARNING"
    finally:
        logger.remove(sink_id)
