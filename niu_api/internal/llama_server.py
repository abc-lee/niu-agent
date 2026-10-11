"""llama.cpp `llama-server` 子进程生命周期 + HTTP 编码客户端（S2）。

设计对齐 niu_api 内唯一长驻子进程范式 IMGateway（`niu_api/channel/gateway.py`）：
Popen 拉起、看门狗探活、terminate→wait→kill 收尾、仅监听 127.0.0.1。
不引入第二套子进程约定。

本模块只负责「进程活着 + 能把文本换成向量」；模型产物的路径真相源在
`niu_api.internal.embedding_artifacts`（S1），此处只消费不重定义。
"""

from __future__ import annotations

import atexit
import collections
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from loguru import logger

from niu_api.internal.embedding_artifacts import (
    describe_missing,
    gguf_path,
    llama_needs_exec_bit,
    llama_server_path,
)

# 端口避开既有：uvicorn 9876、IM adapter 19877
LLAMA_PORT = 19878
_HOST = "127.0.0.1"

# 保守上下文：GGUF 元数据 context_length=262144 与 model card 的 8192 差 32 倍，
# 未做长文本质量验证前取 8192。
#
# -b / -ub 必须同时给够：llama-server 单条输入的 token 上限 = min(逻辑批 -b, 物理批 -ub)，
# 二者任一不足都会让超限输入直接 500（报文：input (N tokens) is too large to process.
# increase the physical batch size (current batch size: X)，其中 X 报的是不足的那一侧）。
# 历史坑（2026-10-11 实测）：原先只写 -ub 2048、-b 取默认值 2048 ⇒ 实际上限 2048，
# 而 Niu 对 LightRAG 声明的 max_seq_length 是 8192 —— 任何 >2048 token 的文本（中文长文
# 尤甚）编码必 500 → 整条 LightRAG 注入回滚 → SkillSync 每分钟重试且永不成功
# （miaodong 2118 tok / scheduled-tasks 2087 tok 两个 skill 卡死即此因）。
# 实测对照：-ub 8192 单独改无效；-b 8192 单独改无效；-b 8192 -ub 8192 方可通过。
_CTX_SIZE = 8192
_BATCH = 8192
_UBATCH = 8192

_STARTUP_TIMEOUT = 60.0   # 冷启动读取 310MB 权重的预算
_HTTP_TIMEOUT = 120.0

# ── S7-T2：单条输入 token 上限 + 超限护栏参数 ───────────────────────────
# 上限来源即上面的 -b/-ub 8192：llama-server 单条输入的 token 上限 = min(-b, -ub)。
_TOKEN_LIMIT = 8192
# 截断重试上界：覆盖 log2(任意实际输入/8192) 的收敛需要。用尽即 raise
# （报文带真实 token 数、已试次数、原文长度）——不得静默跳过，
# 跳过即把"整批回滚"换成"少一条向量的无声失败"。
MAX_TRUNCATE_ATTEMPTS = 6
# 保守字符预筛阈值：报文解析失败时，只对超过该长度的文本调 /tokenize 前置量 token
# （短文本不可能超 8192 token，量了只是白打一次服务端）。
_CHAR_PREFILTER = 7000
# 主通道：llama.cpp 的 500 报文形如
#   input (2250 tokens) is too large to process. increase the physical batch size ...
# 只从这句里取真实 N，不把"这条超限"的判定押在整句英文文案上。
_TOKEN_COUNT_RE = re.compile(r"input\s*\((\d+)\s*tokens?\)", re.IGNORECASE)

# 编码串行化锁（与下面的 _lock 分工：_lock 管进程生命周期，本锁管 HTTP 编码请求）。
# 根因：llama-server 是单实例推理服务，会把并发请求在内部串行化；客户端并发发 N 批，
# 每批的单请求延迟就被顶到约 N × 单批耗时 —— PM 实测 8 批并发总耗时 119.9s，
# 恰好压在 _HTTP_TIMEOUT=120.0s 边界上，导致 repair 的 vdb_chunks 重建必现 timed out。
# 串行化后单批约 21.4s（235 条真实 chunk 实测），既不超时也不让服务端空转排队。
_encode_lock = threading.Lock()

_lock = threading.Lock()
_proc: subprocess.Popen | None = None

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
    return f"http://{_HOST}:{LLAMA_PORT}"


def _popen_flags() -> dict:
    """平台相关 Popen 参数（对齐 IMGateway：Windows 隐忍窗口）。"""
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": 0x08000000}
    return {}


def _build_argv() -> list[str]:
    argv = [
        str(llama_server_path()),
        "-m", str(gguf_path()),
        "--embedding",
        "--port", str(LLAMA_PORT),
        "--host", _HOST,
        "-c", str(_CTX_SIZE),
        "-b", str(_BATCH),
        "-ub", str(_UBATCH),
    ]
    return argv


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

    产物缺失时抛 RuntimeError（文案来自 describe_missing，指名具体文件），
    由调用方走契约 A（.startup_error + sys.exit）——不做静默降级。
    """
    global _proc
    with _lock:
        if _health_ok():
            logger.info("[llama-server] 已有实例在跑，复用")
            return

        missing = describe_missing()
        if missing:
            raise RuntimeError(missing)

        server = llama_server_path()
        if llama_needs_exec_bit() and not (server.stat().st_mode & 0o111):
            # 没跑铁律 7 的 chmod 修复时不静默失败，自己补一次
            logger.warning("[llama-server] 二进制缺可执行位，自动补 chmod +x")
            server.chmod(server.stat().st_mode | 0o111)

        logger.info(f"[llama-server] 启动: {' '.join(_build_argv())}")
        # T1：日志文件已关（stdout 直接丢弃，stderr 接 PIPE 进有界内存缓冲）。
        # 不建 ~/.niu/logs、不再追加 llama_server.log——verbosity 3 每个 embed
        # 请求打 4+ 行、只增不减且无人看；排障靠 _recent_stderr 环形缓冲。
        _proc = subprocess.Popen(
            _build_argv(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            **_popen_flags(),
        )
        _start_stderr_drain(_proc)

        # 注册兜底退出钩子（防 lifespan shutdown 未覆盖的退出路径留下孤儿进程）
        atexit.register(stop)

        if not _wait_ready(_STARTUP_TIMEOUT):
            rc = _proc.poll()
            # 已在 _lock 内：必须调 _stop_locked，不得调外层那个收尾入口——
            # threading.Lock 非重入，锁内再取锁即同线程自死锁（R1-A P1 实证），
            # 会让 preload 永久挂起、.startup_error 不写，契约 A 失效。
            # 注意：本注释内不得出现外层收尾入口的函数名字面量，否则会绊倒 §6 的
            # 源码级回归断言（R2 两角 + PM 自跑均在此栽过，连注释里提到它都不行）。
            _stop_locked()
            raise RuntimeError(
                f"llama-server 未在 {_STARTUP_TIMEOUT:.0f}s 内就绪"
                f"（进程退出码 {rc}）；stderr 尾部 {_STDERR_TAIL_LINES} 行:\n"
                f"{_stderr_tail()}"
            )
        logger.info(f"[llama-server] 就绪 {_base_url()}")


def _stop_locked() -> None:
    """停止子进程。**调用方必须已持有 `_lock`**（start() 的失败路径在锁内）。"""
    global _proc
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


def _post_embeddings(texts: list[str]) -> list[list[float]]:
    """打一次 /v1/embeddings，返回与输入等长的向量列表（条数不符照旧抛）。"""
    payload = json.dumps({"input": texts, "encoding_format": "float"}).encode()
    req = urllib.request.Request(
        f"{_base_url()}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
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
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
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
    备用通道：报文里连 N 都取不到时，只对超过 _CHAR_PREFILTER 字符的文本调
    /tokenize 前置量 token 再判（生产实例已在用的接口，它返回 tokens 数组长度）。

    返回真实 N；判不出超限（含其它 500 成因）返回 None，调用方必须原样 raise——
    不得把真故障吞成静默降级。
    """
    n = _parse_token_count(_http_error_body(err))
    if n is not None:
        return n
    for text in texts:
        if len(text) <= _CHAR_PREFILTER:
            continue
        measured = _tokenize_count(text)
        if measured is not None and measured > _TOKEN_LIMIT:
            return measured
    return None


def _encode_one(text: str) -> list[float]:
    """单条编码 + 超限截断护栏，返回一条 768 维向量。

    截断是**有损**的（skill 整文件一个块，截断=只索引前半），但现状是"整批回滚、
    一个都不入库"，故截断严格更优；彻底解法（长 skill 分块注入）另立方案。

    截断按比例一步算 `len//(N/limit + 1)`，不盲猜折半；达到
    MAX_TRUNCATE_ATTEMPTS 仍不通过即 raise RuntimeError，不得静默跳过。
    """
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
                n = _tokenize_count(target) if len(target) > _CHAR_PREFILTER else None
                if n is None or n <= _TOKEN_LIMIT:
                    raise
            attempts += 1
            if attempts >= MAX_TRUNCATE_ATTEMPTS:
                raise RuntimeError(
                    f"llama-server 单条输入超限，截断 {attempts} 次后仍不通过"
                    f"（真实 token 数 {n}，上限 {_TOKEN_LIMIT}，原长度 {original_len}）"
                ) from err
            # 按比例一步算（等价于 len//(N/limit + 1)，整数运算免 float 下标）
            new_len = max(1, len(target) * _TOKEN_LIMIT // (n + _TOKEN_LIMIT))
            if new_len >= len(target):   # 保底：必须严格变短，否则死循环
                new_len = max(1, len(target) - 1)
            logger.warning(
                f"[llama-server] 单条输入被服务端拒绝（报文真实 token 数 {n}，"
                f"上限 {_TOKEN_LIMIT}），第 {attempts}/{MAX_TRUNCATE_ATTEMPTS} 次截断："
                f"新长度 {new_len} ← 原长度 {len(target)}（本条原始长度 {original_len}）"
            )
            target = target[:new_len]


def encode_batch(texts: list[str]) -> list[list[float]]:
    """批量编码。返回 list[list[float]]（长度 = 输入条数，每条 768 维）。

    任何失败都向上抛——调用方（embedding 适配器）会把它变成既有降级行为，
    此处不吞异常、不返回空列表（空列表会被当成"编码成功但无结果"）。

    超限护栏（S7-T2）：单条 > 8192 token 会让 llama-server **整批** 500，原先整批
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
    with _encode_lock:
        try:
            return _post_embeddings(texts)
        except urllib.error.HTTPError as err:
            if _over_limit_tokens(err, texts) is None:
                raise
        # 护栏：先拆批逐条救回（避免一条拖垮全批），仍超限的那条在 _encode_one 内截断
        return [_encode_one(text) for text in texts]
