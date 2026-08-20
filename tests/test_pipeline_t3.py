"""T3 整理管道队列测试：nap 投递 + _execute_force_pipeline 提取 + 入口 8（转换块留回调）。

设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.1 入口 8/9 + §3.0 None 窗口 + §5/§6 T3。
全 mock：runner._execute_force_pipeline（不真实调压缩管道）+ 转换块 DB 重载（_sync_get_messages 假消息）——
禁真实 LLM、禁图谱写入、messages.db 零新增（转换块孤立 tool 清理路径用无 tool_calls 假消息绕过）。
"""
import asyncio
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

import niu_api.chat as chat_module
import niu_api.compat as compat
from agent import runner as runner_module
from agent.runner import NiuRunner
from niu_api.compat import start_pipeline_queue, stop_pipeline_queue


class _FakeDbMsg:
    """模拟 DB Message 对象（转换块用 getattr 访问 role/content/tool_calls/tool_call_id/id）。"""

    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []
        self.tool_call_id = tool_call_id


def _make_runner():
    """NiuRunner.__new__ 实例，仅赋值测试所需属性（禁真实 __init__/LLM/DB）。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner._nap_running = threading.Event()
    runner._last_ema_user_id = ""
    runner._ema_lock = threading.Lock()
    runner.llm_config = {}
    runner.default_model = ""
    runner._assemble_system_message = lambda *a, **k: None  # 转换块 system 重建 mock（不真组装）
    return runner


def _patch_nap_trigger(monkeypatch, runner):
    """让 _maybe_trigger_nap 走到投递点：空游标 + 1 条 user 消息 + 阈值 0。"""
    runner._read_cursor_locked = lambda *a, **k: ""
    runner._sync_get_messages = lambda: [_FakeDbMsg("u1", "user", "hi")]
    monkeypatch.setattr(runner_module, "_ema_marker_step", lambda *a, **k: ("skip", ""))
    monkeypatch.setattr(runner_module, "_calc_dream_trigger_threshold_dynamic", lambda *a, **k: 0)
    from agent import subagent as subagent_module
    monkeypatch.setattr(subagent_module, "_read_context_window_tokens", lambda: 10000)


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列/去重表/精灵状态（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()
    compat._active_compress_futs.clear()
    compat._SPIRIT_STATE = "idle"
    yield
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()


# ---------------------------------------------------------------------------
# 入口 9：nap 投递（§3.1）
# ---------------------------------------------------------------------------

async def test_nap_dispatch_sets_flag_and_suppresses_retrigger(monkeypatch):
    """投递成功 → _nap_running 置位（投递前语义）；重触发被抑制；worker 完成后 finally 清除。"""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(chat_module, "_main_loop", loop)
    start_pipeline_queue()

    runner = _make_runner()
    entered = threading.Event()
    release = threading.Event()

    def fake_run_nap():
        try:
            entered.set()
            release.wait(5.0)
        finally:
            runner._nap_running.clear()  # 与真实 _run_nap_background（L1505）同语义：自身 finally 恒 clear

    runner._run_nap_background = fake_run_nap
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: runner)
    _patch_nap_trigger(monkeypatch, runner)

    runner._maybe_trigger_nap()
    assert runner._nap_running.is_set()  # 置位 = 投递前（§3.1 入口 9）

    # worker 已开始执行 nap（进入 _run_nap_background）
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 1.0), timeout=2.0)

    # 重触发被 _nap_running 抑制（不重复投递）
    runner._maybe_trigger_nap()
    assert runner._nap_running.is_set()  # 仍在运行（第二次调用未清除/未重启）

    # 释放 → _run_nap_background 自身 finally 清除（P2-1：worker 成功路径不再重复 clear）
    release.set()
    deadline = time.monotonic() + 2.0
    while runner._nap_running.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not runner._nap_running.is_set()


async def test_nap_none_window_sync_fallback(monkeypatch):
    """None 窗口（队列未创建）：同步执行 _run_nap_background 兜底，完成后 _nap_running 清除（§3.0 Option A）。"""
    runner = _make_runner()
    calls = []

    def fake_run_nap():
        calls.append(1)

    runner._run_nap_background = fake_run_nap
    _patch_nap_trigger(monkeypatch, runner)
    assert compat._pipeline_queue is None

    runner._maybe_trigger_nap()

    assert calls == [1]  # 同步执行完成——调用方返回前已执行完
    assert not runner._nap_running.is_set()  # 同步路径 finally 清除


async def test_nap_loop_unavailable_sync_fallback(monkeypatch):
    """队列已建但主 loop 不可用（_main_loop None）：投递不可用 → 同步兜底 + _nap_running 清除。"""
    runner = _make_runner()
    calls = []

    def fake_run_nap():
        calls.append(1)

    runner._run_nap_background = fake_run_nap
    _patch_nap_trigger(monkeypatch, runner)
    start_pipeline_queue()  # 队列存在
    monkeypatch.setattr(chat_module, "_main_loop", None)  # 但 loop 不可用

    runner._maybe_trigger_nap()

    assert calls == [1]
    assert not runner._nap_running.is_set()


# ---------------------------------------------------------------------------
# 入口 8：runner-force 投递 + 转换块（§3.1 / §3.0 / §7.12）
# ---------------------------------------------------------------------------

async def test_entry8_dispatch_waits_worker_then_conversion_block(monkeypatch):
    """投递 runner-force → 回调等待 worker 完成 → 转换块输出 dict 契约（role/content 键 + system 保留）。"""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(chat_module, "_main_loop", loop)
    start_pipeline_queue()

    runner = _make_runner()
    pipeline_called = threading.Event()

    def fake_execute():
        pipeline_called.set()
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: runner)
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("s1", "system", "system prompt"),
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi there"),
    ]

    messages = [{"role": "system", "content": "system prompt"}]
    # 回调阻塞 fut.result(timeout)——必须在独立线程调用（worker 与回调共享事件循环）
    await asyncio.wait_for(
        asyncio.to_thread(runner._on_context_high_usage, messages, 180000, 200000),
        timeout=3.0,
    )

    assert pipeline_called.is_set()  # 队列 worker 真实执行了压缩管道（mock 版，未真实调 runner 压缩）
    # 转换块输出格式断言（§6 T3）：每条 dict + role/content 键 + system 保留在 messages[0]
    assert messages, "转换块应回写消息"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


async def test_entry8_timeout_conversion_block_still_runs(monkeypatch):
    """超时路径：fut.result(timeout=300) 抛 TimeoutError → 转换块仍执行，输出格式正确（§7.12）。"""
    runner = _make_runner()
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi"),
    ]

    seen_timeout = {}

    class _TimedOutFut:
        def result(self, timeout=None):
            seen_timeout["timeout"] = timeout  # 300s 参数化注入断言
            raise FutureTimeoutError("simulated 300s queue wait")

    def fake_dispatch(kind, request=None, held=False):
        return _TimedOutFut()

    monkeypatch.setattr(runner, "_dispatch_to_pipeline", fake_dispatch)

    def _must_not_sync():
        raise AssertionError("超时路径不应同步执行 _execute_force_pipeline")

    runner._execute_force_pipeline = _must_not_sync
    messages = [{"role": "system", "content": "sys"}]
    runner._on_context_high_usage(messages, 180000, 200000)

    assert seen_timeout["timeout"] == 300  # 300s 上限参数化注入
    assert messages, "超时后转换块仍应回写消息"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


async def test_entry8_future_exception_conversion_block_still_runs(monkeypatch):
    """非超时异常路径：fut.result(timeout=300) 抛 RuntimeError（shutdown）→ 转换块仍执行（P3-5）。"""
    runner = _make_runner()
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi"),
    ]

    seen_timeout = {}

    class _FailedFut:
        def result(self, timeout=None):
            seen_timeout["timeout"] = timeout  # 300s 参数化注入断言
            raise RuntimeError("shutting down")  # 非 TimeoutError（stop_pipeline_queue 置入）

    monkeypatch.setattr(runner, "_dispatch_to_pipeline", lambda kind, request=None, held=False: _FailedFut())

    def _must_not_sync():
        raise AssertionError("异常路径不应同步执行 _execute_force_pipeline")

    runner._execute_force_pipeline = _must_not_sync
    messages = [{"role": "system", "content": "sys"}]
    runner._on_context_high_usage(messages, 180000, 200000)

    assert seen_timeout["timeout"] == 300  # 300s 上限参数化注入
    assert messages, "非超时异常后转换块仍应回写消息"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


async def test_entry8_none_window_sync_execution(monkeypatch):
    """None 窗口（队列未创建）：同步执行 _execute_force_pipeline，调用方等执行完成（§3.0 Option A）。"""
    runner = _make_runner()
    calls = []

    def fake_execute():
        calls.append(1)
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: [_FakeDbMsg("m1", "user", "hello")]
    assert compat._pipeline_queue is None

    messages = [{"role": "system", "content": "sys"}]
    runner._on_context_high_usage(messages, 180000, 200000)

    assert calls == [1]  # 同步执行完成——回调返回前压缩管道已执行完（调用方等执行完成）
    assert messages and all(isinstance(m, dict) for m in messages)
    assert messages[0].get("role") == "system"


async def test_entry8_runner_force_dedup(monkeypatch):
    """入口 8 压缩类去重（§3.2）：同键（runner-force）在队时复用同一 future。"""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(chat_module, "_main_loop", loop)
    start_pipeline_queue()

    runner = _make_runner()
    runner._execute_force_pipeline = lambda: {"status": "ok"}
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: runner)

    fut1 = runner._dispatch_to_pipeline("runner-force")
    fut2 = runner._dispatch_to_pipeline("runner-force")
    assert fut1 is fut2  # 同键复用（同步两连投递无 await，worker 不可能中途完成）


async def test_entry8_dispatch_failure_rolls_back_dedup(monkeypatch):
    """投递失败（call_soon_threadsafe 抛 RuntimeError）：回滚去重登记 → 同步兜底执行，去重表无残留。"""
    runner = _make_runner()

    class _BadLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *a, **k):
            raise RuntimeError("loop closed")

    monkeypatch.setattr(chat_module, "_main_loop", _BadLoop())
    start_pipeline_queue()

    calls = []

    def fake_execute():
        calls.append(1)
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: []
    messages = [{"role": "system", "content": "sys"}]
    runner._on_context_high_usage(messages, 100, 200000)

    assert calls == [1]  # 投递失败 → 同步兜底执行
    assert not compat._active_compress_futs  # 去重登记已回滚（无残留）


async def test_enqueue_failure_rolls_back_dedup(monkeypatch):
    """compat _pipeline_enqueue 投递失败（put_nowait 抛异常）：回滚去重登记 + 重新 raise（P3-2/P3-5）。"""
    start_pipeline_queue()
    q = compat._pipeline_queue

    def _boom_put(*a, **k):
        raise RuntimeError("queue closed")

    monkeypatch.setattr(q, "put_nowait", _boom_put)
    with pytest.raises(RuntimeError, match="queue closed"):
        compat._pipeline_enqueue("force", {"mode": "force", "session_id": "s"}, held=False)
    assert not compat._active_compress_futs  # 去重登记已回滚（无残留）

    # 非压缩类（sleep）无去重登记可回滚，异常同样重新抛出
    with pytest.raises(RuntimeError, match="queue closed"):
        compat._pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
    assert not compat._active_compress_futs
