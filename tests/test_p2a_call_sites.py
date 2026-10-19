"""P2a 调用点级测试（计划 §5.3 关闭断言 + §5.4 阻塞闸门）。

只覆盖四个 `runner.chat` 调用点的行为，不改生产代码：

- §5.3 关闭断言：`finally` 里的 `gen.close()` 真的被调用。夹具是自定义 `FakeGen`
  （记录 `close()`，不依赖 `inspect.getgeneratorstate`）——`compat`/`chat_queue` 的
  循环体是 `chunks.append`，循环耗尽后生成器早已结束，GEN_CLOSED 断言恒真、会假绿。
- §5.4 阻塞闸门：async 帧退出（取消 / SSE 断连）后 `_chat_lock` 必须一直持有到
  executor worker 真正结束（concurrent completion done），释放恰好一次，
  且 helper 收到的是 `concurrent.futures.Future`（completion）而不是 asyncio 的 `sf`。
- §5.5 时序回归：`chat_queue` 的残留停止标志只能在 `acquire` **之后**清（D-8）；外层 try 之前的
  早退必须抛原异常且不取锁；`gen.close()` 抛错必须被调用点 finally 单独捕获、不覆盖原异常。

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

# 四个调用点各自的 helper 命名空间（计划 §5.4：spy 必须打在调用点，不能只打 niu_api.chat_lock.*）
_HELPER_TARGETS = {
    "chat": "niu_api.chat.chat_lock.release_chat_lock_when_done",
    "compat": "niu_api.compat.chat_lock.release_chat_lock_when_done",
    "chat_queue": "niu_api.chat_queue.chat_lock.release_chat_lock_when_done",
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


class _RecordingLock(_FakeLock):
    """`_FakeLock` + acquire 事件记录与可控等锁窗口（停止标志时序回归用）。

    `gate` 非 None 时：进入 `acquire` 即置 `acquire_started`，随后**在事件循环内轮询**等待放行
    （不阻塞 loop）—— 测试因此能在"等锁窗口"内断言停止标志未被触碰；`acquire` 事件只在真正
    拿到锁之后才记录。
    """

    def __init__(
        self,
        events: list[str],
        gate: threading.Event | None = None,
        gate_timeout: float = 15.0,
    ) -> None:
        super().__init__(events)
        self._gate = gate
        self._gate_timeout = gate_timeout
        self.acquire_started = threading.Event()

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        if self._gate is not None:
            self.acquire_started.set()
            deadline = time.monotonic() + self._gate_timeout
            while not self._gate.is_set():
                if time.monotonic() > deadline:
                    raise TimeoutError("acquire gate 未在超时内放行")
                await asyncio.sleep(0.01)
        self._events.append("acquire")
        self.held = True
        return True


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


def _patch_queue_env(monkeypatch, runner) -> None:
    """`chat_queue._process_single` 的运行环境。"""
    monkeypatch.setattr(
        "niu_api.chat_queue.get_message_store", AsyncMock(return_value=_fake_store())
    )
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "niu_api.chat.persist_agent_reply",
        AsyncMock(return_value=("msg-id", "persisted-reply")),
    )
    monkeypatch.setattr(
        "agent.context_manager.get_context_manager", AsyncMock(return_value=_fake_cm())
    )


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
# §5.3 四个调用点的关闭断言
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


async def test_close_chat_queue_process_single_closes_generator(monkeypatch):
    """chat_queue._process_single：循环体正常耗尽也必须显式 close。"""
    from niu_api.chat_queue import ChatQueue

    fake = FakeGen("x")
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    monkeypatch.setattr("niu_api.compat._chat_lock", _FakeLock(events))
    _patch_queue_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)

    reply = await ChatQueue(runner)._process_single("你好")

    assert fake.closed is True, "chat_queue._process_single 的 finally 未调用 gen.close()"
    assert reply == "persisted-reply"
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
# §5.4 四条路径的阻塞闸门（取消 / 断连后锁持有到 worker 真正结束）
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


async def test_gate_chat_queue_holds_lock_until_worker_finishes(monkeypatch):
    """chat_queue._process_single：取消后锁不得释放；worker 放行后释放恰好一次。"""
    from niu_api.chat_queue import ChatQueue

    gate = threading.Event()
    fake = FakeGen("chunk-1", gate=gate)
    runner = _make_runner(lambda: fake)
    events: list[str] = []
    lock = _FakeLock(events)
    monkeypatch.setattr("niu_api.compat._chat_lock", lock)  # _process_single 内 `from niu_api.compat import _chat_lock`
    _patch_queue_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)
    calls = _spy_release(monkeypatch, "chat_queue")

    try:
        task = asyncio.create_task(ChatQueue(runner)._process_single("你好"))
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
        # I-7：外层 finally 在 completion 未完成时不得触碰停止标志
        assert events[-1] == "request_stop", f"外层 finally 提前 clear_stop：{events}"

        gate.set()
        await _pump()

        assert completion.done()
        assert fake.closed is True
        assert events.count("release") == 1, "释放必须恰好一次"
        assert events[-2:] == ["clear_stop", "release"], f"clear_stop 必须先于 release：{events}"
        assert lock.locked() is False
    finally:
        gate.set()


# ---------------------------------------------------------------------------
# §5.5 停止标志时序（D-8）与早退路径（P2a 4.4）
# ---------------------------------------------------------------------------


def _spy_clear_stop_with_flag_state(monkeypatch, events: list[str]) -> None:
    """clear_stop spy 按调用时的标志状态记事件：`clear_stop` = 真清残留（标志原为 set），
    `clear_stop_noop` = 标志本已清零的空转调用（helper `_release` 的无条件调用即属此类）。

    spy 必须真清标志（对齐 `agent.runner.clear_stop` 语义），否则 `is_stop_requested()` 判定失真。
    """
    from agent.runner import _stop_requested

    def spy() -> None:
        events.append("clear_stop" if _stop_requested.is_set() else "clear_stop_noop")
        _stop_requested.clear()

    monkeypatch.setattr("agent.runner.clear_stop", spy)


async def test_chat_queue_clears_residual_stop_flag_only_after_acquire(monkeypatch):
    """D-8 回归：残留停止标志只能在 acquire 之后清 —— 等锁窗口内不得触碰，acquire 后恰好一次。"""
    from agent.runner import _stop_requested, is_stop_requested
    from niu_api.chat_queue import ChatQueue

    fake = FakeGen("x")
    runner = _make_runner(lambda: fake)
    flag_at_chat: list[bool] = []  # runner.chat 被调用那一刻的停止标志状态
    real_chat = runner.chat

    def chat_with_flag_probe(*args, **kwargs):
        flag_at_chat.append(is_stop_requested())
        return real_chat(*args, **kwargs)

    runner.chat = chat_with_flag_probe

    events: list[str] = []
    gate = threading.Event()
    lock = _RecordingLock(events, gate=gate)
    monkeypatch.setattr("niu_api.compat._chat_lock", lock)  # _process_single 内局部 import
    _patch_queue_env(monkeypatch, runner)
    _spy_stop_flags(monkeypatch, events)
    _spy_clear_stop_with_flag_state(monkeypatch, events)

    _stop_requested.set()  # 模拟上一轮遗留的停止标志（autouse 夹具已把基线清零）

    try:
        task = asyncio.create_task(ChatQueue(runner)._process_single("你好"))
        await _wait_until(lambda: lock.acquire_started.is_set(), "任务进入 _chat_lock.acquire")

        # 等锁窗口：停止标志不得被触碰（D-8 删掉的 pre-lock clear 块正是这里要拦的）
        assert events == [], f"acquire 完成前不得有任何锁/停止标志事件：{events}"
        assert _stop_requested.is_set() is True, "等锁窗口内残留停止标志应仍在"

        gate.set()
        reply = await task

        assert lock.acquire_calls == 1
        assert "acquire" in events
        assert events[: events.index("acquire")] == [], f"acquire 之前出现 clear_stop：{events}"
        assert events.count("clear_stop") == 1, f"acquire 之后真清残留标志必须恰好一次：{events}"
        assert events.index("clear_stop") > events.index("acquire"), f"clear_stop 早于 acquire：{events}"
        assert flag_at_chat == [False], f"runner.chat 调用时残留停止标志未清：{flag_at_chat}"
        assert reply == "persisted-reply"
        assert fake.closed is True
        assert events.count("release") == 1
        assert events[-1] == "release"
        assert lock.locked() is False
    finally:
        gate.set()  # 断言失败也必须放行 acquire，否则挂到 gate_timeout


async def test_chat_queue_early_failure_before_acquire_keeps_lock_free(monkeypatch):
    """早退回归：外层 try 之前的 await 失败必须抛**原异常**（不得 NameError: acquired），且锁未获取。"""
    from niu_api.chat_queue import ChatQueue

    runner = _make_runner(lambda: FakeGen("x"))
    events: list[str] = []
    lock = _RecordingLock(events)
    monkeypatch.setattr("niu_api.compat._chat_lock", lock)
    monkeypatch.setattr(
        "niu_api.chat_queue.get_message_store", AsyncMock(side_effect=RuntimeError("store boom"))
    )
    _spy_stop_flags(monkeypatch, events)

    with pytest.raises(RuntimeError, match="store boom"):
        await ChatQueue(runner)._process_single("你好")

    assert lock.acquire_calls == 0, "早退路径不得取锁"
    assert events == [], f"早退路径不应有 acquire/release/clear_stop 事件：{events}"
