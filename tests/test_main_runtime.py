import threading

import pytest

from agent.main_runtime import MainAgentRuntime, RunCommand


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    from agent.runner import clear_stop
    clear_stop()
    yield
    clear_stop()


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.kwargs = {}
        self.stream = None
        self.lock = threading.Lock()

    def chat(self, session_id, user_input, stream=True, **kwargs):
        self.stream = stream
        self.kwargs = kwargs
        with self.lock:
            self.calls.append(user_input)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            yield f"chunk:{user_input}"
        finally:
            with self.lock:
                self.active -= 1


class BlockingRunner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def chat(self, session_id, user_input, stream=True, **kwargs):
        self.calls.append(user_input)
        self.started.set()
        self.release.wait(5)
        yield "x"


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
            try:
                yield "x"
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
        def chat(self, *a, **k):
            yield "a"
            yield "b"

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
