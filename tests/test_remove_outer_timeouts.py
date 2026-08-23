"""Wave 2 测试：外层超时整改（方案 §6 T8，docs/superpowers/plans/2026-08-23-remove-outer-subagent-timeouts.md）。

覆盖：
- T-A 唤醒接线：electron 唤醒 / source=="" 不唤醒 / ChatQueue scheduler 不唤醒、im 唤醒
- T-B Case 2 内联直调 + None 分支 + Stop 序列
- T-C /clear 即时清除安全 mock（硬性清单防真实数据破坏）
- T-D 锁/队列等待 helper 终止性与心跳文案
- T-F 睡眠应用前复查（Mode-1 派发前 / Mode-2 应用前）
- T-G HA 推送解耦（监听循环不阻塞 / FIFO / worker 异常隔离）

全 mock、零真实 LLM、零图谱写入、messages.db 零新增。
"""
import asyncio
import json
import threading
import time
from collections import deque
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

import niu_api.compat as compat
from agent import runner as runner_module
from agent.runner import NiuRunner


@pytest.fixture(autouse=True)
def _clean_globals():
    """用例间复位模块级全局：精灵状态 / 停止标志 / 整理队列引用 / chat_lock（防跨 loop 绑定）。"""
    compat._SPIRIT_STATE = "idle"
    runner_module.clear_stop()
    compat._chat_lock = asyncio.Lock()
    yield
    compat._SPIRIT_STATE = "idle"
    runner_module.clear_stop()


class _FakeDbMsg:
    """模拟 DB Message 对象（getattr 访问 role/content/tool_calls/tool_call_id/id）。"""

    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []
        self.tool_call_id = tool_call_id


def _capture_loguru(level="INFO"):
    """loguru sink 捕获（compat 用 loguru 而非 stdlib logging，caplog 捕获不到）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


# ---------------------------------------------------------------------------
# T-A 唤醒接线（§3.4）：仅用户动作翻转 sleep→idle
# ---------------------------------------------------------------------------


async def test_ta_electron_source_wakes(monkeypatch):
    """chat_session source=="electron" → _SPIRIT_STATE 翻转为 idle（/stop 分支提前返回驱动）。"""
    monkeypatch.setattr(
        "niu_api.config.get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(api_key="k")),
    )
    compat._SPIRIT_STATE = "sleep"

    resp = await compat.chat_session(compat.ChatRequest(message="/stop", source="electron"))

    assert resp.reply == "已停止"  # /stop 分支提前返回（不进对话主流程）
    assert compat._SPIRIT_STATE == "idle"


async def test_ta_empty_source_does_not_wake(monkeypatch):
    """source==""（异步子 Agent 回填程序化流量）→ 不翻转（保持 sleep）。"""
    monkeypatch.setattr(
        "niu_api.config.get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(api_key="k")),
    )
    compat._SPIRIT_STATE = "sleep"

    resp = await compat.chat_session(compat.ChatRequest(message="/stop", source=""))

    assert resp.reply == "已停止"
    assert compat._SPIRIT_STATE == "sleep", "程序化来源不得唤醒睡眠管道"


async def test_ta_chatqueue_scheduler_no_wake():
    """ChatQueue worker：channel=="scheduler" → 不翻转；channel=="im" → 翻转（门控按 channel 判据）。"""
    from niu_api.chat_queue import ChatQueue
    from niu_api.chat_queue import ChatRequest as QueueRequest

    q = ChatQueue(runner=SimpleNamespace())
    processed = asyncio.Queue()

    async def fake_process(req):
        await processed.put(req.channel)

    q._process_with_merge = fake_process
    compat._SPIRIT_STATE = "sleep"
    await q.start()
    try:
        # 后台来源：scheduler（ha-watcher 入队同走默认 channel）→ 不唤醒
        await q._queue.put(QueueRequest(content="x", source="scheduler", channel="scheduler"))
        channel = await asyncio.wait_for(processed.get(), timeout=2.0)
        assert channel == "scheduler"
        assert compat._SPIRIT_STATE == "sleep", "scheduler 来源不得唤醒睡眠管道"

        # 用户来源：im 通道 → 唤醒
        await q._queue.put(QueueRequest(content="y", source="im", channel="im"))
        channel = await asyncio.wait_for(processed.get(), timeout=2.0)
        assert channel == "im"
        assert compat._SPIRIT_STATE == "idle"
    finally:
        await q.stop()


# ---------------------------------------------------------------------------
# T-B Case 2 内联（runner._on_context_high_usage，§3.3）
# ---------------------------------------------------------------------------


def _make_runner():
    """NiuRunner.__new__ 实例，仅赋值测试所需属性（禁真实 __init__/LLM/DB）。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner.llm_config = {}
    runner.default_model = ""
    runner._assemble_system_message = lambda *a, **k: None  # 转换块 system 重建 mock
    return runner


async def test_tb_inline_direct_call_zero_queue_dispatch(monkeypatch):
    """spy 契约：_execute_force_pipeline 被直接同步调用；零 _dispatch_to_pipeline/_pipeline_enqueue 投递。"""
    from niu_api.compat import start_pipeline_queue

    runner = _make_runner()
    execute_calls = []

    def fake_execute():
        execute_calls.append(1)
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    dispatch_calls = []
    runner._dispatch_to_pipeline = lambda *a, **k: dispatch_calls.append(a)
    enqueue_calls = []
    monkeypatch.setattr(compat, "_pipeline_enqueue", lambda *a, **k: enqueue_calls.append(a))
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("s1", "system", "system prompt"),
        _FakeDbMsg("m1", "user", "hello"),
    ]

    messages = [{"role": "system", "content": "system prompt"}]
    start_pipeline_queue()  # 队列可用也应零投递（内联化后回调不再触碰队列）
    try:
        result = runner._on_context_high_usage(messages, 180000, 200000)
    finally:
        if compat._pipeline_queue is not None:
            await compat.stop_pipeline_queue()

    assert execute_calls == [1], "压缩管道应被回调直接同步调用（Case 2 内联化）"
    assert dispatch_calls == [], "不得有任何队列投递"
    assert enqueue_calls == [], "不得有任何队列投递"
    assert result == {"status": "ok"}
    # 转换块契约：dict 列表 + system 保留在 messages[0]
    assert all(isinstance(m, dict) and "role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"


def test_tb_none_branch_empty_db_no_raise(monkeypatch):
    """None 返回分支（空 DB 消息）：转换块跳过回写、回调不抛异常、透传 None。"""
    runner = _make_runner()
    runner._execute_force_pipeline = lambda: None  # 管道 None 返回
    runner._sync_get_messages = lambda: []  # 空 DB

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    result = runner._on_context_high_usage(messages, 100, 200000)

    assert result is None
    assert messages == [  # 空 fresh_db_msgs → 不回写，原列表保持
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_tb_stop_sequence_stages_exit_conversion_block_runs(monkeypatch):
    """Stop 中断序列：is_stop_requested=True → 管道阶段检查点退出（None）；
    转换块仍执行（DB 重载 + messages[:] 回写），done 事件广播。"""
    monkeypatch.setattr(runner_module, "is_stop_requested", lambda: True)

    runner = _make_runner()

    def fake_execute():
        # 模拟管道首个阶段检查点：Stop 置位即 bare return（阶段退出）
        if runner_module.is_stop_requested():
            return None
        return {"status": "ok"}

    runner._execute_force_pipeline = fake_execute
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi"),
    ]
    broadcasts = []
    monkeypatch.setattr(
        "niu_api.chat.notify_compact_status_sync",
        lambda status, **k: broadcasts.append(status),
    )

    messages = [{"role": "system", "content": "sys"}]
    result = runner._on_context_high_usage(messages, 180000, 200000)
    assert len(messages) == 3 and all(isinstance(m, dict) for m in messages), "转换块仍应回写消息"
    assert result is None  # 阶段退出透传
    assert messages[0].get("role") == "system"  # system 保留
    assert broadcasts == ["started", "done"], "started/done 事件均须广播（防前端圆环卡死）"


# ---------------------------------------------------------------------------
# T-C /clear 即时清除（§3.6）：安全 mock 硬性清单——防真实数据破坏
# ---------------------------------------------------------------------------


async def test_tc_clear_chat_safe_mocks(monkeypatch):
    """/clear 四步各恰一次、零 tidy 投递、_SPIRIT_STATE 置 idle；全程不触真实 DB/tmp/游标。

    硬性 mock 清单（方案 §6 T8）：
    1. compat.get_message_store → fake store（clear_messages recorder）
    2. compat._reset_all_cursors → no-op recorder
    3. agent.tmp_dir.cleanup_all_tmp → no-op recorder（patch 源模块命名空间——函数级
       import，patch compat 拦不住，错目标=真实 rmtree ~/.niu/tmp）
    4. niu_api.chat.get_or_create_runner → SimpleNamespace 假 runner
    5. agent.runner.request_stop/clear_stop → spy
    """
    events = []

    class _FakeStore:
        async def clear_messages(self):
            events.append("clear_messages")
            return 5

    monkeypatch.setattr(compat, "get_message_store", AsyncMock(return_value=_FakeStore()))

    async def fake_reset_cursors():
        events.append("reset_cursors")

    monkeypatch.setattr(compat, "_reset_all_cursors", fake_reset_cursors)

    def fake_cleanup_tmp():
        events.append("cleanup_tmp")
        return 0

    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", fake_cleanup_tmp)
    monkeypatch.setattr(
        "niu_api.chat.get_or_create_runner",
        lambda: SimpleNamespace(handler=None, _decay_pool=SimpleNamespace(clear=lambda: None)),
    )
    stop_calls = []
    monkeypatch.setattr(runner_module, "request_stop", lambda: stop_calls.append("request_stop"))
    monkeypatch.setattr(runner_module, "clear_stop", lambda: stop_calls.append("clear_stop"))
    monkeypatch.setattr(runner_module, "drain_supplements", lambda: events.append("drain") or [])

    # 零 tidy 投递哨兵：投递路径一旦被触碰立即失败
    def _boom(*a, **k):
        raise AssertionError("/clear 不得投递任何整理管道")

    monkeypatch.setattr(compat, "_pipeline_enqueue", _boom)
    assert compat._pipeline_queue is None

    # 换新锁防跨用例 loop 绑定；预置睡眠验证无条件唤醒
    compat._chat_lock = asyncio.Lock()
    compat._SPIRIT_STATE = "sleep"

    result = await compat.clear_chat(request=None)  # 新实现不再读 body

    assert result["success"] is True
    assert result["deleted_count"] == 5
    # 四步各恰一次：drain_supplements → clear_messages → cleanup_tmp → reset_cursors
    assert events == ["drain", "clear_messages", "cleanup_tmp", "reset_cursors"]
    assert stop_calls.count("request_stop") == 1  # 停主 Agent 恰一次
    assert "clear_stop" in stop_calls  # 防御性清除保留
    assert compat._SPIRIT_STATE == "idle"  # 无条件唤醒睡眠管道
    assert not compat._chat_lock.locked()  # finally 释放


# ---------------------------------------------------------------------------
# T-D 锁/队列等待 helper（§3.9）：心跳文案钉死 + 终止性
# ---------------------------------------------------------------------------


async def test_td_lock_retry_heartbeat_then_success(monkeypatch):
    """锁被占 N 个 chunk 后释放 → True；心跳日志 "chat lock busy, retrying" ≥1（loguru sink 捕获）。"""
    await compat._chat_lock.acquire()  # 测试内占锁制造真实争用

    async def release_later():
        await asyncio.sleep(0.05)
        if compat._chat_lock.locked():
            compat._chat_lock.release()

    releaser = asyncio.create_task(release_later())
    monkeypatch.setattr(compat, "TIDY_WAIT_CHUNK", 0.01)

    messages, sink_id = _capture_loguru()
    try:
        ok = await compat._acquire_chat_lock_with_retry("TestPrefix")
    finally:
        logger.remove(sink_id)
        await releaser
    assert ok is True
    assert compat._chat_lock.locked()  # 成功后持有锁
    compat._chat_lock.release()  # 释放 helper 获取的锁

    heartbeats = [m for m in messages if "chat lock busy, retrying" in m]
    assert len(heartbeats) >= 1, f"应有心跳日志，实际 {messages}"
    assert "[TestPrefix]" in heartbeats[0]


async def test_td_lock_gives_up_with_max_elapsed(monkeypatch):
    """max_elapsed 注入（仅测试）+ 锁永不释放 → ~max_elapsed 返回 False（终止性）。"""
    await compat._chat_lock.acquire()
    monkeypatch.setattr(compat, "TIDY_WAIT_CHUNK", 0.01)
    try:
        t0 = time.monotonic()
        ok = await asyncio.wait_for(
            compat._acquire_chat_lock_with_retry("T", max_elapsed=0.05), timeout=5
        )
        elapsed = time.monotonic() - t0
    finally:
        compat._chat_lock.release()
    assert ok is False
    assert elapsed < 5.0, "必须在 max_elapsed 附近放弃而非无限等待"


async def test_td_queue_idle_immediate_true():
    """队列未在处理 → 立即 True，零等待零日志。"""
    q = SimpleNamespace(_processing=False, _processing_done=asyncio.Event())
    messages, sink_id = _capture_loguru()
    try:
        ok = await compat._wait_queue_idle_with_retry(q, "QP")
    finally:
        logger.remove(sink_id)
    assert ok is True
    assert not any("queue busy" in m for m in messages)


async def test_td_queue_busy_heartbeat_then_success(monkeypatch):
    """队列 busy → 分片等待 + 心跳 "queue busy, retrying"；置位后返回 True。"""
    q = SimpleNamespace(_processing=True, _processing_done=asyncio.Event())
    monkeypatch.setattr(compat, "TIDY_WAIT_CHUNK", 0.01)

    async def set_done_later():
        await asyncio.sleep(0.03)
        q._processing_done.set()

    finisher = asyncio.create_task(set_done_later())
    messages, sink_id = _capture_loguru()
    try:
        ok = await compat._wait_queue_idle_with_retry(q, "QP")
    finally:
        logger.remove(sink_id)
        await finisher
    assert ok is True
    heartbeats = [m for m in messages if "queue busy, retrying" in m]
    assert len(heartbeats) >= 1
    assert "[QP]" in heartbeats[0]


async def test_td_queue_busy_gives_up_with_max_elapsed(monkeypatch):
    """队列永不空闲 + max_elapsed 注入 → 返回 False（终止性）。"""
    q = SimpleNamespace(_processing=True, _processing_done=asyncio.Event())
    monkeypatch.setattr(compat, "TIDY_WAIT_CHUNK", 0.01)
    t0 = time.monotonic()
    ok = await asyncio.wait_for(
        compat._wait_queue_idle_with_retry(q, "QP", max_elapsed=0.05), timeout=5
    )
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# T-F 睡眠应用前复查（§3.4）：Mode-2 应用前 / Mode-1 派发前 is_sleeping()=False → interrupted
# （驱动模式仿 tests/test_pipeline_sleep_checkpoints.py，全 mock 零真实依赖）
# ---------------------------------------------------------------------------

MODE2_PLAN = "keep=1\nupdate=2|[摘要] 压缩后的第二条"


class _FakeCalc:
    def __init__(self, tokens_per_msg):
        self.tokens_per_msg = tokens_per_msg

    def count_message_single(self, role, content, tool_calls=None):
        return self.tokens_per_msg


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        return None


def _tf_messages():
    return [
        _FakeDbMsg("m1", "user", "hello 1"),
        _FakeDbMsg("m2", "assistant", "hello 2"),
    ]


def _run_sleep_tidy_tf(tokens_per_msg, sleep_side_effect, subagent_reply=None, cursor_value="m2"):
    """直接调 _tidy_context_impl sleep 分支，全 mock 驱动到压缩段。

    cursor_value="m2"：模拟 entity/dream 游标已追平（否则压缩前置校验保守不压）。
    返回 (result, store, call_mock)。
    """
    from niu_api.compat import _tidy_context_impl

    store = MagicMock()
    store.get_messages = AsyncMock(return_value=_tf_messages())
    call_mock = MagicMock()
    if subagent_reply is None:
        call_mock.return_value = json.dumps({"ok": True})
    else:
        call_mock.side_effect = lambda **kw: (
            subagent_reply if kw.get("agent_name") == "context-manager" else json.dumps({"ok": True})
        )

    patches = [
        patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc(tokens_per_msg)),
        patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        patch("niu_api.compat._read_protect_recent_count", return_value=0),
        patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        # 四个游标文件 READ 强制缺失（compat 函数内 `from pathlib import Path`，patch 类方法本身）
        patch("pathlib.Path.exists", return_value=False),
        patch("niu_api.compat._write_cursor_with_lock"),
        patch("niu_api.compat._read_cursor_value", return_value=cursor_value),
        patch("niu_api.compat._read_compress_target_tokens", return_value=1000),
        patch("niu_api.compat.is_sleeping", side_effect=sleep_side_effect),
    ]
    with ExitStack() as stack:
        stack.enter_context(patch("niu_api.compat.get_message_store", new=AsyncMock(return_value=store)))
        for p in patches:
            stack.enter_context(p)
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, store, call_mock


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def test_tf_mode2_interrupted_before_apply_zero_deletion():
    """Mode-2（usage≥50%）：方案解析成功后唤醒 → interrupted；零删除/更新执行、不碰队列锁。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 4  # CP1/CP2/CP3 仍睡眠；第 4 次 = 应用前复查 → 已被唤醒

    def _boom(*a, **k):
        raise AssertionError("interrupted 后不得触碰 ChatQueue 门禁")

    with patch("niu_api.chat_queue.get_chat_queue", _boom):
        result, store, call_mock = _run_sleep_tidy_tf(
            tokens_per_msg=2500,  # 5000/8000 = 62.5% ∈ [50%, 70%) → Mode-2 且不被 skip 阈值拦截
            sleep_side_effect=sleep_side_effect,
            subagent_reply=MODE2_PLAN,
        )

    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock)[-1] == "context-manager"  # 方案已解析（cm 已跑完）
    non_get = [c for c in store.mock_calls if c[0] != "get_messages"]
    assert non_get == [], f"零删除/更新执行，实际 store 调用 {non_get}"


def test_tf_mode1_interrupted_before_dispatch():
    """Mode-1（usage<50%）：派发 context-manager 前唤醒 → interrupted；cm 不派发。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 4

    result, store, call_mock = _run_sleep_tidy_tf(
        tokens_per_msg=100,  # 200/8000 = 2.5% → Mode-1
        sleep_side_effect=sleep_side_effect,
    )

    assert result == {"status": "interrupted", "reason": "woke_up"}
    agents = _called_agents(call_mock)
    assert "context-manager" not in agents, f"cm 不应派发，实际 {agents}"


# ---------------------------------------------------------------------------
# T-G HA 解耦（§3.10）：监听循环永不被推送阻塞 / 单 worker FIFO / worker 异常隔离
# ---------------------------------------------------------------------------


class _FakeTimeModule:
    """可控时钟：每次 .time() 前进 20s（驱动 30s ping 间隔快速到期）。"""

    def __init__(self):
        self._now = 1000.0

    def time(self):
        self._now += 20.0
        return self._now

    def sleep(self, s):  # watcher._wait_for_config_change 兜底（本测试路径不触达）
        time.sleep(min(s, 0.01))


class _FakeWS:
    """脚本化 WebSocket：auth → 订阅 ack+事件 → 之后阻塞 recv（保持循环存活）。"""

    def __init__(self):
        self.inbox = deque([{"type": "auth_required"}])
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "auth":
            self.inbox.append({"type": "auth_ok"})
        elif msg.get("type") == "subscribe_trigger":
            self.inbox.append({"id": msg["id"], "success": True})
            # 订阅确认之后到达一条触发事件（驱动一次推送）
            self.inbox.append({
                "type": "event", "id": msg["id"],
                "event": {"variables": {"trigger": {"entity_id": "lock.x"}}},
            })

    async def recv(self):
        while not self.inbox:
            await asyncio.sleep(0.01)
        return json.dumps(self.inbox.popleft())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_tg_listen_loop_not_blocked_by_push(monkeypatch):
    """推送阻塞期间监听循环继续跑：ping/配置检查持续计数；事件照常 submit。"""
    import niu_api.internal.ha_watcher.watcher as watcher_mod
    from niu_api.internal.ha_watcher.watcher import _HAWatcher

    w = _HAWatcher()
    fake_ws = _FakeWS()

    class _FakeWebsocketsModule:
        @staticmethod
        def connect(url, max_size=0):
            return fake_ws

    monkeypatch.setitem(__import__("sys").modules, "websockets", _FakeWebsocketsModule)
    monkeypatch.setattr(watcher_mod, "time", _FakeTimeModule())
    monkeypatch.setattr(
        w, "_read_config",
        lambda: {"ha_url": "http://ha", "ha_token": "t", "triggers": [{"id": "d1", "entity_id": "lock.x"}]},
    )
    monkeypatch.setattr(w, "_wait_for_config_change", lambda timeout=30: None)

    config_checks = {"n": 0}

    def fake_check_config():
        config_checks["n"] += 1
        return False

    monkeypatch.setattr(w, "_check_config_changed", fake_check_config)

    push_started = threading.Event()
    block_push = threading.Event()

    def blocking_push(description):
        push_started.set()
        block_push.wait(5.0)  # 模拟 fut.result() 无限挂起（解耦前会冻结整个监听循环）

    monkeypatch.setattr(w, "_push_to_chat", blocking_push)

    w._running = True
    task = asyncio.create_task(w._connect_and_listen())
    try:
        await asyncio.wait_for(asyncio.to_thread(push_started.wait, 3.0), timeout=5.0)
        pings_sent = [m for m in fake_ws.sent if m.get("type") == "ping"]
        assert pings_sent, "推送阻塞期间监听循环应继续发应用层 ping"
        assert config_checks["n"] >= 2, "推送阻塞期间配置变更检测应持续进行"
        assert len(fake_ws.sent) >= 2  # auth + subscribe + ping... 循环未冻结的直接证据
    finally:
        w._running = False
        task.cancel()
        block_push.set()  # 解除推送阻塞，让 worker 收敛
        w.stop()  # pool.shutdown(wait=False)


def test_tg_single_worker_fifo_order():
    """单 worker 线程池：两个事件顺序提交 → 顺序处理（FIFO 保 HA 事件顺序）。"""
    from niu_api.internal.ha_watcher.watcher import _HAWatcher

    w = _HAWatcher()
    order = []

    def slow_push(desc):
        order.append(desc)
        time.sleep(0.02)

    w._push_to_chat = slow_push
    try:
        w._submit_push("first")
        w._submit_push("second")
        w._push_pool.shutdown(wait=True)
    finally:
        pass
    assert order == ["first", "second"]


def test_tg_worker_survives_push_exception():
    """worker 内异常不杀池：第一个推送抛异常，后续事件仍被处理。"""
    from niu_api.internal.ha_watcher.watcher import _HAWatcher

    w = _HAWatcher()
    calls = []

    def flaky_push(desc):
        calls.append(desc)
        if desc == "boom":
            raise RuntimeError("push failed")

    w._push_to_chat = flaky_push
    w._submit_push("boom")
    w._submit_push("after-failure")
    w._push_pool.shutdown(wait=True)
    assert calls == ["boom", "after-failure"], "异常后常驻 worker 必须存活"
