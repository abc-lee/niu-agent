"""像素护栏行为套件（plan §2 T1，spec D-26 演进版）：三动作分判（pass / downscale /
reject）+ 配置现读（缺键/类型错/坏 JSON 回保守默认）+ total 函数回归 + 拒收原因可辨 +
WARNING 契约。只读图头、不解码像素（夹具全是「图头 + 垃圾体」，体字节只为撑体积预算）。"""
import json
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


@pytest.fixture(autouse=True)
def _empty_config(monkeypatch, tmp_path):
    """缺省配置（无 media_ingest 段）⇒ 门槛取模块默认 8MP；需要覆盖门槛的用例自行重写该文件。"""
    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
    yield p


# ── ① 三动作分判（默认门槛 8MP）──────────────────────────────


def test_downscale_png_over_threshold(tmp_path, _empty_config):
    """PNG 4000×4000（16MP > 8MP）⇒ downscale，reason 含「超直送门槛」，dims 如实。"""
    p = tmp_path / "a.png"
    p.write_bytes(_png_header(4000, 4000))
    action, reason, dims = pixel_guard.classify(str(p))
    assert (action, dims) == ("downscale", (4000, 4000))
    assert "超直送门槛" in reason


def test_pass_png_under_threshold(tmp_path, _empty_config):
    """PNG 2828×2828（≈7.99MP，预算内）⇒ pass。"""
    p = tmp_path / "b.png"
    p.write_bytes(_png_header(2828, 2828))
    action, reason, dims = pixel_guard.classify(str(p))
    assert (action, reason, dims) == ("pass", "", (2828, 2828))


def test_downscale_jpeg_incident_dims_with_exif_prefix(tmp_path, _empty_config):
    """JPEG 3861×2252（8.69MP，2026-10-13 事故件尺寸）+ 长 EXIF 前缀 ⇒ downscale（不再拒收）。"""
    p = tmp_path / "c.jpg"
    p.write_bytes(_jpeg(3861, 2252, exif_payload=b"\xa5" * 512))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert action == "downscale" and "超直送门槛" in reason


def test_pass_jpeg_under_threshold(tmp_path, _empty_config):
    """JPEG 3000×2000（6MP，预算内，无 EXIF）⇒ pass。"""
    p = tmp_path / "d.jpg"
    p.write_bytes(_jpeg(3000, 2000))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert (action, reason) == ("pass", "")


def test_reject_over_byte_budget_within_pixel_budget(tmp_path, _empty_config):
    """体积 > 30MiB（= 30×1024×1024 B，与 `GUARD_MAX_BYTES` 同口径；W×H 在预算内）⇒ reject（病态闸，I-6），原因含「超体积预算」。"""
    p = tmp_path / "e.png"
    p.write_bytes(_png_header(100, 100) + b"\x00" * (31 * 1024 * 1024))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert action == "reject" and "超体积预算" in reason


def test_reject_byte_budget_wins_over_pixel(tmp_path, _empty_config):
    """体积超限 + 像素超门槛 ⇒ 体积闸先判 ⇒ reject（不再是 downscale），原因只含体积文案。"""
    p = tmp_path / "f.png"
    p.write_bytes(_png_header(4000, 4000) + b"\x00" * (31 * 1024 * 1024))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert action == "reject" and "超体积预算" in reason and "超直送门槛" not in reason


def test_downscale_vp8_over_threshold(tmp_path, _empty_config):
    """WEBP VP8 4000×4000 ⇒ downscale——守护 VP8 正向尺寸解码路径（起始码偏移 23–25）。"""
    p = tmp_path / "g.webp"
    p.write_bytes(_webp_vp8(4000, 4000))
    action, reason, dims = pixel_guard.classify(str(p))
    assert action == "downscale" and "4000×4000" in reason and dims == (4000, 4000)


def test_downscale_vp8l_over_threshold(tmp_path, _empty_config):
    """WEBP VP8L 4000×4000 ⇒ downscale——VP8L 的 14 位位打包解码正确。"""
    p = tmp_path / "h.webp"
    p.write_bytes(_webp_vp8l(4000, 4000))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert action == "downscale" and "4000×4000" in reason


def test_downscale_extreme_aspect_ratio_png(tmp_path, _empty_config):
    """PNG 100000×100（10MP，极端长宽比）⇒ downscale——struct ">I" 支持 >65535 的 PNG 宽。"""
    p = tmp_path / "i.png"
    p.write_bytes(_png_header(100000, 100))
    action, reason, _dims = pixel_guard.classify(str(p))
    assert action == "downscale" and "100000×100" in reason


def test_reject_unreadable_header_over_byte_budget(tmp_path, _empty_config):
    """图头读不出 + 体积超限 ⇒ reject（病态，I-6 矩阵角元）。"""
    p = tmp_path / "junk.xyz"
    p.write_bytes(b"\x00\x01" * (31 * 1024 * 1024 // 2 + 1))
    action, reason, dims = pixel_guard.classify(str(p))
    assert (action, dims) == ("reject", None) and "超体积预算" in reason


def test_pass_unreadable_header_within_budget(tmp_path, _empty_config):
    """图头读不出 + 体积在预算内 ⇒ pass（fail-open，WARNING 见下）。"""
    p = tmp_path / "junk2.xyz"
    p.write_bytes(b"\x00\x01" * 100)
    action, reason, dims = pixel_guard.classify(str(p))
    assert (action, reason, dims) == ("pass", "", None)


# ── ② 配置现读与兼底（I-5）────────────────────────────────────


def test_threshold_config_drives_downscale(tmp_path, _empty_config):
    """门槛调到极高 ⇒ 16MP 变 pass；调到极低 ⇒ 小图变 downscale（现读、热生效）。"""
    p = tmp_path / "a.png"
    p.write_bytes(_png_header(4000, 4000))
    _empty_config.write_text(json.dumps({"media_ingest": {"max_pixels": 100_000_000}}), encoding="utf-8")
    assert pixel_guard.classify(str(p))[0] == "pass"
    _empty_config.write_text(json.dumps({"media_ingest": {"max_pixels": 1_000_000}}), encoding="utf-8")
    assert pixel_guard.classify(str(p))[0] == "downscale"


@pytest.mark.parametrize("bad", ["8000000", 8000000.5, True, 0, -3, None, [], {"x": 1}])
def test_threshold_wrong_type_falls_back_to_default(tmp_path, _empty_config, bad):
    """门槛类型错（含布尔——isinstance(True, int) 的坑）⇒ 回默认 8MP ⇒ 16MP 图仍是 downscale。"""
    p = tmp_path / "a.png"
    p.write_bytes(_png_header(4000, 4000))
    _empty_config.write_text(json.dumps({"media_ingest": {"max_pixels": bad}}), encoding="utf-8")
    assert pixel_guard.classify(str(p))[0] == "downscale"


def test_broken_config_falls_back_to_default(tmp_path, _empty_config):
    """坏 JSON ⇒ 默认 8MP（16MP ⇒ downscale；7.99MP ⇒ pass）。"""
    big, small = tmp_path / "big.png", tmp_path / "small.png"
    big.write_bytes(_png_header(4000, 4000))
    small.write_bytes(_png_header(2828, 2828))
    _empty_config.write_text("{不是JSON", encoding="utf-8")
    assert pixel_guard.classify(str(big))[0] == "downscale"
    assert pixel_guard.classify(str(small))[0] == "pass"


# ── ③ total 函数回归（任何畸形输入零外抛，队列永不冻结）──────


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
    """任何畸形/未知输入 ⇒ ``classify`` 零抛异常、按契约放行 (pass, "", None)——绝不崩 tick。"""
    if name == "missing_file":
        action, reason, _dims = pixel_guard.classify(str(tmp_path / "does_not_exist.png"))
        assert (action, reason) == ("pass", "")
        return
    if name == "nul_in_path":
        action, reason, _dims = pixel_guard.classify(str(tmp_path / "nul\0path.png"))
        assert (action, reason) == ("pass", "")
        return
    path = tmp_path / f"{name}.bin"
    path.write_bytes(build())
    action, reason, _dims = pixel_guard.classify(str(path))  # 不抛即通过
    assert (action, reason) == ("pass", "")


# ── ④ 拒收原因可辨 + WARNING 契约 ─────────────────────────────


def test_two_reasons_are_distinguishable(tmp_path, _empty_config):
    """拒收（体积）与缩放（像素）的 reason 文案不同（拒收提示/SSE 事件原样透传 reason）。"""
    px = tmp_path / "px.png"
    px.write_bytes(_png_header(4000, 4000))
    by = tmp_path / "by.png"
    by.write_bytes(_png_header(100, 100) + b"\x00" * (31 * 1024 * 1024))
    _a_px, r_px = pixel_guard.classify(str(px))[:2]
    _a_by, r_by = pixel_guard.classify(str(by))[:2]
    assert "超直送门槛" in r_px and "超体积预算" not in r_px
    assert "超体积预算" in r_by and "超直送门槛" not in r_by


def test_warning_emitted_when_header_unreadable(tmp_path, _empty_config):
    """契约：图头读不出 ⇒ 放行 + WARNING。本用例把契约的「WARNING 半」钉住。"""
    from loguru import logger

    sink_id = logger.add(lambda m: captured.append(m), level="WARNING")
    captured = []
    try:
        p = tmp_path / "trunc.png"
        p.write_bytes(_png_header(4000, 4000)[:20])  # 20B 截断头
        action, reason, _dims = pixel_guard.classify(str(p))
        assert (action, reason) == ("pass", "")
        assert any("[PixelGuard]" in m for m in captured), "未捕获 [PixelGuard] WARNING"
    finally:
        logger.remove(sink_id)
