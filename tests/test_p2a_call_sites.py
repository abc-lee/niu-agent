"""P2a 调用点级测试（计划 §5.3 关闭断言 + §5.4 阻塞闸门）。

只覆盖三个 `runner.chat` 调用点的行为，不改生产代码：

- §5.3 关闭断言：`finally` 里的 `gen.close()` 真的被调用。夹具是自定义 `FakeGen`
  （记录 `close()`，不依赖 `inspect.getgeneratorstate`）——`compat` 的
  循环体是 `chunks.append`，循环耗尽后生成器早已结束，GEN_CLOSED 断言恒真、会假绿。
  `close()` 抛错必须被调用点 finally 单独捕获、不覆盖原异常。
- §5.4 阻塞闸门：async 帧退出（取消 / SSE 断连）后 `_chat_lock` 必须一直持有到
  executor worker 真正结束（concurrent completion done），释放恰好一次，
  且 helper 收到的是 `concurrent.futures.Future`（completion）而不是 asyncio 的 `sf`。

P2b Task 2b：`chat_queue` 不再是 `runner.chat` 调用点——gen.close、`_chat_lock` 持有/释放、
clear_stop 时序（D-8）与取锁失败早退全部搬进 `MainAgentRuntime`，对应断言由
tests/test_main_runtime.py 覆盖（`test_generator_closed_on_finish`、
`test_runtime_holds_chat_lock_until_on_complete`、`test_clear_stop_precedes_chat_lock_release`、
`test_chat_lock_acquire_timeout_yields_error_outcome_without_release`）。

全 mock：假锁 / 假生成器 / AsyncMock store / 假 persist —— 禁真实 LLM、禁真实 messages.db。
"""

import asyncio
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_LLM_CFG = {
    "type": "openai",
    "apikey": "test-api-key",
    "apibase": "https://api.example.com",
    "model": "test-model",
}

# 三个调用点各自的 helper 命名空间（计划 §5.4：spy 必须打在调用点，不能只打 niu_api.chat_lock.*）
_HELPER_TARGETS = {
    "chat": "niu_api.chat.chat_lock.release_chat_lock_when_done",
    "compat": "niu_api.compat.chat_lock.release_chat_lock_when_done",
}


# ---------------------------------------------------------------------------
# 夹具（计划 §5.3 / §5.4）
# ---------------------------------------------------------------------------


class FakeGen:
    """记录 `close()` 的假生成器：先返回一个 `first`，再 `StopIteration`。

    `closed` 只能由调用点 `finally` 的显式 `gen.close()` 置位，因此
    `fake.closed is True` 才真正证明"显式关闭"存在。

    `gate` 非 None 时（§5.4）：取第二项之前先置 `blocked`（供测试确认"已进入循环体
    之后的阻塞点"），再阻塞在 Event —— 模拟"async 帧已退出、worker 仍在跑"的窗口。

    `close_raises=True` 时 `close()` 先置 `closed` 再抛 `RuntimeError`（验证 close 异常被调用点
    finally 单独捕获、不覆盖原异常）。
    """

    def __init__(
        self,
        first,
        gate: threading.Event | None = None,
        gate_timeout: float = 15.0,
        close_raises: bool = False,
    ):
        self._first = first
        self._yielded = False
        self._gate = gate
        self._gate_timeout = gate_timeout
        self._close_raises = close_raises
        self.blocked = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._yielded:
            self._yielded = True
            return self._first
        if self._gate is not None:
            self.blocked.set()
            if not self._gate.wait(timeout=self._gate_timeout):
                raise TimeoutError("FakeGen gate 未在超时内放行")
        raise StopIteration

    def close(self) -> None:
        # 先置 `closed` 再抛：「close 被调用」与「close 抛错」两个断言可同时成立
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close boom")


class _Boom:
    """`if chunk:` 即抛的假 chunk（SSE 循环体第一行触发异常，生成器仍 SUSPENDED）。"""

    def __bool__(self) -> bool:
        raise RuntimeError("chunk truthiness boom")


class _FakeLock:
    """`_chat_lock` 替身：async `acquire` / `release` / `locked`，release 记一条事件。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.held = False
        self.acquire_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        self.held = True
        return True

    def release(self) -> None:
        self._events.append("release")
        self.held = False

    def locked(self) -> bool:
        return self.held


# ---------------------------------------------------------------------------
# 环境 patch 助手（沿用 test_pipeline_queue_t4 / test_chat_queue 的 patch 清单）
# ---------------------------------------------------------------------------


def _fake_store() -> MagicMock:
    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")
    store.get_messages = AsyncMock(return_value=[])
    return store


def _fake_cm() -> MagicMock:
    """`get_context_manager` 是调用点函数内的局部 import → 必须 patch 源模块。"""
    cm = MagicMock()
    cm.get_context_for_chat = AsyncMock(return_value=[])
    return cm


def _make_runner(gen_factory):
    """`runner.chat` 必须是**工厂 lambda**（直接赋实例会在调用点 `runner.chat(...)` 处 TypeError）。"""
    runner = MagicMock()
    runner.chat = lambda *args, **kwargs: gen_factory()
    runner.last_return_value = None  # 显式 None，避免 Mock 属性把 getattr 默认值变成 Mock
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    runner.get_im_channel = lambda: ""
    runner.should_push_im = lambda: False
    return runner


def _patch_chat_env(monkeypatch, runner) -> None:
    """`/chat`（SSE）与 `/chat/sync` 的运行环境。"""
    monkeypatch.setattr("niu_api.chat._load_llm_config", lambda: dict(_LLM_CFG))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr("niu_api.chat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr(
        "niu_api.chat.persist_agent_reply",
        AsyncMock(return_value=("msg-id", "persisted-reply")),
    )
    monkeypatch.setattr(
        "agent.context_manager.get_context_manager", AsyncMock(return_value=_fake_cm())
    )


def _patch_compat_env(monkeypatch, runner) -> None:
    """`compat /chat/session` 的运行环境。"""
    monkeypatch.setattr(
        "niu_api.config.get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(api_key="test-api-key")),
    )
    monkeypatch.setattr("niu_api.compat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr(
        "agent.context_manager.get_context_manager", AsyncMock(return_value=_fake_cm())
    )
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "niu_api.chat.persist_agent_reply",
        AsyncMock(return_value=("msg-id", "persisted-reply")),
    )
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: [])
    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", AsyncMock(return_value=False))


def _spy_stop_flags(monkeypatch, events: list[str]) -> None:
    """helper 内部是函数级 `from agent.runner import clear_stop, request_stop` → patch 模块属性即生效。"""
    monkeypatch.setattr("agent.runner.clear_stop", lambda: events.append("clear_stop"))
    monkeypatch.setattr("agent.runner.request_stop", lambda: events.append("request_stop"))


def _spy_release(monkeypatch, call_site: str) -> list[tuple]:
    """按调用点命名空间包装 helper：记录参数后**必须委托真 helper**（否则锁永不释放）。"""
    from niu_api import chat_lock as chat_lock_mod

    calls: list[tuple] = []
    real = chat_lock_mod.release_chat_lock_when_done

    def spy(completion, lock, watchdog_seconds: float = 300.0):
        calls.append((completion, lock))
        return real(completion, lock, watchdog_seconds)

    monkeypatch.setattr(_HELPER_TARGETS[call_site], spy)
    return calls


@pytest.fixture(autouse=True)
def _clean_stop_flag():
    """真实停止标志基线清零（直接取 `threading.Event`，不受本用例 monkeypatch 影响）。"""
    from agent.runner import _stop_requested

    _stop_requested.clear()
    yield
    _stop_requested.clear()


async def _pump(rounds: int = 8) -> None:
    """跑若干轮事件循环：worker 线程的 `call_soon_threadsafe` 落地需要真实轮次。"""
    for _ in range(rounds):
        await asyncio.sleep(0.01)


async def _wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"等待 {what} 超时")
        await asyncio.sleep(0.01)


def _assert_completion_is_concurrent_future(completion) -> None:
    """闸门用例的核心断言：helper 收到的是 completion，不是会被 cancel 的 asyncio `sf`。"""
    assert isinstance(completion, Future), f"helper 收到的不是 concurrent completion：{completion!r}"
    assert not isinstance(completion, asyncio.Future), (
        f"helper 误收 asyncio sf（会被 cancel 伪装成 done）：{completion!r}"
    )


# ---------------------------------------------------------------------------
# §5.3 三个调用点的关闭断言
# ---------------------------------------------------------------------------


async def test_close_chat_sync_closes_generator_when_loop_body_raises(monkeypatch):
    """/chat/sync：循环体 `full_reply += chunk` 抛 TypeError → finally 必须 close 生成器。"""
    import niu_api.chat as chat_mod

    fake = FakeGen(object())  # 首项即触发循环体异常，生成器停在 yield 处
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    monkeypatch.setattr("niu_api.chat._chat_lock", _FakeLock(events))
    _patch_chat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)

    resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))

    assert fake.closed is True, "/chat/sync 的 finally 未调用 gen.close()"
    assert resp.reply.startswith("Error:")
    assert events.count("release") == 1
    assert events[-2:] == ["clear_stop", "release"]


async def test_close_chat_sse_closes_generator_when_loop_body_raises(monkeypatch):
    """/chat SSE：`if chunk:` 即抛 → sync_stream 的 finally 必须 close 生成器（先 close 再 sentinel）。"""
    import niu_api.chat as chat_mod

    fake = FakeGen(_Boom())
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    monkeypatch.setattr("niu_api.chat._chat_lock", _FakeLock(events))
    _patch_chat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)

    resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="s"))
    parts: list[str] = []
    async for part in resp.body_iterator:
        parts.append(part)

    assert fake.closed is True, "/chat SSE 的 sync_stream finally 未调用 gen.close()"
    assert any('"error"' in p for p in parts), f"SSE 应下发 error 事件，实际 {parts}"
    assert any('"done": true' in p for p in parts), f"SSE 应正常收尾，实际 {parts}"
    assert events.count("release") == 1
    assert events[-2:] == ["clear_stop", "release"]


async def test_close_compat_chat_session_closes_generator(monkeypatch):
    """compat /chat/session：循环体正常耗尽也必须显式 close（GEN_CLOSED 断言在此恒真）。"""
    import niu_api.compat as compat_mod

    fake = FakeGen("x")  # append 正常，第二次 __next__ 结束循环
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    monkeypatch.setattr("niu_api.compat._chat_lock", _FakeLock(events))
    _patch_compat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
    )

    assert fake.closed is True, "compat /chat/session 的 finally 未调用 gen.close()"
    assert resp.reply == "persisted-reply"
    assert events.count("release") == 1
    assert events[-2:] == ["clear_stop", "release"]


async def test_close_chat_sync_close_exception_does_not_mask_original_error(monkeypatch):
    """close() 抛错必须被 finally 单独捕获：不覆盖循环体原异常、不改变返回、release 仍恰好一次。"""
    import niu_api.chat as chat_mod

    fake = FakeGen(object(), close_raises=True)  # 循环体 += 先抛 TypeError，close 再抛
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    monkeypatch.setattr("niu_api.chat._chat_lock", _FakeLock(events))
    _patch_chat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)

    resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))

    assert fake.closed is True, "close 必须被调用（即使它抛错）"
    assert "can only concatenate str" in resp.reply, f"close 抛错覆盖了原异常：{resp.reply}"
    assert "close boom" not in resp.reply, f"close 异常泄漏到返回值：{resp.reply}"
    assert events.count("release") == 1, "close 抛错后释放必须仍恰好一次"
    assert events[-2:] == ["clear_stop", "release"]


# ---------------------------------------------------------------------------
# §5.4 三条路径的阻塞闸门（取消 / 断连后锁持有到 worker 真正结束）
# ---------------------------------------------------------------------------


async def test_gate_chat_sync_holds_lock_until_worker_finishes(monkeypatch):
    """/chat/sync：取消 async 任务后锁不得释放；worker 放行后释放恰好一次。"""
    import niu_api.chat as chat_mod

    gate = threading.Event()
    fake = FakeGen("chunk-1", gate=gate)
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    lock = _FakeLock(events)
    monkeypatch.setattr("niu_api.chat._chat_lock", lock)
    _patch_chat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)
    calls = _spy_release(monkeypatch, "chat")

    try:
        task = asyncio.create_task(
            chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))
        )
        await _wait_until(lambda: fake.blocked.is_set(), "worker 进入生成器阻塞点")

        assert lock.locked() is True, "worker 仍在跑，锁已被提前释放"
        assert calls == [], "async 帧尚未退出，helper 不应被调用"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(calls) == 1, f"helper 应被调用一次，实际 {len(calls)}"
        completion, passed_lock = calls[0]
        _assert_completion_is_concurrent_future(completion)
        assert passed_lock is lock
        assert not completion.done(), "worker 未结束，completion 不应 done"
        assert lock.locked() is True, "completion 未完成就释放了锁"
        assert events.count("release") == 0
        assert "request_stop" in events, "pending completion 必须请求停止（helper pending 分支）"

        gate.set()
        await _pump()

        assert completion.done()
        assert fake.closed is True, "worker 收尾时未 close 生成器"
        assert events.count("release") == 1, "释放必须恰好一次"
        assert events[-2:] == ["clear_stop", "release"], f"clear_stop 必须先于 release：{events}"
        assert lock.locked() is False
    finally:
        gate.set()  # 任何断言失败都必须放行 worker，否则 executor 线程挂到 gate_timeout


async def test_gate_chat_sse_holds_lock_until_worker_finishes(monkeypatch):
    """/chat SSE：先驱动到第一个 chunk 再 `body_iterator.aclose()`（断连），锁必须继续持有。"""
    import niu_api.chat as chat_mod

    gate = threading.Event()
    fake = FakeGen("chunk-1", gate=gate)
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    lock = _FakeLock(events)
    monkeypatch.setattr("niu_api.chat._chat_lock", lock)
    _patch_chat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)
    calls = _spy_release(monkeypatch, "chat")

    try:
        resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="s"))
        # 未启动的 async generator 上 aclose() 不执行任何函数体 → 必须先读到第一个事件
        first = await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=5.0)
        assert "chunk-1" in first, f"第一个 SSE 事件应是 chunk，实际 {first!r}"
        await _wait_until(lambda: fake.blocked.is_set(), "worker 进入生成器阻塞点")

        assert lock.locked() is True, "worker 仍在跑，锁已被提前释放"
        assert calls == [], "断连前 helper 不应被调用"

        await resp.body_iterator.aclose()

        assert len(calls) == 1, f"helper 应被调用一次，实际 {len(calls)}"
        completion, passed_lock = calls[0]
        _assert_completion_is_concurrent_future(completion)
        assert passed_lock is lock
        assert not completion.done(), "worker 未结束，completion 不应 done"
        assert lock.locked() is True, "completion 未完成就释放了锁"
        assert events.count("release") == 0
        assert "request_stop" in events

        gate.set()
        await _pump()

        assert completion.done()
        assert fake.closed is True, "断连后 worker 收尾时未 close 生成器"
        assert events.count("release") == 1, "释放必须恰好一次"
        assert events[-2:] == ["clear_stop", "release"], f"clear_stop 必须先于 release：{events}"
        assert lock.locked() is False
    finally:
        gate.set()


async def test_gate_compat_chat_session_holds_lock_until_worker_finishes(monkeypatch):
    """compat /chat/session：取消后锁不得释放；worker 放行后释放恰好一次。"""
    import niu_api.compat as compat_mod

    gate = threading.Event()
    fake = FakeGen("chunk-1", gate=gate)
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    lock = _FakeLock(events)
    monkeypatch.setattr("niu_api.compat._chat_lock", lock)
    _patch_compat_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)
    calls = _spy_release(monkeypatch, "compat")

    try:
        task = asyncio.create_task(
            compat_mod.chat_session(
                compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
            )
        )
        await _wait_until(lambda: fake.blocked.is_set(), "worker 进入生成器阻塞点")

        assert lock.locked() is True, "worker 仍在跑，锁已被提前释放"
        assert calls == [], "async 帧尚未退出，helper 不应被调用"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(calls) == 1, f"helper 应被调用一次，实际 {len(calls)}"
        completion, passed_lock = calls[0]
        _assert_completion_is_concurrent_future(completion)
        assert passed_lock is lock
        assert not completion.done()
        assert lock.locked() is True, "completion 未完成就释放了锁"
        assert events.count("release") == 0
        assert "request_stop" in events

        gate.set()
        await _pump()

        assert completion.done()
        assert fake.closed is True
        assert events.count("release") == 1, "释放必须恰好一次"
        assert events[-2:] == ["clear_stop", "release"], f"clear_stop 必须先于 release：{events}"
        assert lock.locked() is False
    finally:
        gate.set()


