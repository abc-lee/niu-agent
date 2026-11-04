"""见缝插针末轮残留 → 独立 follow-up run（计划 v2 Task 5）。

覆盖：队列项元数据 / 合并口径同源 / follow-up 命令构造 / 直驱 submit_followup_run
（持久化 + IM 快照 + 失败兜底）/ runtime 尾部 flush（含 lock 失败、latch 早退、
首帧抛错、用户停止丢弃）。
"""
import asyncio
import logging
import threading
import time

import pytest

from agent.main_runtime import MainAgentRuntime, RunCommand, RunOutcome
from agent.runner import (
    SupplementItem,
    drain_supplement,
    drain_supplement_items,
    drain_supplements,
    enqueue_supplement,
    format_supplement_text,
    requeue_supplement_items,
)
# 复用 test_main_runtime 的夹具与替身（同一 fixture 定义：只借用，不改语义）
from tests.test_main_runtime import (  # noqa: F401 — fixture 复用
    BlockingRunner,
    FakeRunner,
    runtime_env,
)


# ---------------------------------------------------------------------------
# 隔离：全局补充队列跨用例复位（残留不得变成下一个用例的 follow-up run）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_supplement_queue():
    drain_supplements()
    try:
        yield
    finally:
        drain_supplements()


class _StubStore:
    """最小 store：persist_agent_reply 的 add_message 契约 + 历史读取。"""

    def __init__(self):
        self.added = []

    async def get_messages(self, limit=None):
        return []

    async def add_message(self, role, content, **kwargs):
        self.added.append({"role": role, "content": content, **kwargs})
        return f"msg-{len(self.added)}"


class _ImRunner:
    """IM 标志快照替身（follow-up on_complete 读这两项）。"""

    def __init__(self, push=True, channel="cid-x"):
        self._push = push
        self._channel = channel

    def should_push_im(self):
        return self._push

    def get_im_channel(self):
        return self._channel


async def _async_noop(*args, **kwargs):
    return None


def _items(*, message_id="m1", session_id="s1", source="electron",
           channel="electron", channel_id=""):
    return [SupplementItem(
        content="末轮补充", message_id=message_id, session_id=session_id,
        source=source, channel=channel, channel_id=channel_id,
    )]


def _parent():
    return RunCommand(content="父任务", session_id="parent-session")


# ---------------------------------------------------------------------------
# 1. 队列 / 合并口径 / 命令构造（纯函数）
# ---------------------------------------------------------------------------


def test_item_carries_metadata():
    enqueue_supplement(
        "补充正文", message_id="m9", session_id="s9", source="electron",
        channel="electron", channel_id="cid9",
    )
    items = drain_supplement_items()
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, SupplementItem)
    assert (item.content, item.message_id, item.session_id, item.source,
            item.channel, item.channel_id) == ("补充正文", "m9", "s9", "electron", "electron", "cid9")

    # 旧位置参数调用仍可用；程序注入默认 channel="inherit"
    enqueue_supplement("程序注入")
    items = drain_supplement_items()
    assert [i.content for i in items] == ["程序注入"]
    assert items[0].channel == "inherit" and items[0].message_id is None

    # drain_supplements 仍返回 str
    enqueue_supplement("纯文本补充")
    assert drain_supplements() == ["纯文本补充"]


def test_format_matches_drain_supplement():
    enqueue_supplement("甲")
    enqueue_supplement("乙")
    items = drain_supplement_items()
    merged = format_supplement_text([i.content for i in items])

    # 同两条 item 放回后，drain_supplement() 必须是同一合并口径（P3-3 同源）
    requeue_supplement_items(items)
    assert drain_supplement() == merged == "[补充] 甲\n[补充] 乙"


def test_build_command_excludes_persisted_rows():
    from niu_api.main_agent_supplement_followup import build_followup_command

    items = [
        SupplementItem(
            content="补充一", message_id="m1", session_id="s9",
            source="electron", channel="electron", channel_id="cid9",
        ),
        SupplementItem(content="补充二", message_id=None, session_id="s2"),
    ]
    cmd = build_followup_command(items, _parent(), on_complete=lambda outcome: None)

    assert cmd.content == format_supplement_text(["补充一", "补充二"]) == "[补充] 补充一\n[补充] 补充二"
    assert cmd.exclude_message_ids == ["m1"], "只排除已落库的 user 行"
    assert cmd.session_id == "s9", "会话取自首条 item"
    assert cmd.source == "electron" and cmd.channel == "electron" and cmd.channel_id == "cid9"
    assert cmd.stream is False and cmd.resources is None


def test_group_items_preserves_first_seen_order():
    from niu_api.main_agent_supplement_followup import group_items

    items = [
        SupplementItem(content="注入一", session_id="s1", source="user", channel="inherit"),
        SupplementItem(
            content="桌面一", message_id="m1", session_id="s1",
            source="electron", channel="electron",
        ),
        SupplementItem(content="注入二", session_id="s1", source="user", channel="inherit"),
        SupplementItem(
            content="桌面二", message_id="m2", session_id="s2",
            source="electron", channel="electron",
        ),
    ]
    groups = group_items(items)
    assert [[i.content for i in group] for group in groups] == [
        ["注入一", "注入二"], ["桌面一"], ["桌面二"],
    ], "必须按 (session, source, channel, channel_id) 分组且保持首次出现顺序"


# ---------------------------------------------------------------------------
# 2. 直驱 submit_followup_run
# ---------------------------------------------------------------------------


async def test_submit_followup_persists_via_make_on_complete(monkeypatch):
    from niu_api import chat as chat_mod
    from niu_api import main_agent_supplement_followup as followup_mod

    store = _StubStore()
    runner = _ImRunner(push=True, channel="cid-x")

    async def _get_store():
        return store

    monkeypatch.setattr("agent.session.get_message_store", _get_store)
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: runner)
    monkeypatch.setattr(chat_mod, "_main_loop", asyncio.get_running_loop())
    monkeypatch.setattr(chat_mod, "notify_new_message", _async_noop)

    pushes = []

    async def _push(runner_arg, reply, snapshot=None):
        pushes.append((runner_arg, reply, dict(snapshot or {})))
        return True

    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", _push)

    submitted = []

    class _Runtime:
        def submit(self, cmd):
            submitted.append(cmd)
            return object()

    items = _items(message_id="m1")
    await followup_mod.submit_followup_run(_Runtime(), items, _parent())

    assert len(submitted) == 1, "submit_followup_run 必须提交一条新命令"
    cmd = submitted[0]
    assert cmd.content == "末轮补充"
    assert cmd.exclude_message_ids == ["m1"]

    # on_complete 由 runtime 线程调用：用 worker 线程模拟，避免在主循环上自锁
    await asyncio.to_thread(cmd.on_complete, RunOutcome(result="顺延回复"))
    await asyncio.sleep(0.05)

    assert store.added, "真实 make_on_complete 必须落库回复"
    assert store.added[0]["role"] == "assistant"
    assert store.added[0]["content"] == "顺延回复"

    assert pushes, "IM 来源必须自行调度 push_im_reply"
    assert pushes[0][1] == "顺延回复"
    snapshot = pushes[0][2]
    assert snapshot["should_push_im"] is True
    assert snapshot["im_channel_id"] == "cid-x"
    assert snapshot["reply"] == "顺延回复"


async def test_submit_followup_run_submits_one_command_per_channel_group(monkeypatch):
    """P3-3：不同通道/会话的残留项不得共用首条元数据——每组独立一条命令。"""
    from niu_api import chat as chat_mod
    from niu_api import main_agent_supplement_followup as followup_mod

    store = _StubStore()

    async def _get_store():
        return store

    monkeypatch.setattr("agent.session.get_message_store", _get_store)
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: _ImRunner(push=False, channel=""))

    submitted = []

    class _Runtime:
        def submit(self, cmd):
            submitted.append(cmd)
            return object()

    items = [
        SupplementItem(content="程序注入", session_id="s1", source="user", channel="inherit"),
        SupplementItem(
            content="桌面补充", message_id="m1", session_id="s1",
            source="electron", channel="electron",
        ),
        SupplementItem(content="注入二", session_id="s1", source="user", channel="inherit"),
    ]
    await followup_mod.submit_followup_run(_Runtime(), items, _parent())

    assert len(submitted) == 2, "inherit 组与 electron 组必须各自提交一条命令"
    assert submitted[0].channel == "inherit" and submitted[0].exclude_message_ids == []
    assert submitted[0].content == "[补充] 程序注入\n[补充] 注入二"
    assert submitted[1].channel == "electron" and submitted[1].exclude_message_ids == ["m1"]
    assert submitted[1].content == "桌面补充"


async def test_submit_failure_writes_neutral_reply(monkeypatch):
    from niu_api import chat as chat_mod
    from niu_api import main_agent_persist as persist_mod
    from niu_api import main_agent_supplement_followup as followup_mod

    store = _StubStore()

    async def _get_store():
        return store

    monkeypatch.setattr("agent.session.get_message_store", _get_store)
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: _ImRunner())

    recorded = {}

    async def _persist(store_arg, outcome, holder):
        recorded["store"] = store_arg
        recorded["outcome"] = outcome
        recorded["holder"] = holder

    monkeypatch.setattr(persist_mod, "persist_run_outcome", _persist)

    class _DeadRuntime:
        def submit(self, cmd):
            raise RuntimeError("main runtime is stopped")

    await followup_mod.submit_followup_run(_DeadRuntime(), _items(), _parent())

    assert recorded, "submit 失败必须落中性行（不留孤儿 user 行）"
    assert recorded["store"] is store
    assert recorded["outcome"].neutral_degrade is True
    assert isinstance(recorded["outcome"].error, RuntimeError)


async def test_setup_failure_requeues_items(monkeypatch):
    from niu_api import main_agent_supplement_followup as followup_mod

    async def _boom_store():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("agent.session.get_message_store", _boom_store)

    class _Runtime:
        def submit(self, cmd):  # pragma: no cover — setup 失败不得走到 submit
            raise AssertionError("setup 失败不得提交命令")

    await followup_mod.submit_followup_run(_Runtime(), _items(message_id=None), _parent())

    requeued = drain_supplement_items()
    assert len(requeued) == 1, "setup 失败必须原样重入队（不得静默丢消息）"
    assert requeued[0].content == "末轮补充"


# ---------------------------------------------------------------------------
# 3. runtime 尾部 flush
# ---------------------------------------------------------------------------


async def test_tail_leftover_schedules_followup(runtime_env, monkeypatch):
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append((runtime, list(items), parent))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)

    runner = FakeRunner()
    orig_chat = runner.chat

    def chat(session_id, user_input, stream=True, **kwargs):
        enqueue_supplement(
            "末轮补充", message_id="m1", session_id="s1",
            source="electron", channel="electron",
        )
        yield from orig_chat(session_id, user_input, stream, **kwargs)

    runner.chat = chat

    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="主任务"))
        assert h.wait(2) == "chunk:主任务"
        runtime_env.pump()
    finally:
        rt.stop()

    assert len(calls) == 1, "run 结束时残留的补充消息必须顺延成 follow-up run"
    _, items, parent = calls[0]
    assert [i.message_id for i in items] == ["m1"]
    assert parent.content == "主任务"
    assert drain_supplement_items() == []


async def test_followup_history_excludes_user_row(runtime_env, monkeypatch):
    from niu_api import chat as chat_mod
    from niu_api import main_agent_persist as persist_mod

    runner = FakeRunner()
    runner.should_push_im = lambda: False
    runner.get_im_channel = lambda: ""
    orig_chat = runner.chat
    state = {"enqueued": False}

    def chat(session_id, user_input, stream=True, **kwargs):
        if not state["enqueued"]:
            state["enqueued"] = True
            enqueue_supplement(
                "补充内容", message_id="m1", session_id="s",
                source="electron", channel="electron",
            )
        yield from orig_chat(session_id, user_input, stream, **kwargs)

    runner.chat = chat

    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: runner)
    monkeypatch.setattr(
        persist_mod, "make_on_complete",
        lambda store, holder, on_overflow=None: (lambda outcome: None),
    )

    async def _push(runner_arg, reply, snapshot=None):
        return True

    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", _push)

    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="主任务"))
        assert h.wait(2) == "chunk:主任务"
        deadline = time.time() + 5
        while time.time() < deadline:
            runtime_env.pump(1)
            if len(runtime_env.context_manager.calls) >= 2 and not rt.is_busy():
                break
            time.sleep(0.01)
    finally:
        rt.stop()

    calls = runtime_env.context_manager.calls
    assert len(calls) >= 2, f"follow-up run 必须真的执行：{calls}"
    assert calls[-1]["exclude_ids"] == {"m1"}, "follow-up 历史必须排除已落库 user 行（P2-3）"


async def test_tail_followup_end_to_end_persists_assistant_reply(runtime_env, monkeypatch):
    """P3-4：真实 runtime + 真实 submit_followup_run/make_on_complete（不 mock persist）。

    run 中 enqueue 带 message_id 的补充 → 尾部 flush 顺延 → 第二个 run 真实执行 →
    on_complete 把 follow-up 回复写进 store（role="assistant"），队列收口（P3-6）。
    """
    from niu_api import chat as chat_mod

    store = _StubStore()

    async def _get_store():
        return store

    monkeypatch.setattr("agent.session.get_message_store", _get_store)
    monkeypatch.setattr(chat_mod, "notify_new_message", _async_noop)
    monkeypatch.setattr(chat_mod, "notify_llm_error_sync", lambda *a: None)

    async def _push(runner_arg, reply, snapshot=None):
        return True

    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", _push)

    class _E2ERunner(FakeRunner):
        """第一轮先入队补充，第二轮给出顺延回复。"""

        def __init__(self):
            super().__init__()
            self.should_push_im = lambda: False
            self.get_im_channel = lambda: ""

        def chat(self, session_id, user_input, stream=True, **kwargs):
            self.last_return_value = None
            self.calls.append(user_input)
            if user_input == "主任务":
                enqueue_supplement(
                    "末轮补充", message_id="m1", session_id="s1",
                    source="electron", channel="electron",
                )
                yield "父回合回复"
            else:
                yield "顺延回复"
            self.last_return_value = {"result": "CURRENT_TASK_DONE"}

    runner = _E2ERunner()
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: runner)

    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="主任务"))
        assert h.wait(2) == "父回合回复"
        deadline = time.time() + 5
        while time.time() < deadline:
            runtime_env.pump(1)
            if any(row["role"] == "assistant" for row in store.added) and not rt.is_busy():
                break
            time.sleep(0.01)
    finally:
        rt.stop()

    assert runner.calls == ["主任务", "末轮补充"], f"顺延 run 必须真的执行：{runner.calls}"
    assistant_rows = [row for row in store.added if row["role"] == "assistant"]
    assert assistant_rows, f"follow-up 回复必须落库（真实 make_on_complete）：{store.added}"
    assert assistant_rows[-1]["content"] == "顺延回复"
    assert runtime_env.context_manager.calls[-1]["exclude_ids"] == {"m1"}
    assert drain_supplement_items() == [], "队列必须收口（无跨轮泄漏）"


async def test_tail_leftover_flushes_on_lock_failure(runtime_env, monkeypatch):
    """P1-1：锁获取失败也要 flush——旧 _execute.finally 会整段跳过（与 pumped 无关）。"""
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append(list(items))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)

    rt = MainAgentRuntime(runner=FakeRunner())
    monkeypatch.setattr(rt, "_acquire_chat_lock", lambda loop: (False, TimeoutError("lock busy")))

    enqueue_supplement(
        "锁争用时的补充", message_id="m2", session_id="s",
        source="electron", channel="electron",
    )
    rt.start()
    try:
        h = rt.submit(RunCommand(content="锁争用"))
        assert h.wait(2) is None
        assert "lock busy" in (h.error or "")
        runtime_env.pump()
    finally:
        rt.stop()

    assert len(calls) == 1 and calls[0][0].message_id == "m2"


async def test_flush_exception_requeues_drained_items(runtime_env, monkeypatch):
    """P3-1：drain 成功后 flush 抛错（如主循环查询失败）必须原样重入队，不得静默丢。"""
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append(list(items))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)

    rt = MainAgentRuntime(runner=FakeRunner())

    def _boom():
        raise RuntimeError("loop lookup exploded")

    monkeypatch.setattr(rt, "_get_main_loop", _boom)

    enqueue_supplement(
        "flush 抛错时的补充", message_id="m6", session_id="s",
        source="electron", channel="electron",
    )
    rt.start()
    try:
        h = rt.submit(RunCommand(content="主任务"))
        assert h.wait(2) is None
        assert "loop lookup exploded" in (h.error or "")
        runtime_env.pump()
    finally:
        rt.stop()

    assert calls == [], "flush 抛错不得调度 follow-up"
    requeued = drain_supplement_items()
    assert [i.message_id for i in requeued] == ["m6"], "已 drain 的项必须原样重入队（P3-1）"


async def test_tail_leftover_dropped_on_stop(runtime_env, monkeypatch, caplog):
    """P3-1：用户主动停止 → 不调度 follow-up，丢弃必须 fail-loud。"""
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append(list(items))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)
    caplog.set_level(logging.WARNING, logger="agent.main_runtime")

    runner = BlockingRunner()
    rt = MainAgentRuntime(runner=runner)
    rt.start()
    try:
        h = rt.submit(RunCommand(content="长任务"))
        assert runner.started.wait(2)
        enqueue_supplement("停止期间的补充", message_id="m3", session_id="s")
        h.cancel()
        runner.release.set()
        assert h.wait(2) is None
        assert h.error == "cancelled"
        runtime_env.pump()
    finally:
        rt.stop()

    assert calls == [], "停止后不得自动复活 follow-up"
    assert "dropped after stop" in caplog.text
    assert drain_supplement_items() == []


async def test_latch_early_exit_drops_supplement(runtime_env, monkeypatch, caplog):
    """P3-3：latch 早退（命令未通过 latch 检查）也走丢弃 + warning。"""
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append(list(items))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)
    caplog.set_level(logging.WARNING, logger="agent.main_runtime")

    gate = threading.Event()

    def _blocked_lock(loop):
        gate.wait(2)
        return True, None

    runner = FakeRunner()
    rt = MainAgentRuntime(runner=runner)
    monkeypatch.setattr(rt, "_acquire_chat_lock", _blocked_lock)

    enqueue_supplement("latch 残留", message_id="m4", session_id="s")
    rt.start()
    try:
        h = rt.submit(RunCommand(content="latch"))
        h.cancel()  # 命令仍卡在 _acquire_chat_lock，尚未过 latch 检查
        gate.set()
        assert h.wait(2) is None
        assert h.error == "cancelled"
        runtime_env.pump()
    finally:
        rt.stop()

    assert runner.calls == [], "latch 早退不得执行 runner.chat"
    assert calls == [], "latch 早退后不得调度 follow-up"
    assert "dropped after stop" in caplog.text


async def test_first_frame_error_still_schedules_followup(runtime_env, monkeypatch):
    """P3-3：首帧抛错（异常终态）仍须顺延——用户消息必须被回答（R10）。"""
    from niu_api import main_agent_supplement_followup as followup_mod

    calls = []

    async def _fake_submit(runtime, items, parent):
        calls.append(list(items))

    monkeypatch.setattr(followup_mod, "submit_followup_run", _fake_submit)

    class _BoomRunner:
        last_return_value = None

        def chat(self, *args, **kwargs):
            raise RuntimeError("boom first frame")
            yield  # pragma: no cover

    rt = MainAgentRuntime(runner=_BoomRunner())
    enqueue_supplement(
        "异常终态的补充", message_id="m5", session_id="s",
        source="electron", channel="electron",
    )
    rt.start()
    try:
        h = rt.submit(RunCommand(content="首帧炸"))
        assert h.wait(2) is None
        assert "boom first frame" in (h.error or "")
        runtime_env.pump()
    finally:
        rt.stop()

    assert len(calls) == 1 and calls[0][0].message_id == "m5"
