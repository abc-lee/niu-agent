"""`llama_cpp` 配置段（`~/.niu/config/user-config.json`）的**唯一读取点**（方案 §5.1，D-6 配方）。

port / host / context_size / batch / ubatch / image_max_tokens（可选）/
startup_timeout / http_timeout / extra_argv / variant / binary_path 一律从这里
现读——缺省＝现状值（零缺省漂移，D-4：不写任何键时与写死时代逐字节一致）。

读取配方照抄 `media_ingest_config.py`：CONFIG_PATH **函数级 import**（保持测试
`monkeypatch("niu_api.config.CONFIG_PATH")` 注入点生效）、**每次现读**（护栏类现读
生效；argv 类需重启 llama-server 生效——R2 P3-8）、文件缺失 / 坏 JSON / 类型错 /
越界 ⇒ **回缺省 + WARNING、绝不抛**（配置写坏不得让程序起不来，D-6）。文件完好但
没有 `llama_cpp` 段是存量装机的**正常形态**（用户没配）⇒ 静默回缺省、不告警。

两条硬不变式（R2 P2-2 + R3 P2-5 边界修正，**对派生值同样成立**）：
① 生效 `min(batch,ubatch)` **硬钳制 ≤ 10000**——否则派生 `image_max_tokens =
   min − 64` 会同时撕开 `N ≤ 9999` 与 `0.09N + 300 < 1200`（`media_worker` 的
   `INFLIGHT_TIMEOUT`）两条约束；超出 ⇒ 该值钳到 10000 + WARNING 说明原因。
② `image_max_tokens()` = `min(batch,ubatch) − 模板开销(0) − 64`（缺省 ⇒ 8128，
   严格 < 8192）；显式写了 `N` ⇒ 必须同时满足 `N ≤ 9999` 且 `N < min(batch,ubatch)`，
   违反 ⇒ 回派生值 + WARNING（R1 P2-2：独立键不做校验必被服务端 500）。

派生量分侧导出（R5 N1：两侧系数不同，**不得合并**成一个函数——lightrag 侧缺省
2000 恰是被实测判死前值，7000 恰是被实测判死的值，合并会把一侧放宽 3.5 倍）：
- `embed_token_limit()`：两侧共用（`lightrag_adapter.EMBED_TOKEN_LIMIT` 的同源值）。
- `char_prefilter_server()`：服务器侧（`llama_server._CHAR_PREFILTER`）。
- `char_prefilter_adapter()` / `split_char_budget_adapter()`：lightrag 侧守卫。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# ── 缺省值＝现状值（与 llama_server.py 写死时代的模块常量逐字一致，D-4）──────
DEFAULT_PORT = 19878
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONTEXT_SIZE = 8192
DEFAULT_BATCH = 8192
DEFAULT_UBATCH = 8192
DEFAULT_STARTUP_TIMEOUT = 60.0   # 冷启动读取 310MB 权重的预算
DEFAULT_HTTP_TIMEOUT = 120.0
DEFAULT_VARIANT = "cpu"          # 仅文案/提示用（D-3：真正判据是 binary_path 或布局）

# 硬钳制上限：生效 min(batch,ubatch) ≤ 10000 ⇒ 派生 N ≤ 9936 ≤ 9999，且
# 0.09×9936+300 = 1194.24 < 1200（INFLIGHT_TIMEOUT，严格小于，非相等）。
MAX_EFF_MIN = 10000
# 显式 image_max_tokens 的独立上界（派生路径由 MAX_EFF_MIN 保证 ≤ 9936）。
MAX_EXPLICIT_IMAGE_TOKENS = 9999
# 模板 token 开销：一次性真机实测（2026-10-13）——仓库 GGUF 的 chat_template 为纯透传，
# /tokenize 开/关模板对照 200 字输入均为 100 token ⇒ 开销 0。
# ⚠️ 本值绑定**当前模型**的 chat_template：换模型必须对新模板重测再更新（照搬旧值
# 会让图片 token 预算静默错位）。
_TEMPLATE_TOKENS = 0
# 边界安全余量：图片 token(≤N) + 模板(0) 必须**严格** < min(batch,ubatch)，否则服务端 500。
_IMAGE_MARGIN = 64

# extra_argv 冲突守卫（R1 P3-3）：与程序固定语义开关冲突的 flag——出现即剔除
# （含其值）+ WARNING。不剔除的恶果：服务实际绑定的端口 ≠ Python 探活/Rust 清理
# 所用端口 ⇒ "等满超时 Fatal" 与 "杀不到真实实例" 两类事故。
_CONFLICT_FLAGS = frozenset({
    "--port", "--host", "-m", "--mmproj", "--embedding", "-c", "-b", "-ub",
})
# 其中需要跟一个**值**的 flag（`--embedding` 是布尔开关，无值可吞）。
_VALUE_FLAGS = frozenset({
    "--port", "--host", "-m", "--mmproj", "-c", "-b", "-ub",
})


@dataclass
class LlamaCppConfig:
    """一次现读后解析出的 `llama_cpp` 段（全部键都已校验/钳制）。

    `batch`/`ubatch` 是**钳制后**的生效值（≤ MAX_EFF_MIN）；`image_max_tokens`
    是**已裁决**的最终值（显式合法用显式，否则派生），恒满足两条不变式。
    """

    port: int
    host: str
    context_size: int
    batch: int
    ubatch: int
    image_max_tokens: int
    startup_timeout: float
    http_timeout: float
    extra_argv: tuple[str, ...]
    variant: str
    binary_path: str | None


def _load_section() -> tuple[dict | None, bool]:
    """现读 user-config.json 的 `llama_cpp` 段，返回 `(段, 是否致命)`。

    文件缺失 / 坏 JSON / 顶级类型错 / 段类型错 ⇒ `(None, True)`（回缺省须 WARNING）；
    文件完好但**没有该段** ⇒ `(None, False)`（存量装机正常形态＝用户没配，静默）。
    绝不抛。
    """
    from niu_api.config import CONFIG_PATH  # 函数级 import：测试 monkeypatch 注入点

    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 坏输入回默认，配置读取不得炸调用方
        return None, True
    if not isinstance(data, dict):
        return None, True
    if "llama_cpp" not in data:
        return None, False  # 无段＝正常缺省形态，静默
    sec = data["llama_cpp"]
    return (sec, False) if isinstance(sec, dict) else (None, True)


def _plain_int(value: object) -> int | None:
    """严格 int（拒绝 bool/float/str）；否则 None。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _validate_port(sec: dict) -> int:
    v = _plain_int(sec.get("port"))
    if v is None:
        if "port" in sec:
            logger.warning("[llama-config] port={} 类型错/越界（需 1–65535 的整数）→ 回缺省 {}",
                           sec.get("port"), DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= v <= 65535:
        logger.warning("[llama-config] port={} 越界（需 1–65535）→ 回缺省 {}", v, DEFAULT_PORT)
        return DEFAULT_PORT
    return v


def _validate_host(sec: dict) -> str:
    v = sec.get("host")
    if not isinstance(v, str) or not v:
        if "host" in sec:
            logger.warning("[llama-config] host={!r} 类型错/为空 → 回缺省 {}", v, DEFAULT_HOST)
        return DEFAULT_HOST
    return v


def _validate_context_size(sec: dict) -> int:
    v = _plain_int(sec.get("context_size"))
    if v is None:
        if "context_size" in sec:
            logger.warning("[llama-config] context_size={!r} 类型错（需正整数）→ 回缺省 {}",
                           sec.get("context_size"), DEFAULT_CONTEXT_SIZE)
        return DEFAULT_CONTEXT_SIZE
    if v <= 0:
        logger.warning("[llama-config] context_size={} 越界（需正整数）→ 回缺省 {}",
                       v, DEFAULT_CONTEXT_SIZE)
        return DEFAULT_CONTEXT_SIZE
    return v


def _validate_batch_or_ubatch(sec: dict, key: str, default: int) -> int:
    """batch/ubatch：≤0 或类型错 ⇒ 回缺省 + WARNING；> MAX_EFF_MIN ⇒ **钳制**（非回缺省）
    到 MAX_EFF_MIN + WARNING 说明原因（硬不变式①对派生值也成立）。"""
    v = _plain_int(sec.get(key))
    if v is None:
        if key in sec:
            logger.warning("[llama-config] {}={!r} 类型错（需正整数）→ 回缺省 {}",
                           key, sec.get(key), default)
        return default
    if v <= 0:
        logger.warning("[llama-config] {}={} 越界（需正整数）→ 回缺省 {}",
                       key, v, default)
        return default
    if v > MAX_EFF_MIN:
        logger.warning(
            "[llama-config] {}={} 超出硬钳制上限 {} → 生效 {}：生效 min(batch,ubatch) 必须 ≤ {}，"
            "否则派生 image_max_tokens(=min−64) 会同时违反 N≤9999 与 0.09N+300<1200"
            "（media_worker 在途超时）两条不变式",
            key, v, MAX_EFF_MIN, MAX_EFF_MIN, MAX_EFF_MIN)
        return MAX_EFF_MIN
    return v


def _validate_image_max_tokens(sec: dict, eff_min: int) -> int:
    """显式 `image_max_tokens` 是**可选覆盖**：不写 ⇒ 派生 `eff_min − 模板开销 − 64`；
    显式 N 必须同时满足 N ≤ 9999 且 N < eff_min，否则回派生值 + WARNING。"""
    derived = eff_min - _TEMPLATE_TOKENS - _IMAGE_MARGIN
    if "image_max_tokens" not in sec:
        return derived
    v = sec.get("image_max_tokens")
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        logger.warning("[llama-config] image_max_tokens={!r} 类型错（需正整数）→ 回派生值 {} "
                       "(min(batch,ubatch)−{}−{})", v, derived, _TEMPLATE_TOKENS, _IMAGE_MARGIN)
        return derived
    if v > MAX_EXPLICIT_IMAGE_TOKENS or v >= eff_min:
        logger.warning(
            "[llama-config] image_max_tokens={} 违反不变式（需 ≤{} 且 < min(batch,ubatch)={})"
            " → 回派生值 {}：违反前者会撕开 media_worker 在途超时约束，"
            "违反后者服务端直接 500",
            v, MAX_EXPLICIT_IMAGE_TOKENS, eff_min, derived)
        return derived
    return v


def _validate_timeouts(sec: dict, key: str, default: float) -> float:
    v = sec.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        if key in sec:
            logger.warning("[llama-config] {}={!r} 类型错/非正 → 回缺省 {}",
                           key, v, default)
        return float(default)
    return float(v)


def _validate_extra_argv(sec: dict) -> tuple[str, ...]:
    v = sec.get("extra_argv", [])
    if not isinstance(v, (list, tuple)):
        logger.warning("[llama-config] extra_argv={!r} 类型错（需字符串列表）→ 回缺省 []", v)
        return ()
    kept = tuple(t for t in v if isinstance(t, str))
    dropped = [t for t in v if not isinstance(t, str)]
    if dropped:
        logger.warning("[llama-config] extra_argv 中 {} 项非字符串已丢弃：{}", len(dropped), dropped)
    return kept


def _validate_variant(sec: dict) -> str:
    v = sec.get("variant")
    if not isinstance(v, str):
        if "variant" in sec:
            logger.warning("[llama-config] variant={!r} 类型错（需字符串）→ 回缺省 {}",
                           v, DEFAULT_VARIANT)
        return DEFAULT_VARIANT
    return v


def _validate_binary_path(sec: dict) -> str | None:
    if "binary_path" not in sec:
        return None
    v = sec.get("binary_path")
    if v is None:
        return None
    if not isinstance(v, str):
        logger.warning("[llama-config] binary_path={!r} 类型错（需字符串或 null）→ 按 null 处理", v)
        return None
    return v


def get() -> LlamaCppConfig:
    """现读 `llama_cpp` 段并解析出全键校验后的配置（每次调用现读，绝不抛）。

    文件缺失 / 坏 JSON / 段类型错 ⇒ 全键回缺省 + WARNING；无段（正常形态）⇒ 静默回缺省。
    缺省值与写死时代逐字节一致，故最坏情形＝行为不变。
    """
    sec, fatal = _load_section()
    if fatal:
        logger.warning("[llama-config] user-config.json 缺失/坏 JSON/llama_cpp 段类型错"
                       " → 全部键按缺省读取（零漂移，不影响启动）")
    if sec is None:
        sec = {}

    batch = _validate_batch_or_ubatch(sec, "batch", DEFAULT_BATCH)
    ubatch = _validate_batch_or_ubatch(sec, "ubatch", DEFAULT_UBATCH)
    eff_min = min(batch, ubatch)
    return LlamaCppConfig(
        port=_validate_port(sec),
        host=_validate_host(sec),
        context_size=_validate_context_size(sec),
        batch=batch,
        ubatch=ubatch,
        image_max_tokens=_validate_image_max_tokens(sec, eff_min),
        startup_timeout=_validate_timeouts(sec, "startup_timeout", DEFAULT_STARTUP_TIMEOUT),
        http_timeout=_validate_timeouts(sec, "http_timeout", DEFAULT_HTTP_TIMEOUT),
        extra_argv=_validate_extra_argv(sec),
        variant=_validate_variant(sec),
        binary_path=_validate_binary_path(sec),
    )


# ── 派生量（同源化：凡与 -b/-ub 耦合的护栏一律从这里现算，R1 P1-1）───────────


def embed_token_limit() -> int:
    """llama-server 单条输入 token 上限 = 生效 min(batch, ubatch)。

    **两侧共用**的唯一读数（服务器侧超限护栏与 lightrag 侧 EMBED_TOKEN_LIMIT 同源）。
    """
    c = get()
    return min(c.batch, c.ubatch)


def char_prefilter_server() -> int:
    """服务器侧字符预筛阈值 = int(生效 min(batch,ubatch) × 7000 / 8192)。

    缺省（8192）⇒ 恰为 7000（与写死时代 `_CHAR_PREFILTER` 一致，防漂移）。
    ⚠️ 不得与 `char_prefilter_adapter()` 合并（R5 N1：7000×4.000=28000 已被实测
    判死，只能用在服务端 500 报文的解析路径）。
    """
    c = get()
    return int(min(c.batch, c.ubatch) * 7000 / 8192)


def char_prefilter_adapter() -> int:
    """lightrag 侧字符预筛阈值 = int(生效 min(batch,ubatch) × 2000 / 8192)。

    缺省（8192）⇒ 恰为 2000（2000×4.000=8000 < 8192 的硬预筛）。
    ⚠️ 不得与 `char_prefilter_server()` 合并（R5 N1：两者系数 2000 vs 7000 不同，
    合并＝把 lightrag 侧守卫默认放宽 3.5 倍，而 7000 正是被实测判死的值）。
    """
    c = get()
    return int(min(c.batch, c.ubatch) * 2000 / 8192)


def split_char_budget_adapter() -> int:
    """lightrag 侧分段子块字符预算 = int(生效 min(batch,ubatch) × 1500 / 8192)。

    缺省（8192）⇒ 恰为 1500（1500×4.000=6000 < 8192，切分子块无需再次计量）。
    """
    c = get()
    return int(min(c.batch, c.ubatch) * 1500 / 8192)


def image_max_tokens() -> int:
    """图片 token 预算 = 显式合法覆盖，否则派生 min(batch,ubatch) − 模板开销 − 64。

    缺省 ⇒ 8128（严格 < 8192）。两条不变式（N ≤ 9999 且 N < min(batch,ubatch)）
    对派生与显式两种来源都成立。
    """
    return get().image_max_tokens


def media_http_timeout() -> float:
    """媒体编码独立超时 = image_max_tokens() × 0.09 + 300.0。

    按 0.09 s/token 最坏吞吐定量；+300 s 余量覆盖串行排队与数十 MB 请求体传输。
    缺省 ⇒ 8128×0.09+300 = 1031.52，必须严格 < media_worker.INFLIGHT_TIMEOUT(1200)
    ——由生效 min ≤ 10000 的硬钳制保证（0.09×9936+300=1194.24 < 1200）。
    """
    return float(image_max_tokens()) * 0.09 + 300.0


def llama_argv_overrides() -> list[str]:
    """`extra_argv` 的冲突守卫后取值（供 T3 的 `_build_argv()` 追加到 argv 尾部）。

    用户可写 ["--ngl","99"]、["-dev","cuda"] 等任意自定参数；但与程序固定语义
    冲突的 flag（`_CONFLICT_FLAGS`）一律**剔除该 flag 及其值**（`--port=123` 的
    等号形态一并剔除）+ WARNING——否则服务实际绑定端口 ≠ 探活/清理所用端口。
    其余条目原样保留。
    """
    raw = get().extra_argv
    out: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if "=" in tok:
            flag = tok.split("=", 1)[0]
            if flag in _CONFLICT_FLAGS:
                logger.warning("[llama-config] extra_argv 含冲突参数 {!r}（与程序固定语义"
                               " --port/--host/-m/--mmproj/--embedding/-c/-b/-ub 冲突）"
                               " → 已连同其值剔除", tok)
                i += 1
                continue
        if tok in _CONFLICT_FLAGS:
            logger.warning("[llama-config] extra_argv 含冲突参数 {!r} → 已连同其值剔除", tok)
            i += 1
            if tok in _VALUE_FLAGS and i < len(raw):
                i += 1  # 吞掉该 flag 的值
            continue
        out.append(tok)
        i += 1
    return out
