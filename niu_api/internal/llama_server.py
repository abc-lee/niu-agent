"""llama.cpp `llama-server` 子进程生命周期 + HTTP 编码客户端（S2）。

设计对齐 niu_api 内唯一长驻子进程范式 IMGateway（`niu_api/channel/gateway.py`）：
Popen 拉起、看门狗探活、terminate→wait→kill 收尾、仅监听 127.0.0.1。
不引入第二套子进程约定。

本模块只负责「进程活着 + 能把文本换成向量」；模型产物的路径真相源在
`niu_api.internal.embedding_artifacts`（S1），此处只消费不重定义。
"""

from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

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
# 未做长文本质量验证前取 8192。-ub 2048 为实测可用值（512 会触发
# cap image_max_tokens 告警，虽属 Phase 2 但同一进程）。
_CTX_SIZE = 8192
_UBATCH = 2048

_STARTUP_TIMEOUT = 60.0   # 冷启动读取 310MB 权重的预算
_HTTP_TIMEOUT = 120.0

_lock = threading.Lock()
_proc: subprocess.Popen | None = None


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
        "-ub", str(_UBATCH),
    ]
    return argv


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{_base_url()}/health", timeout=5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001 —— 探活失败即未就绪，无需区分原因
        return False


def _wait_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok():
            return True
        # 进程已退出则无需再等（早失败，避免白等满超时）
        if _proc is not None and _proc.poll() is not None:
            logger.error(f"[llama-server] 进程提前退出，code={_proc.returncode}")
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

        log_dir = Path.home() / ".niu" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "llama_server.log"

        logger.info(f"[llama-server] 启动: {' '.join(_build_argv())}")
        with open(log_path, "ab") as log_fh:
            _proc = subprocess.Popen(
                _build_argv(),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **_popen_flags(),
            )

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
                f"（进程退出码 {rc}）；日志见 {log_path}"
            )
        logger.info(f"[llama-server] 就绪 {_base_url()}（日志 {log_path}）")


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
            logger.warning("[llama-server] terminate 超时，升级为 kill")
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:  # noqa: BLE001 —— 收尾失败不得阻断整体停机
        logger.error(f"[llama-server] 停止失败: {e}")


def stop() -> None:
    """停止由本模块拉起的 llama-server。对齐 IMGateway：terminate→wait→kill。"""
    with _lock:
        _stop_locked()


def is_alive() -> bool:
    return _health_ok()


def encode_batch(texts: list[str]) -> list[list[float]]:
    """批量编码。返回 list[list[float]]（长度 = 输入条数，每条 768 维）。

    任何失败都向上抛——调用方（embedding 适配器）会把它变成既有降级行为，
    此处不吞异常、不返回空列表（空列表会被当成"编码成功但无结果"）。
    """
    if not texts:
        return []
    if not _health_ok():
        raise RuntimeError("llama-server 未就绪（health 检查失败）")
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
