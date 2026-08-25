"""T2 全局整理队列测试：worker 串行互斥 + queued 语义 + 去重 + worker 守护 + shutdown pending failed。

设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.0-3.3 / §5 T2 / §6 T2。
全 mock：`_tidy_context_impl` / `is_sleeping` / `get_or_create_runner`——禁真实 LLM、禁图谱写入、messages.db 零新增。
"""
import asyncio

import pytest

import niu_api.compat as compat
from niu_api.compat import (
    _pipeline_enqueue,
    start_pipeline_queue,
    stop_pipeline_queue,
    tidy_context,
)


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
# worker 串行互斥（不断言 FIFO）
# ---------------------------------------------------------------------------

async def test_worker_serial_mutex(monkeypatch):
    """两任务并发投递：执行不重叠（每个 start 后紧跟同任务 end），不断言顺序。"""
    order: list[tuple[str, int]] = []

    async def fake_impl(request, chat_lock_already_held=False):
        order.append(("start", request["tag"]))
        await asyncio.sleep(0.05)
        order.append(("end", request["tag"]))
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    monkeypatch.setattr(compat, "is_sleeping", lambda: True)
    start_pipeline_queue()
    futs = [
        _pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s", "tag": i}, held=False)
        for i in range(2)
    ]
    results = await asyncio.gather(*(asyncio.wrap_future(f) for f in futs))
    assert [r["status"] for r in results] == ["ok", "ok"]
    starts = [i for i, (ev, _) in enumerate(order) if ev == "start"]
    assert len(starts) == 2
    for s in starts:
        assert order[s + 1] == ("end", order[s][1])  # start 后紧跟同任务 end → 无重叠
    assert len(order) == 4


# ---------------------------------------------------------------------------
# queued 语义（端点改造）
# ---------------------------------------------------------------------------

async def test_tidy_sleep_returns_queued_immediately(monkeypatch):
    """mode='sleep' → 投递 + 立即返回 {"status":"queued"}，不等 impl 完成。"""
    entered = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await asyncio.Event().wait()  # 永不完成——若端点等待则 wait_for 超时失败
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    monkeypatch.setattr(compat, "is_sleeping", lambda: True)
    start_pipeline_queue()
    resp = await asyncio.wait_for(tidy_context({"session_id": "s", "mode": "sleep"}), timeout=1.0)
    assert resp == {"status": "queued"}
    # worker 后台确实开始处理该任务（投递生效）
    await asyncio.wait_for(entered.wait(), timeout=1.0)


from unittest.mock import AsyncMock


async def test_tidy_force_rejected_compact_direct(monkeypatch):
    """Task 3 收编：mode='force' 随白名单收缩被拒；mode='compact' 直达机械压实
    （不经整理队列、不触 _tidy_context_impl），并回传圆环 usage。"""
    from agent.context_assembler import compaction

    impl_spy = AsyncMock()
    monkeypatch.setattr(compat, "_tidy_context_impl", impl_spy)  # compact 不得经 impl
    monkeypatch.setattr(compat, "get_message_store", AsyncMock(return_value=object()))
    monkeypatch.setattr("niu_api.chat.notify_compact_status_sync", lambda *a, **k: None)

    async def fake_compact(store, system_msg=None, **kw):
        return [{"role": "user", "content": "[历史索引]"}], {
            "usage": 0.35, "tokens_estimate": 3500, "context_window": 10000,
            "keep_turns": 3, "units_total": 5, "blocks_archived": 2,
            "blocks_total": 2, "tools_placeholderized": 0, "emergency": False,
        }

    monkeypatch.setattr(compaction, "compact_now_detailed", fake_compact)

    resp_force = await tidy_context({"session_id": "s", "mode": "force"})
    assert resp_force["status"] == "error"

    start_pipeline_queue()  # 队列可用也不得被 compact 触碰
    try:
        resp = await tidy_context({"session_id": "s", "mode": "compact"})
    finally:
        if compat._pipeline_queue is not None:
            await compat.stop_pipeline_queue()
    assert resp["status"] == "ok" and resp["mode"] == "compact"
    assert resp["usage"] == 0.35
    impl_spy.assert_not_awaited()


async def test_tidy_none_window_sync(monkeypatch):
    """None 窗口（队列未创建）：sleep 同步执行 impl，调用方等完成（§3.0 Option A）。

    Task 3 注：force 分支已随白名单收缩移除——compact 不走 _tidy_context_impl，
    同步语义由 test_tidy_force_rejected_compact_direct 覆盖。
    """
    called: list[dict] = []

    async def fake_impl(request, chat_lock_already_held=False):
        called.append(request)
        return {"status": "success"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    assert compat._pipeline_queue is None
    resp = await tidy_context({"session_id": "s", "mode": "sleep"})
    assert resp == {"status": "success"}
    assert len(called) == 1


# ---------------------------------------------------------------------------
# 去重（键 = kind + skip_compress + force_protect_recent）
# ---------------------------------------------------------------------------

async def test_dedup_same_key_reuses(monkeypatch):
    """同键在队/执行中任务复用同一 future（键相同复用）。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    start_pipeline_queue()
    fut1 = _pipeline_enqueue("force", {"mode": "force", "session_id": "s"}, held=False)
    await asyncio.wait_for(entered.wait(), timeout=1.0)  # worker 执行 fut1（未 done）
    fut2 = _pipeline_enqueue("force", {"mode": "force", "session_id": "s"}, held=False)
    assert fut1 is fut2  # 键相同 → 复用
    release.set()
    result = await asyncio.wait_for(asyncio.wrap_future(fut1), timeout=1.0)
    assert result["status"] == "ok"
    assert fut2.done()  # 复用同一 future → 一起完成


async def test_dedup_skip_compress_independent(monkeypatch):
    """skip_compress 是独立去重维度：同 kind 不同 skip_compress 不命中（clear_chat 语义隔离）。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    start_pipeline_queue()
    fut1 = _pipeline_enqueue("force", {"mode": "force", "session_id": "s"}, held=False)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    fut2 = _pipeline_enqueue(
        "force", {"mode": "force", "session_id": "s", "skip_compress": True}, held=False
    )
    assert fut1 is not fut2  # skip_compress 不同 → 不命中
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(asyncio.wrap_future(fut1), asyncio.wrap_future(fut2)), timeout=1.0
    )
    assert [r["status"] for r in results] == ["ok", "ok"]


async def test_dedup_kind_differs_no_hit(monkeypatch):
    """kind 不同（force vs runner-force）不命中——可同时各 1 个在队（§7.9）。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    # 假 runner：无 _execute_force_pipeline → worker 兜底 error（不真实调 runner/LLM）
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: object())
    start_pipeline_queue()
    fut1 = _pipeline_enqueue("force", {"mode": "force", "session_id": "s"}, held=False)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    fut2 = _pipeline_enqueue("runner-force", {"mode": "force", "session_id": "s"}, held=False)
    assert fut1 is not fut2  # kind 不同 → 不命中
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(asyncio.wrap_future(fut1), asyncio.wrap_future(fut2)), timeout=1.0
    )
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "error"  # 假 runner 无该方法 → worker 兜底（安全方向）


# ---------------------------------------------------------------------------
# worker 守护：异常重建 / CancelledError 不重建
# ---------------------------------------------------------------------------

async def test_worker_exception_rebuilds(monkeypatch):
    """worker 异常退出 → 打 error 日志 + 重建，重建后队列仍可用。"""
    monkeypatch.setattr(compat, "is_sleeping", lambda: False)  # sleep 任务立即 cancelled，不碰 impl
    start_pipeline_queue()
    first = compat._pipeline_worker_task
    assert first is not None and not first.done()
    q = compat._pipeline_queue
    # 先 patch get()：worker 当前挂在原 get 协程上（不受影响），patch 只作用于其下一次调用
    real_get = q.get
    n = {"calls": 0}

    async def flaky_get():
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("worker crash")
        return await real_get()

    monkeypatch.setattr(q, "get", flaky_get)
    # 投递一个无害任务：worker 完成一次正常循环（证明崩溃前队列工作正常），
    # 随后下一次 get() 走 flaky_get → 崩溃（get 在 try 之外，异常逃逸）
    fut0 = _pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
    r0 = await asyncio.wait_for(asyncio.wrap_future(fut0), timeout=1.0)
    assert r0["status"] == "cancelled"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while compat._pipeline_worker_task is first and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert compat._pipeline_worker_task is not first  # 守护已重建
    assert first.done() and first.exception() is not None  # 原任务异常退出

    # 重建后队列仍可用
    fut1 = _pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
    r1 = await asyncio.wait_for(asyncio.wrap_future(fut1), timeout=1.0)
    assert r1["status"] == "cancelled"


async def test_worker_cancel_no_rebuild():
    """CancelledError（shutdown）→ 守护不重建。"""
    start_pipeline_queue()
    task = compat._pipeline_worker_task
    task.cancel()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert task.cancelled()
    assert compat._pipeline_worker_task is task  # 未重建（仍指向已取消的原任务）


# ---------------------------------------------------------------------------
# shutdown：pending futures 全 failed
# ---------------------------------------------------------------------------

async def test_shutdown_pending_failed(monkeypatch):
    """停 worker 前排出队列剩余项 fut.set_exception(RuntimeError('shutting down'))。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    monkeypatch.setattr(compat, "is_sleeping", lambda: True)
    start_pipeline_queue()
    fut1 = _pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
    await asyncio.wait_for(entered.wait(), timeout=1.0)  # worker 执行 fut1（阻塞中）
    fut2 = _pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)  # 留在队中
    await stop_pipeline_queue()
    assert compat._pipeline_queue is None
    # fut2（pending）→ RuntimeError("shutting down")
    with pytest.raises(RuntimeError, match="shutting down"):
        await asyncio.wrap_future(fut2)
    # fut1（执行中被取消）→ worker finally 兜底 set_result
    assert fut1.done()
    release.set()
