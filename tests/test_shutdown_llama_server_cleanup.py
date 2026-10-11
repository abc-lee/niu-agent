"""S6：/api/shutdown 显式停 llama-server（T1 主路径）的行为验证。

背景：兜底退出线程的 os._exit(0) 会跳过 lifespan shutdown / atexit / finally，
llama-server 子进程因此成孤儿、端口 19878 不释放（plan §1）。修法是在兜底线程
启动之前显式调 llama_server.stop()。

四条测试全部 mock，禁真实进程 / 禁真实 LLM：
  1. handler 调用了 stop（I-2 锚：不依赖 atexit）
  2. 顺序不变量（I-1）：stop 的记录早于 _start_force_exit_fallback 的记录
  3. stop 抛异常时 handler 仍返回 {"status": "shutting down"} 且不 raise（I-6）
  4. stop 抛异常时兜底仍必须启动（I-6 另一半）

致命坑（R2-B P2 实测，绝对不要省）：
  - 四条都必须 patch `niu_api.compat._start_force_exit_fallback`，否则真实 daemon
    线程 3 秒后 os._exit(0) 会杀掉 pytest 进程，且退出码 0 → CI 读作成功。
  - 绝对禁止 patch `threading.Thread`（或 compat.threading.Thread）：asyncio.to_thread
    底层就是 ThreadPoolExecutor/threading.Thread，进程级替换会让 await 永久挂死。
  - 同时 patch `niu_api.internal.lightrag_manager.shutdown_pending_futures`，
    避免 handler 首句真的去等 LightRAG。
"""
from unittest.mock import patch

from niu_api.compat import shutdown

_PENDING_FUTURES_PATCH = "niu_api.internal.lightrag_manager.shutdown_pending_futures"
_FALLBACK_PATCH = "niu_api.compat._start_force_exit_fallback"


async def test_shutdown_calls_llama_server_stop():
    """① handler 必须显式调停 llama-server（patch 的是源头模块，非 compat 属性）。"""
    with patch(_PENDING_FUTURES_PATCH), \
            patch(_FALLBACK_PATCH) as fallback, \
            patch("niu_api.internal.llama_server.stop") as stop:
        result = await shutdown()

    assert result == {"status": "shutting down"}
    assert stop.call_count == 1, "handler 必须显式调 llama_server.stop()（I-2：不得只靠 atexit）"
    assert fallback.call_count == 1, "兜底线程仍须正常启动"


async def test_shutdown_stop_before_force_exit_fallback():
    """② 顺序不变量（I-1）：stop 的记录必须早于兜底启动的记录。

    共享一个调用序列 list：stop mock 在 worker 线程（asyncio.to_thread 内）追加，
    兜底 mock 在 event loop 线程追加；顺序由 handler 的代码位置保证，不观测
    threading.Thread。
    """
    calls = []

    def _record_stop():
        calls.append("stop")

    def _record_fallback():
        calls.append("fallback")

    with patch(_PENDING_FUTURES_PATCH), \
            patch(_FALLBACK_PATCH, side_effect=_record_fallback), \
            patch("niu_api.internal.llama_server.stop", side_effect=_record_stop):
        await shutdown()

    assert calls == ["stop", "fallback"], (
        "stop 必须早于 _start_force_exit_fallback()（I-1）——"
        f"os._exit 之前不显式停 llama-server 会泄漏端口 19878，实际序列 {calls}"
    )


async def test_shutdown_stop_failure_still_returns_shutting_down():
    """③ stop 抛异常不得阻断关机：仍返回契约响应且不 raise（I-6，否则 HTTP 变 5xx）。"""
    with patch(_PENDING_FUTURES_PATCH), \
            patch(_FALLBACK_PATCH) as fallback, \
            patch("niu_api.internal.llama_server.stop", side_effect=RuntimeError("boom")):
        result = await shutdown()

    assert result == {"status": "shutting down"}
    assert fallback.call_count == 1, "stop 失败时兜底线程仍必须启动"


async def test_shutdown_fallback_still_started_when_stop_fails():
    """④ stop 抛异常时兜底仍必须启动（I-6 另一半：否则 Python 永不退出）。"""
    with patch(_PENDING_FUTURES_PATCH), \
            patch(_FALLBACK_PATCH) as fallback, \
            patch("niu_api.internal.llama_server.stop", side_effect=OSError("kill failed")):
        result = await shutdown()

    assert result == {"status": "shutting down"}
    fallback.assert_called_once_with()
