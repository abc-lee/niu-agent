"""客户端大图缩放与编码分派（HANDOVER D 节机制定案，2026-10-13；spec D-26 演进；
v1.1 HEIC/白名单口径——用户 2026-10-13 拍板）。

**白名单**（定案口径）：直送/转换**仅限** JPEG/PNG/WEBP（内置）+ HEIC/HEIF（可选依赖
`pillow-heif`）；**白名单外一律拦截**（拒收 + 临时提示：BMP/TIFF/GIF/不可辨识等），
**不试送、不尝试转换**。

分派规则（`encode_media_any`，工作者的缺省编码入口）：
- 护栏判 `reject`（体积超预算的病态件）⇒ 生产上 tick ⑥ 已弃行，dispatcher 直接调到时
  抛 RuntimeError（走既有失败归类，原因可辨，I-6）；
- 护栏判 `downscale`（W×H 超门槛；HEIC 图头护栏读不出，此分支只可能是内置白名单格式）⇒
  **内存中缩放**到 `media_ingest.downscale_long_side` 长边后发送（EXIF 方向转正 → 转 RGB →
  LANCZOS → JPEG），原图文件不动、不落临时文件（I-2/I-3/I-4）；
- 护栏判 `pass`（门槛内或图头读不出，护栏级 fail-open）⇒ dispatcher **按白名单分判**
  （magic 嗅探，用户定案口径）：
  - **HEIC/HEIF 且 `pillow-heif` 在位**（`importlib.util.find_spec` 一次探测、模块级缓存、
    不每次重 import）⇒ **转 JPEG 再走**（服务端不解 HEIC：超门槛 ⇒ 缩放后送；门槛内 ⇒
    不放大、直编码）；转码失败 ⇒ **该条处置为失败**（可辨错误，有界重试后弃行 + 临时提示）
    ——服务端不解 HEIC，原图直送必败，**不降级原图直送**；
  - **HEIC/HEIF 但 `pillow-heif` 缺失** ⇒ **处置为失败**：抛可辨 RuntimeError（原因点明
    「HEIC 支持未安装，可按手册选装」），绝不直送、不 fail-open；工作者的既有有界失败
    重试/弃单机制兜底（有界重试后弃行 + 临时提示到 Chat 页面，原图照旧入库）；
  - **内置白名单**（JPEG/PNG/WEBP）：门槛内 ⇒ **原图字节直送**（I-1）；图头读不出 ⇒
    用 Pillow 图头再试取尺寸，读出且超门槛 ⇒ 缩放；Pillow 也读不出 ⇒ 原图直送
    （服务端 `--image-max-tokens` 是最后防线，与护栏放行同契约）；
  - **白名单外格式**（BMP/TIFF/GIF/不可辨识）⇒ **拦截**：抛可辨 RuntimeError
    （原因点明「白名单外」），拒收 + 临时提示，不试送、不尝试转换（I-6②）。
- **内置格式缩放失败**（解码错误 / 内存不足 / Pillow 不可用等一切异常）⇒ WARNING + **降级为原图直送**（fail-open，同护栏「读不出 ⇒ 放行」契约，I-7）；**HEIC 转码失败不降级原图直送**（服务端不解 HEIC，原图直送必败）⇒ 抛可辨失败（有界重试后弃行 + 临时提示，原图照旧入库）；原图仍不可编码 ⇒ 既有有界失败重试/弃单机制兜底。

两个阈值都经 `media_ingest_config` 现读（唯一读取点，改配置免重启生效，I-5）。
缩放跑在池 worker 线程、解码段不占编码串行锁（I-11）。
"""
from __future__ import annotations

import io

from loguru import logger

_heif_registered = False


def _ensure_heif_opener() -> None:
    """幂等地注册 pillow-heif 的 HEIC/HEIF 解码器。**仅在 `heif_dependency_available()`
    为真之后才会在判定链上被调用**（依赖缺失 ⇒ 该 HEIC 条已在上游处置为失败 + 临时提示，
    绝不会走到这里再直送）；注册失败静默（软故障）：该 HEIC 的转换会失败 ⇒ 走 I-7 降级
    原图直送，原图仍不可编码 ⇒ 既有有界失败机制兜底（原因可辨，用户可按手册可选安装节修复）。"""
    global _heif_registered
    if _heif_registered:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _heif_registered = True
    except Exception:  # noqa: BLE001 —— HEIF 能力是增强项，注册失败不炸缩放主链
        pass


#: HEIC/HEIF 家族的 ISO-BMFF `ftyp` brand（用于 dispatcher 的白名单 magic 嗅探）。
#: **与 `agent/image_channel.py:89`（分析面）的 brand 集对齐**（取两集合并集：那集合缺 `heif`，本集合缺 `heim`/`heis`/`msf1` ⇒
#: 并集，使 `heis`/`msf1` 等品牌照片在分析面与向量面被同一口径识别，不出现同一文件两链路行为分裂）；
#: 不共享跨包常量（T1 不得触碰面含 `agent/**`）——任一侧增减 brand 须同步另一侧。
_HEIF_BRANDS = (b"heic", b"heif", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")

#: `pillow_heif` 安装状态的一次性探测缓存（None = 尚未探测；模块级 ⇒ 进程内只探一次，
#: 不每次重 import；用户按手册装完重启后缓存刷新）。
_heif_dep_probe: bool | None = None


def heif_dependency_available() -> bool:
    """`importlib.util.find_spec("pillow_heif")` **一次探测**（结果模块级缓存，不每次
    重 import）：pillow-heif 是否在位。用户装完需重启程序生效。"""
    global _heif_dep_probe
    if _heif_dep_probe is None:
        from importlib.util import find_spec

        _heif_dep_probe = find_spec("pillow_heif") is not None
    return _heif_dep_probe


def _sniff_kind(path: str) -> str:
    """magic 嗅探文件格式（只读文件头前若干字节，不解码）：`"built-in"`（JPEG/PNG/WEBP）
    / `"heic"`（ISO-BMFF `ftyp` + HEIC/HEIF brand）/ `"other"`（白名单外与不可辨识：
    BMP/TIFF/GIF/垃圾字节…）。**total 函数**：读失败 ⇒ `"other"`，绝不抛。
    只服务于 dispatcher 的白名单分判，不替代护栏的图头解析。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except (OSError, ValueError):  # ValueError = 路径含 NUL（open 抛它、不是 OSError）
        return "other"
    if head[:2] == b"\xff\xd8" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return "built-in"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "built-in"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heic"
    return "other"


def _pillow_dims(path: str) -> tuple[int, int] | None:
    """用 Pillow 图头（惰性、不解码像素）再试取尺寸，**total 函数**：覆盖护栏图头解析读不出
    而 magic 已在白名单内的形态；读不出（未知格式/损坏）→ None，绝不抛。仅由分派器的内置
    白名单分支（magic 已确认 JPEG/PNG/WEBP）可达，本分支永不需要 HEIF opener（HEIC 分支不走
    本函数）。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 —— 读不出 = None（fail-open 契约）
        return None


def downscale_to_long_side(path: str, long_side: int) -> bytes:
    """读 `path` 的图 → **EXIF 方向转正**（I-3）→ **转 RGB**（I-4，JPEG 不支持
    alpha/调色板/16bit 直存）→ 长边缩到 `long_side`（LANCZOS）→ **JPEG 字节（内存）**。

    不写任何文件、不改原图（I-2）。任何异常（打不开/解码失败/内存不足）向外抛，
    由 `encode_media_any` 统一降级（I-7）。
    """
    from PIL import Image, ImageOps  # 函数级 import：Pillow 缺失时由调用方 try 兜住

    _ensure_heif_opener()
    with Image.open(path) as im0:
        im = ImageOps.exif_transpose(im0)
        if im is None:  # 旧版 Pillow 对无方向标签图返回 None 的兼容
            im = im0
        im = im.convert("RGB")
        im.load()  # 强制全量解码完成于文件关闭之前
        w, h = im.size
        scale = long_side / max(w, h)
        if scale >= 1.0:
            # 配置竞态（门槛刚调低、此图实际已小于目标）：不放大，原样编码
            out = im
        else:
            out = im.resize(
                (max(1, round(w * scale)), max(1, round(h * scale))),
                Image.Resampling.LANCZOS,
            )
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def _try_downscale_and_send(path: str) -> list[float] | None:
    """缩放（或 HEIC 转码）并发送；成功 ⇒ 返回向量；缩放/转码/发送段异常 ⇒ WARNING + 返回 None
    （**调用方按分支决定后续**：内置格式分支降级为原图直送，I-7；HEIC 分支抛可辨失败——服务端
    不解 HEIC，原图直送必败，不降级）。发送走 `encode_media_bytes`（内存字节 → data: URI）。"""
    from niu_api.internal import llama_server
    from niu_api.internal.media_ingest_config import media_ingest_downscale_long_side

    long_side = media_ingest_downscale_long_side()  # 只读一次：缩放与日志同源（两次现读之间改配置 ⇒ 日志与实际不符）
    try:
        data = downscale_to_long_side(path, long_side)
        logger.info(f"[MediaDownscale] 内存缩放/转码后发送（长边 {long_side}）: {path}")
        return llama_server.encode_media_bytes(data, "image/jpeg")
    except Exception as e:  # noqa: BLE001 —— 失败后续见 docstring（内置降级 / HEIC 抛可辨失败）
        logger.warning(f"[MediaDownscale] 缩放/转码失败: {path}: {type(e).__name__}: {e}")
        return None


def encode_media_any(path: str) -> list[float]:
    """工作者的**缺省编码入口**：护栏判定 + 白名单分判 +（超门槛 / HEIC）缩放或转码 + 发送。
    行为矩阵见模块 docstring（v1.1 白名单定案，用户 2026-10-13；v1.3：HEIC 转码失败不再降级原图直送）。

    除 `encode_media` 本就抛的原因（文件缺失 / 实例未就绪 / 未加载 mmproj）外，新增三种
    **可辨** RuntimeError（都是确定性拦截，均不会到达任何编码器入口）：
    - 「白名单外」——白名单外格式（BMP/TIFF/GIF/不可辨识）⇒ 拦截（拒收 + 临时提示，
      由工作者既有失败机制兜底）；
    - 「HEIC 支持未安装」——HEIC/HEIF + pillow-heif 缺失 ⇒ 该条处置为失败（临时提示点明
      可按手册选装，绝不直送、不 fail-open）；
    - 「HEIC 转码失败」——HEIC + pillow-heif 在位但转码失败（解码错误等）⇒ 该条处置为失败
      （服务端不解 HEIC，原图直送必败 ⇒ 不降级原图直送，原图照旧入库）；
    `reject` 动作抛 RuntimeError（生产上不可达——tick ⑥ 已弃行，直达只可能来自
    测试或未来调用方，走既有失败归类，原因可辨）。
    """
    from niu_api.internal import llama_server, pixel_guard
    from niu_api.internal.media_ingest_config import media_ingest_max_pixels

    action, reason, _dims = pixel_guard.classify(path)
    if action == "reject":
        raise RuntimeError(f"护栏拒收（{reason}）")
    kind = _sniff_kind(path)
    if kind == "other":
        # 白名单外格式（BMP/TIFF/GIF/不可辨识）：一律拦截（I-6②）——拒收 + 临时提示
        # （由工作者既有有界失败重试/弃单机制兜底），不试送、不尝试转换
        raise RuntimeError(f"白名单外图片格式，拦截: {path}（仅支持 JPEG/PNG/WEBP/HEIC）")
    if kind == "heic":
        # HEIC 图头护栏读不出（只能从 pass 进入）；服务端不解 HEIC ⇒ 一律转 JPEG 再走
        if not heif_dependency_available():
            raise RuntimeError(
                f"HEIC 支持未安装（可选依赖 pillow-heif），该条处置为失败: {path}——"
                "按手册「可选安装：苹果格式支持（HEIC/HEIF）」节安装后重启程序生效"
            )
        # pillow-heif 在位：超门槛 ⇒ 缩放后送；门槛内 ⇒ 不放大、直编码
        vector = _try_downscale_and_send(path)
        if vector is not None:
            return vector
        # 转码失败（解码错误等）：服务端不解 HEIC ⇒ 原图直送必败（白烧有界重试预算）
        # ⇒ 不降级原图直送；抛可辨失败走既有有界重试/弃行 + 临时提示（原图照旧入库）
        raise RuntimeError(
            f"HEIC 转码失败，该条处置为失败: {path}——原图照旧入库；按手册「可选安装：苹果格式支持（HEIC/HEIF）」节"
            "核对 pillow-heif 安装"
        )
    elif action == "downscale":
        # 内置白名单超门槛 ⇒ 缩放（I-2）；缩放失败 ⇒ I-7：降级原图直送
        vector = _try_downscale_and_send(path)
        if vector is not None:
            return vector
    else:  # "pass" + 内置白名单（JPEG/PNG/WEBP）
        # 门槛内且图头读得出 ⇒ 原图直送（I-1）；图头读不出 ⇒ 用 Pillow 图头再试取尺寸：
        # 超门槛 ⇒ 缩放；白名单内仍读不出 ⇒ 原图直送（服务端 token 上限是最后防线）
        dims = _pillow_dims(path)
        if dims is not None and dims[0] * dims[1] > media_ingest_max_pixels():
            vector = _try_downscale_and_send(path)
            if vector is not None:
                return vector
    # 直送（含缩放/转换失败降级与白名单内读不出 fail-open）：原文件字节，与改动前同一条路径（I-1）
    return llama_server.encode_media(path)
