"""P3 D-4 SSE 桥接单测 —— `/chat` 的有界 chunk 队列 + 必达终止槽。

覆盖计划 §2 D-4 / 不变量 I-3 / I-4 / I-5 与风险 R1/R2：

1. **有界队列只丢 chunk**：消费者挂起期间 runtime 高速投递 ⇒ 队列满只丢 chunk，
   收到的 chunk 仍是发出顺序的前缀（`done/error/cancelled` 走独立 terminal 槽，不受影响）；
2. **终止必达**：满队列场景下 `done` 仍恰好一次（前端不悬挂）；
3. **error 映射**：run 的 `error` 事件 → SSE `error` 帧 **+ 补 `done`** 帧；
4. **cancelled 映射**：run 的 `cancelled` 事件 → 只补 `done`（不发 error 帧）；
5. **断连不取消**（I-4）：`body_iterator.aclose()` 只退订，不 `handle.cancel()`，
   run 继续、结果照常经 on_complete 落库；
6. **message_id 来自 holder**（I-5）：`done` 帧的 message_id 即 `persist_run_outcome`
   写进 holder 的那个 id（正常与 error 两条分支都成立）。

全 mock：`FakeRuntime`（经 `agent.main_runtime.get_main_runtime` 注入，D-8）+
`main_loop_thread()`（on_complete 桥接目标）+ 假 store / 假 persist ——
禁真实 LLM、禁真实 messages.db、禁真实 ~/.niu。
"""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

from p2b_fakes import FakeRuntime, main_loop_thread

import niu_api.chat as chat_mod
from niu_api.main_agent_persist import NEUTRAL_REPLY

_LLM_CFG = {
    "type": "openai",
    "apikey": "test-api-key",
    "apibase": "https://api.example.com",
    "model": "test-model",
}

# D-4 的队列上限（niu_api/chat.py：asyncio.Queue(maxsize=256)）
_CHUNK_MAXSIZE = 256
_DONE_RV = {"result": "CURRENT_TASK_DONE", "data": None, "messages": []}


# ---------------------------------------------------------------------------
# 夹具 / 助手
# ---------------------------------------------------------------------------


def _fake_store() -> MagicMock:
    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")
    store.get_messages = AsyncMock(return_value=[])
    return store


def _make_runner(gen_factory, return_value=_DONE_RV) -> MagicMock:
    """`runner.chat` 工厂：`FakeRuntime` 只允许被它的替身线程调用（I-1）。"""
    runner = MagicMock()
    runner.chat = lambda *a, **kw: gen_factory()
    runner.last_return_value = return_value
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    return runner


def _patch_env(monkeypatch, runner, runtime, persist_return=("msg-42", "persisted-reply")):
    """`/chat` 端点的外部依赖（消费方命名空间 patch）。返回 persist_agent_reply 替身。"""
    monkeypatch.setattr("niu_api.chat._load_llm_config", lambda: dict(_LLM_CFG))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr("niu_api.chat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr("niu_api.chat.notify_llm_error_sync", lambda *a: None)
    persist = AsyncMock(return_value=persist_return)
    monkeypatch.setattr("niu_api.chat.persist_agent_reply", persist)
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: runtime)
    return persist


async def _open_chat(message: str = "你好"):
    """调 `/chat` 并返回 (StreamingResponse, body_iterator)。"""
    resp = await chat_mod.chat(chat_mod.ChatRequest(message=message, session_id="s"))
    return resp, resp.body_iterator


def _frames(parts: list[str]) -> list[dict]:
    out: list[dict] = []
    for part in parts:
        for line in part.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


def _chunks(frames: list[dict]) -> list[str]:
    return [f["chunk"] for f in frames if "chunk" in f]


def _done_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("done")]


def _error_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if "error" in f]


# ---------------------------------------------------------------------------
# 1. 有界队列：满则只丢 chunk
# ---------------------------------------------------------------------------


async def test_chunk_queue_is_bounded_and_drops_only_chunks(monkeypatch):
    """消费者挂起期间 runtime 投 600 个 chunk ⇒ 队列满只丢 chunk，终止事件仍必达。"""
    total = 600
    release = threading.Event()

    def gen():
        yield "c0"
        release.wait(10)  # 等测试读到第一个 chunk 后才继续高速投递
        for i in range(1, total):
            yield f"c{i}"

    runner = _make_runner(gen)
    runtime = FakeRuntime(runner=runner)
    _patch_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            _resp, it = await _open_chat()
            first = await it.__anext__()  # 生成器停在 yield —— 消费者不再排空队列
            assert "c0" in first, f"第一个 SSE 帧应是 chunk，实际 {first!r}"
            release.set()
            # 等 runtime 把余下 chunk 全部投递完（消费者仍挂起 ⇒ 队列必然溢出），
            # 不用固定 sleep：慢机器上也能保证"投递数 > maxsize"这一前提成立
            await asyncio.to_thread(runtime.handles[0].wait, 10.0)
            await asyncio.sleep(0.05)  # 让已排队的 offer 回调落地
            parts = [first]
            async for part in it:
                parts.append(part)
    finally:
        release.set()
        runtime.stop()

    frames = _frames(parts)
    got = _chunks(frames)
    assert got, f"应至少收到一个 chunk，实际: {frames}"
    assert len(got) < total, f"队列满必须丢 chunk：收到 {len(got)}/{total}"
    assert len(got) <= _CHUNK_MAXSIZE + 1, (
        f"chunk 上限应是 maxsize(+已取走的 1)，实际收到 {len(got)}"
    )
    # 丢的只能是 chunk：收到的仍是发出顺序的前缀（不重排、不重复）
    assert got == [f"c{i}" for i in range(len(got))], f"chunk 顺序/内容被破坏：{got[:5]}…{got[-5:]}"
    # I-3：满队列下终止事件仍恰好一次
    assert len(_done_frames(frames)) == 1, f"done 必须必达且恰好一次：{frames[-3:]}"
    assert not _error_frames(frames), f"正常回合不得出现 error 帧：{_error_frames(frames)}"


# ---------------------------------------------------------------------------
# 2. 终止事件映射
# ---------------------------------------------------------------------------


async def test_error_terminal_emits_error_frame_then_done(monkeypatch):
    """run 的 `error` 事件 → SSE `error` 帧 + 补 `done` 帧（前端不悬挂）；
    error 帧文案取 holder（与 DB 中性口径一致），绝不转发 `str(outcome.error)`。"""
    def boom():
        raise RuntimeError("boom")

    runner = _make_runner(boom)
    runtime = FakeRuntime(runner=runner)
    _patch_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            _resp, it = await _open_chat()
            parts = [part async for part in it]
    finally:
        runtime.stop()

    frames = _frames(parts)
    errors = _error_frames(frames)
    assert len(errors) == 1, f"应恰有一个 error 帧，实际: {frames}"
    assert errors[0]["error"] == NEUTRAL_REPLY, f"error 帧必须与 DB 中性口径一致：{errors[0]}"
    assert "boom" not in errors[0]["error"], f"原始错误细节不得转发给前端：{errors[0]}"
    dones = _done_frames(frames)
    assert len(dones) == 1, f"error 之后必须补 done（I-3 必达）：{frames}"
    assert frames[-1].get("done") is True, f"done 必须是最后一帧：{frames}"
    # I-5：error 分支的 message_id 同样来自 holder（降级行已落库）
    assert dones[0].get("message_id") == "msg-42", f"done 帧 message_id 应来自 holder：{dones[0]}"


async def test_cancelled_terminal_maps_to_done_without_error_frame(monkeypatch):
    """run 的 `cancelled` 事件 → 只补 `done`（不发 error 帧）——前端不悬挂也不误报。"""
    release = threading.Event()

    def gen():
        yield "c0"
        release.wait(10)
        yield "c1"

    runner = _make_runner(gen)
    runtime = FakeRuntime(runner=runner)
    _patch_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            _resp, it = await _open_chat()
            first = await it.__anext__()
            assert "c0" in first
            # 回合真在跑：cancel 该命令（latch）后放行生成器
            runtime.handles[0].cancel()
            release.set()
            parts = [first]
            async for part in it:
                parts.append(part)
    finally:
        release.set()
        runtime.stop()

    frames = _frames(parts)
    assert not _error_frames(frames), f"cancelled 不得发 error 帧：{frames}"
    dones = _done_frames(frames)
    assert len(dones) == 1, f"cancelled 必须映射成恰好一个 done：{frames}"
    assert runtime.outcomes and runtime.outcomes[0].cancelled is True
    assert runtime.handles[0]._stop_latched is True, "cancel 必须落到该命令的 latch"


# ---------------------------------------------------------------------------
# 3. 断连只退订，不取消 run（I-4）
# ---------------------------------------------------------------------------


async def test_client_disconnect_does_not_cancel_the_run(monkeypatch):
    """`aclose()` 后：命令未被 latch、run 继续跑完、结果照常经 on_complete 落库。"""
    release = threading.Event()

    def gen():
        yield "c0"
        release.wait(10)
        yield "c1"

    runner = _make_runner(gen)
    runtime = FakeRuntime(runner=runner)
    persist = _patch_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            _resp, it = await _open_chat()
            first = await it.__anext__()
            assert "c0" in first

            await it.aclose()  # 客户端断连
            handle = runtime.handles[0]
            assert handle._stop_latched is False, "断连不得 cancel run（I-4）"

            release.set()
            await asyncio.to_thread(handle.wait, 10.0)  # run 必须自己收口
    finally:
        release.set()
        runtime.stop()

    assert len(runtime.outcomes) == 1, "断连后 run 仍必须产出 outcome"
    assert runtime.outcomes[0].cancelled is False, "断连不得被误判成取消"
    assert runtime.commands[0].on_event is not None, "sink 必须挂在命令上（D-3）"
    assert persist.await_count == 1, "结果仍必须经 on_complete 落库（I-4/I-5）"
    # 断连后 sink 不再调度：terminal 事件不会把已退订的连接叫醒
    assert handle._done.is_set()


# ---------------------------------------------------------------------------
# 4. message_id 来自 holder（I-5）
# ---------------------------------------------------------------------------


async def test_done_frame_message_id_comes_from_holder(monkeypatch):
    """正常回合：`done` 帧的 message_id == persist 写进 holder 的 id。"""
    def gen():
        yield "回复"

    runner = _make_runner(gen)
    runtime = FakeRuntime(runner=runner)
    _patch_env(monkeypatch, runner, runtime, persist_return=("msg-777", "回复"))

    try:
        with main_loop_thread():
            _resp, it = await _open_chat()
            parts = [part async for part in it]
    finally:
        runtime.stop()

    frames = _frames(parts)
    dones = _done_frames(frames)
    assert len(dones) == 1, f"done 应恰好一次：{frames}"
    assert dones[0].get("message_id") == "msg-777", (
        f"message_id 必须来自 holder（I-5），实际: {dones[0]}"
    )
    assert dones[0].get("session_id") == "s"
    assert _chunks(frames) == ["回复"]


async def test_no_runtime_yields_neutral_error_then_done_without_submitting(monkeypatch):
    """D-8：无 runtime 单例 ⇒ SSE 发中性 error + done，且绝不顺手拉起单例。"""
    from niu_api.main_agent_persist import NEUTRAL_REPLY

    monkeypatch.setattr("niu_api.chat._load_llm_config", lambda: dict(_LLM_CFG))
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: None)

    _resp, it = await _open_chat()
    parts = [part async for part in it]

    frames = _frames(parts)
    assert len(_error_frames(frames)) == 1, f"应有一个中性 error 帧：{frames}"
    assert _error_frames(frames)[0]["error"] == NEUTRAL_REPLY
    assert len(_done_frames(frames)) == 1, f"必须补 done 收尾：{frames}"
    assert not _chunks(frames), f"无 runtime 不应有 chunk：{frames}"
