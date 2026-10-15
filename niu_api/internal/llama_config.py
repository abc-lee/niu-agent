"""`llama_cpp` 配置段（`~/.niu/config/user-config.json`）**v2：一条完整命令行**（方案 §5'，唯一读取点）。

v2（2026-10-15 定稿，取代 §5）：启动参数写进 **一条完整命令行**（`command` 键），
程序原样执行它，并**只从这一行里取三件**（§5'.2）：① `--port` / `--host`（探活与
编码都要连它）② 第一个词（可执行体）③ 多模态标识（`--mmproj` / `--no-mmproj`，三态）。
其余一律不解释——参数合法性、模型能否加载、mmproj 是否匹配，llama.cpp 自己会说。

旧结构化键（`port` / `host` / `context_size` / `batch` / `ubatch` / `image_max_tokens` /
`extra_argv` / `variant` / `binary_path` / `gguf_path` / `mmproj_path`）**一律不再读**：
出现时只发一条 WARNING「已废弃，语义并入 `command`」（每进程一次），不生效、不写回。
v2 明确删除的复杂度（防回退）：argv 拼装、extra_argv 与冲突守卫、min(batch,ubatch) 钳制、
image_max_tokens 校验/派生、variant/binary_path/gguf_path/mmproj_path 键。

读取契约（§5'.5）：每次调用**现读**；文件缺失 / 坏 JSON / 段类型错 / 键类型错 ⇒
回缺省 + WARNING、**绝不抛**（配置写坏不得让程序起不来）。
缺段补写（存量装机）：`llama_cpp` 段**不存在** ⇒ 原子写入缺省段（四个键，`command: null`）
+ 日志一行；**段已存在 ⇒ 文件一个字都不动**（单个缺键也不补）。程序永不写包内。
布局路径由 `embedding_artifacts.llama_server_path()` / `media_artifacts.mmproj_path()`
提供，本模块**惰性 import**（函数内 import，不放模块顶层）。
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# ── 缺省值＝现状值（与 llama_server.py 写死时代的模块常量逐字一致，D-4）──────
DEFAULT_PORT = 19878
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONTEXT_SIZE = 8192
DEFAULT_STARTUP_TIMEOUT = 60.0   # 冷启动读取 310MB 权重的预算
DEFAULT_HTTP_TIMEOUT = 120.0
# 媒体编码独立超时：缺省维持现状量级（8128×0.09+300，8128 是内置缺省行的
# --image-max-tokens 值）；v2 不再随任何结构化键现算，只保留**安全缺省 + 可覆盖键**。
DEFAULT_MEDIA_HTTP_TIMEOUT = 1031.52

# 旧结构化键：v2 一律不读，语义全部并入 `command`（出现 ⇒ 一条 WARNING，每进程一次）。
_DEPRECATED_KEYS = frozenset({
    "port", "host", "context_size", "batch", "ubatch",
    "image_max_tokens", "extra_argv", "variant",
    "binary_path", "gguf_path", "mmproj_path",
})

_warned_reasons: set[str] = set()


def _warn_once(reason: str, msg: str, *args: object) -> None:
    """进程内只警告一次（同 `embedding_artifacts._warn_once` 纪律）：同一 reason 至多告警一次。"""
    if reason not in _warned_reasons:
        _warned_reasons.add(reason)
        logger.warning(msg, *args)


@dataclass
class LlamaCppConfig:
    """一次现读后解析出的 `llama_cpp` 段（全部键都已校验 / 回缺省）。

    `command` 是用户写的**原行**（str 或 None；None ⇒ 用内置缺省行，生效行见
    `command_line()`）；三个超时是校验后的生效值（类型错 / 非正 ⇒ 缺省）。
    """

    command: str | None
    startup_timeout: float
    http_timeout: float
    media_http_timeout: float


# ── 配置读取（配方照抄 media_ingest_config.py：CONFIG_PATH 函数级 import、每次现读、绝不抛）──


def _load_section() -> tuple[dict | None, bool]:
    """现读 user-config.json 的 `llama_cpp` 段，返回 `(段, 是否致命)`。

    文件缺失 / 坏 JSON / 顶级类型错 / 段类型错 ⇒ `(None, True)`（回缺省须 WARNING）；
    文件完好但**没有该段** ⇒ `(None, False)`（存量装机正常形态，调用方做缺段补写）。
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
        return None, False  # 无段＝存量装机正常形态（调用方补写）
    sec = data["llama_cpp"]
    return (sec, False) if isinstance(sec, dict) else (None, True)


def _atomic_write_json(path: Path, data: dict) -> bool:
    """原子 JSON 写（tempfile + os.replace——reader 永远看到完整文件）。

    失败 → 降级并返回 False（绝不抛——缺段补写不得毒化调用方，同 model_probe 写纪律）：
    前置（建目录 / 建临时文件）也在 try 内，目录不可写 / 磁盘满同样降级跳过。
    前置失败与写失败告警用两个独立 reason（`atomic_write_pre` / `atomic_write`），
    同进程内两条各可达一次——写失败不再被先发生的前置失败永久吞掉。
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    except OSError:
        _warn_once("atomic_write_pre", "[llama_config] 原子写 user-config.json 前置失败（目录不可写/磁盘满）")
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _warn_once("atomic_write", "[llama_config] 原子写 user-config.json 失败")
        return False


def _materialize_default_section() -> None:
    """缺段补写（存量装机）：`llama_cpp` 段**不存在** ⇒ 原子写入缺省段（四个键，
    `command: null` ⇒ 内置缺省行；生效命令行由启动日志逐字打印）+ 日志一行；**段已存在 ⇒ 文件一个字都不动**（单个缺键也不补）。

    纪律照 model_probe._write_vision_capabilities：文件缺失（首启 seed 的事）/ 坏 JSON /
    顶级非对象 ⇒ 跳过写入（不写坏文件）；只改 `llama_cpp` 单键，其余段 / 字段原样保留；
    原子写（tempfile + os.replace）。永不抛。
    """
    from niu_api.config import CONFIG_PATH

    path = Path(CONFIG_PATH)
    if not path.exists():
        return  # 文件不存在＝首启未 seed（memory/user-config.json 由启动器负责），非本模块职责
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 损坏不写坏文件
        logger.warning("[llama_config] user-config.json 读取失败，跳过 llama_cpp 缺段补写: {}", e)
        return
    if not isinstance(data, dict) or "llama_cpp" in data:
        return
    data["llama_cpp"] = {
        "command": None,
        "startup_timeout": DEFAULT_STARTUP_TIMEOUT,
        "http_timeout": DEFAULT_HTTP_TIMEOUT,
        "media_http_timeout": DEFAULT_MEDIA_HTTP_TIMEOUT,
    }
    if _atomic_write_json(path, data):
        logger.info("[llama_config] 已写入缺省 `llama_cpp` 段（段可见、可改；`command` 为 null ⇒ 内置缺省行，生效命令行由启动日志逐字打印）")


def get() -> LlamaCppConfig:
    """现读 `llama_cpp` 段并解析出全键校验后的配置（每次调用现读，绝不抛，§5'.5）。

    缺段补写在此触发：文件完好但无 `llama_cpp` 段 ⇒ 先原子写入缺省段，再回缺省。
    旧结构化键**不生效**：出现（port/host/context_size/batch/ubatch/image_max_tokens/
    extra_argv/variant/binary_path/gguf_path/mmproj_path）⇒ 一条 WARNING「已废弃，语义
    并入 `command`」（每进程一次），不生效、不写回。
    """
    sec, fatal = _load_section()
    if fatal:
        _warn_once("file_bad", "[llama_config] user-config.json 缺失/损坏或 llama_cpp 段类型错，回缺省")
        return LlamaCppConfig(None, DEFAULT_STARTUP_TIMEOUT, DEFAULT_HTTP_TIMEOUT, DEFAULT_MEDIA_HTTP_TIMEOUT)
    if sec is None:
        _materialize_default_section()
        return LlamaCppConfig(None, DEFAULT_STARTUP_TIMEOUT, DEFAULT_HTTP_TIMEOUT, DEFAULT_MEDIA_HTTP_TIMEOUT)
    found = sorted(k for k in sec if k in _DEPRECATED_KEYS)
    if found:
        _warn_once(
            "deprecated_keys",
            "[llama_config] llama_cpp 含已废弃结构化键 {}：语义已并入 `command`，本次不生效、不写回",
            found,
        )
    return LlamaCppConfig(
        command=_validate_command(sec),
        startup_timeout=_validate_timeout(sec, "startup_timeout", DEFAULT_STARTUP_TIMEOUT),
        http_timeout=_validate_timeout(sec, "http_timeout", DEFAULT_HTTP_TIMEOUT),
        media_http_timeout=_validate_timeout(sec, "media_http_timeout", DEFAULT_MEDIA_HTTP_TIMEOUT),
    )


def _validate_command(sec: dict) -> str | None:
    """`command` 用户原行：str 且非空白 ⇒ 原样采用；null/缺失 ⇒ None（⇒ 内置缺省行）；
    类型错 ⇒ WARNING + None（回缺省）。"""
    v = sec.get("command")
    if v is None:
        return None
    if isinstance(v, str) and v.strip():
        return v
    _warn_once("command_type", "[llama_config] llama_cpp.command 类型错（应为字符串或 null），用内置缺省行")
    return None


def _validate_timeout(sec: dict, key: str, default: float) -> float:
    """超时（startup/http/media_http）：合法数值且 > 0 ⇒ 采用（int 转 float）；
    null/缺失 ⇒ 缺省；类型错 / 非正 ⇒ WARNING + 缺省。绝不抛。"""
    v = sec.get(key)
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    _warn_once(f"timeout_{key}", "[llama_config] llama_cpp.{} 类型错（应为数值 > 0），用缺省 {}", key, default)
    return default


# ── 生效行与"只取三件"（§5'.2）──────────────────────────────────────────────


def default_command() -> str:
    """内置缺省行（§5'.1：不写 `command` 的存量装机行为与今天逐字一致）。

    `<布局二进制> -m <布局 GGUF> --embedding --port 19878 --host 127.0.0.1
    -c 8192 -b 8192 -ub 8192` ＋（`media_ingest.enabled` 开）`--mmproj <布局 mmproj>
    --image-max-tokens 8128` /（关）`--no-mmproj`。
    布局路径**惰性 import**（函数内）：`embedding_artifacts.llama_server_path()` /
    `embedding_artifacts.gguf_path()` / `media_artifacts.mmproj_path()`；多模态支路跟随
    `media_ingest.enabled` 功能开关（它不是 llama.cpp 参数，只是选缺省行两支的开关）。
    引号策略按平台（矩阵实测选定，勿拍脑袋改回单一策略）：POSIX 分支 `shlex.quote`
    逐字不变；win32 消费链（非 posix 分词 + 剥配对引号，`argv()`）不认 POSIX 三段转义
    （含单引号的路径被撕碎、首词截断、启动报"可执行体不存在"），改为**词含空白或单引号
    时用双引号包裹、否则裸词**：非 posix 分词把引号词当整体（引号保留），剥配对引号后
    整词还原；裸词在反斜杠/双引号/`&`/`(` 上无特殊义（非 posix 分词里皆是词字符），逐字保留。
    已知边界：词里同时含 `=` 与（空白或单引号）时不整词还原、残留首尾 `"`——既有消费侧残留，非本策略引入（详见
    `docs/superpowers/reviews/2026-10-15-thirdparty-T3a2-verify-final.md` 发现 1）。
    """
    from niu_api.internal.embedding_artifacts import gguf_path, llama_server_path
    from niu_api.internal.media_artifacts import mmproj_path
    from niu_api.internal.media_ingest_config import media_ingest_enabled

    parts = [
        str(llama_server_path()),
        "-m", str(gguf_path()),
        "--embedding",
        "--port", str(DEFAULT_PORT),
        "--host", DEFAULT_HOST,
        "-c", str(DEFAULT_CONTEXT_SIZE),
        "-b", "8192",
        "-ub", "8192",
    ]
    if media_ingest_enabled():
        parts += ["--mmproj", str(mmproj_path()), "--image-max-tokens", "8128"]
    else:
        parts.append("--no-mmproj")
    if sys.platform == "win32":
        # win32 消费侧（非 posix 分词 + 剥配对引号）不认 POSIX 三段转义 `'…'"'"'…'`
        # （含单引号的路径被撕碎）⇒ 词含空白或单引号时用双引号包裹、否则裸词。
        return " ".join(
            f'"{p}"' if (any(ch.isspace() for ch in p) or "'" in p) else p
            for p in parts
        )
    return " ".join(shlex.quote(p) for p in parts)


def command_line() -> str:
    """生效行：用户 `command` 非空 ⇒ 原样返回（100% 归用户，程序一个字都不改）；
    否则 `default_command()`。"""
    cfg = get()
    if cfg.command:
        return cfg.command
    return default_command()


def argv() -> list[str]:
    """生效行分词：POSIX 用 shell 规则（`shlex.split`）；Windows 路径含反斜杠，
    禁用以 posix 语义（`\\U`、`\\n` 会被当转义符吃掉 ⇒ 绝对路径神秘损坏），改为非 posix
    （反斜杠保留）+ 剥引号与引号值回接（`_win32_unquote`）；两分支解析失败 ⇒ 按空白分词
    + WARNING，**不改行**。

    残留边界（win32 专属，防后人误判）：非 posix shlex **不认词内引号** ⇒ `-m="带空格 路径"`
    会先被切成 `-m="带空格` + `路径"` 两段，`=` 后的值段随后剥配对引号并按空白回接；
    回接不到闭引号（如引号不配对）则保留原样、按空白分词降级。**带空格的值请用分写形态
    `-m "路径"`**（实测正常，POSIX 分支天然如此，不依赖回接）。POSIX 分支一字不改。
    """
    line = command_line()
    if sys.platform == "win32":
        try:
            toks = shlex.split(line, posix=False)
        except ValueError:
            _warn_once("shlex", f"[llama_config] command 行分词失败，退空白分词（行未改）: {line}")
            toks = line.split()
        return _win32_unquote(toks)
    try:
        return shlex.split(line)
    except ValueError:
        _warn_once("shlex", f"[llama_config] command 行引号不配平等，shlex 解析失败，退空白分词（行未改）: {line}")
        return line.split()


def _win32_unquote(toks: list[str]) -> list[str]:
    """win32 专用后处理：对每个 token 先 `head, sep, tail = t.partition("=")`，若 `sep and head`
    ⇒ 值段是 `tail`，否则整词是值段；值段剥配对引号——修 `--port="8128"` / `--host="0.0.0.0"` /
    `-m="…"` 三类等号+引号形态（非 posix shlex 不剥词内引号，引号原样留在值段 ⇒ 旧代码下
    `port()` 回缺省、`host()` 静默返回带引号串、带空格路径在空白处截断）。
    """
    out: list[str] = []
    i = 0
    while i < len(toks):
        head, sep, tail = toks[i].partition("=")
        value = tail if (sep and head) else toks[i]
        value, i = _unquote_win32_value(toks, i, value)
        out.append(head + sep + value if sep and head else value)
    return out


def _unquote_win32_value(toks: list[str], i: int, value: str) -> tuple[str, int]:
    """剥值段成对的首尾引号；开引号悬空（非 posix shlex 把带空格引号值切在空白处，
    `-m="a b"` ⇒ `-m="a` + `b"`）⇒ 向前回接后续 token 直到首个以同类闭引号结尾的 token，
    再剥首尾引号；回接不到闭引号（引号不配对等）⇒ 保留原样，交调用方按坏行降级。
    返回 `(值, 下一 token 下标)`。"""
    if len(value) > 1 and value[0] in "\"'":
        q = value[0]
        if value.endswith(q):
            return value[1:-1], i + 1
        j = i + 1
        while j < len(toks) and not toks[j].endswith(q):
            value += " " + toks[j]
            j += 1
        if j < len(toks):
            value += " " + toks[j]
            return value[1:-1], j + 1
    return value, i + 1


def _scan_value(tokens: list[str], names: tuple[str, ...]) -> str | None:
    """扫 argv 里的取值型 flag：`--port 19878` 与 `--port=19878` 等号形态都支持；
    行尾孤 flag（无值）与未出现都视为未找到 ⇒ None。"""
    for i, tok in enumerate(tokens):
        for name in names:
            if tok == name:
                if i + 1 < len(tokens):
                    return tokens[i + 1]
                return None  # flag 悬在行尾没有值——按未找到处理
            prefix = name + "="
            if tok.startswith(prefix):
                return tok[len(prefix):]
    return None


def port() -> int:
    """生效行里扫 `--port`（含 `--port=19878` 等号形态）；解析不到 / 非正整数 ⇒
    `19878` + 一条 WARNING（每进程只警告一次）。"""
    raw = _scan_value(argv(), ("--port",))
    if raw is None:
        _warn_once("port", "[llama_config] 行内未解析到 --port ⇒ 程序按 127.0.0.1:19878 探活与编码；请确保这一行绑定同一地址端口，或在这一行里显式写上 --port")
        return DEFAULT_PORT
    try:
        v = int(raw)
    except ValueError:
        _warn_once("port", f"[llama_config] --port 值 {raw!r} 非整数，回缺省 19878")
        return DEFAULT_PORT
    if v <= 0:
        _warn_once("port", f"[llama_config] --port 值 {v} 非正，回缺省 19878")
        return DEFAULT_PORT
    return v


def host() -> str:
    """生效行里扫 `--host`（含 `--host=…` 等号形态）；解析不到 / 空白 ⇒
    `127.0.0.1` + 一条 WARNING（每进程只警告一次）。"""
    raw = _scan_value(argv(), ("--host",))
    if raw is None or not raw.strip():
        _warn_once("host", "[llama_config] 行内未解析到 --host ⇒ 程序按 127.0.0.1:19878 探活与编码；请确保这一行绑定同一地址端口，或在这一行里显式写上 --host")
        return DEFAULT_HOST
    return raw.strip()


def context_size() -> int:
    """生效行里扫 `-c` / `--ctx-size`（含等号形态）；解析不到 / 非法（非整数 / 非正）⇒
    `8192`（零漂移）+ 一条 WARNING（每进程只警告一次）。"""
    raw = _scan_value(argv(), ("-c", "--ctx-size"))
    if raw is None:
        _warn_once("ctx", "[llama_config] 行内未解析到 -c/--ctx-size，回缺省 8192")
        return DEFAULT_CONTEXT_SIZE
    try:
        v = int(raw)
    except ValueError:
        _warn_once("ctx", f"[llama_config] -c 值 {raw!r} 非整数，回缺省 8192")
        return DEFAULT_CONTEXT_SIZE
    if v <= 0:
        _warn_once("ctx", f"[llama_config] -c 值 {v} 非正，回缺省 8192")
        return DEFAULT_CONTEXT_SIZE
    return v


def embed_token_limit() -> int:
    """llama-server 单条输入 token 上限 = 生效行里的 `-c` 上下文大小。

    v2：旧 `min(batch,ubatch)` 语义随结构化键一并消失，输入改为行里的 `-c`
    （§5'.3：解析不到 ⇒ 回缺省 8192，零漂移）。`llama_server` 侧已改调本模块函数
    （旧同名常量已删）；lightrag 侧将在 T3a2-2 改为调 `char_prefilter_adapter()` /
    `split_char_budget_adapter()`。
    """
    return context_size()


def char_prefilter_server() -> int:
    """服务器侧字符预筛阈值 = int(embed_token_limit() × 7000 / 8192)。

    两侧系数各自独立（R5 N1：**不得合并**成一个函数——lightrag 侧缺省 2000 恰是被实测
    判死前值，7000 恰是被实测判死的值，合并会把一侧放宽 3.5 倍）。
    供 `llama_server` 侧预筛（该侧已改调本函数，旧 `_CHAR_PREFILTER` 常量已删）。
    """
    return int(embed_token_limit() * 7000 / 8192)


def char_prefilter_adapter() -> int:
    """lightrag 侧字符预筛阈值 = int(embed_token_limit() × 2000 / 8192)。独立于服务器侧系数。"""
    return int(embed_token_limit() * 2000 / 8192)


def split_char_budget_adapter() -> int:
    """lightrag 侧分段子块字符预算 = int(embed_token_limit() × 1500 / 8192)。独立于两侧系数。"""
    return int(embed_token_limit() * 1500 / 8192)


def executable() -> str:
    """生效行的第一个词（可执行体）；行空 ⇒ 回缺省行的第一个词。"""
    tokens = argv()
    if tokens:
        return tokens[0]
    return default_command().split()[0]


def model_path() -> str | None:
    """生效行里 `-m` / `--model` 的值（模型权重路径，顺带存在性检查由启动方做）；行里没有 ⇒ `None`。"""
    return _scan_value(argv(), ("-m", "--model"))


def mmproj_state() -> bool | None:
    """多模态标识**三态**判定（仍从生效行里判，§5'.2）：

    - 行里有 `--mmproj` ⇒ `True`（媒体链可用前提之一）；
    - 行里有 `--no-mmproj` ⇒ `False`（明确关闭，防御服务端静默加载同目录 mmproj）；
    - **两者都没有 ⇒ `None`（"未声明"——绝不静默当 True**，`media_ready()` 按未声明处理，
      状态端点 reason 如实写明；自写行时二者必须选一个）。
    - 两个都出现 ⇒ 行里**后出现**者胜（llama.cpp 的 flag 覆盖语义）。
    """
    state: bool | None = None
    for tok in argv():
        if tok == "--mmproj" or tok.startswith("--mmproj="):
            state = True
        elif tok == "--no-mmproj":
            state = False
    return state


# ── 超时三键（程序行为，不是 llama.cpp 参数）────────────────────────────────


def startup_timeout() -> float:
    """等 llama-server 就绪的预算（程序行为）：配置值，缺省 60.0。"""
    return get().startup_timeout


def http_timeout() -> float:
    """探活 / 编码 / 检索的 HTTP 客户端超时（程序行为）：配置值，缺省 120.0。"""
    return get().http_timeout


def media_http_timeout() -> float:
    """媒体编码独立超时：配置值，缺省 1031.52（现状量级；v2 不再随任何键现算）。"""
    return get().media_http_timeout
