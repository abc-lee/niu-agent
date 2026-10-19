import asyncio
import threading

import pytest

from agent.main_runtime import MainAgentRuntime, RunCommand, RunHandle, RunOutcome


class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages=None):
        self.messages = messages or []

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


class FakeContextManager:
    """记录 get_context_for_chat 入参，返回固定 view（不 transform）。"""

    def __init__(self, view=None, error=None):
        self.view = [] if view is None else view
        self.error = error
        self.calls = []

    async def get_context_for_chat(self, exclude_last=True, *, exclude_ids=None):
        self.calls.append({"exclude_last": exclude_last, "exclude_ids": exclude_ids})
        if self.error is not None:
            raise self.error
        return self.view


class LoopEnv:
    """专用 loop 线程 + 本用例专属 _chat_lock + 假历史源（全 mock，不碰真实 DB）。"""

    def __init__(self, loop, lock, store, context_manager):
        self.loop = loop
        self.lock = lock
        self.store = store
        self.context_manager = context_manager

    def run(self, coro, timeout=5.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def pump(self, times=3):
        for _ in range(times):
            self.run(asyncio.sleep(0.01))


@pytest.fixture(autouse=True)
def runtime_env(monkeypatch):
    """P2b 环境：_main_loop 线程、每用例新 _chat_lock、假 store/CM、单例与停止标志复位。"""
    from agent.runner import clear_stop
    from niu_api import chat as chat_mod
    from niu_api import compat as compat_mod

    MainAgentRuntime._instance = None
    clear_stop()

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, name="test-main-loop", daemon=True)
    loop_thread.start()
    lock = asyncio.Lock()
    store = FakeStore()
    context_manager = FakeContextManager()

    async def _get_message_store():
        return store

    async def _get_context_manager(message_store=None):
        return context_manager

    monkeypatch.setattr(chat_mod, "_main_loop", loop)
    monkeypatch.setattr(compat_mod, "_chat_lock", lock)
    monkeypatch.setattr("agent.session.get_message_store", _get_message_store)
    monkeypatch.setattr("agent.context_manager.get_context_manager", _get_context_manager)

    try:
        yield LoopEnv(loop, lock, store, context_manager)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(3)
        if not loop_thread.is_alive():
            loop.close()
        clear_stop()
        MainAgentRuntime._instance = None


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.kwargs = {}
        self.stream = None
        self.lock = threading.Lock()
        # NiuRunner 语义：每轮开始重置，正常收尾才写值（取消/硬关闭留 None）
        self.last_return_value = None
        self._persisted_msgs = None
        self._extracted_at_msgs = None

    def chat(self, session_id, user_input, stream=True, **kwargs):
        self.stream = stream
        self.kwargs = kwargs
        self.last_return_value = None
        with self.lock:
            self.calls.append(user_input)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            yield f"chunk:{user_input}"
            self.last_return_value = {"result": "CURRENT_TASK_DONE"}
        finally:
            with self.lock:
                self.active -= 1


class BlockingRunner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.last_return_value = None

    def chat(self, session_id, user_input, stream=True, **kwargs):
        self.calls.append(user_input)
        self.started.set()
        self.release.wait(5)
        yield "x"
        self.last_return_value = {"result": "CURRENT_TASK_DONE"}


def test_singleton():
    MainAgentRuntime._instance = None
    a = MainAgentRuntime.instance()
    b = MainAgentRuntime.instance()
    assert a is b
    MainAgentRuntime._instance = None


def test_fifo_and_one_at_a_time():
    runner = FakeRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h1 = rt.submit(RunCommand(content="one"))
        h2 = rt.submit(RunCommand(content="two"))
        events1 = list(h1.events())
        events2 = list(h2.events())
        assert h1.wait(2) == "chunk:one"
        assert h2.wait(2) == "chunk:two"
        assert [e["type"] for e in events1] == ["chunk", "done"]
        assert [e["type"] for e in events2] == ["chunk", "done"]
        assert runner.calls == ["one", "two"]
        assert runner.max_active == 1
    finally:
        rt.stop()


def test_is_busy_while_running():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        assert rt.is_busy() is True
        runner.release.set()
        assert h.wait(2) == "x"
        assert rt.is_busy() is False
    finally:
        rt.stop()


def test_stop_joins_thread():
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    assert rt.stop() is True
    assert rt._thread is None


def test_stop_timeout_resolves_active_and_queue():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    h1 = rt.submit(RunCommand(content="block"))
    assert runner.started.wait(2)
    h2 = rt.submit(RunCommand(content="queued"))
    assert rt.stop(timeout=0.2) is False
    assert h1.wait(1) is None
    assert h1.error == "cancelled"
    assert list(h1.events())[-1]["type"] == "cancelled"
    assert h2.wait(1) is None
    assert h2.error == "runtime stopped"
    assert runner.calls == ["block"]
    assert list(h2.events())[-1]["type"] == "error"
    assert rt.is_busy() is True
    runner.release.set()
    if rt._thread is not None:
        rt._thread.join(2)


def test_error_event_on_runner_exception():
    class BoomRunner:
        def chat(self, *a, **k):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    rt = MainAgentRuntime(runner=BoomRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        events = list(h.events())
        assert events[0]["type"] == "error"
        assert "boom" in events[0]["error"]
        assert h.wait(2) is None
    finally:
        rt.stop()


def test_future_resolved():
    import concurrent.futures

    runner = FakeRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        fut = concurrent.futures.Future()
        h = rt.submit(RunCommand(content="f", reply_future=fut))
        h.wait(2)
        assert fut.result(2) == "chunk:f"
    finally:
        rt.stop()


def test_stopped_return_value_marks_cancelled():
    class StoppedRunner:
        last_return_value = {"result": "STOPPED"}

        def chat(self, *a, **k):
            yield "partial"

    rt = MainAgentRuntime(runner=StoppedRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        events = list(h.events())
        assert events[-1]["type"] == "cancelled"
        assert h.wait(2) is None
        assert h.error == "cancelled"
    finally:
        rt.stop()


def test_resources_channel_stream_forwarded():
    runner = FakeRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(
            content="x", channel_id="cid-1", stream=False,
            resources=[{"path": "/tmp/a", "mode": "doc"}],
        ))
        h.wait(2)
        assert runner.stream is False
        assert runner.kwargs["channel_id"] == "cid-1"
        assert runner.kwargs["resources"] == [{"path": "/tmp/a", "mode": "doc"}]
    finally:
        rt.stop()


def test_generator_closed_on_finish():
    class CloseAware:
        def __init__(self):
            self.closed = threading.Event()

        def chat(self, *a, **k):
            self.last_return_value = None
            try:
                yield "x"
                self.last_return_value = {"result": "CURRENT_TASK_DONE"}
            finally:
                self.closed.set()

    runner = CloseAware()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        h.wait(2)
        assert runner.closed.is_set()
    finally:
        rt.stop()


def test_multi_chunk_join():
    class MultiChunk:
        last_return_value = None

        def chat(self, *a, **k):
            yield "a"
            yield "b"
            self.last_return_value = {"result": "CURRENT_TASK_DONE"}

    rt = MainAgentRuntime(runner=MultiChunk())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert h.wait(2) == "ab"
    finally:
        rt.stop()


def test_clear_stop_between_commands():
    from agent.runner import request_stop

    runner = FakeRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        request_stop()
        h = rt.submit(RunCommand(content="x"))
        assert h.wait(2) == "chunk:x"
    finally:
        rt.stop()


def test_wait_timeout_raises():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        with pytest.raises(TimeoutError):
            h.wait(0.1)
        runner.release.set()
        h.wait(2)
    finally:
        rt.stop()


def test_submit_after_stop_raises():
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    rt.stop()
    with pytest.raises(RuntimeError):
        rt.submit(RunCommand(content="x"))


def test_start_idempotent():
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    try:
        thread = rt._thread
        rt.start()
        assert rt._thread is thread
    finally:
        rt.stop()


def test_restart_after_stop():
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    rt.stop()
    rt.start()
    try:
        h = rt.submit(RunCommand(content="again"))
        assert h.wait(2) == "chunk:again"
    finally:
        rt.stop()


def test_start_after_stop_timeout_raises():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        assert rt.stop(timeout=0.2) is False
        with pytest.raises(RuntimeError):
            rt.start()
        runner.release.set()
        if rt._thread is not None:
            rt._thread.join(2)
    finally:
        runner.release.set()


def test_queued_is_busy():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h1 = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        h2 = rt.submit(RunCommand(content="queued"))
        assert rt.is_busy() is True
        runner.release.set()
        assert h1.wait(2) == "x"
        assert h2.wait(2) == "x"
    finally:
        rt.stop()


def test_cancel_active_latches_running_command():
    class SignalRunner:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.saw_stop = threading.Event()

        def chat(self, *a, **k):
            self.started.set()
            self.release.wait(2)
            from agent.runner import is_stop_requested
            if is_stop_requested():
                self.saw_stop.set()
                return
            yield "x"

    runner = SignalRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert runner.started.wait(2)
        rt.cancel_active()
        runner.release.set()
        events = list(h.events())
        assert runner.saw_stop.is_set()
        assert events[-1]["type"] == "cancelled"
    finally:
        rt.stop()


def test_cancel_requests_stop():
    class SignalRunner:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.saw_stop = threading.Event()

        def chat(self, *a, **k):
            self.started.set()
            self.release.wait(2)
            from agent.runner import is_stop_requested
            if is_stop_requested():
                self.saw_stop.set()
                return
            yield "x"

    runner = SignalRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert runner.started.wait(2)
        h.cancel()
        runner.release.set()
        events = list(h.events())
        assert runner.saw_stop.is_set()
        assert events[-1]["type"] == "cancelled"
    finally:
        rt.stop()


def test_cancel_queued_command_never_runs():
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h1 = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        h2 = rt.submit(RunCommand(content="queued"))
        h2.cancel()
        runner.release.set()
        assert h1.wait(2) == "x"
        events = list(h2.events())
        assert events[-1]["type"] == "cancelled"
        assert runner.calls == ["block"]
    finally:
        rt.stop()


def test_cancelled_future_raises():
    import concurrent.futures

    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        fut = concurrent.futures.Future()
        h = rt.submit(RunCommand(content="x", reply_future=fut))
        assert runner.started.wait(2)
        h.cancel()
        runner.release.set()
        h.wait(2)
        with pytest.raises(RuntimeError):
            fut.result(2)
    finally:
        rt.stop()


def test_error_future_raises():
    import concurrent.futures

    class BoomRunner:
        def chat(self, *a, **k):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    rt = MainAgentRuntime(runner=BoomRunner())
    rt.start()
    try:
        fut = concurrent.futures.Future()
        h = rt.submit(RunCommand(content="x", reply_future=fut))
        h.wait(2)
        with pytest.raises(RuntimeError):
            fut.result(2)
    finally:
        rt.stop()


# ---------------------------------------------------------------------------
# P2b Task 1：RunOutcome / on_complete / 历史 exclude_ids / _chat_lock / 状态 / 取消 / 忙
# ---------------------------------------------------------------------------


def test_run_command_new_fields_and_outcome_defaults():
    cmd = RunCommand(content="x")
    assert cmd.exclude_message_ids == []
    assert cmd.on_complete is None
    other = RunCommand(content="y", exclude_message_ids=["a"], on_complete=lambda o: None)
    assert cmd.exclude_message_ids == []          # default_factory 不共享
    assert other.exclude_message_ids == ["a"]
    assert other.on_complete is not None

    outcome = RunOutcome()
    assert (outcome.result, outcome.error, outcome.return_value) == ("", None, None)
    assert (outcome.persisted_msgs, outcome.extracted_at_msgs) == (None, None)
    assert (outcome.history_len, outcome.cancelled, outcome.neutral_degrade) == (0, False, False)


def test_on_complete_runs_before_done_and_snapshots_success(runtime_env):
    view = [{"role": "user", "content": "h1"}, {"role": "assistant", "content": "h2"}]
    runtime_env.context_manager.view = view
    gate = threading.Event()
    seen = {}
    handles = {}

    class SnapshotRunner:
        def __init__(self):
            self.history = None
            self.last_return_value = None

        def chat(self, *a, **k):
            self.history = k["history"]
            gate.wait(5)
            yield "partial"
            self.last_return_value = {"result": "CURRENT_TASK_DONE"}
            self._persisted_msgs = [{"role": "assistant", "content": "p"}]
            self._extracted_at_msgs = ["sub"]

    def on_complete(outcome):
        seen["done"] = handles["h"]._done.is_set()
        seen["result"] = handles["h"].result
        seen["lock_held"] = runtime_env.lock.locked()
        seen["outcome"] = outcome

    runner = SnapshotRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(
            content="x", exclude_message_ids=["u1", "u2"], on_complete=on_complete,
        ))
        handles["h"] = h
        gate.set()
        assert h.wait(3) == "partial"
    finally:
        rt.stop()

    # I-4：on_complete 早于 done；D-5：回调仍在 _chat_lock 内；handle 未被提前写结果
    assert seen["done"] is False
    assert seen["result"] is None
    assert seen["lock_held"] is True
    outcome = seen["outcome"]
    assert outcome.result == "partial"
    assert outcome.cancelled is False
    assert outcome.error is None
    assert outcome.neutral_degrade is False
    assert outcome.history_len == len(view)
    # 成功回合必须快照 runner 三字段（否则正常回合重复入库）
    assert outcome.return_value == {"result": "CURRENT_TASK_DONE"}
    assert outcome.persisted_msgs == [{"role": "assistant", "content": "p"}]
    assert outcome.extracted_at_msgs == ["sub"]
    # 历史：同一 view 对象送进 runner.chat（未二次 transform）+ exclude_ids 透传
    assert runner.history is view
    assert runtime_env.context_manager.calls == [
        {"exclude_last": False, "exclude_ids": {"u1", "u2"}},
    ]


def test_on_complete_exception_does_not_break_done(runtime_env):
    def boom(outcome):
        raise RuntimeError("callback boom")

    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=boom))
        assert h.wait(3) == "chunk:x"
        runtime_env.pump()
        assert runtime_env.lock.locked() is False
    finally:
        rt.stop()


def test_history_failure_yields_error_outcome_and_still_calls_on_complete(runtime_env):
    runtime_env.context_manager.error = RuntimeError("db down")
    runner = FakeRunner()
    outcomes = []
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        assert h.wait(3) is None
        assert "db down" in (h.error or "")
        outcome = outcomes[0]
        assert isinstance(outcome.error, RuntimeError)
        assert outcome.neutral_degrade is False
        assert outcome.cancelled is False
        assert outcome.return_value is None and outcome.persisted_msgs is None
        assert outcome.history_len == 0
        assert runner.calls == []                 # 历史失败 → 不跑 runner.chat
    finally:
        rt.stop()


def test_runtime_holds_chat_lock_until_on_complete(runtime_env):
    seen = {}

    class LockAwareRunner:
        def __init__(self):
            self.last_return_value = None

        def chat(self, *a, **k):
            seen["held_during_run"] = runtime_env.lock.locked()
            yield "x"
            self.last_return_value = {"result": "CURRENT_TASK_DONE"}

    def on_complete(outcome):
        seen["held_in_callback"] = runtime_env.lock.locked()

    rt = MainAgentRuntime(runner=LockAwareRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=on_complete))
        assert h.wait(3) == "x"
        runtime_env.pump()
        assert seen["held_during_run"] is True
        assert seen["held_in_callback"] is True   # I-3/I-4：持久化完成前不释放
        assert runtime_env.lock.locked() is False
    finally:
        rt.stop()


def test_chat_lock_released_when_runner_raises(runtime_env):
    class BoomRunner:
        def chat(self, *a, **k):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    rt = MainAgentRuntime(runner=BoomRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert h.wait(3) is None
        runtime_env.pump()
        assert runtime_env.lock.locked() is False  # 失败不持锁
    finally:
        rt.stop()


class RecordingLock:
    """假 _chat_lock：记录 release；gate 非空时 acquire 阻塞到 gate 打开。"""

    def __init__(self, held=False, gate=None):
        self._held = held
        self._gate = gate
        self.releases = 0

    def locked(self):
        return self._held

    async def acquire(self):
        if self._gate is not None:
            while not self._gate.is_set():
                await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(30)
        self._held = True
        return True

    def release(self):
        self.releases += 1
        self._held = False


def test_chat_lock_acquire_timeout_yields_error_outcome_without_release(runtime_env, monkeypatch):
    from niu_api import compat as compat_mod

    monkeypatch.setattr("agent.main_runtime._LOCK_ACQUIRE_TIMEOUT", 0.05)
    monkeypatch.setattr("agent.main_runtime._LOCK_FUTURE_TIMEOUT", 5.0)
    runtime_releases = []
    monkeypatch.setattr(
        MainAgentRuntime, "_release_chat_lock",
        lambda self, loop: runtime_releases.append("runtime_release"),
    )
    lock = RecordingLock(held=True)               # 别的持有者（scheduler 轮）占着锁
    monkeypatch.setattr(compat_mod, "_chat_lock", lock)

    runner = FakeRunner()
    outcomes = []
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        assert h.wait(3) is None
        assert "timeout" in (h.error or "")
        outcome = outcomes[0]
        assert isinstance(outcome.error, TimeoutError)
        assert outcome.neutral_degrade is False   # 锁失败保留 timeout 分类
        assert outcome.cancelled is False
        assert runner.calls == []                 # 取锁失败跳过步骤 3-5
        runtime_env.pump()
        assert runtime_releases == []             # runtime 不自行释放
        assert lock.releases == 0
        assert lock.locked() is True              # 仍归原持有者
    finally:
        lock.release()
        rt.stop()


def test_abandoned_acquire_coroutine_leaves_no_orphan_waiter(runtime_env, monkeypatch):
    from niu_api import compat as compat_mod

    monkeypatch.setattr("agent.main_runtime._LOCK_ACQUIRE_TIMEOUT", 5.0)
    monkeypatch.setattr("agent.main_runtime._LOCK_FUTURE_TIMEOUT", 0.1)
    runtime_releases = []
    monkeypatch.setattr(
        MainAgentRuntime, "_release_chat_lock",
        lambda self, loop: runtime_releases.append("runtime_release"),
    )
    gate = threading.Event()
    lock = RecordingLock(gate=gate)
    monkeypatch.setattr(compat_mod, "_chat_lock", lock)

    outcomes = []
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        threading.Timer(0.3, gate.set).start()    # 锁在 runtime 放弃之后才空出来
        assert h.wait(3) is None
        assert "timeout" in (h.error or "")
        assert outcomes[0].neutral_degrade is False
        runtime_env.pump(5)
        assert runtime_releases == []             # runtime 一律不释放
        assert lock.releases <= 1                 # 协程若已拿到锁则就地自释放
        assert lock.locked() is False             # 锁最终空闲，无孤儿 waiter
    finally:
        gate.set()
        rt.stop()


def test_missing_main_loop_yields_error_outcome(runtime_env, monkeypatch):
    from niu_api import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_main_loop", None)
    outcomes = []
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        assert h.wait(3) is None
        assert "main event loop" in (h.error or "")
        assert isinstance(outcomes[0].error, RuntimeError)
        assert outcomes[0].neutral_degrade is False
    finally:
        rt.stop()


def test_clear_stop_precedes_chat_lock_release(runtime_env, monkeypatch):
    from agent import runner as runner_mod
    from niu_api import compat as compat_mod

    order = []

    class OrderedLock:
        def __init__(self):
            self._held = False

        def locked(self):
            return self._held

        async def acquire(self):
            self._held = True
            return True

        def release(self):
            order.append("release")
            self._held = False

    monkeypatch.setattr(compat_mod, "_chat_lock", OrderedLock())
    monkeypatch.setattr(runner_mod, "clear_stop", lambda: order.append("clear_stop"))
    rt = MainAgentRuntime(runner=FakeRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert h.wait(3) == "chunk:x"
    finally:
        rt.stop()
    assert order[-2:] == ["clear_stop", "release"]


def test_residual_stop_flag_cleared_only_after_lock_acquired(runtime_env, monkeypatch):
    """D-8 等价锁：残留全局 stop flag 不得在取锁前清掉——acquire 时 clear_stop 计数为 0 且
    flag 仍 True；acquire 之后、runner.chat 之前恰好清一次。"""
    from agent import runner as runner_mod
    from agent.runner import clear_stop as real_clear_stop
    from agent.runner import is_stop_requested, request_stop
    from niu_api import compat as compat_mod

    order: list[str] = []
    seen: dict[str, object] = {}

    class OrderedLock:
        def __init__(self):
            self._held = False

        def locked(self):
            return self._held

        async def acquire(self):
            self._held = True
            order.append("acquire")
            seen["clear_stops_before_acquire"] = order.count("clear_stop")
            seen["stop_at_acquire"] = is_stop_requested()

        def release(self):
            order.append("release")
            self._held = False

    class StopAwareRunner(FakeRunner):
        def chat(self, session_id, user_input, stream=True, **kwargs):
            order.append("chat")
            seen["clear_stops_after_acquire"] = order.count("clear_stop")
            seen["stop_at_chat"] = is_stop_requested()
            yield from super().chat(session_id, user_input, stream=stream, **kwargs)

    def _recorded_clear_stop() -> None:
        order.append("clear_stop")
        real_clear_stop()  # 只记录不真清标志会让协作式停止被残留标志误触发

    monkeypatch.setattr(compat_mod, "_chat_lock", OrderedLock())
    monkeypatch.setattr(runner_mod, "clear_stop", _recorded_clear_stop)

    request_stop()  # 上一轮遗留的标志：命令仍必须执行（D-8），且清标志只能在锁后
    rt = MainAgentRuntime(runner=StopAwareRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x"))
        assert h.wait(3) == "chunk:x"
    finally:
        rt.stop()

    assert order[0] == "acquire"
    assert seen["clear_stops_before_acquire"] == 0
    assert seen["stop_at_acquire"] is True
    assert seen["clear_stops_after_acquire"] == 1
    assert seen["stop_at_chat"] is False


class StateRunner:
    def __init__(self):
        self.channels = []
        self.forces = []
        self._request_source = "user"
        self.source_during_run = None
        self.last_return_value = None

    def set_im_channel(self, channel_id):
        self.channels.append(channel_id)

    def set_im_force(self, value):
        self.forces.append(value)

    def chat(self, *a, **k):
        self.source_during_run = self._request_source
        yield "x"
        self.last_return_value = {"result": "CURRENT_TASK_DONE"}


@pytest.mark.parametrize(
    "channel,source,want_channels,want_forces,want_source",
    [
        ("electron", "user", [""], [False], "user"),
        ("im", "im", ["cid-9"], [], "user"),
        ("scheduler", "scheduler", [], [True], "scheduler"),
        ("electron", "ha-watcher", [""], [False], "scheduler"),
    ],
)
def test_runtime_owns_channel_and_source_state(
    runtime_env, channel, source, want_channels, want_forces, want_source,
):
    runner = StateRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(
            content="x", channel=channel, source=source, channel_id="cid-9",
        ))
        assert h.wait(3) == "x"
    finally:
        rt.stop()
    assert runner.source_during_run == want_source
    assert runner.channels == want_channels
    assert runner.forces == want_forces
    assert runner._request_source == "user"       # finally 恢复进入时的值


def test_cancel_active_latches_active_and_pending(runtime_env):
    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h1 = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        h2 = rt.submit(RunCommand(content="queued"))
        rt.cancel_active()
        assert h1._stop_latched is True
        assert h2._stop_latched is True
        runner.release.set()
        assert h2.wait(3) is None
        assert h2.error == "cancelled"
        assert list(h2.events())[-1]["type"] == "cancelled"
        assert runner.calls == ["block"]          # pending 早退不跑 runner.chat
    finally:
        rt.stop()


def test_early_exit_outcome_has_no_round_snapshot(runtime_env):
    runtime_env.context_manager.view = [{"role": "user", "content": "h1"}]
    runner = BlockingRunner()
    outcomes = []
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        rt.submit(RunCommand(content="block"))   # 占住 active 槽
        assert runner.started.wait(2)
        h2 = rt.submit(RunCommand(content="queued", on_complete=outcomes.append))
        h2.cancel()
        runner.release.set()
        assert h2.wait(3) is None
        assert h2.error == "cancelled"
        outcome = outcomes[0]
        assert outcome.cancelled is True
        assert outcome.error is None
        assert outcome.result == ""
        assert outcome.return_value is None
        assert outcome.persisted_msgs is None
        assert outcome.history_len == 0           # 早退不读历史
        assert runner.calls == ["block"]
    finally:
        rt.stop()


def test_cancelled_with_return_value_keeps_partial_text_and_snapshot(runtime_env):
    view = [{"role": "user", "content": "h1"}]
    runtime_env.context_manager.view = view

    class CooperativeStopRunner:
        """yield 一块后协作式停止：本轮 rv 非 None（STOPPED）。"""

        def chat(self, *a, **k):
            from agent.runner import request_stop
            yield "partial"
            request_stop()
            self.last_return_value = {"result": "STOPPED"}
            self._persisted_msgs = [{"role": "assistant", "content": "p"}]
            self._extracted_at_msgs = ["sub"]

    outcomes = []
    rt = MainAgentRuntime(runner=CooperativeStopRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        assert h.wait(3) is None                  # 取消：handle 不给文本
        assert h.error == "cancelled"
        outcome = outcomes[0]
        assert outcome.cancelled is True
        assert outcome.error is None
        assert outcome.result == "partial"        # 部分文本保留（I-11）
        assert outcome.return_value == {"result": "STOPPED"}
        assert outcome.persisted_msgs == [{"role": "assistant", "content": "p"}]
        assert outcome.extracted_at_msgs == ["sub"]
        assert outcome.history_len == len(view)   # 公共构造，不只成功分支
    finally:
        rt.stop()


def test_cancelled_without_return_value_keeps_persisted_when_advanced(runtime_env):
    view = [{"role": "user", "content": "h1"}, {"role": "assistant", "content": "h2"}]
    runtime_env.context_manager.view = view

    class AdvancedNoRvRunner:
        """生成器推进过但本轮没有 return_value（硬取消形状）。"""

        def chat(self, *a, **k):
            from agent.runner import request_stop
            yield "partial"
            request_stop()
            self._persisted_msgs = ["m1"]
            self.last_return_value = None

    outcomes = []
    rt = MainAgentRuntime(runner=AdvancedNoRvRunner())
    rt.start()
    try:
        h = rt.submit(RunCommand(content="x", on_complete=outcomes.append))
        assert h.wait(3) is None
        outcome = outcomes[0]
        assert outcome.cancelled is True
        assert outcome.return_value is None
        assert outcome.persisted_msgs == ["m1"]   # advanced 保真本轮新鲜值
        assert outcome.extracted_at_msgs is None
        assert outcome.history_len == len(view)
    finally:
        rt.stop()


def test_is_main_agent_busy_covers_active_and_pending(runtime_env):
    from agent.main_runtime import is_main_agent_busy

    assert is_main_agent_busy() is False          # 无单例
    runner = BlockingRunner()
    rt = MainAgentRuntime.instance(runner=runner)
    rt.start()
    try:
        h1 = rt.submit(RunCommand(content="block"))
        assert runner.started.wait(2)
        assert is_main_agent_busy() is True       # active
        h2 = rt.submit(RunCommand(content="queued"))
        assert is_main_agent_busy() is True       # pending
        runner.release.set()
        assert h1.wait(3) == "x"
        assert h2.wait(3) == "x"
        assert is_main_agent_busy() is False      # 空
    finally:
        rt.stop()
        assert is_main_agent_busy() is False


def test_stop_instance_noop_without_singleton_or_live_thread(runtime_env):
    assert MainAgentRuntime._instance is None
    MainAgentRuntime.stop_instance()              # 无单例：no-op

    rt = MainAgentRuntime.instance(runner=FakeRunner())
    rt.start()
    assert rt.stop() is True
    MainAgentRuntime.stop_instance()              # 线程已退出：no-op，不抛
    assert rt._thread is None


def test_stop_instance_cancels_active_then_joins(runtime_env):
    from agent.main_runtime import is_main_agent_busy

    runner = BlockingRunner()
    rt = MainAgentRuntime.instance(runner=runner)
    rt.start()
    h = rt.submit(RunCommand(content="block"))
    assert runner.started.wait(2)
    runner.release.set()
    MainAgentRuntime.stop_instance()
    assert h.wait(2) is None                      # 先 cancel → 取消收尾
    assert h.error == "cancelled"
    assert rt._thread is None
    assert is_main_agent_busy() is False


def test_drained_command_gets_neutral_outcome(runtime_env):
    runner = BlockingRunner()
    outcomes = []
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    rt.submit(RunCommand(content="block"))
    assert runner.started.wait(2)
    h2 = rt.submit(RunCommand(content="queued", on_complete=outcomes.append))
    try:
        assert rt.stop(timeout=0.2) is False
        assert h2.wait(1) is None
        outcome = outcomes[0]
        assert outcome.neutral_degrade is True    # 命令从未执行 → 中性降级
        assert isinstance(outcome.error, RuntimeError)
        assert outcome.cancelled is False
        assert list(h2.events())[-1]["type"] == "error"
    finally:
        runner.release.set()
        if rt._thread is not None:
            rt._thread.join(3)


def test_start_side_drain_skips_on_complete(runtime_env):
    outcomes = []
    rt = MainAgentRuntime(runner=FakeRunner())
    command = RunCommand(content="stale", on_complete=outcomes.append)
    handle = RunHandle(command)
    handle._runtime = rt
    rt._queue.put((command, handle))
    rt.start()                                    # start() 侧 drain：不调 on_complete（D-5）
    try:
        assert handle.wait(2) is None
        assert handle.error == "runtime stopped"
        assert outcomes == []
        assert list(handle.events())[-1]["type"] == "error"
    finally:
        rt.stop()


def test_context_manager_exclude_ids_filters_before_assembly(tmp_path):
    from agent.context_manager import ContextManager
    from agent.session import Message

    def _msg(idx, role, content):
        return Message(
            id=f"m{idx:03d}", role=role, content=content, tool_calls=[],
            tool_call_id="", created_at="2026-10-19T10:00:00", rowid=idx + 1,
        )

    store = FakeStore([_msg(0, "user", "Q1"), _msg(1, "assistant", "A1"), _msg(2, "user", "Q2")])
    manager = ContextManager(
        store, max_tokens=1_000_000, blocks_db_path=tmp_path / "context_blocks.db",
    )

    full = asyncio.run(manager.get_context_for_chat(exclude_last=False))
    assert [m["content"] for m in full] == ["Q1", "A1", "Q2"]

    filtered = asyncio.run(manager.get_context_for_chat(exclude_last=False, exclude_ids={"m002"}))
    assert [m["content"] for m in filtered] == ["Q1", "A1"]

    # 空集短路 + 不传时行为与旧签名一致
    same = asyncio.run(manager.get_context_for_chat(exclude_last=False, exclude_ids=set()))
    assert [m["content"] for m in same] == ["Q1", "A1", "Q2"]
    assert [m["content"] for m in asyncio.run(manager.get_context_for_chat())] == ["Q1", "A1"]

    # keyword-only：位置传参必须报错
    with pytest.raises(TypeError):
        asyncio.run(manager.get_context_for_chat(False, {"m002"}))
