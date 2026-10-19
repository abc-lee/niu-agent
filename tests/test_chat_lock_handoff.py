"""P2a chat lock handoff 测试（计划 §5.2，全 mock，不连真实 LLM）。

覆盖点：
1. completion 已完成：立即 clear_stop → release，各一次
2. completion pending：调用时 request_stop；不立即 release；完成后经 loop release 一次
3. completion None：release 一次
4. 幂等：同一 completion 第二次调用只 warning，releases == 1
5. watchdog：短阈值 + pending → error + request_stop，不 release；完成后计时器取消
6. 无 running loop 且 pending：记录 error、clear_stop，**不 release**
7. 已启动后取消包装：不得 release；worker 真正完成后 release 一次（v1.5 核心回归锁）
8. 排队中被取消：sf done-callback 主动终结 completion → release 一次
9. start_executor 结果/异常透传（B5-P1 已修）

假锁与停止标志都经 `_Recorder` 记成 `(event, order)`，顺序断言靠 order。
"""

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from niu_api.chat_lock import release_chat_lock_when_done, start_executor


class _Recorder:
    """事件序列记录器：每条事件记成 ``(event, order)``，order 从 1 递增。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def record(self, event: str) -> None:
        self.events.append((event, len(self.events) + 1))

    def count(self, event: str) -> int:
        return sum(1 for name, _ in self.events if name == event)

    def order_of(self, event: str) -> int:
        for name, order in self.events:
            if name == event:
                return order
        raise AssertionError(f"event {event!r} 未记录：{self.events}")


class _FakeLock:
    """假锁：每次 release() 记一条事件（不抛错，便于断言「只尝试一次」）。"""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.held = True

    def locked(self) -> bool:
        return self.held

    def release(self) -> None:
        self._recorder.record("release")
        self.held = False


async def _pump(rounds: int = 4) -> None:
    """让事件循环跑若干轮，把 call_soon / call_soon_threadsafe 排队的回调落地。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _logged(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def fake_lock(recorder: _Recorder) -> _FakeLock:
    return _FakeLock(recorder)


@pytest.fixture
def stop_spy(recorder: _Recorder, monkeypatch) -> _Recorder:
    """拦截停止标志操作：helper 内部是函数级 ``from agent.runner import ...``，
    所以 patch 模块属性即可生效。"""
    from agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "clear_stop", lambda: recorder.record("clear_stop"))
    monkeypatch.setattr(runner_mod, "request_stop", lambda: recorder.record("request_stop"))
    return recorder


# 1 ------------------------------------------------------------------
def test_done_completion_releases_immediately(stop_spy, fake_lock):
    completion = Future()
    completion.set_result("ok")

    release_chat_lock_when_done(completion, fake_lock)

    assert stop_spy.count("clear_stop") == 1
    assert stop_spy.count("release") == 1
    assert stop_spy.count("request_stop") == 0
    assert stop_spy.order_of("clear_stop") < stop_spy.order_of("release")
    assert fake_lock.locked() is False


# 2 ------------------------------------------------------------------
async def test_pending_completion_defers_release(stop_spy, fake_lock):
    completion = Future()

    release_chat_lock_when_done(completion, fake_lock)

    assert stop_spy.count("request_stop") == 1
    assert stop_spy.count("release") == 0
    assert stop_spy.count("clear_stop") == 0
    assert fake_lock.locked() is True

    completion.set_result("ok")
    await _pump()

    assert stop_spy.count("release") == 1
    assert stop_spy.count("clear_stop") == 1
    assert stop_spy.order_of("clear_stop") < stop_spy.order_of("release")
    assert fake_lock.locked() is False


# 3 ------------------------------------------------------------------
def test_none_completion_releases_once(stop_spy, fake_lock):
    release_chat_lock_when_done(None, fake_lock)

    assert stop_spy.count("release") == 1
    assert stop_spy.count("clear_stop") == 1
    assert stop_spy.count("request_stop") == 0
    assert fake_lock.locked() is False


# 4 ------------------------------------------------------------------
async def test_second_call_on_same_completion_is_ignored(stop_spy, fake_lock, caplog):
    caplog.set_level(logging.WARNING, logger="niu_api.chat_lock")
    completion = Future()

    release_chat_lock_when_done(completion, fake_lock)
    release_chat_lock_when_done(completion, fake_lock)

    assert len(_logged(caplog, "already scheduled")) == 1
    assert stop_spy.count("request_stop") == 1
    assert stop_spy.count("release") == 0
    assert fake_lock.locked() is True

    completion.set_result(None)
    await _pump()

    assert stop_spy.count("release") == 1
    assert fake_lock.locked() is False


# 5 ------------------------------------------------------------------
async def test_watchdog_requests_stop_but_never_releases(stop_spy, fake_lock, caplog):
    caplog.set_level(logging.ERROR, logger="niu_api.chat_lock")
    completion = Future()

    release_chat_lock_when_done(completion, fake_lock, watchdog_seconds=0.05)
    await asyncio.sleep(0.2)

    assert len(_logged(caplog, "still running after")) == 1
    # 调用时一次 + watchdog 一次
    assert stop_spy.count("request_stop") == 2
    assert stop_spy.count("release") == 0
    assert fake_lock.locked() is True

    completion.set_result(None)
    await _pump()
    assert stop_spy.count("release") == 1
    assert fake_lock.locked() is False

    # 计时器已取消：完成后再等一个阈值，不得再 error、不得再 release
    await asyncio.sleep(0.2)
    assert len(_logged(caplog, "still running after")) == 1
    assert stop_spy.count("request_stop") == 2
    assert stop_spy.count("release") == 1


# 6 ------------------------------------------------------------------
def test_pending_completion_without_loop_keeps_lock(stop_spy, fake_lock, caplog):
    caplog.set_level(logging.ERROR, logger="niu_api.chat_lock")
    completion = Future()
    thread_errors: list[BaseException] = []

    def _call_in_plain_thread() -> None:
        try:
            release_chat_lock_when_done(completion, fake_lock)
        except BaseException as exc:  # 线程内异常必须浮出来，否则用例静默通过
            thread_errors.append(exc)

    worker = threading.Thread(target=_call_in_plain_thread, daemon=True)
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    assert thread_errors == []
    assert any("no running loop" in msg for msg in _logged(caplog, "no running loop"))
    assert stop_spy.count("clear_stop") == 1
    assert stop_spy.count("request_stop") == 0
    assert stop_spy.count("release") == 0
    assert fake_lock.locked() is True


# 7 ------------------------------------------------------------------
async def test_cancel_after_worker_started_does_not_release(stop_spy, fake_lock):
    gate = threading.Event()
    entered = threading.Event()

    def blocking_worker():
        entered.set()
        gate.wait(10)
        return "late"

    loop = asyncio.get_running_loop()
    sf, completion = start_executor(loop, blocking_worker)

    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "worker 未真正启动，用例前提不成立"

    assert sf.cancel() is True
    await _pump(2)
    assert sf.done() and sf.cancelled()
    assert not completion.done(), "worker 仍在跑，completion 必须仍 pending"

    release_chat_lock_when_done(completion, fake_lock)
    assert stop_spy.count("release") == 0
    assert stop_spy.count("request_stop") == 1
    assert fake_lock.locked() is True

    gate.set()
    for _ in range(200):
        if completion.done():
            break
        await asyncio.sleep(0.01)
    assert completion.done(), "放行后 worker 未结束"
    await _pump()

    assert stop_spy.count("release") == 1
    assert stop_spy.count("clear_stop") == 1
    assert fake_lock.locked() is False


# 8 ------------------------------------------------------------------
async def test_cancel_while_queued_terminates_completion(stop_spy, fake_lock):
    loop = asyncio.get_running_loop()
    blocker = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="p2a-queued")
    loop.set_default_executor(pool)
    pool.submit(blocker.wait)  # 占满唯一 worker，后续提交只能排队

    entered = threading.Event()

    def queued_worker():
        entered.set()
        return "never"

    sf, completion = start_executor(loop, queued_worker)

    try:
        assert sf.cancel() is True  # 排队中取消成功，worker 永不启动
        release_chat_lock_when_done(completion, fake_lock)

        # 断言前必须 pump：sf done-callback → 终结 completion → call_soon_threadsafe(release)
        await _pump()

        assert entered.is_set() is False, "排队中被取消的 worker 不应执行"
        assert completion.done()
        assert stop_spy.count("release") == 1
        assert stop_spy.count("clear_stop") == 1
        assert fake_lock.locked() is False
    finally:
        # 结束前必须放行占满任务的 Event，否则 loop 关闭时 shutdown(wait=True) 挂死。
        # 不做 set_default_executor(None) 恢复：新建 loop 的 _default_executor 本就是 None。
        blocker.set()
        pool.shutdown(wait=False)


# 9 ------------------------------------------------------------------
async def test_start_executor_propagates_result_and_exception():
    loop = asyncio.get_running_loop()

    sf, completion = start_executor(loop, lambda: "ok")
    assert await sf == "ok"
    assert completion.result() == "ok"

    boom = RuntimeError("worker boom")

    def failing_worker():
        raise boom

    sf2, completion2 = start_executor(loop, failing_worker)
    with pytest.raises(RuntimeError) as excinfo:
        await sf2
    assert excinfo.value is boom
    assert completion2.exception() is boom
