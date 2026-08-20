"""T4 内部路径接入测试：入口 3/4/5 fire-and-forget + held=False；入口 6 阻塞契约 + 600s 超时降级；入口 7 chat_queue await + 降级 attempt 串行。

设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.1 / §5 T4 / §6 T4。
全 mock：`_tidy_context_impl`（阻塞/记录型假实现）/ runner / store / persist——禁真实 LLM、
禁图谱写入、messages.db 零新增（全程不触真实 DB；入口 3/4/5 的 persist 也 mock 掉）。
"""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import niu_api.compat as compat
from niu_api.compat import start_pipeline_queue, stop_pipeline_queue


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列/去重表/精灵状态/整理锁（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()
    compat._active_compress_futs.clear()
    compat._SPIRIT_STATE = "idle"
    yield
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()


def _overflow_runner(user_input: str) -> tuple[Mock, dict]:
    """mock runner：chat 产出 chunk 后设置 last_return_value = CONTEXT_OVERFLOW。"""
    runner = Mock()
    return_value = {
        "result": "CONTEXT_OVERFLOW",
        "data": {"overflow": True, "tokens_used": 150000},
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_input},
        ],
    }

    def chat_generator(session_id, message, stream=True, **kwargs):
        yield "reply-chunk"
        runner.last_return_value = return_value

    runner.chat = chat_generator
    runner.last_return_value = None
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    return runner, return_value


def _spy_enqueue(monkeypatch) -> tuple[list, list[Future]]:
    """包装 compat._pipeline_enqueue：记录调用（kind/request/held）并继续真实投递。"""
    calls: list[tuple[str, dict, bool]] = []
    futs: list[Future] = []
    real_enqueue = compat._pipeline_enqueue

    def spy(kind: str, request: dict | None = None, held: bool = False) -> Future:
        calls.append((kind, dict(request or {}), held))
        fut = real_enqueue(kind, request, held)
        futs.append(fut)
        return fut

    monkeypatch.setattr(compat, "_pipeline_enqueue", spy)
    return calls, futs


# ---------------------------------------------------------------------------
# 入口 3：/chat（SSE）溢出 → fire-and-forget 投递 force + held=False
# ---------------------------------------------------------------------------

async def test_entry3_chat_sse_overflow_fire_and_forget(monkeypatch):
    """/chat 溢出：投递 force（held=False）后立即返回，不等整理完成（fire-and-forget）。

    阻塞型假 impl（等 release 才完成）：端点返回时 impl 尚未完成 → 未 await；
    worker 侧收到的 chat_lock_already_held 为 False（held 透传）。
    """
    from niu_api.chat import ChatRequest, chat as chat_endpoint

    mock_runner, _ = _overflow_runner("你好")

    entered = asyncio.Event()
    release = asyncio.Event()
    impl_held: list[bool] = []

    async def fake_impl(request, chat_lock_already_held=False):
        impl_held.append(chat_lock_already_held)
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    start_pipeline_queue()
    spy_calls, spy_futs = _spy_enqueue(monkeypatch)

    with (
        patch("niu_api.chat.get_or_create_runner", return_value=mock_runner),
        patch("niu_api.chat._load_llm_config", return_value={
            "type": "openai", "apikey": "test-api-key",
            "apibase": "https://api.example.com", "model": "test-model",
        }),
        patch("niu_api.chat.notify_new_message", new_callable=AsyncMock),
        patch("niu_api.chat.get_message_store", new_callable=AsyncMock),
        patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "reply-chunk")),
    ):
        async def _run_sse():
            resp = await chat_endpoint(ChatRequest(message="你好", session_id="test-session"))
            parts = []
            async for part in resp.body_iterator:
                parts.append(part)
            return "".join(parts)

        # 端点应在整理任务完成前返回：错误实现（await 未完成 future）→ wait_for 超时红相
        body = await asyncio.wait_for(_run_sse(), timeout=3.0)

    # 投递语义：force + session_id/mode + held=False
    assert spy_calls == [("force", {"session_id": "test-session", "mode": "force"}, False)], spy_calls
    # fire-and-forget：端点返回时整理尚未完成（impl 阻塞中，future 未 done）
    assert not spy_futs[0].done(), "fire-and-forget：投递后端点返回时 future 不应已完成"
    # SSE 正常收尾
    assert "reply-chunk" in body and '"done": true' in body

    # 清理：放行 impl，等待 worker 完成（完成后 held 透传必已记录）
    release.set()
    await asyncio.wait_for(asyncio.wrap_future(spy_futs[0]), timeout=1.0)
    assert impl_held == [False], impl_held  # worker 侧 held=False 透传（等 _chat_lock 语义）


# ---------------------------------------------------------------------------
# 入口 4：/chat/sync 溢出 → fire-and-forget 投递 force + held=False
# ---------------------------------------------------------------------------

async def test_entry4_chat_sync_overflow_fire_and_forget(monkeypatch):
    """/chat/sync 溢出：投递 force（held=False）后立即返回响应，不等整理完成。"""
    from niu_api.chat import ChatRequest, chat_sync as chat_sync_endpoint

    mock_runner, _ = _overflow_runner("你好")

    entered = asyncio.Event()
    release = asyncio.Event()
    impl_held: list[bool] = []

    async def fake_impl(request, chat_lock_already_held=False):
        impl_held.append(chat_lock_already_held)
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    start_pipeline_queue()
    spy_calls, spy_futs = _spy_enqueue(monkeypatch)

    cm = MagicMock()
    cm.get_context_for_chat = AsyncMock(return_value=[])

    with (
        patch("niu_api.chat.get_or_create_runner", return_value=mock_runner),
        patch("niu_api.chat._load_llm_config", return_value={
            "type": "openai", "apikey": "test-api-key",
            "apibase": "https://api.example.com", "model": "test-model",
        }),
        patch("niu_api.chat.notify_new_message", new_callable=AsyncMock),
        patch("niu_api.chat.get_message_store", new_callable=AsyncMock),
        patch("agent.context_manager.get_context_manager", new_callable=AsyncMock, return_value=cm),
        patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "reply-chunk")),
    ):
        resp = await asyncio.wait_for(
            chat_sync_endpoint(ChatRequest(message="你好", session_id="test-session")), timeout=3.0
        )

    assert spy_calls == [("force", {"session_id": "test-session", "mode": "force"}, False)], spy_calls
    assert not spy_futs[0].done(), "fire-and-forget：返回响应时整理不应已完成"
    assert resp.reply == "reply-chunk"  # 响应正常返回（未阻塞）

    release.set()
    await asyncio.wait_for(asyncio.wrap_future(spy_futs[0]), timeout=1.0)
    assert impl_held == [False], impl_held  # worker 侧 held=False 透传


# ---------------------------------------------------------------------------
# 入口 5：compat chat_session（IM/队列）溢出 → fire-and-forget 投递 force + held=False
# ---------------------------------------------------------------------------

async def test_entry5_chat_session_overflow_fire_and_forget(monkeypatch):
    """chat_session 溢出：投递 force（held=False）后立即返回响应，不等整理完成。"""
    from niu_api.compat import chat_session, ChatRequest

    mock_runner, _ = _overflow_runner("你好")

    entered = asyncio.Event()
    release = asyncio.Event()
    impl_held: list[bool] = []

    async def fake_impl(request, chat_lock_already_held=False):
        impl_held.append(chat_lock_already_held)
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    start_pipeline_queue()
    spy_calls, spy_futs = _spy_enqueue(monkeypatch)

    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")
    cm = MagicMock()
    cm.get_context_for_chat = AsyncMock(return_value=[])

    with (
        patch("niu_api.config.get_config", return_value=SimpleNamespace(llm=SimpleNamespace(api_key="test"))),
        patch("niu_api.compat.get_message_store", AsyncMock(return_value=store)),
        patch("agent.context_manager.get_context_manager", AsyncMock(return_value=cm)),
        patch("niu_api.chat.get_or_create_runner", return_value=mock_runner),
        patch("niu_api.chat.notify_new_message", AsyncMock()),
        patch("agent.runner.clear_stop"),
        patch("agent.runner.drain_supplements"),
        patch("niu_api.chat.persist_agent_reply", AsyncMock(return_value=("persisted-msg-id", "persisted-reply"))),
    ):
        resp = await asyncio.wait_for(
            chat_session(ChatRequest(message="你好", session_id="default", source="electron")), timeout=3.0
        )

    assert spy_calls == [("force", {"session_id": "default", "mode": "force"}, False)], spy_calls
    assert not spy_futs[0].done(), "fire-and-forget：返回响应时整理不应已完成"
    assert resp.reply == "persisted-reply"

    release.set()
    await asyncio.wait_for(asyncio.wrap_future(spy_futs[0]), timeout=1.0)
    assert impl_held == [False], impl_held  # worker 侧 held=False 透传


# ---------------------------------------------------------------------------
# 入口 6：clear_chat（/clear force_tidy）→ 投递 + await 600s 超时 + held=True
# ---------------------------------------------------------------------------

def _patch_clear_chat_env(monkeypatch, order: list[str], clear_result=5):
    """clear_chat 运行环境（照抄 test_clear_brain_state mock 清单）+ 记录清空顺序的 store。"""
    import niu_api.chat as chat_module

    class FakeStore:
        async def clear_messages(self):
            order.append("clear")
            return clear_result

    async def fake_get_message_store():
        return FakeStore()

    async def fake_reset_all_cursors():
        return None

    class FakeRequest:
        async def json(self):
            return {"force_tidy": True}

    monkeypatch.setattr("agent.runner.request_stop", lambda: None)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: None)
    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: None)
    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", lambda: 0)
    monkeypatch.setattr(compat, "_reset_all_cursors", fake_reset_all_cursors)
    return FakeRequest()


async def test_entry6_clear_chat_awaits_tidy_then_clear(monkeypatch):
    """入口 6 阻塞契约：整理完成才清空（mock 断言顺序 impl-start→impl-end→clear）+ held=True + skip_compress。"""
    from niu_api.compat import clear_chat

    release = asyncio.Event()
    order: list[str] = []

    async def fake_impl(request, chat_lock_already_held=False):
        order.append("impl-start")
        await release.wait()
        order.append("impl-end")
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    start_pipeline_queue()
    spy_calls, spy_futs = _spy_enqueue(monkeypatch)

    fake_req = _patch_clear_chat_env(monkeypatch, order)

    async def release_later():
        await asyncio.sleep(0.05)
        release.set()

    rl = asyncio.create_task(release_later())
    result = await asyncio.wait_for(clear_chat(fake_req), timeout=3.0)
    await rl

    # 阻塞契约：整理（impl）完成后才清空
    assert order == ["impl-start", "impl-end", "clear"], order
    # 投递语义：force + skip_compress=True + held=True（clear_chat 持锁 → worker 透传）
    assert spy_calls == [
        ("force", {"session_id": "default", "mode": "force", "skip_compress": True}, True)
    ], spy_calls
    assert result["success"] is True
    assert result["deleted_count"] == 5
    assert spy_futs[0].done()


async def test_entry6_clear_chat_600s_timeout_degrades(monkeypatch):
    """入口 6 超时降级：整理 600s（参数化压成 0.01s）未完成 → 清空照常（clear-messages-only）。"""
    from niu_api.compat import clear_chat

    entered = asyncio.Event()
    never = asyncio.Event()
    order: list[str] = []

    async def fake_impl(request, chat_lock_already_held=False):
        entered.set()
        await never.wait()  # 永不完成 → 触发 600s 超时
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

    # 600s 超时参数化：包装 asyncio.wait_for 用 0.01s 生效（clear_chat 内 wait_for 调用点）
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout=None, *args, **kwargs):
        return await real_wait_for(aw, 0.01, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    fake_req = _patch_clear_chat_env(monkeypatch, order)
    # 外层守卫用 asyncio.timeout（TimerHandle 实现，不走 asyncio.wait_for——不受 fast 补丁影响）：
    # 若实现错误（阻塞等待）→ 3s 取消 clear_chat 红相；正确实现 → tidy wait_for 0.01s 超时降级
    async with asyncio.timeout(3.0):
        result = await clear_chat(fake_req)

    # 走投递路径（enqueue 被调）+ 超时降级 clear-messages-only
    assert spy_calls == [
        ("force", {"session_id": "default", "mode": "force", "skip_compress": True}, True)
    ], spy_calls
    assert entered.is_set(), "impl 应已启动（队列路径）——超时的是等待而非投递"
    assert order == ["clear"], f"整理未完成 → 只清空（clear-messages-only），实际 {order}"
    assert result["success"] is True


# ---------------------------------------------------------------------------
# 入口 7：chat_queue _retry_force_compression → 投递 + await wrap_future + 降级 attempt 串行
# ---------------------------------------------------------------------------

def _make_chat_queue():
    from niu_api.chat_queue import ChatQueue

    return ChatQueue(Mock())


async def test_entry7_retry_awaits_serial_degrade(monkeypatch):
    """入口 7：3 次降级 attempt 串行 await——前一 future done 才投下一个；
    参数 force_protect_recent 透传（None/5/2）、held=False；tokens_after 无 → 继续降级。"""
    q = _make_chat_queue()
    enq_calls: list[tuple[str, dict, bool]] = []
    futs: list[Future] = []

    def fake_enqueue(kind, request=None, held=False):
        if futs:
            assert futs[-1].done(), f"attempt {len(enq_calls) + 1} 投递时前一 attempt 未完成——不串行"
        enq_calls.append((kind, dict(request or {}), held))
        f = Future()
        futs.append(f)
        return f

    monkeypatch.setattr(compat, "_pipeline_enqueue", fake_enqueue)
    monkeypatch.setattr(compat, "_pipeline_queue", object())  # 非 None → 走投递路径
    monkeypatch.setattr("niu_api.chat.notify_compact_status_sync", Mock())
    monkeypatch.setattr(compat, "_tidy_context_impl", AsyncMock())  # None 窗口不被走（防御确认）

    async def completer():
        # 依次完成各 attempt：无 tokens_after → 每个 attempt 都继续降级。
        # 增量完成（futs 随方法执行增长）——方法串行 await 前一个 future，必须逐个放行。
        completed = 0
        while completed < 3:
            if len(futs) > completed and not futs[completed].done():
                futs[completed].set_result({"status": "ok", "tokens_after": 0})
                completed += 1
            await asyncio.sleep(0.01)

    c = asyncio.create_task(completer())
    await asyncio.wait_for(q._retry_force_compression("default", delay=0), timeout=3.0)
    await c

    assert len(enq_calls) == 3, f"3 次降级 attempt，实际 {len(enq_calls)}"
    for i, (kind, request, held) in enumerate(enq_calls):
        assert kind == "force"
        assert held is False, f"attempt {i + 1} held 应为 False（worker 自拿锁）"
        assert request["mode"] == "force"
        assert request.get("force_protect_recent") == [None, 5, 2][i], f"attempt {i + 1} 降级参数"
    assert all(f.done() for f in futs)


async def test_entry7_retry_success_returns_early(monkeypatch):
    """入口 7：attempt 1 压缩后降到安全水位 → 提前 return，不再投递降级 attempt。"""
    q = _make_chat_queue()
    futs: list[Future] = []

    def fake_enqueue(kind, request=None, held=False):
        f = Future()
        f.set_result({"status": "ok", "tokens_after": 5})  # <= safe_level(100)
        futs.append(f)
        return f

    monkeypatch.setattr(compat, "_pipeline_enqueue", fake_enqueue)
    monkeypatch.setattr(compat, "_pipeline_queue", object())
    monkeypatch.setattr("niu_api.chat.notify_compact_status_sync", Mock())
    monkeypatch.setattr("agent.subagent._read_context_window_tokens", lambda: 1000)
    monkeypatch.setattr("agent.subagent._read_warning_threshold", lambda: 0.1)  # safe_level = 100

    await asyncio.wait_for(q._retry_force_compression("default", delay=0), timeout=3.0)

    assert len(futs) == 1, f"成功即返回，不应再降级重试，实际 {len(futs)}"


async def test_entry7_retry_none_window_sync(monkeypatch):
    """入口 7 None 窗口（队列未创建）：同步执行 impl（§3.0 Option A），降级参数仍透传。"""
    q = _make_chat_queue()
    impl_calls: list[dict] = []

    async def fake_impl(request, chat_lock_already_held=False):
        impl_calls.append(dict(request))
        return {"status": "ok", "tokens_after": 0}

    monkeypatch.setattr(compat, "_pipeline_queue", None)
    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    monkeypatch.setattr("niu_api.chat.notify_compact_status_sync", Mock())

    await asyncio.wait_for(q._retry_force_compression("default", delay=0), timeout=3.0)

    assert len(impl_calls) == 3
    assert [r.get("force_protect_recent") for r in impl_calls] == [None, 5, 2]
    assert all(r["mode"] == "force" for r in impl_calls)
