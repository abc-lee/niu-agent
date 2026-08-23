"""T3 整理管道队列测试：nap 投递 + _execute_force_pipeline 提取 + 入口 8（内联直调 + 转换块）。

设计见 docs/superpowers/plans/2026-08-23-remove-outer-subagent-timeouts.md §3.3（入口 8 内联化，
不经队列、无外层等待上限）与 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.1 入口 9。
全 mock：runner._execute_force_pipeline（不真实调压缩管道）+ 转换块 DB 重载（_sync_get_messages 假消息）——
禁真实 LLM、禁图谱写入、messages.db 零新增（转换块孤立 tool 清理路径用无 tool_calls 假消息绕过）。
"""
import asyncio
import threading
import time

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
# 入口 8：runner-force 内联直调（Case 2：不经队列、无外层等待上限）+ 转换块
# ---------------------------------------------------------------------------

def test_entry8_inline_direct_call_then_conversion_block(monkeypatch):
    """直调契约：_execute_force_pipeline 被同步调用、零队列投递；转换块输出 dict 契约。"""
    runner = _make_runner()
    calls = []

    def fake_execute():
        calls.append(1)
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("s1", "system", "system prompt"),
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi there"),
    ]

    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        compat, "_pipeline_enqueue",
        lambda kind, request=None, held=False: enqueue_calls.append((kind, request, held)),
    )
    start_pipeline_queue()  # 队列可用也应零投递（内联化后回调不再触碰队列）

    messages = [{"role": "system", "content": "system prompt"}]
    result = runner._on_context_high_usage(messages, 180000, 200000)

    assert calls == [1]  # 直调完成——回调返回前压缩管道已执行完
    assert enqueue_calls == [], f"不应有任何队列投递，实际 {enqueue_calls}"
    assert result == {"status": "ok"}
    # 转换块输出格式断言（§6 T3）：每条 dict + role/content 键 + system 保留在 messages[0]
    assert messages, "转换块应回写消息"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


def test_entry8_none_return_conversion_block_still_runs(monkeypatch):
    """None 返回分支（Stop 中断序列：阶段检查点 bare return）→ 转换块仍执行，回调透传 None。"""
    runner = _make_runner()
    calls = []

    def fake_execute():
        calls.append(1)
        return None  # Stop 中断 / skip 的 None 返回

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi"),
    ]

    messages = [{"role": "system", "content": "sys"}]
    result = runner._on_context_high_usage(messages, 180000, 200000)

    assert calls == [1]
    assert result is None  # None 返回分支透传
    assert messages, "None 返回后转换块仍应回写消息"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


def test_entry8_no_queue_dependency(monkeypatch):
    """无队列依赖：全局整理队列未创建时直调照常工作（原 None 窗口同步兜底成为唯一路径）。"""
    runner = _make_runner()
    calls = []

    def fake_execute():
        calls.append(1)
        return {"status": "skipped", "reason": "stop"}

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: [_FakeDbMsg("m1", "user", "hello")]
    assert compat._pipeline_queue is None  # 队列未创建（fixture 复位）

    messages = [{"role": "system", "content": "sys"}]
    result = runner._on_context_high_usage(messages, 100, 200000)

    assert calls == [1]
    assert result["status"] == "skipped"
    assert messages and all(isinstance(m, dict) for m in messages)
    assert messages[0].get("role") == "system"


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
