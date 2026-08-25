"""T4 内部路径接入测试：入口 3/4/5 fire-and-forget + held=False；入口 6 直接清空（不再投递/等待 tidy）；入口 7 chat_queue await + 降级 attempt 串行。

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
    """/chat 溢出：直接调机械压实（fire-and-forget），零队列投递（Task 3 收编）。"""
    from niu_api.chat import ChatRequest
    from niu_api.chat import chat as chat_endpoint

    mock_runner, _ = _overflow_runner("你好")

    compact_calls: list[str] = []

    def fake_ffc(store, source="chat"):
        compact_calls.append(source)

    monkeypatch.setattr("niu_api.chat.fire_and_forget_compaction", fake_ffc)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

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

    # 收编语义：直接调压实（fire-and-forget），零队列投递
    assert compact_calls == ["ChatSSE"], compact_calls
    assert spy_calls == [], f"不应有任何队列投递，实际 {spy_calls}"
    # SSE 正常收尾
    assert "reply-chunk" in body and '"done": true' in body


# ---------------------------------------------------------------------------
# 入口 4：/chat/sync 溢出 → fire-and-forget 投递 force + held=False
# ---------------------------------------------------------------------------

async def test_entry4_chat_sync_overflow_fire_and_forget(monkeypatch):
    """/chat/sync 溢出：直接调机械压实（fire-and-forget），零队列投递（Task 3 收编）。"""
    from niu_api.chat import ChatRequest
    from niu_api.chat import chat_sync as chat_sync_endpoint

    mock_runner, _ = _overflow_runner("你好")

    compact_calls: list[str] = []

    def fake_ffc(store, source="chat"):
        compact_calls.append(source)

    monkeypatch.setattr("niu_api.chat.fire_and_forget_compaction", fake_ffc)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

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

    assert compact_calls == ["ChatSync"], compact_calls
    assert spy_calls == [], f"不应有任何队列投递，实际 {spy_calls}"
    assert resp.reply == "reply-chunk"  # 响应正常返回（未阻塞）


# ---------------------------------------------------------------------------
# 入口 5：compat chat_session（IM/队列）溢出 → fire-and-forget 机械压实（Task 3 收编）
# ---------------------------------------------------------------------------

async def test_entry5_chat_session_overflow_fire_and_forget(monkeypatch):
    """chat_session 溢出：直接调机械压实（fire-and-forget），零队列投递（Task 3 收编）。"""
    from niu_api.compat import ChatRequest, chat_session

    mock_runner, _ = _overflow_runner("你好")

    compact_calls: list[str] = []

    def fake_ffc(store, source="chat"):
        compact_calls.append(source)

    monkeypatch.setattr("niu_api.chat.fire_and_forget_compaction", fake_ffc)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

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

    assert compact_calls == ["ChatSession"], compact_calls
    assert spy_calls == [], f"不应有任何队列投递，实际 {spy_calls}"
    assert resp.reply == "persisted-reply"


# ---------------------------------------------------------------------------
# 入口 6：clear_chat（/clear）→ 直接清空，不再投递/等待 tidy（§3.6 重写）
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


async def test_entry6_clear_chat_ignores_force_tidy_clears_directly(monkeypatch):
    """/clear 直接清空：后端不再读取 force_tidy 投递通道——请求体带 force_tidy=True 也零投递、零 tidy 执行。"""
    from niu_api.compat import clear_chat

    order: list[str] = []

    async def must_not_run(request, chat_lock_already_held=False):
        raise AssertionError("clear 不应再触发 tidy 管道")

    monkeypatch.setattr(compat, "_tidy_context_impl", must_not_run)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

    fake_req = _patch_clear_chat_env(monkeypatch, order)
    result = await asyncio.wait_for(clear_chat(fake_req), timeout=3.0)

    assert spy_calls == [], f"不应有任何队列投递，实际 {spy_calls}"
    assert order == ["clear"], f"应直接清空（无 tidy 前置），实际 {order}"
    assert result["success"] is True
    assert result["deleted_count"] == 5


async def test_entry6_clear_chat_no_tidy_wait_degrade_path(monkeypatch):
    """无降级路径：不存在"等 tidy 超时降级清空"分支——清空即时完成，不等任何在途整理。

    tripwire：若实现回退为直接 await tidy（旧阻塞契约），asyncio.timeout(3) 红相取消。
    """
    from niu_api.compat import clear_chat

    order: list[str] = []
    entered = asyncio.Event()

    async def slow_impl(request, chat_lock_already_held=False):
        entered.set()
        await asyncio.sleep(30)  # 若被 await 则必然超时红相
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", slow_impl)
    start_pipeline_queue()
    spy_calls, _ = _spy_enqueue(monkeypatch)

    fake_req = _patch_clear_chat_env(monkeypatch, order)
    async with asyncio.timeout(3.0):
        result = await clear_chat(fake_req)

    assert not entered.is_set(), "tidy impl 不应被启动（零投递、零直调）"
    assert spy_calls == [], f"不应有任何队列投递，实际 {spy_calls}"
    assert order == ["clear"], f"应直接清空，实际 {order}"
    assert result["success"] is True


# ---------------------------------------------------------------------------
# 入口 7：chat_queue _retry_force_compression——已随 Task 3 溢出投递面收编整删。
# （降级重试链退役，终态语义=压实后仍超限放行服务端报错走既有降级回复），
# 原三用例（serial_degrade / success_returns_early / none_window_sync）一并删除。
