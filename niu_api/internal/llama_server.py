"""llama.cpp `llama-server` 子进程生命周期 + HTTP 编码客户端（S2）。

设计对齐 niu_api 内唯一长驻子进程范式 IMGateway（`niu_api/channel/gateway.py`）：
Popen 拉起、看门狗探活、terminate→wait→kill 收尾、仅监听 127.0.0.1。
不引入第二套子进程约定。

本模块只负责「进程活着 + 能把文本换成向量」；启动行（可执行体/全部参数/多模态形态）
的唯一真相源是 `niu_api.internal.llama_config` 的 `llama_cpp.command` 行（v2：配置里
一条完整命令行，程序**原样执行**、逐字打进启动日志，只从行里取三件——地址端口、
可执行体、多模态三态，见 §5'.2）；模型产物布局路径的真相源在
`niu_api.internal.embedding_artifacts`（S1），此处只消费不重定义。
"""

from __future__ import annotations

import atexit
import base64
import collections
import http.client
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from pathlib import Path
from loguru import logger

from niu_api.internal import llama_config

from niu_api.internal.embedding_artifacts import (
    describe_missing,
    llama_needs_exec_bit,
    llama_server_path,
)

from niu_api.internal.media_artifacts import (
    describe_missing_media,
    media_artifacts_ready,
    mmproj_path,
)
from niu_api.internal.media_ingest_config import media_ingest_enabled

# ── 媒体编码后端故障类别（2026-10-17 用户定稿：只有它可重试）─────────────


class MediaEncodeBackendError(RuntimeError):
    """媒体编码后端（llama-server）故障——唯一可重试的失败类别。

    未就绪 / HTTP 错误 / 连接失败 / 超时 / 响应结构异常都归此类；
    mmproj 未加载等**需用户动作才能恢复**的配置问题不归此类（一次处置）。
    """

# ── S7-T2：单条输入 token 上限 + 超限护栏参数 ───────────────────────────
# 上限 = 生效行里的 `-c`（llama_config.embed_token_limit()，缺省 8192 零漂移）：
# llama-server 单条输入的 token 上限随用户那一行的 -c/-b/-ub 走，程序不再自留常量。
# 截断重试上界：覆盖 log2(任意实际输入/上限) 的收敛需要。用尽即 raise
# （报文带真实 token 数、已试次数、原文长度）——不得静默跳过，
# 跳过即把"整批回滚"换成"少一条向量的无声失败"。
MAX_TRUNCATE_ATTEMPTS = 6
# 主通道：llama.cpp 的 500 报文形如
#   input (2250 tokens) is too large to process. increase the physical batch size ...
# 只从这句里取真实 N，不把"这条超限"的判定押在整句英文文案上。
_TOKEN_COUNT_RE = re.compile(r"input\s*\((\d+)\s*tokens?\)", re.IGNORECASE)

# 编码串行化锁（与下面的 _lock 分工：_lock 管进程生命周期，本锁管 HTTP 编码请求）。
# 根因：llama-server 是单实例推理服务，会把并发请求在内部串行化；客户端并发发 N 批，
# 每批的单请求延迟就被顶到约 N × 单批耗时 —— PM 实测 8 批并发总耗时 119.9s，
# 恰好压在当时 HTTP 超时 120.0s 边界上，导致 repair 的 vdb_chunks 重建必现 timed out。
# 串行化后单批约 21.4s（235 条真实 chunk 实测），既不超时也不让服务端空转排队。
_encode_lock = threading.Lock()

# ── 交互向量活跃度（媒体编码让路门，D-31；用户口径①②）───────────────────
# 媒体工作者以「交互向量闲置 ≥ VECTOR_IDLE_GATE_SECONDS ∧ 交互编码不在途」为启动新编码的
# 前置条件（media_worker.tick ⑤）。只记录/计数**文本/交互**编码（encode_batch——LightRAG 检索/
# 入库/命名/搜索的嵌入全经它）；媒体自身编码（encode_media*）既不**打点**也不计在途——否则
# 连编会把自己的下一项越推越晚，排空被卡成每项间隔 30s（用户口径①）。
_activity_lock = threading.Lock()
_last_text_encode_at: float | None = None
_text_inflight = 0


def _mark_text_encode() -> None:
    global _last_text_encode_at
    with _activity_lock:
        _last_text_encode_at = time.monotonic()


def _begin_text_encode() -> None:
    """文本编码在途 +1（**含等待 _encode_lock 的排队段**——「不抢」判据靠它）。"""
    global _text_inflight
    with _activity_lock:
        _text_inflight += 1


def _end_text_encode() -> None:
    global _text_inflight
    with _activity_lock:
        _text_inflight -= 1


def vector_idle_seconds() -> float:
    """距最近一次**文本编码完成**的秒数；进程内从未发生过 ⇒ inf（视为一直闲置、立即放行）。
    纯内存读、零探活、零阻塞——供媒体工作者逐拍查询（tick 由调度循环每 10s 调一次）。"""
    with _activity_lock:
        ts = _last_text_encode_at
    if ts is None:
        return float("inf")
    return time.monotonic() - ts


def text_encode_busy() -> bool:
    """是否有文本/交互编码在途（含排队等锁段）。媒体门据此「不抢」（用户口径②）。"""
    with _activity_lock:
        return _text_inflight > 0


_lock = threading.Lock()
_proc: subprocess.Popen | None = None
# 本进程拉起的实例的多模态形态（T1，v2 起三态，取自生效行的 llama_config.mmproj_state()）：
# True＝行里有 --mmproj（媒体链可用前提之一）；False＝--no-mmproj 明确关闭；
# None＝未声明（行里两者皆无）——**绝不按 True 处理**（§5'.2）。
# media_ready() = 旗标为 True ∧ 健康——**不是**"开关开 ∧ 健康"（那是静默降级，审查 P2）。
# 旗标在**三处**复位：start() 的新实例路径按生效行置位；_stop_locked 收尾（含自行崩溃后经
# 它复位）；start() 的复用分支发现本模块实例已死时（P2①：自行崩溃旗标未复位 + 外部纯文本
# 实例占用本行 --port 端口 ⇒ 只看"健康 ∧ 旗标"会假真）。复用分支在本实例存活/从未拉起时
# 不动旗标。
_mmproj_loaded: bool | None = False

# ── S7-T1：llama-server 子进程 stderr 的有界内存排障缓冲 ──────────────────
# 日志文件已关（~/.niu/logs/llama_server.log 不再生成：verbosity 3 每个 embed
# 请求打 4+ 行、只增不减且无人看）。但"起不来的原因"（模型加载失败、bad allocation）
# 原本只见于该文件，全丢后退化成只剩退出码，而 Windows 恰是最需排障的一端。
# 故 stderr 接 PIPE，由守护线程逐行读进这个环形缓冲：不落盘、有界（50 行），
# 只在两处失败分支（进程提前退出 / terminate 超时升级 kill）把尾部带给 loguru。
_recent_stderr: collections.deque[str] = collections.deque(maxlen=50)
_stderr_thread: threading.Thread | None = None
_STDERR_TAIL_LINES = 8


def _base_url() -> str:
    # 地址＝生效行里解析出的 --host/--port（llama_config 现读；解析不到回落
    # 127.0.0.1:19878 并告警——探活/编码与 llama.cpp 实际绑定端口不一致是配置错误，
    # 文案引导去检查那一行，而不是假装"服务端就用 19878"）。
    return f"http://{llama_config.host()}:{llama_config.port()}"


def _popen_flags() -> dict:
    """平台相关 Popen 参数（对齐 IMGateway：Windows 隐忍窗口）。"""
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": 0x08000000}
    return {}


def _build_argv() -> list[str]:
    """生效命令行按 shell 规则分词（v2：`llama_cpp.command` 行是唯一真相——程序
    **原样执行**这一行，不再自己拼任何参数；缺省行生成、--port/--host/-c 解析、
    多模态三态判定全在 llama_config）。"""
    return llama_config.argv()


def _inside_layout(executable: str) -> bool:
    """首词是否在布局目录（布局二进制所在目录）内（§5'.2 / F5 布局判定）。

    布局内（内置缺省行/存量装机）⇒ 行为与写死时代逐字一致：布局产物 Fatal
    （describe_missing 文案）+ 可执行位补 chmod。布局外（用户自备 GPU/AMD 构建
    放到别处）⇒ 跳过布局产物 Fatal 与 chmod——用户自己的构建自己保证可执行位，
    预检只如实检查该可执行体本身。
    """
    try:
        return Path(executable).parent == llama_server_path().parent
    except Exception:  # noqa: BLE001 —— 路径判定不得炸启动（判否即按布局外处理）
        return False


def _exists_quietly(path: str) -> bool:
    """文件存在性（F5：文件 stat 一律包 try——怪路径/超长/权限异常都判"不存在"，
    由调用方如实报错，绝不以未捕获 FileNotFoundError 炸穿启动路径）。"""
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _preflight_check(argv: list[str], inside_layout: bool) -> None:
    """拉起前的存在性预检（拆成可单测的小函数：传入命令行与布局判定，不埋在 start()）。

    ① 可执行体（首词）：布局内 ⇒ `describe_missing()` 已覆盖二进制（行为与今天
       逐字一致；Fatal 文案首行 = launcher 的 MISSING_ARTIFACTS_HANDLE，不得改）；
       布局外 ⇒ 只查首词本身，不存在 ⇒ 如实报该路径（绝不因布局产物缺失而拒启
       用户自备构建——F5）。
    ② I-2（沿用今天顺序）：开关开而家目录 mmproj 产物缺失 ⇒ spawn 之前 fail-loud，
       不拉起一个必然编码不了的纯文本实例。
    ③ 行里若带 `-m <路径>` ⇒ 顺带检查该路径存在（找不到 ⇒ 启动前如实报该路径）。
    ④ chmod 只发生在布局内（布局外用户自己的构建自己保证可执行位）。
    """
    exe = argv[0] if argv else llama_config.executable()
    if inside_layout:
        missing = describe_missing()
        if missing:
            raise RuntimeError(missing)
    elif not _exists_quietly(exe):
        raise RuntimeError(
            f"可执行体不存在: {exe}（llama_cpp.command 行首词；请把构建放到该路径，或改这一行）"
        )
    if media_ingest_enabled() and not media_artifacts_ready():
        # 报文必须指名缺失产物路径：describe_missing_media 在真实路径已就位时会返回
        # 空串（测试会把本模块的 mmproj_path 指向不存在的临时路径），此时以本模块的
        # 产物路径兜底。
        raise RuntimeError(
            describe_missing_media()
            or f"mmproj 产物缺失: {mmproj_path()}（请按手册安装后重启 llama-server）"
        )
    model = llama_config.model_path()
    if model is not None and not _exists_quietly(model):
        raise RuntimeError(f"模型文件不存在: {model}（llama_cpp.command 行 -m 的值）")
    if inside_layout and llama_needs_exec_bit():
        try:
            mode = Path(exe).stat().st_mode
        except OSError:
            mode = None  # 检查与 chmod 之间文件消失 ⇒ 跳过 chmod，spawn 阶段会如实报错
        if mode is not None and not (mode & 0o111):
            # 没跑铁律 7 的 chmod 修复时不静默失败，自己补一次
            logger.warning("[llama-server] 二进制缺可执行位，自动补 chmod +x")
            Path(exe).chmod(mode | 0o111)


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{_base_url()}/health", timeout=5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001 —— 探活失败即未就绪，无需区分原因
        return False


def _stderr_tail(n: int = _STDERR_TAIL_LINES) -> str:
    """取缓冲尾部 n 行拼成字符串；空缓冲明说"无输出"，别让告警看起来像丢了信息。"""
    lines = list(_recent_stderr)[-n:]
    if not lines:
        return "无输出"
    return "\n".join(lines)


def _drain_stderr(proc: subprocess.Popen) -> None:
    """守护线程体：逐行读子进程 stderr 进环形缓冲。

    线程内绝不 raise——排障线程炸掉只会让 PIPE 无人读，反过来把子进程堵死；
    读异常（含 EOF）静默结束即可。
    """
    stream = proc.stderr
    if stream is None:
        return
    try:
        for raw in stream:
            try:
                _recent_stderr.append(raw.decode("utf-8", "replace").rstrip())
            except Exception:  # noqa: BLE001 —— 单行失败不应终止 drain
                continue
    except Exception:  # noqa: BLE001 —— 读失败静默结束
        pass


def _start_stderr_drain(proc: subprocess.Popen) -> None:
    """起 stderr drain 守护线程（必须 daemon：否则解释器退出时被它挂住）。"""
    global _stderr_thread
    _recent_stderr.clear()   # 别把上一代进程的输出混进来误导归因
    thread = threading.Thread(
        target=_drain_stderr,
        args=(proc,),
        name="llama-server-stderr",
        daemon=True,
    )
    _stderr_thread = thread
    thread.start()


def _join_stderr_drain(timeout: float = 2.0) -> None:
    """有界地等 drain 线程把尾部读进缓冲（子进程已退出 ⇒ stderr EOF ⇒ 线程自然结束）。

    只为让告警拿到完整尾部，绝不因此拖死启动/停机路径。
    """
    thread = _stderr_thread
    if thread is None:
        return
    try:
        thread.join(timeout=timeout)
    except Exception:  # noqa: BLE001 —— join 失败不影响主流程
        pass


def _wait_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok():
            return True
        # 进程已退出则无需再等（早失败，避免白等满超时）
        if _proc is not None and _proc.poll() is not None:
            # 先把 stderr 尾部读全再打（进程已退出 ⇒ drain 线程即将见到 EOF）
            _join_stderr_drain(timeout=1.0)
            logger.warning(
                f"[llama-server] 进程提前退出，code={_proc.returncode}；"
                f"stderr 尾部 {_STDERR_TAIL_LINES} 行:\n{_stderr_tail()}"
            )
            return False
        time.sleep(0.5)
    return False


def start() -> None:
    """确保 llama-server 在跑。幂等：已就绪则直接返回。

    拉起的是 `llama_cpp.command` 生效行（v2：一条完整命令行，原样执行；不写该键
    时用内置缺省行，行为与写死时代逐字一致）。预检（_preflight_check）：布局内
    沿用产物 Fatal（文案首行与 launcher 的 MISSING_ARTIFACTS_HANDLE 逐字一致）；
    布局外只如实报可执行体 / -m 路径缺失。抛 RuntimeError 时由调用方走契约 A
    （.startup_error + sys.exit）——不做静默降级。
    """
    global _proc, _mmproj_loaded
    with _lock:
        if _health_ok():
            # 先判本模块实例死活（审查 P2①）：--mmproj 实例自行崩溃/OOM（未走 stop ⇒
            # 旗标不复位）后，外部纯文本实例占用本行 --port 端口（健康真）时，只看"健康 ∧
            # 旗标"⇒ media_ready() 假真 ⇒ data: URI 被 POST 到纯文本实例（反向静默降级）。
            # _proc 非 None 且已退出 ⇒ 活体是外部的 ⇒ 先复位旗标，再走开关/旗标判定。
            if _proc is not None and _proc.poll() is not None:
                _mmproj_loaded = False
            # 有活体实例（本模块所拉或用户手起）：开关开而实例未以 --mmproj 加载（旗标
            # 非真，含三态里的 None＝未声明）⇒ fail-loud 指向重启处置 —— 绝不静默按文本
            # 模式继续（spec §5.4 🔴）。状态文案区分"未声明"与"明确关闭"（§5'.2）。
            if media_ingest_enabled() and _mmproj_loaded is not True:
                if _mmproj_loaded is None:
                    raise RuntimeError(
                        "media_ingest.enabled 已开，但当前 llama-server 实例未声明多模态"
                        "（行内既无 --mmproj 也无 --no-mmproj）。请在 llama_cpp.command 行里"
                        "写上 --mmproj 或 --no-mmproj（手册要求二者必选其一），重启 Niu 使"
                        " llama-server 带 --mmproj 重新拉起。"
                    )
                raise RuntimeError(
                    "media_ingest.enabled 已开，但当前 llama-server 实例未加载 mmproj"
                    "（旗标为假：实例未以 --mmproj 拉起，或外部实例占用了本行 --port 端口）。"
                    "请按手册停掉现有实例并重启 Niu，使 llama-server 带 --mmproj 重新拉起。"
                )
            logger.info("[llama-server] 已有实例在跑，复用")
            return

        argv = _build_argv()
        if not argv or not argv[0]:
            raise RuntimeError(
                "llama_cpp.command 解析后为空命令行（整行只有引号或空白）；"
                "请在行里写上可执行体与参数，或删掉 command 键改用内置缺省行"
            )
        # 新实例前置检查（spawn 之前），拆成可单测的小函数（F5：首词在布局外时跳过
        # 布局产物 Fatal 与 chmod，只如实检查可执行体 / -m 路径）。
        _preflight_check(argv, _inside_layout(argv[0]) if argv else True)

        # 启动日志逐字打印生效行（不做任何重排；"看得见"的约定见 §5'.2/§18.6 F3）。
        logger.info(f"[llama-server] 启动: {llama_config.command_line()}")
        # T1：日志文件已关（stdout 直接丢弃，stderr 接 PIPE 进有界内存缓冲）。
        # 不建 ~/.niu/logs、不再追加 llama_server.log——verbosity 3 每个 embed
        # 请求打 4+ 行、只增不减且无人看；排障靠 _recent_stderr 环形缓冲。
        _proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            **_popen_flags(),
        )
        _start_stderr_drain(_proc)
        # 新实例旗标显式置位/复位（三态取自生效行，§5'.2：行里有 --mmproj ⇒ True、
        # --no-mmproj ⇒ False、两者都无 ⇒ None 未声明——绝不按 True 处理）。
        # 幂等复用路径不动旗标，只有真正换了实例才更新它。
        _mmproj_loaded = llama_config.mmproj_state()

        # 注册兜底退出钩子（防 lifespan shutdown 未覆盖的退出路径留下孤儿进程）
        atexit.register(stop)

        startup_timeout = llama_config.startup_timeout()
        if not _wait_ready(startup_timeout):
            rc = _proc.poll()
            # 已在 _lock 内：必须调 _stop_locked，不得调外层那个收尾入口——
            # threading.Lock 非重入，锁内再取锁即同线程自死锁（R1-A P1 实证），
            # 会让 preload 永久挂起、.startup_error 不写，契约 A 失效。
            # 注意：本注释内不得出现外层收尾入口的函数名字面量，否则会绊倒 §6 的
            # 源码级回归断言（R2 两角 + PM 自跑均在此栽过，连注释里提到它都不行）。
            _stop_locked()
            raise RuntimeError(
                f"llama-server 未在 {startup_timeout:.0f}s 内就绪"
                f"（进程退出码 {rc}）；stderr 尾部 {_STDERR_TAIL_LINES} 行:\n"
                f"{_stderr_tail()}"
            )
        logger.info(f"[llama-server] 就绪 {_base_url()}")


def _stop_locked() -> None:
    """停止子进程。**调用方必须已持有 `_lock`**（start() 的失败路径在锁内）。"""
    global _proc, _mmproj_loaded
    # 旗标复位必须在**提前返回之前**（审查 B7）：mmproj 实例自行崩溃（未走停止流程）
    # 时若按函数尾部自然写法放复位行，会被 `poll() is not None` 的提前返回跳过 ⇒
    # 旗标残留 True ⇒ 外部纯文本实例占用本行 --port 端口时 media_ready() 假真 ⇒
    # data: URI 会被 POST 到纯文本实例 ⇒ 反向静默降级。
    _mmproj_loaded = False
    proc, _proc = _proc, None
    if proc is None or proc.poll() is not None:
        return
    logger.info("[llama-server] 停止子进程")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning(
                "[llama-server] terminate 超时，升级为 kill；"
                f"stderr 尾部 {_STDERR_TAIL_LINES} 行:\n{_stderr_tail()}"
            )
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:  # noqa: BLE001 —— 收尾失败不得阻断整体停机
        logger.error(f"[llama-server] 停止失败: {e}")
    finally:
        # 进程已退 ⇒ stderr EOF ⇒ drain 线程即将结束；有界等它一下，把尾部读全
        _join_stderr_drain(timeout=2.0)


def stop() -> None:
    """停止由本模块拉起的 llama-server。对齐 IMGateway：terminate→wait→kill。"""
    with _lock:
        _stop_locked()


def is_alive() -> bool:
    return _health_ok()

def media_ready() -> bool:
    """运行实例是否**真实**加载了 mmproj 且健康（T1）。

    旗标为 True ∧ 健康 —— **不是**"开关开 ∧ 健康"（那是静默降级：开关热翻转为开而
    实例是纯文本形态时会漏过，审查 P2）。旗标三态取自生效行（llama_config.mmproj_state()）：
    True（行里有 --mmproj）/ False（--no-mmproj 明确关闭）/ None（未声明）——
    **None 绝不按 True 处理**（§5'.2：自写这一行时二者必须选一个，都没写就是未声明）。
    旗标只在 start() 的新实例路径按生效行置位，复位有三处：_stop_locked 收尾（含自行
    崩溃后经它）、start() 的复用分支发现本模块实例已死时（P2①）、以及新实例路径置位。
    """
    return _mmproj_loaded is True and _health_ok()


def _post_media(data_uri: str) -> list[float]:
    """媒体（`data:` base64 URI）**单次发送**到 /v1/embeddings。

    与 _post_embeddings 同型，但用独立的媒体超时（llama_config.media_http_timeout()，
    缺省 1031.52＝现状量级 8128×0.09+300：大图编码 470–700 s，复用文本超时必然超时
    降级，审查 B8）。**不复用 _encode_one 的文本截断护栏**：data: URI 是数十 MB 的
    base64 长串，护栏会把图片从中间截断 ⇒ 服务端 400 或产出垃圾向量（审查 B6）。

    **为什么必须三层嵌套**（`input[0].content[0].image_url.url`，勿再"简化"）：
    /v1/embeddings 的 `input` 字符串槽是**文本专用**，裸 data: URI 会被当文本分词
    （数十 MB base64 ⇒ 数十万 token ⇒ 超 -ub 8192 ⇒ HTTP 500）；服务端只认三层
    嵌套里的 image_url 形态并在此解析图片，且 **`data:` 前缀必须保留**（与
    gemma2-sandbox mm_test.py 的 img_emb() 同型）。2026-10-13 真机实测：旧形状
    `{"input": [data_uri]}` 500（≈91 万 token 超物理批）；本形状 200/768 维/1.5 s；
    少一层 content（`{"input": [{"type": "image_url", ...}]}`）被服务端 400 拒。

    故障归类：发送段错误（HTTP / 连接 / 超时 / JSON 与响应字节解码 / 传输中断）与响应结构异常（条数不符 / 畸形结构：
    ``data`` 非 dict、条目缺 ``embedding`` 键、类型不符）一律包一层抛 ``MediaEncodeBackendError``
    （**唯一可重试的失败类别**）；未就绪在 encode 入口判定，同样抛它。
    """
    payload = json.dumps(
        {
            "input": [
                {"content": [{"type": "image_url", "image_url": {"url": data_uri}}]}
            ],
            "encoding_format": "float",
        }
    ).encode()
    req = urllib.request.Request(
        f"{_base_url()}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=llama_config.media_http_timeout()) as resp:
            data = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError, http.client.HTTPException, OSError,
            UnicodeDecodeError) as err:
        # 媒体编码后端故障（HTTP / 连接 / 超时 / 响应解析与字节解码 / 传输中断）——唯一可重试的失败类别：
        # 保留原始错误信息、包一层抛（工作者端据此区分「1/3 重试→弃单」与「一次处置」）。
        raise MediaEncodeBackendError(f"llama-server 媒体编码失败（{type(err).__name__}）: {err}") from err
    try:
        items = data.get("data") or []
        if len(items) != 1:
            # 响应结构异常（后端故障类别）：条数不符照抛，同样包一层。
            raise MediaEncodeBackendError(f"llama-server 返回条数不符：期望 1，实得 {len(items)}")
        return list(map(float, items[0]["embedding"]))
    except (AttributeError, KeyError, TypeError, IndexError,
            ValueError, OverflowError) as err:
        # 畸形响应（``data`` 非 dict ⇒ AttributeError；条目缺 embedding 键 ⇒ KeyError；
        # 类型不符 ⇒ TypeError/IndexError；embedding 含不可转 float 的值 ⇒ ValueError/OverflowError）
        # 同属后端故障类别：不穿透成裸异常，否则工作者会误判「非后端」而一次处置，
        # 吃掉本可用于响应结构异常的重试预算。
        raise MediaEncodeBackendError(f"llama-server 响应结构异常: {type(err).__name__}: {err}") from err


def encode_media(abs_path: str) -> list[float]:
    """单张图片 → 768 维向量（与文本同一空间，C1/C8）。

    前置检查**先于**任何发送：健康检查失败或实例未加载 mmproj（旗标非真，含三态里
    的 None＝未声明）⇒ 抛错，绝不静默按文本模式继续（子情形 d：开关热翻转为开而
    运行实例是纯文本形态）。
    图片按落盘文件的**真实扩展名**取 MIME 前缀（审查 A7：.jpg/.jpeg → image/jpeg、
    .png → image/png，其余按 mimetypes 推断，不得一律写死 image/jpeg）。
    发送走 _encode_lock 串行化：llama-server 单实例会把并发请求内部排队，媒体大图
    编码期间文本编码排队并可能超时 —— 该二阶伤害在手册披露，本锁是既有串行约定。
    """
    if not _health_ok():
        raise MediaEncodeBackendError("llama-server 未就绪（health 检查失败）")
    # 本守卫只查旗标、**不查开关**（调用方只在需要媒体时才会调用本函数），
    # 措辞因此不得宣称"开关已开"——开关状态与本守卫无关。
    if _mmproj_loaded is None:
        raise RuntimeError(
            "当前 llama-server 实例未声明多模态（行内既无 --mmproj 也无 --no-mmproj），"
            "无法编码媒体。请在 llama_cpp.command 行里写上 --mmproj 后重启 llama-server。"
        )
    if _mmproj_loaded is not True:
        raise RuntimeError(
            "当前 llama-server 实例未加载 mmproj（纯文本形态或产物缺失），"
            "无法编码媒体。请按手册重启 llama-server 使其带 --mmproj 拉起。"
        )
    path = Path(abs_path)
    if not path.is_file():
        raise RuntimeError(f"图片文件不存在: {abs_path}")
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    uri = f"data:{mime};base64,{b64}"
    with _encode_lock:
        return _post_media(uri)


def encode_media_bytes(data: bytes, mime: str = "image/jpeg") -> list[float]:
    """内存字节 → 768 维向量（与文本同一空间）。客户端缩放路径（HANDOVER D 机制）经此
    发送缩放后的 JPEG；前置检查与 `encode_media` 同型（健康 + mmproj 旗标，先于任何
    发送），发送同走 `_encode_lock` 串行 + `_post_media` 的三层嵌套形态。"""
    if not _health_ok():
        raise MediaEncodeBackendError("llama-server 未就绪（health 检查失败）")
    if _mmproj_loaded is None:
        raise RuntimeError(
            "当前 llama-server 实例未声明多模态（行内既无 --mmproj 也无 --no-mmproj），"
            "无法编码媒体。请在 llama_cpp.command 行里写上 --mmproj 后重启 llama-server。"
        )
    if _mmproj_loaded is not True:
        raise RuntimeError(
            "当前 llama-server 实例未加载 mmproj（纯文本形态或产物缺失），"
            "无法编码媒体。请按手册重启 llama-server 使其带 --mmproj 拉起。"
        )
    uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    with _encode_lock:
        return _post_media(uri)


def _post_embeddings(texts: list[str]) -> list[list[float]]:
    """打一次 /v1/embeddings，返回与输入等长的向量列表（条数不符照旧抛）。"""
    payload = json.dumps({"input": texts, "encoding_format": "float"}).encode()
    req = urllib.request.Request(
        f"{_base_url()}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=llama_config.http_timeout()) as resp:
        data = json.loads(resp.read())
    items = data.get("data") or []
    if len(items) != len(texts):
        raise RuntimeError(
            f"llama-server 返回条数不符：期望 {len(texts)}，实得 {len(items)}"
        )
    return [list(map(float, item["embedding"])) for item in items]


def _http_error_body(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 —— 读不到报文就当空，主通道自然判否
        return ""


def _parse_token_count(body: str) -> int | None:
    """主通道：从 llama.cpp 的 500 报文解析 `input (N tokens)` 得真实 N。"""
    m = _TOKEN_COUNT_RE.search(body)
    return int(m.group(1)) if m else None


def _tokenize_count(text: str) -> int | None:
    """备用通道：用 /tokenize 量真实 token 数（生产实例已在用的接口）。

    它返回 JSON 的 `tokens` 数组，长度即 token 数。量不到返回 None——不猜。
    """
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        f"{_base_url()}/tokenize",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=llama_config.http_timeout()) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 —— 量不到就说量不到，交由调用方照旧抛
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    return len(tokens) if isinstance(tokens, list) else None


def _over_limit_tokens(err: urllib.error.HTTPError, texts: list[str]) -> int | None:
    """双通道判定"这批里有人超限"。

    主通道：报文里的 `input (N tokens)` —— llama.cpp 只在这条输入处理不了时才会
    报这个形状，且带的就是该输入的真实 token 数 N ⇒ 判超限、拿真实 N。
    不押"is too large to process"那句英文（文案改坏时此正则仍然命中）。
    备用通道：报文里连 N 都取不到时，只对超过服务器侧字符预筛阈值
    （llama_config.char_prefilter_server()＝上限×7000/8192，缺省恰 7000）的文本
    调 /tokenize 前置量 token 再判（生产实例已在用的接口，它返回 tokens 数组长度）。

    返回真实 N；判不出超限（含其它 500 成因）返回 None，调用方必须原样 raise——
    不得把真故障吞成静默降级。
    """
    n = _parse_token_count(_http_error_body(err))
    if n is not None:
        return n
    limit = llama_config.embed_token_limit()
    prefilter = llama_config.char_prefilter_server()
    for text in texts:
        if len(text) <= prefilter:
            continue
        measured = _tokenize_count(text)
        if measured is not None and measured > limit:
            return measured
    return None


def _encode_one(text: str) -> list[float]:
    """单条编码 + 超限截断护栏，返回一条 768 维向量。

    截断是**有损**的（skill 整文件一个块，截断=只索引前半），但现状是"整批回滚、
    一个都不入库"，故截断严格更优；彻底解法（长 skill 分块注入）另立方案。

    截断按比例一步算 `len//(N/limit + 1)`，不盲猜折半；达到
    MAX_TRUNCATE_ATTEMPTS 仍不通过即 raise RuntimeError，不得静默跳过。
    """
    # 上限与字符预筛现读自 llama_config（生效行 -c 派生，缺省 8192/7000 零漂移）
    limit = llama_config.embed_token_limit()
    prefilter = llama_config.char_prefilter_server()
    target = text
    original_len = len(text)
    attempts = 0
    while True:
        try:
            return _post_embeddings([target])[0]
        except urllib.error.HTTPError as err:
            n = _parse_token_count(_http_error_body(err))
            if n is None:
                # 报文里连 N 都取不到：只对仍长的文本用 /tokenize 前置量一次；
                # 量不到、或没超过我们声明的上限，就原样抛——不猜、不吞。
                n = _tokenize_count(target) if len(target) > prefilter else None
                if n is None or n <= limit:
                    raise
            attempts += 1
            if attempts >= MAX_TRUNCATE_ATTEMPTS:
                raise RuntimeError(
                    f"llama-server 单条输入超限，截断 {attempts} 次后仍不通过"
                    f"（真实 token 数 {n}，上限 {limit}，原长度 {original_len}）"
                ) from err
            # 按比例一步算（等价于 len//(N/limit + 1)，整数运算免 float 下标）
            new_len = max(1, len(target) * limit // (n + limit))
            if new_len >= len(target):   # 保底：必须严格变短，否则死循环
                new_len = max(1, len(target) - 1)
            logger.warning(
                f"[llama-server] 单条输入被服务端拒绝（报文真实 token 数 {n}，"
                f"上限 {limit}），第 {attempts}/{MAX_TRUNCATE_ATTEMPTS} 次截断："
                f"新长度 {new_len} ← 原长度 {len(target)}（本条原始长度 {original_len}）"
            )
            target = target[:new_len]


def encode_batch(texts: list[str]) -> list[list[float]]:
    """批量编码。返回 list[list[float]]（长度 = 输入条数，每条 768 维）。

    任何失败都向上抛——调用方（embedding 适配器）会把它变成既有降级行为，
    此处不吞异常、不返回空列表（空列表会被当成"编码成功但无结果"）。

    超限护栏（S7-T2）：单条超上限 token 会让 llama-server **整批** 500，原先整批
    上抛 ⇒ 整条 LightRAG 注入回滚、下一轮重试、且无可见报错。现在：整批失败且判为
    超限时先拆批逐条重试（把没问题的条目先救回来），对仍超限的那条按比例截断重试；
    判不出超限的错误一律原样 raise。截断重试有上界（MAX_TRUNCATE_ATTEMPTS），
    返回条数始终与输入一致。
    """
    if not texts:
        return []
    if not _health_ok():
        raise RuntimeError("llama-server 未就绪（health 检查失败）")
    # 串行化：单实例 llama-server 会把并发请求内部排队，客户端并发只会把单请求延迟
    # 顶到 HTTP 超时（实测 8 批并发 ≈119.9s vs 超时 120.0s，必现 timed out）。
    # 拆批/量 token/截断重试全在锁内；护栏重试有上界，不会无限阻塞调用方。
    _begin_text_encode()
    try:
        with _encode_lock:
            try:
                result = _post_embeddings(texts)
            except urllib.error.HTTPError as err:
                if _over_limit_tokens(err, texts) is None:
                    raise
                # 护栏：先拆批逐条救回（避免一条拖垮全批），仍超限的那条在 _encode_one 内截断
                result = [_encode_one(text) for text in texts]
    finally:
        _end_text_encode()
    _mark_text_encode()   # 仅成功路径打点（失败/异常不打点，保守）
    return result
