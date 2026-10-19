"""P3 三端点提交契约测试（计划 §4.3–§4.6 / I-1 / I-2 / I-5 / I-6 / D-6 / D-8）。

覆盖：

1. **提交的 `RunCommand` 字段**：`/chat`（stream=True, channel=electron, source=user）、
   `/chat/sync`（stream=False）、compat `/chat/session`（stream=False，
   `source=="electron"` → channel=electron；`source==""` 程序化回填 → channel=inherit）；
   三端点都把本次 user 行 id 放进 `exclude_message_ids`（runtime 读历史必须排除它们）。
2. **无直接 `runner.chat`**（I-1）：`runner.chat` 只允许在 runtime 线程里被调用
   （断言调用线程名＝替身线程名），端点线程零调用。
3. **submit 失败 ⇒ 中性文案 + 中性降级行**（D-8，与 ChatQueue 等价）：三端点写完 user 行后
   submit 抛错 ⇒ 先 `persist_run_outcome(neutral_degrade)` 落中性 assistant 行（不留孤立
   user 行），再返回/下发 `NEUTRAL_REPLY`。
4. **busy 判据 = `is_round_active()`**（I-6）：回合真在跑 ⇒ ask_user 直注 / supplement 入队
   且**不提交命令**；只有 pending（`is_busy()` 真、`is_round_active()` 假）⇒ 走正常 FIFO 提交。
5. **gateway snapshot**（D-6）：compat 把 on_complete 锁内快照（should_push_im /
   im_channel_id）交给 `push_im_reply`，wait 之后不再现读 runner 标志。

全 mock：`FakeRuntime` / 手写 runtime 替身 + `main_loop_thread()` + 假 store / 假 persist ——
禁真实 LLM、禁真实 messages.db、禁真实 ~/.niu。
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from p2b_fakes import FakeRuntime, main_loop_thread

import niu_api.chat as chat_mod
import niu_api.compat as compat_mod
from niu_api.main_agent_persist import NEUTRAL_REPLY

_LLM_CFG = {
    "type": "openai",
    "apikey": "test-api-key",
    "apibase": "https://api.example.com",
    "model": "test-model",
}
_DONE_RV = {"result": "CURRENT_TASK_DONE", "data": None, "messages": []}
_FAKE_RUNTIME_THREAD = "fake-main-agent-runtime"  # p2b_fakes.FakeRuntime 的线程名


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _StubHandle:
    """`RunHandle` 替身：命令永不执行（只记录提交），wait 立即返回。"""

    def __init__(self, command):
        self.command = command
        self.id = command.id
        self._stop_latched = False

    def wait(self, timeout: float | None = None) -> str | None:
        return None

    def cancel(self) -> None:
        self._stop_latched = True


class _RecordingRuntime:
    """只记录提交的 runtime 替身（busy 判据可逐条设定）。"""

    def __init__(self, *, round_active: bool, busy: bool, submit_error: BaseException | None = None):
        self.commands: list = []
        self.handles: list = []
        self._round_active = round_active
        self._busy = busy
        self._submit_error = submit_error

    def is_round_active(self) -> bool:
        return self._round_active

    def is_busy(self) -> bool:
        return self._busy

    def submit(self, command):
        self.commands.append(command)
        if self._submit_error is not None:
            raise self._submit_error
        handle = _StubHandle(command)
        self.handles.append(handle)
        return handle


def _fake_store(msg_id: str = "user-msg-1") -> MagicMock:
    store = MagicMock()
    store.add_message = AsyncMock(return_value=msg_id)
    store.get_messages = AsyncMock(return_value=[])
    return store


def _make_runner(return_value=_DONE_RV) -> MagicMock:
    """runner.chat 记录**调用线程名**——I-1 的判据（只允许 runtime 线程调用）。"""
    runner = MagicMock()
    runner.chat_calls: list[str] = []

    def chat(*a, **kw):
        runner.chat_calls.append(threading.current_thread().name)
        return iter(["回复"])

    runner.chat = chat
    runner.last_return_value = return_value
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    runner.should_push_im.return_value = True
    runner.get_im_channel.return_value = "oc_snap"
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()
    return runner


def _patch_chat_env(monkeypatch, runner, runtime) -> AsyncMock:
    monkeypatch.setattr("niu_api.chat._load_llm_config", lambda: dict(_LLM_CFG))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr("niu_api.chat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr("niu_api.chat.notify_llm_error_sync", lambda *a: None)
    persist = AsyncMock(return_value=("msg-42", "回复"))
    monkeypatch.setattr("niu_api.chat.persist_agent_reply", persist)
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: runtime)
    return persist


def _patch_compat_env(monkeypatch, runner, runtime) -> tuple[AsyncMock, list]:
    """compat 环境；返回 (persist 替身, push_im_reply 调用记录)。"""
    monkeypatch.setattr(
        "niu_api.config.get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(api_key="test-api-key")),
    )
    monkeypatch.setattr("niu_api.compat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr("niu_api.chat.notify_llm_error_sync", lambda *a: None)
    persist = AsyncMock(return_value=("msg-42", "回复"))
    monkeypatch.setattr("niu_api.chat.persist_agent_reply", persist)
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: runtime)

    push_calls: list[tuple] = []

    async def fake_push(runner_arg, reply, snapshot=None):
        push_calls.append((runner_arg, reply, snapshot))
        return True

    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", fake_push)
    return persist, push_calls


def _frames(parts: list[str]) -> list[dict]:
    out: list[dict] = []
    for part in parts:
        for line in part.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


async def _drain(resp) -> list[str]:
    return [part async for part in resp.body_iterator]


# ---------------------------------------------------------------------------
# 1. 提交的 RunCommand 字段
# ---------------------------------------------------------------------------


async def test_chat_sse_command_fields(monkeypatch):
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="sess-1"))
            parts = await _drain(resp)
    finally:
        runtime.stop()

    assert len(runtime.commands) == 1
    cmd = runtime.commands[0]
    assert cmd.content == "你好"
    assert cmd.session_id == "sess-1"
    assert cmd.stream is True, "/chat 必须提交流式命令"
    assert cmd.source == "user" and cmd.channel == "electron"
    assert cmd.exclude_message_ids == ["user-msg-1"], "本次 user 行 id 必须排除在历史之外"
    assert cmd.on_event is not None, "SSE 必须挂 on_event sink（D-3）"
    assert cmd.on_complete is not None, "必须挂共享持久化 on_complete（D-2）"
    assert any(f.get("done") for f in _frames(parts))


async def test_chat_sync_command_fields(monkeypatch):
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="sess-2"))
    finally:
        runtime.stop()

    assert len(runtime.commands) == 1
    cmd = runtime.commands[0]
    assert cmd.stream is False, "/chat/sync 必须提交非流式命令（D-5）"
    assert cmd.source == "user" and cmd.channel == "electron"
    assert cmd.exclude_message_ids == ["user-msg-1"]
    assert resp.reply == "回复" and resp.message_id == "msg-42"


async def test_compat_session_command_fields_for_electron_source(monkeypatch):
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_compat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await compat_mod.chat_session(
                compat_mod.ChatRequest(message="你好", session_id="ignored", source="electron")
            )
    finally:
        runtime.stop()

    assert len(runtime.commands) == 1
    cmd = runtime.commands[0]
    assert cmd.stream is False, "compat /chat/session 必须提交非流式命令（D-6）"
    assert cmd.source == "electron"
    assert cmd.channel == "electron", "source==electron ⇒ channel=electron（清 IM 标志）"
    assert cmd.session_id == "default"
    assert cmd.exclude_message_ids == ["user-msg-1"]
    assert resp.reply == "回复" and resp.message_id == "msg-42"


async def test_compat_session_programmatic_backfill_uses_inherit_channel(monkeypatch):
    """D-6：`source==""`（异步子 Agent 回填）⇒ channel=inherit，既不清 channel/force 也不置 force。"""
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_compat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await compat_mod.chat_session(
                compat_mod.ChatRequest(message="回填结果", session_id="default", source="")
            )
    finally:
        runtime.stop()

    assert len(runtime.commands) == 1
    cmd = runtime.commands[0]
    assert cmd.source == ""
    assert cmd.channel == "inherit", "程序化回填必须走 inherit 分支（D-6）"
    assert cmd.stream is False
    assert resp.reply == "回复"


# ---------------------------------------------------------------------------
# 2. 无直接 runner.chat（I-1）：调用只发生在 runtime 线程
# ---------------------------------------------------------------------------


async def test_runner_chat_is_called_only_from_runtime_thread(monkeypatch):
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="s"))
            await _drain(resp)
            resp2 = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))
            assert resp2.reply == "回复"
    finally:
        runtime.stop()

    assert runner.chat_calls, "runner.chat 应被 runtime 线程调用"
    assert set(runner.chat_calls) == {_FAKE_RUNTIME_THREAD}, (
        f"端点线程不得直接 runner.chat（I-1）：{runner.chat_calls}"
    )


async def test_compat_runner_chat_is_called_only_from_runtime_thread(monkeypatch):
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _patch_compat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await compat_mod.chat_session(
                compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
            )
    finally:
        runtime.stop()

    assert resp.reply == "回复"
    assert set(runner.chat_calls) == {_FAKE_RUNTIME_THREAD}, (
        f"compat 端点线程不得直接 runner.chat（I-1）：{runner.chat_calls}"
    )
    # 规则 5：端点侧绝不改写 IM 标志
    runner.set_im_channel.assert_not_called()
    runner.set_im_force.assert_not_called()


# ---------------------------------------------------------------------------
# 3. submit 失败 ⇒ 中性文案 + 中性降级行（D-8，与 ChatQueue 等价）
# ---------------------------------------------------------------------------


async def test_chat_sync_submit_failure_returns_neutral_and_persists_degraded_row(monkeypatch):
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=False, busy=False,
                                submit_error=RuntimeError("main runtime is stopping"))
    persist = _patch_chat_env(monkeypatch, runner, runtime)

    resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))

    assert len(runtime.commands) == 1, "命令应已提交（失败在 submit 本身）"
    assert resp.reply == NEUTRAL_REPLY, f"submit 失败必须返回中性文案：{resp.reply!r}"
    assert resp.message_id == "msg-42", f"降级行必须落库（不留孤立 user 行）：{resp.message_id!r}"
    assert persist.await_count == 1, "submit 失败必须先落中性降级行（与 ChatQueue 一致）"
    args, kwargs = persist.await_args
    assert args[3] == NEUTRAL_REPLY, f"必须走 neutral_degrade 分支：{args}"
    assert kwargs["degraded_reason"] == "", f"neutral 降级不写 degraded_reason：{kwargs}"
    assert not runner.chat_calls, "submit 失败后 runner.chat 绝不被调用"


async def test_chat_sse_submit_failure_emits_neutral_error_then_done(monkeypatch):
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=False, busy=False,
                                submit_error=RuntimeError("main runtime is stopping"))
    persist = _patch_chat_env(monkeypatch, runner, runtime)

    resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="s"))
    frames = _frames(await _drain(resp))

    errors = [f for f in frames if "error" in f]
    dones = [f for f in frames if f.get("done")]
    assert len(errors) == 1 and errors[0]["error"] == NEUTRAL_REPLY, f"应下发中性 error：{frames}"
    assert len(dones) == 1, f"必须补 done 收尾（I-3）：{frames}"
    assert not [f for f in frames if "chunk" in f], "submit 失败不得有 chunk"
    assert persist.await_count == 1, "submit 失败必须先落中性降级行（不留孤立 user 行）"
    assert dones[0].get("message_id") == "msg-42", f"done 帧 message_id 应来自降级行：{dones[0]}"


async def test_compat_submit_failure_returns_neutral_and_persists_degraded_row(monkeypatch):
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=False, busy=False,
                                submit_error=RuntimeError("main runtime is stopping"))
    persist, push_calls = _patch_compat_env(monkeypatch, runner, runtime)

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
    )

    assert len(runtime.commands) == 1, "命令应已提交（失败在 submit 本身）"
    assert resp.reply == NEUTRAL_REPLY, f"submit 失败必须返回中性文案：{resp.reply!r}"
    assert resp.message_id == "msg-42", f"降级行必须落库（不留孤立 user 行）：{resp.message_id!r}"
    assert persist.await_count == 1, "submit 失败必须先落中性降级行（与 ChatQueue 一致）"
    assert not runner.chat_calls, "submit 失败后 runner.chat 绝不被调用"


async def test_endpoint_without_runtime_uses_neutral_text(monkeypatch):
    """D-8：无 runtime 单例 ⇒ 中性文案 / 忙文案，绝不顺手 `instance()` 拉起线程。"""
    runner = _make_runner()
    _patch_chat_env(monkeypatch, runner, None)
    _patch_compat_env(monkeypatch, runner, None)

    enqueued: list[str] = []
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda msg: enqueued.append(msg))

    sync_resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))
    assert sync_resp.reply == NEUTRAL_REPLY, f"/chat/sync 无 runtime 必须中性文案：{sync_resp.reply!r}"
    assert sync_resp.message_id is None
    assert not runner.chat_calls, "无 runtime 时 runner.chat 绝不被调用"

    # compat：无 runtime 且 `_chat_lock` 空闲 ⇒ 中性忙文案，不入补充队列、不落 user 行
    compat_resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
    )
    assert compat_resp.reply == "系统正忙，请稍后再试", f"compat 无 runtime 必须中性忙文案：{compat_resp.reply!r}"
    assert enqueued == [], "无 runtime 时不得入补充队列"


# ---------------------------------------------------------------------------
# 4. busy 判据 = is_round_active()（I-6）
# ---------------------------------------------------------------------------


async def test_compat_treats_held_chat_lock_as_round_active(monkeypatch):
    """I-6：无 runtime 单例时 busy 判据回退 `_chat_lock.locked()`——/compact 持锁即视为回合在跑。"""
    runner = _make_runner()
    runtime = None
    _patch_compat_env(monkeypatch, runner, runtime)

    class _HeldLock:
        def locked(self) -> bool:
            return True

    monkeypatch.setattr(compat_mod, "_chat_lock", _HeldLock())
    enqueued: list[str] = []
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda msg: enqueued.append(msg))

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="补充一句", session_id="default", source="electron")
    )

    assert resp.reply == "已收到", f"持锁即忙：必须走补充队列：{resp.reply!r}"
    assert enqueued == ["补充一句"]
    assert resp.message_id == "user-msg-1", "补充消息仍要落 user 行并回 id"
    assert not runner.chat_calls, "忙路径绝不 runner.chat"


async def test_busy_round_enqueues_supplement_without_submitting(monkeypatch):
    """回合真在跑 ⇒ supplement 入队、立即返回「已收到」，且绝不提交第二条命令。"""
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=True, busy=True)
    _patch_compat_env(monkeypatch, runner, runtime)

    enqueued: list[str] = []
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda msg: enqueued.append(msg))

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="补充一句", session_id="default", source="electron")
    )

    assert resp.reply == "已收到", f"忙时应立即返回「已收到」：{resp.reply!r}"
    assert enqueued == ["补充一句"], "忙时必须入队 supplement"
    assert runtime.commands == [], "忙时不得提交命令（I-6）"
    assert resp.message_id == "user-msg-1", "补充消息仍要落 user 行并回 id"


async def test_busy_round_routes_ask_user_answer_directly(monkeypatch):
    """忙时 ask_user 回答走 set_answer 直注（不入补充队列、不提交命令）。"""
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=True, busy=True)
    _patch_compat_env(monkeypatch, runner, runtime)

    registry = MagicMock()
    registry.set_answer.return_value = True
    enqueued: list[str] = []
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda msg: enqueued.append(msg))

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="回答内容", session_id="default", source="electron")
    )

    assert resp.reply == "已收到"
    registry.set_answer.assert_called_once_with("main-agent", "回答内容")
    assert enqueued == [], "ask_user 回答不得落补充队列"
    assert runtime.commands == [], "ask_user 直注不得提交命令"


async def test_pending_only_runtime_submits_through_fifo(monkeypatch):
    """只有 pending（is_busy 真、is_round_active 假）⇒ 走正常 FIFO 提交，不入 supplement 队列。"""
    runner = _make_runner()
    runtime = _RecordingRuntime(round_active=False, busy=True)
    _patch_compat_env(monkeypatch, runner, runtime)

    enqueued: list[str] = []
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda msg: enqueued.append(msg))

    resp = await compat_mod.chat_session(
        compat_mod.ChatRequest(message="排队消息", session_id="default", source="electron")
    )

    assert enqueued == [], "pending 不算忙：不得把消息塞进补充队列（I-6）"
    assert len(runtime.commands) == 1, "pending 时必须正常提交命令"
    assert runtime.commands[0].content == "排队消息"
    # 替身不执行命令 ⇒ holder 兜底中性文案（不是端点自己拼错误）
    assert resp.reply == NEUTRAL_REPLY


# ---------------------------------------------------------------------------
# 5. gateway snapshot（D-6）
# ---------------------------------------------------------------------------


async def test_compat_passes_holder_snapshot_to_push_im_reply(monkeypatch):
    """wait 之后只读 on_complete 锁内快照：push_im_reply 收到 holder 的 should_push_im / im_channel_id。"""
    runner = _make_runner()
    runtime = FakeRuntime(runner=runner)
    _persist, push_calls = _patch_compat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await compat_mod.chat_session(
                compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
            )
    finally:
        runtime.stop()

    assert len(push_calls) == 1, "IM 投递必须无条件发生（错误文案同样投递）"
    pushed_runner, reply, snapshot = push_calls[0]
    assert pushed_runner is runner
    assert reply == "回复"
    assert snapshot is not None, "compat 必须把 holder 快照传给 push_im_reply（D-6）"
    assert snapshot["should_push_im"] is True
    assert snapshot["im_channel_id"] == "oc_snap"
    assert resp.reply == "回复"
