"""P2b Task 3b 接线测试 —— 启动/关闭、忙信号、/stop /clear、submit 失败。

对应计划 §4.4（启动/关闭 I-7）、§4.5（忙信号 / stop / clear）与 §5.3：
1. `start_chat_queue()`：runner → runtime.start → worker（I-7 顺序），重复 start 不重复建 worker；
2. `stop_chat_queue()`：先停 worker，再 `asyncio.to_thread(MainAgentRuntime.stop_instance)`
   （join 不在事件循环线程），`_queue_stopped` 保持 True；
3. 忙信号：scheduler `_is_backend_busy`、db_monitor 两处、`/api/chat/status`
   在 `is_main_agent_busy()` 或 `_chat_lock.locked()` 时判忙，两者皆假才判闲；
4. compat `/stop`、`clear_chat` 与 ChatQueue scheduler 侧 `/stop`：有 runtime 单例 →
   `cancel_active()`，无单例/无方法回落 `request_stop()`；
5. `ChatQueue` submit 失败：`_persist_outcome` 被调、中性降级文案返回、user 行仍写；
6. `ChatQueue.start()` / `reload_runner()` 把 runner 引用同步进 runtime（D-7）。

全 mock：零真实 LLM、零真实 messages.db、零真实 MainAgentRuntime 工作线程。
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from p2b_fakes import FakeRuntime, main_loop_thread

import niu_api.chat as chat_mod
import niu_api.chat_queue as chat_queue_mod
import niu_api.compat as compat
import niu_api.db_monitor as db_monitor
from agent.main_runtime import MainAgentRuntime, RunOutcome
from niu_api.chat_queue import ChatQueue


@pytest.fixture(autouse=True)
def _reset_wiring_globals():
    """单例 / 队列 / 停止标志 / _chat_lock 复位——防跨用例串 runner，防锁跨 loop 绑定。"""
    from agent.runner import clear_stop

    chat_queue_mod._queue = None
    chat_queue_stopped_before = chat_queue_mod._queue_stopped
    MainAgentRuntime._instance = None
    # chat.py 在 import 期把 `_chat_lock` 绑成模块级常量——两处必须同指这把新锁
    chat_lock_before = chat_mod._chat_lock
    compat._chat_lock = chat_mod._chat_lock = asyncio.Lock()
    compat._SPIRIT_STATE = "idle"
    db_monitor._last_remind.clear()
    clear_stop()
    yield
    chat_queue_mod._queue = None
    chat_queue_mod._queue_stopped = chat_queue_stopped_before
    MainAgentRuntime._instance = None
    compat._SPIRIT_STATE = "idle"
    db_monitor._last_remind.clear()
    chat_mod._chat_lock = chat_lock_before
    clear_stop()


# ── 替身 ──────────────────────────────────────────────────────────────────


class TracingRuntime(FakeRuntime):
    """`FakeRuntime` + 调用顺序记录（set_runner / start / stop / cancel_active）。"""

    def __init__(self, trace: list):
        super().__init__()
        self._trace = trace

    def set_runner(self, runner) -> None:
        self._trace.append(("set_runner", runner))
        super().set_runner(runner)

    def start(self) -> None:
        self._trace.append(("runtime.start", None))
        super().start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._trace.append(("runtime.stop", None))
        return super().stop(timeout)

    def cancel_active(self) -> None:
        self._trace.append(("cancel_active", None))
        super().cancel_active()


class TracingQueue(ChatQueue):
    """`ChatQueue` + worker 起停记录（与 runtime 事件同一条 trace 上排序）。"""

    def __init__(self, runner, runtime, trace: list):
        super().__init__(runner, runtime)
        self._trace = trace

    async def start(self):
        self._trace.append(("q.start", None))
        await super().start()

    async def stop(self):
        self._trace.append(("q.stop", None))
        await super().stop()


class CancelSpyRuntime:
    """只暴露 `cancel_active` 的 runtime 替身（compat 用 getattr 探测，鸭子类型即可）。"""

    def __init__(self, trace: list):
        self._trace = trace

    def cancel_active(self) -> None:
        self._trace.append("cancel_active")


class NoCancelRuntime:
    """无 `cancel_active` 的 runtime 替身（§4.5 回落分支的触发条件）。"""


def _fake_config():
    """chat_session 入口的 LLM 配置门（/stop 分支之前，必须非空 api_key）。"""
    return SimpleNamespace(llm=SimpleNamespace(api_key="k"))


def _bare_scheduler():
    """`_is_backend_busy` 不读任何实例字段——绕过 __init__ 的 DB/TaskStore 依赖。"""
    from niu_api.internal.scheduler.scheduler import Scheduler

    return Scheduler.__new__(Scheduler)


# ── 1. start_chat_queue：runner → runtime → worker（I-7）──────────────────


async def test_start_chat_queue_orders_runner_then_runtime_then_worker(monkeypatch):
    trace: list = []
    runner = MagicMock(name="runner")
    q = TracingQueue(runner, TracingRuntime(trace), trace)
    monkeypatch.setattr(chat_queue_mod, "get_chat_queue", lambda: q)

    await chat_queue_mod.start_chat_queue()
    try:
        kinds = [kind for kind, _ in trace]
        assert kinds[0] == "set_runner", "runtime 必须先拿到 ChatQueue 的 runner"
        assert trace[0][1] is runner
        assert kinds.index("runtime.start") > 0
        assert kinds.index("q.start") > kinds.index("runtime.start"), "worker 晚于 runtime 线程"
        assert q._runtime._runner is runner
        assert q._running is True and q._worker_task is not None
    finally:
        await q.stop()
        q._runtime.stop()


async def test_start_chat_queue_second_call_reuses_worker(monkeypatch):
    """重复 start：worker 协程不重建（ChatQueue.start 已 running 早退）。"""
    trace: list = []
    q = TracingQueue(MagicMock(name="runner"), TracingRuntime(trace), trace)
    monkeypatch.setattr(chat_queue_mod, "get_chat_queue", lambda: q)

    await chat_queue_mod.start_chat_queue()
    first_task = q._worker_task
    await chat_queue_mod.start_chat_queue()

    assert q._worker_task is first_task
    assert not first_task.done()
    assert [kind for kind, _ in trace].count("q.start") == 2  # 第二次早退，无新 task
    await q.stop()
    q._runtime.stop()


# ── 2. stop_chat_queue：先 worker，再 to_thread(stop_instance) ─────────────


async def test_stop_chat_queue_stops_worker_then_runtime_off_loop(monkeypatch):
    trace: list = []
    q = TracingQueue(MagicMock(name="runner"), TracingRuntime(trace), trace)
    chat_queue_mod._queue = q
    loop_thread = threading.current_thread()
    monkeypatch.setattr(
        MainAgentRuntime,
        "stop_instance",
        classmethod(lambda cls: trace.append(("stop_instance", threading.current_thread()))),
    )

    await chat_queue_mod.start_chat_queue()
    await chat_queue_mod.stop_chat_queue()

    kinds = [kind for kind, _ in trace]
    assert "q.stop" in kinds and "stop_instance" in kinds
    assert kinds.index("q.stop") < kinds.index("stop_instance"), "I-7 反向关闭：worker 先停"
    assert "runtime.stop" not in kinds, "关闭走 stop_instance，不是实例 stop"
    stop_thread = trace[kinds.index("stop_instance")][1]
    assert stop_thread is not loop_thread, "D-7：runtime join 不得在事件循环线程"
    assert q._running is False and q._worker_task.cancelled()
    assert chat_queue_mod._queue is None
    assert chat_queue_mod._queue_stopped is True, "关服期间新 enqueue 必须显式失败"
    with pytest.raises(RuntimeError, match="stopped"):
        chat_queue_mod.get_chat_queue()


async def test_stop_chat_queue_without_queue_still_marks_stopped(monkeypatch):
    """无队列实例：不碰 runtime，但 `_queue_stopped` 仍置 True。"""
    calls: list = []
    monkeypatch.setattr(
        MainAgentRuntime, "stop_instance", classmethod(lambda cls: calls.append("stop_instance"))
    )

    await chat_queue_mod.stop_chat_queue()

    assert calls == []
    assert chat_queue_mod._queue_stopped is True
    with pytest.raises(RuntimeError, match="stopped"):
        chat_queue_mod.get_chat_queue()


# ── 3. 忙信号：runtime busy 或 _chat_lock 持锁 ────────────────────────────


def test_scheduler_backend_busy_true_when_main_agent_busy(monkeypatch):
    scheduler = _bare_scheduler()
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: True)

    with main_loop_thread():
        assert scheduler._is_backend_busy() is True


def test_scheduler_backend_busy_true_when_chat_lock_held(monkeypatch):
    scheduler = _bare_scheduler()
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)

    with main_loop_thread() as loop:
        lock = compat._chat_lock

        async def _acquire():
            await lock.acquire()

        async def _release():
            lock.release()

        asyncio.run_coroutine_threadsafe(_acquire(), loop).result(3)
        try:
            assert scheduler._is_backend_busy() is True
        finally:
            asyncio.run_coroutine_threadsafe(_release(), loop).result(3)


def test_scheduler_backend_busy_false_when_both_idle(monkeypatch):
    scheduler = _bare_scheduler()
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)

    with main_loop_thread():
        assert scheduler._is_backend_busy() is False


def test_scheduler_backend_busy_reads_runtime_singleton_without_patch(monkeypatch):
    """不 patch `is_main_agent_busy`：单例存在且 is_busy → 判忙（D-8 单一信号真实生效）。"""
    scheduler = _bare_scheduler()
    runtime = MainAgentRuntime()
    monkeypatch.setattr(runtime, "is_busy", lambda: True)
    MainAgentRuntime._instance = runtime

    with main_loop_thread():
        assert scheduler._is_backend_busy() is True


async def test_db_monitor_drain_skips_when_main_agent_busy(monkeypatch):
    """链路 A：主 Agent 忙 → 不 peek、不推 SSE（消息留 MainAgentRequestQueue）。"""
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: True)
    request_queue = MagicMock(name="request-queue")
    monkeypatch.setattr(
        "agent.main_agent_request_queue.get_main_agent_request_queue", lambda: request_queue
    )
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    await db_monitor._drain_main_agent_request_queue()

    request_queue.peek.assert_not_called()
    notify.assert_not_called()


async def test_db_monitor_drain_skips_when_chat_lock_held(monkeypatch):
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    request_queue = MagicMock(name="request-queue")
    monkeypatch.setattr(
        "agent.main_agent_request_queue.get_main_agent_request_queue", lambda: request_queue
    )
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    await compat._chat_lock.acquire()
    try:
        await db_monitor._drain_main_agent_request_queue()
    finally:
        compat._chat_lock.release()

    request_queue.peek.assert_not_called()
    notify.assert_not_called()


async def test_db_monitor_drain_proceeds_when_idle(monkeypatch):
    """对照组：两信号皆假 → 守卫放行（peek 被读，空队列则不推）。"""
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    request_queue = MagicMock(name="request-queue")
    request_queue.peek.return_value = None
    monkeypatch.setattr(
        "agent.main_agent_request_queue.get_main_agent_request_queue", lambda: request_queue
    )
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    await db_monitor._drain_main_agent_request_queue()

    request_queue.peek.assert_called_once()
    notify.assert_not_called()


def test_db_monitor_reminder_skips_when_main_agent_busy(monkeypatch):
    """链路三：主 Agent 忙 → 不读等待注册表、不推提醒。"""
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: True)
    registry = MagicMock(name="ask-registry")
    registry.waiting_unique_names.return_value = ["sub-a"]
    monkeypatch.setattr("agent.ask_main_agent.get_pending_ask_registry", lambda: registry)
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    db_monitor._remind_pending_asks(now=1000.0, ask_queue_empty=True)

    registry.waiting_unique_names.assert_not_called()
    notify.assert_not_called()


async def test_db_monitor_reminder_skips_when_chat_lock_held(monkeypatch):
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    registry = MagicMock(name="ask-registry")
    registry.waiting_unique_names.return_value = ["sub-a"]
    monkeypatch.setattr("agent.ask_main_agent.get_pending_ask_registry", lambda: registry)
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    await compat._chat_lock.acquire()
    try:
        db_monitor._remind_pending_asks(now=1000.0, ask_queue_empty=True)
    finally:
        compat._chat_lock.release()

    registry.waiting_unique_names.assert_not_called()
    notify.assert_not_called()


def test_db_monitor_reminder_pushes_when_idle(monkeypatch):
    """对照组：两信号皆假 + 有等待子 Agent → 推提醒（守卫确为忙信号本身）。"""
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    registry = MagicMock(name="ask-registry")
    registry.waiting_unique_names.return_value = ["sub-a"]
    monkeypatch.setattr("agent.ask_main_agent.get_pending_ask_registry", lambda: registry)
    request_queue = MagicMock(name="request-queue")
    request_queue.is_empty.return_value = True
    monkeypatch.setattr(
        "agent.main_agent_request_queue.get_main_agent_request_queue", lambda: request_queue
    )
    notify = MagicMock(name="notify", return_value=True)
    monkeypatch.setattr(chat_mod, "notify_new_message_sync", notify)

    db_monitor._remind_pending_asks(now=1000.0, ask_queue_empty=True)

    assert notify.call_count == 1
    assert db_monitor._last_remind == {"sub-a": 1000.0}


async def test_chat_status_busy_when_main_agent_busy(monkeypatch):
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: True)
    monkeypatch.setattr("agent.mcp_loader.get_mcp_load_failures", lambda: [])

    assert (await chat_mod.chat_status())["busy"] is True


async def test_chat_status_busy_when_chat_lock_held(monkeypatch):
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    monkeypatch.setattr("agent.mcp_loader.get_mcp_load_failures", lambda: [])

    await compat._chat_lock.acquire()
    try:
        assert (await chat_mod.chat_status())["busy"] is True
    finally:
        compat._chat_lock.release()


async def test_chat_status_idle_when_both_signals_clear(monkeypatch):
    monkeypatch.setattr("agent.main_runtime.is_main_agent_busy", lambda: False)
    monkeypatch.setattr("agent.mcp_loader.get_mcp_load_failures", lambda: [])

    assert (await chat_mod.chat_status())["busy"] is False


async def test_chat_status_busy_from_runtime_singleton_without_patch(monkeypatch):
    runtime = MainAgentRuntime()
    monkeypatch.setattr(runtime, "is_busy", lambda: True)
    MainAgentRuntime._instance = runtime
    monkeypatch.setattr("agent.mcp_loader.get_mcp_load_failures", lambda: [])

    assert (await chat_mod.chat_status())["busy"] is True


# ── 4. /stop：优先 runtime.cancel_active，无单例回落 request_stop ──────────


async def test_compat_stop_prefers_runtime_cancel_active(monkeypatch):
    trace: list = []
    monkeypatch.setattr("niu_api.config.get_config", _fake_config)
    monkeypatch.setattr("agent.runner.request_stop", lambda: trace.append("request_stop"))
    MainAgentRuntime._instance = CancelSpyRuntime(trace)

    resp = await compat.chat_session(compat.ChatRequest(message="/stop", source="electron"))

    assert resp.reply == "已停止"
    assert trace == ["cancel_active"], "有单例必须走 cancel_active（latch active + pending）"


async def test_compat_stop_falls_back_without_singleton(monkeypatch):
    trace: list = []
    monkeypatch.setattr("niu_api.config.get_config", _fake_config)
    monkeypatch.setattr("agent.runner.request_stop", lambda: trace.append("request_stop"))
    MainAgentRuntime._instance = None

    resp = await compat.chat_session(compat.ChatRequest(message="/stop", source="electron"))

    assert resp.reply == "已停止"
    assert trace == ["request_stop"]


async def test_chat_queue_stop_prefers_runtime_cancel_active(monkeypatch):
    """scheduler 侧 /stop：不入队，直接 latch runtime（FakeRuntime.cancel_active 真实生效）。"""
    trace: list = []
    runtime = TracingRuntime(trace)
    runner = MagicMock(name="runner")
    q = ChatQueue(runner, runtime)

    result, future = await q.enqueue_and_wait_with_future(
        "/stop", source="scheduler", channel="scheduler", timeout=1.0
    )

    assert (result, future) == ("已停止", None)
    assert "cancel_active" in [kind for kind, _ in trace]
    assert q._queue.empty()
    runner.chat.assert_not_called()


async def test_chat_queue_stop_falls_back_without_cancel_active(monkeypatch):
    trace: list = []
    monkeypatch.setattr("agent.runner.request_stop", lambda: trace.append("request_stop"))
    q = ChatQueue(MagicMock(name="runner"), NoCancelRuntime())

    result, future = await q.enqueue_and_wait_with_future(
        "/stop", source="scheduler", channel="scheduler", timeout=1.0
    )

    assert (result, future) == ("已停止", None)
    assert trace == ["request_stop"]


# ── 5. clear_chat：先 cancel_active，再走清空 ─────────────────────────────


class _ClearStore:
    """记录 clear_messages 时点的假 store（用于排序 cancel 早于清空）。"""

    def __init__(self, trace: list):
        self._trace = trace

    async def clear_messages(self) -> int:
        self._trace.append("clear_messages")
        return 3


class _ClearRequest:
    async def json(self) -> dict:
        return {}


def _patch_clear_mocks(monkeypatch, trace: list) -> None:
    """clear_chat 的 mock 清单（同 test_clear_brain_state 范式，不碰真实 DB / tmp）。"""
    async def fake_get_message_store():
        return _ClearStore(trace)

    monkeypatch.setattr("agent.runner.request_stop", lambda: trace.append("request_stop"))
    monkeypatch.setattr("agent.runner.clear_stop", lambda: trace.append("clear_stop"))
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: None)
    monkeypatch.setattr("agent.runner.cleanup_suspended_sync_subagents", lambda *a, **k: None)
    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", lambda: 0)
    monkeypatch.setattr("agent.md_mirror.truncate_relay_files", lambda: None)
    monkeypatch.setattr("agent.context_assembler.reset_derived_state", lambda: None)
    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(
        chat_mod,
        "get_or_create_runner",
        lambda: SimpleNamespace(
            handler=MagicMock(), _decay_pool=MagicMock(), _brain_injector=None
        ),
    )


async def test_clear_chat_cancels_active_before_touching_store(monkeypatch):
    trace: list = []
    _patch_clear_mocks(monkeypatch, trace)
    MainAgentRuntime._instance = CancelSpyRuntime(trace)

    result = await compat.clear_chat(_ClearRequest())

    assert result["success"] is True and result["deleted_count"] == 3
    assert "request_stop" not in trace
    assert trace.index("cancel_active") < trace.index("clear_messages"), (
        "§4.5：取消必须先于清空（清空期间不得再有新 LLM 轮）"
    )


async def test_clear_chat_falls_back_to_request_stop_without_singleton(monkeypatch):
    trace: list = []
    _patch_clear_mocks(monkeypatch, trace)
    MainAgentRuntime._instance = None

    result = await compat.clear_chat(_ClearRequest())

    assert result["success"] is True
    assert trace[0] == "request_stop"
    assert "cancel_active" not in trace


# ── 6. ChatQueue submit 失败：中性降级 + user 行仍写 ──────────────────────


class _RecordingStore:
    """记录 user 行写入的假 store（`_process_single` 只需 add_message）。"""

    def __init__(self):
        self.user_rows: list[tuple[str, str]] = []

    async def add_message(self, role: str, content: str, **kwargs) -> str:
        self.user_rows.append((role, content))
        return f"msg-{len(self.user_rows)}"


class _SubmitFailingRuntime(FakeRuntime):
    """submit 抛错（复刻 runtime 已停 / 正在停的窗口）。"""

    def submit(self, command):
        raise RuntimeError("main runtime is stopping")


def _patch_process_single_mocks(monkeypatch, store, *, persist_error=None):
    """`_process_single` / `_persist_outcome` 的外部依赖补丁（零 DB、零 SSE）。"""
    notified: list = []
    persisted: list = []

    async def fake_notify(message_id, role, content, source="electron"):
        notified.append((message_id, role, content, source))
        return True

    async def fake_persist(store_, rv, history_len, reply, **kwargs):
        if persist_error is not None:
            raise persist_error
        persisted.append((rv, history_len, reply, kwargs))
        return ("assistant-msg", reply)

    monkeypatch.setattr(chat_queue_mod, "get_message_store", lambda: _Awaitable(store))
    monkeypatch.setattr(chat_mod, "notify_new_message", fake_notify)
    monkeypatch.setattr(chat_mod, "persist_agent_reply", fake_persist)
    return notified, persisted


class _Awaitable:
    """`await get_message_store()` 语义下可直接返回的对象（AsyncMock 之外的显式替身）。"""

    def __init__(self, target):
        self._target = target

    def __await__(self):
        yield from ()
        return self._target


async def test_submit_failure_returns_neutral_reply_and_persists(monkeypatch):
    store = _RecordingStore()
    notified, persisted = _patch_process_single_mocks(monkeypatch, store)
    runtime = _SubmitFailingRuntime()
    runner = MagicMock(name="runner")
    q = ChatQueue(runner, runtime)

    outcomes: list[RunOutcome] = []
    original = q._persist_outcome

    async def spy(store_, outcome, holder):
        outcomes.append(outcome)
        return await original(store_, outcome, holder)

    monkeypatch.setattr(q, "_persist_outcome", spy)

    reply = await q._process_single("你好", "default", ["你好"], channel="electron", source="user")

    assert reply == "[系统繁忙，请重试]"
    assert len(outcomes) == 1, "submit 失败必须在事件循环线程内直接落降级"
    assert outcomes[0].neutral_degrade is True
    assert isinstance(outcomes[0].error, RuntimeError)
    # user 行仍写（I-5）：命令从未执行也不能丢用户输入
    assert store.user_rows == [("user", "你好")]
    assert notified == [("msg-1", "user", "你好", "electron")]
    # 中性降级：固定文案 + degraded_reason 空（无真实错误可分类）
    assert len(persisted) == 1
    rv, history_len, text, kwargs = persisted[0]
    assert rv is None and history_len == 0
    assert text == "[系统繁忙，请重试]"
    assert kwargs["source"] == "electron" and kwargs["persisted_msgs"] is None
    assert kwargs["degraded_reason"] == ""
    runner.chat.assert_not_called()


async def test_submit_failure_survives_persist_error(monkeypatch):
    """降级落库自身失败也不得抛：仍返回中性文案（user 行已写）。"""
    store = _RecordingStore()
    _patch_process_single_mocks(monkeypatch, store, persist_error=RuntimeError("db down"))
    q = ChatQueue(MagicMock(name="runner"), _SubmitFailingRuntime())

    reply = await q._process_single("你好", "default", ["你好"])

    assert reply == "[系统繁忙，请重试]"
    assert store.user_rows == [("user", "你好")]


# ── 7. runner 引用同步 runtime（D-7 / R12）────────────────────────────────


async def test_chat_queue_start_injects_runner_into_runtime():
    runtime = FakeRuntime()
    runner = MagicMock(name="runner")
    q = ChatQueue(runner, runtime)
    assert runtime._runner is None

    await q.start()
    try:
        assert runtime._runner is runner, "start() 必须先把 runner 交给 runtime"
    finally:
        await q.stop()


async def test_reload_runner_syncs_runtime_reference(monkeypatch):
    old_runner = MagicMock(name="old-runner")
    new_runner = MagicMock(name="new-runner")
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: new_runner)
    runtime = FakeRuntime()
    q = ChatQueue(old_runner, runtime)
    await q.start()
    assert runtime._runner is old_runner

    q.reload_runner()

    assert q._runner is new_runner
    assert runtime._runner is new_runner, "R12：reload_runner 必须同步 runtime 引用"
    await q.stop()
