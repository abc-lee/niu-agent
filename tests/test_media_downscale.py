"""T1：客户端缩放与编码分派行为套件（HANDOVER D 机制，v1.1 白名单定案）：门槛两侧分判（含配置现读）、
EXIF 方向转正、色彩模式归一、病态 reject 安全网、白名单分判（白名单外 BMP/未知格式 ⇒ 拦截、
绝不试送）、HEIC 分判（pillow-heif 在位 ⇒ 转 JPEG 再走；在位但转码失败 ⇒ 可辨失败、不降级原图直送；缺失 ⇒ 失败 + 提示，编码器面零调用）、
缩放失败降级原图直送（fail-open，I-7）、免重启热生效。编码器面（encode_media /
encode_media_bytes）全部打桩——绝不向真实 llama-server 发送（同 test_llama_server_mmproj 纪律）。"""
import base64
import importlib.util
import io
import json

import pytest
from PIL import Image

from niu_api.internal import llama_server, media_downscale, pixel_guard


def _make_image(path, w, h, mode="RGB", fmt="JPEG", exif_orientation=None, **kw):
    im = Image.new(mode, (w, h))
    if exif_orientation is not None:
        exif = im.getexif()
        exif[0x0112] = exif_orientation
        im.save(path, format=fmt, exif=exif, **kw)
    else:
        im.save(path, format=fmt, **kw)
    return str(path)


def _decode_uri(uri: str) -> Image.Image:
    """把 data: URI 解回 PIL 图（断言载荷形态与内容）。"""
    assert uri.startswith("data:image/jpeg;base64,")
    b64 = uri.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.fixture(autouse=True)
def _patch_encoders(monkeypatch):
    """打桩 llama_server 的两个发送入口，记录调用形态（绝不真发）。"""
    calls = {"path": [], "bytes": []}

    def fake_encode_media(abs_path):
        calls["path"].append(abs_path)
        return [0.1] * 768

    def fake_encode_bytes(data, mime="image/jpeg"):
        calls["bytes"].append((data, mime))
        return [0.2] * 768

    monkeypatch.setattr(llama_server, "encode_media", fake_encode_media)
    monkeypatch.setattr(llama_server, "encode_media_bytes", fake_encode_bytes)
    return calls


@pytest.fixture(autouse=True)
def _default_config(monkeypatch, tmp_path):
    """缺省配置（无 media_ingest 段 ⇒ 一切阈值取模块默认：门槛 8MP / 长边 1024）。"""
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "user-config.json"))
    (tmp_path / "user-config.json").write_text(json.dumps({}), encoding="utf-8")


def test_under_threshold_sends_original_bytes(tmp_path, _patch_encoders):
    """I-1：W×H ≤ 门槛（3000×2000 = 6MP < 8MP）⇒ 原图字节直送，encode_media_bytes 零调用。"""
    p = _make_image(tmp_path / "small.jpg", 3000, 2000)
    out = media_downscale.encode_media_any(p)
    assert out == [0.1] * 768
    assert _patch_encoders["path"] == [p] and not _patch_encoders["bytes"]


def test_pillow_dims_retries_when_guard_cannot_read(tmp_path, _patch_encoders, monkeypatch):
    """I-6/Pillow 第二道尺寸读取机制（分派器 `pass` + 内置白名单分支）：护栏读不出图头（fail-open pass，
dims=None）但 magic 在白名单内 ⇒ 分派器用 Pillow 图头再试取尺寸：读出且超门槛 ⇒ 缩放（走 bytes 入口、
直送入口零调用）。构造（与真实场景的等价性见回报）：对护栏的尺寸读钩子 `_image_size` 做最小桩（置 None），
即「护栏读不出图头」这一状态本身（真实形态：JPEG 的 SOF 段在护栏解析器够不着处 / 图头截断），`classify`
自身的 fail-open 逻辑（体积闸、WARNING、pass）照原样执行；文件是 Pillow 可解码的真 20MP JPEG。"""
    monkeypatch.setattr(pixel_guard, "_image_size", lambda path: None)
    p = _make_image(tmp_path / "hidden.jpg", 5000, 4000)  # 20MP > 8MP 门槛；JPEG magic 在白名单内
    media_downscale.encode_media_any(p)
    assert not _patch_encoders["path"]  # 直送入口零调用
    assert len(_patch_encoders["bytes"]) == 1
    data, mime = _patch_encoders["bytes"][0]
    assert mime == "image/jpeg"
    got = _decode_uri(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    assert got.mode == "RGB"
    assert max(got.size) == 1024  # 缩放目标长边（缺省配置）
    assert min(got.size) == round(1024 * 4000 / 5000)  # 长宽比保持


def test_over_threshold_downscales_in_memory(tmp_path, _patch_encoders):
    """I-2：12MP 图超门槛 ⇒ 内存缩放后发送（JPEG 字节、image/jpeg MIME），直送入口零调用、原文件未被改写。"""
    p = _make_image(tmp_path / "big.png", 4000, 3000, mode="RGBA", fmt="PNG")
    before = (tmp_path / "big.png").stat().st_mtime_ns
    media_downscale.encode_media_any(p)
    assert (tmp_path / "big.png").stat().st_mtime_ns == before  # 原图未动
    assert not _patch_encoders["path"]
    data, mime = _patch_encoders["bytes"][0]
    assert mime == "image/jpeg"
    got = _decode_uri(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    assert got.mode == "RGB"  # I-4：RGBA ⇒ RGB
    assert max(got.size) == 1024  # I-5 默认长边 1024
    assert min(got.size) == round(1024 * 3000 / 4000)  # 长宽比保持（4000×3000 → 1024×768）


def test_exif_orientation_applied_before_sending(tmp_path, _patch_encoders, monkeypatch):
    """I-3：orientation=6（竖拍存成横置）⇒ 发送前像素转正，宽高互换后再按长边缩放。
    3000×2000 = 6MP 小于缺省 8MP 门槛 ⇒ 不钉低门槛会走直送（`bytes` 为空、取 `[0]` 报错）；
    故钉 `max_pixels = 5_000_000` 使其真进缩放分支（同 `test_hot_config_change...` 配方）。"""
    cfg = tmp_path / "user-config.json"
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(cfg))
    cfg.write_text(json.dumps({"media_ingest": {"max_pixels": 5_000_000}}), encoding="utf-8")
    p = _make_image(tmp_path / "rot.jpg", 3000, 2000, exif_orientation=6)
    media_downscale.encode_media_any(p)
    data, _mime = _patch_encoders["bytes"][0]
    got = _decode_uri(f"data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"))
    assert got.size == (round(1024 * 2000 / 3000), 1024)  # 转正后 2000×3000，长边 3000→1024


def test_cmyk_and_palette_normalized(tmp_path, _patch_encoders):
    """I-4：CMYK 与调色板（P）都归一到 RGB 再缩放——**每腿在循环内各自断言**（每次 `encode_media_any` 后
    立即取该腿新增的 bytes 载荷、解码断 RGB，并钉住「该腿确实走了转换入口」）：只取最后一次载荷会让
    P 腿若缩放失败降级直送时，断言仍落在 CMYK 载荷上而假绿。"""
    for name, mode, fmt in [("c.jpg", "CMYK", "JPEG"), ("p.png", "P", "PNG")]:
        p = _make_image(tmp_path / name, 4000, 4000, mode=mode, fmt=fmt)
        n_before = len(_patch_encoders["bytes"])
        media_downscale.encode_media_any(p)
        new = _patch_encoders["bytes"][n_before:]
        assert len(new) == 1, f"{mode} 腿：该腿未走 bytes 入口（缩放失败会降级原图直送）"
        data, mime = new[0]
        assert mime == "image/jpeg"
        assert _decode_uri(f"data:{mime};base64," + base64.b64encode(data).decode("ascii")).mode == "RGB"
    assert not _patch_encoders["path"]


def test_hot_config_change_takes_effect_without_restart(tmp_path, _patch_encoders, monkeypatch):
    """I-5：现读 ⇒ 改门槛立即改变分判（免重启）。3000×2000 = 6MP：门槛 5MP ⇒ 缩放；门槛 7MP ⇒ 直送。"""
    cfg = tmp_path / "user-config.json"
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(cfg))
    p = _make_image(tmp_path / "mid.jpg", 3000, 2000)
    cfg.write_text(json.dumps({"media_ingest": {"max_pixels": 5_000_000}}), encoding="utf-8")
    media_downscale.encode_media_any(p)
    assert _patch_encoders["bytes"] and not _patch_encoders["path"]
    cfg.write_text(json.dumps({"media_ingest": {"max_pixels": 7_000_000}}), encoding="utf-8")
    media_downscale.encode_media_any(p)
    assert len(_patch_encoders["path"]) == 1 and len(_patch_encoders["bytes"]) == 1


def test_reject_action_raises_with_distinguishable_reason(tmp_path, _patch_encoders):
    """I-6/I-8：体积超预算（读得出图头、像素在门槛内）⇒ dispatcher 直达时抛 RuntimeError（安全网，
    生产上 tick ⑥ 已先弃行），reason 可辨。"""
    p = tmp_path / "huge.bin"
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct_pack_ih(100, 100) + b"\x00" * (31 * 1024 * 1024))
    with pytest.raises(RuntimeError, match="护栏拒收"):
        media_downscale.encode_media_any(str(p))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_out_of_whitelist_bmp_blocked(tmp_path, _patch_encoders):
    """白名单定案：BMP（护栏**读不出图头** ⇒ 放行（fail-open），随后由分派器按白名单拦截）⇒ 一律拦截（拒收 + 临时提示）：
    不试送、不尝试转换——断言两个编码器入口零调用（与改动前「BMP 照样缩放」相反）。"""
    p = _make_image(tmp_path / "big.bmp", 3000, 3000, fmt="BMP")
    with pytest.raises(RuntimeError, match="白名单外"):
        media_downscale.encode_media_any(p)
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_unknown_format_blocked_without_sending(tmp_path, _patch_encoders):
    """白名单定案：白名单外/不可辨识格式（护栏与 magic 嗅探都认不出）⇒ 一律拦截
    （可辨 RuntimeError「白名单外」，工作者侧按失败 + 临时提示兜底）——绝不试送。"""
    p = tmp_path / "mystery.xyz"
    p.write_bytes(b"\x00\x01\x02not-an-image" * 100)
    with pytest.raises(RuntimeError, match="白名单外"):
        media_downscale.encode_media_any(str(p))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_sniff_nonexistent_path_blocked_distinguishably(tmp_path, _patch_encoders):
    """I-8/`_sniff_kind` total 契约（非法输入：不存在的路径）：`open` 抛 FileNotFoundError（OSError）⇒ 嗅探
    返回 `"other"` 不外抛，分派器抛可辨「白名单外」RuntimeError（失败可辨、不静默，也不落入直送/缩放路径）——
    两个编码器入口从未被调用。"""
    with pytest.raises(RuntimeError, match="白名单外"):
        media_downscale.encode_media_any(str(tmp_path / "no-such-file.jpg"))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_sniff_directory_path_blocked_distinguishably(tmp_path, _patch_encoders):
    """非法输入：目录。`open(目录, "rb")` 抛 IsADirectoryError（OSError 子类）⇒ 嗅探返回 `"other"` 不外抛
    ⇒ 可辨「白名单外」拦截（目录不是合法图片：既不直送也不缩放）。"""
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(RuntimeError, match="白名单外"):
        media_downscale.encode_media_any(str(d))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_sniff_nul_path_blocked_distinguishably(tmp_path, _patch_encoders):
    """非法输入：路径含 NUL。`open` 抛 ValueError（**不是 OSError**）⇒ 嗅探返回 `"other"` 不外抛
    ⇒ 可辨「白名单外」拦截（与不存在路径/目录两条同型：不静默、不落入直送/缩放路径）。"""
    with pytest.raises(RuntimeError, match="白名单外"):
        media_downscale.encode_media_any(str(tmp_path / "nul\x00file.jpg"))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]


def test_downscale_failure_degrades_to_original_send(tmp_path, _patch_encoders):
    """I-7：图头读得出（4000×4000 PNG 头）但像素数据损坏 ⇒ 判 downscale，Pillow 解码失败
    ⇒ WARNING + 降级原图直送，绝不把异常抛进队列消费链。"""
    p = tmp_path / "corrupt.png"
    # 合法 4000×4000 PNG 图头 + 垃圾体（Pillow 打开会失败）
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + (4000).to_bytes(4, "big") + (4000).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00" + b"\x00" * 8 + b"garbage"
    )
    media_downscale.encode_media_any(str(p))  # 不得抛
    assert _patch_encoders["path"] == [str(p)] and not _patch_encoders["bytes"]


@pytest.mark.skipif(
    importlib.util.find_spec("pillow_heif") is None,
    reason="构造真 HEIC 测试图需要 pillow-heif（可选依赖）；缺失时该条本就走行下的失败分支",
)
def test_heic_with_heif_lib_converts_to_jpeg(tmp_path, _patch_encoders, monkeypatch):
    """白名单定案：pillow-heif 在位 + HEIC ⇒ **转 JPEG 再走**（服务端不解 HEIC，门槛内也不直送）；
    转换路径走 encode_media_bytes、直送入口零调用。编码器面打桩——绝不真发。
    夹具前提：先 `_ensure_heif_opener()` 注册 HEIF opener（Pillow 注册后才认识 HEIF 的保存；包在位但未注册
    ≠ 包缺失——下方 skipif 只挡后者；未注册时 save(format="HEIF") 抛 `KeyError: 'HEIF'`）。"""
    monkeypatch.setattr(media_downscale, "_heif_dep_probe", True)  # 钉住探测结果：在位
    media_downscale._ensure_heif_opener()  # 夹具前提：先注册 HEIF opener，否则 _make_image(fmt="HEIF") 抛 KeyError
    p = _make_image(tmp_path / "photo.heic", 300, 300, fmt="HEIF")
    out = media_downscale.encode_media_any(p)
    assert out == [0.2] * 768  # encode_media_bytes 桩的返回值（走了转换路径）
    assert not _patch_encoders["path"]  # 直送入口零调用（HEIC 从不以裸字节直送）
    data, mime = _patch_encoders["bytes"][0]
    assert mime == "image/jpeg"
    got = _decode_uri(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    assert got.mode == "RGB" and got.size == (300, 300)  # 未超目标长边：不放大、原样转码


def test_heic_without_heif_lib_fails_and_never_sends(tmp_path, _patch_encoders, monkeypatch):
    """白名单定案：HEIC + pillow-heif 缺失 ⇒ 该条**处置为失败**（可辨原因点明「HEIC 支持未安装」，
    可按手册选装），绝不直送、不 fail-open——断言两个编码器入口（直送/转换）**零调用**：
    从未向编码器面发出过任何请求；工作者侧由既有有界失败重试/弃单机制兜底
    （弃行 + 临时提示到 Chat 页面，原图照旧入库）。"""
    monkeypatch.setattr(media_downscale, "_heif_dep_probe", False)  # 钉住探测结果：缺失
    p = tmp_path / "photo.heic"
    p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)  # HEIC magic（ftyp + heic brand）+ 垃圾体
    with pytest.raises(RuntimeError, match="HEIC 支持未安装"):
        media_downscale.encode_media_any(str(p))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]  # 从未向编码器发过任何请求


def test_heic_transcode_failure_raises_without_sending(tmp_path, _patch_encoders, monkeypatch):
    """白名单定案（I-7；行为矩阵「读不出，magic = HEIC/HEIF，pillow-heif 在位」行的转码失败子分支）：
    HEIC + 依赖在位但转码失败（解码错误等）⇒ 该条**处置为失败**（可辨「HEIC 转码失败」，原图照旧入库），
    **不降级原图直送**（服务端不解 HEIC，直送必败、白烧有界重试预算）——断言两个编码器入口零调用，
    与缺依赖用例同型（绝不真发）。"""
    monkeypatch.setattr(media_downscale, "_heif_dep_probe", True)  # 钉住探测结果：在位
    media_downscale._ensure_heif_opener()  # 夹具前提：opener 已注册（依赖缺失时注册静默跳过，不依赖运行环境巧合）

    def _boom(path, long_side):
        raise OSError("模拟 HEIC 解码/转码失败")

    monkeypatch.setattr(media_downscale, "downscale_to_long_side", _boom)  # 转码段抛错 ⇒ _try_downscale_and_send 捕获后返回 None
    p = tmp_path / "photo.heic"
    p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)  # HEIC magic（ftyp + heic brand）+ 垃圾体
    with pytest.raises(RuntimeError, match="HEIC 转码失败"):
        media_downscale.encode_media_any(str(p))
    assert not _patch_encoders["path"] and not _patch_encoders["bytes"]  # 从未向编码器发过任何请求


def struct_pack_ih(w, h):
    import struct

    return struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
