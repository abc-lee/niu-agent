"""压缩后刷新使用率：compute_context_usage_estimate 全量估算函数"""
import pytest

from niu_api import compat


class _Msg:
    def __init__(self, role, content, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


@pytest.mark.asyncio
async def test_compute_usage_estimate_uses_db_estimate(monkeypatch):
    """无真实 tokens 时用全量 messages.db 估算 → usage = total/context_window"""
    msgs = [_Msg("user", "你好" * 100), _Msg("assistant", "回复" * 100)]

    async def _fake_get_store():
        return _FakeStore(msgs)

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 1000)

    usage = await compat.compute_context_usage_estimate()
    assert usage > 0.0  # 有消息必然 >0（compute 不 clamp，usage 可合法超 1.0，勿断言上界）


@pytest.mark.asyncio
async def test_compute_usage_estimate_empty_db(monkeypatch):
    """空 messages.db → usage = 0"""

    async def _fake_get_store():
        return _FakeStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 1000)

    usage = await compat.compute_context_usage_estimate()
    assert usage == 0.0


@pytest.mark.asyncio
async def test_compute_usage_estimate_error_safe(monkeypatch):
    """异常 → 返回 None 不抛出（调用方 or 0.0 兜底）"""
    def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(compat, "get_message_store", boom)

    usage = await compat.compute_context_usage_estimate()
    assert usage is None


@pytest.mark.asyncio
async def test_compute_usage_estimate_zero_window(monkeypatch):
    """context_window ≤ 0 → None（无效配置，不伪装 0%）"""

    async def _fake_get_store():
        return _FakeStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 0)

    usage = await compat.compute_context_usage_estimate()
    assert usage is None


@pytest.mark.asyncio
async def test_compute_usage_estimate_injected_store(monkeypatch):
    """注入 store/context_window 时不重复扫描（get_stats 复用）"""
    msgs = [_Msg("user", "hi")]
    calls = []
    monkeypatch.setattr(compat, "get_message_store", lambda: calls.append("called") or _FakeStore([]))

    usage = await compat.compute_context_usage_estimate(store=_FakeStore(msgs), context_window=100)
    assert usage > 0.0
    assert calls == []  # 未调用 get_message_store（已注入）


@pytest.mark.asyncio
async def test_compute_usage_estimate_injected_messages(monkeypatch):
    """注入 messages 时不扫描 store（Task 3 传压缩后列表，避免二次全表扫描）"""
    msgs = [_Msg("user", "hi"), _Msg("assistant", "hello")]
    scanned = []

    class _ScanStore(_FakeStore):
        async def get_messages(self):
            scanned.append("scan")
            return await super().get_messages()

    async def _fake_get_store():
        return _ScanStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)

    usage = await compat.compute_context_usage_estimate(messages=msgs, context_window=1000)
    assert usage > 0.0  # 从注入列表算出
    assert scanned == []  # store.get_messages 未被调用（messages 已注入，无全表扫描）


class _FakeStore:
    def __init__(self, msgs):
        self._msgs = msgs

    async def get_messages(self):
        return self._msgs


class _FakeHandler:
    def __init__(self):
        self._last_prompt_tokens = 60000


class _FakeRunner:
    def __init__(self):
        self.handler = _FakeHandler()


def _patch_notify_env(monkeypatch, chat_mod):
    """mock runner/loop/broadcast；call_soon_threadsafe 签名 (self, fn, ev) 与真实一致"""
    fake_runner = _FakeRunner()
    monkeypatch.setattr(chat_mod, "get_runner", lambda: fake_runner)
    monkeypatch.setattr(chat_mod, "_main_loop",
                        type("L", (), {"is_closed": lambda self: False,
                                       "call_soon_threadsafe": lambda self, fn, ev: fn(ev)})())
    captured = {}
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: captured.update(ev))
    return fake_runner, captured


@pytest.mark.asyncio
async def test_notify_done_resets_prompt_tokens(monkeypatch):
    """done+reset_tokens=True：主 runner._last_prompt_tokens 置 0（旧值压缩后失效）"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("done", mode="sleep", usage=0.3, reset_tokens=True)
    assert fake_runner.handler._last_prompt_tokens == 0
    assert captured == {"type": "compact_status", "status": "done", "mode": "sleep", "usage": 0.3}


@pytest.mark.asyncio
async def test_notify_done_without_reset_keeps_tokens(monkeypatch):
    """done 但 reset_tokens=False（未实际压缩的 skip 路径）：不置 0、usage 透传"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("done", mode="sleep")
    assert fake_runner.handler._last_prompt_tokens == 60000  # 未动
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_notify_started_keeps_tokens(monkeypatch):
    """started 事件：不置 0、usage 透传为 None"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("started", mode="sleep")
    assert fake_runner.handler._last_prompt_tokens == 60000  # 未动
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_notify_done_runner_none_defensive(monkeypatch):
    """get_runner 返回 None（runner 未初始化）：reset 块静默跳过、无广播、无副作用"""
    from niu_api import chat as chat_mod

    monkeypatch.setattr(chat_mod, "get_runner", lambda: None)
    monkeypatch.setattr(chat_mod, "_main_loop", None)  # 显式：loop 未启动 → 提前 return
    broadcast_calls = []
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: broadcast_calls.append(ev))

    chat_mod.notify_compact_status_sync("done", mode="sleep", reset_tokens=True)  # 不抛
    assert broadcast_calls == []  # _main_loop None → 未广播


@pytest.mark.asyncio
async def test_notify_done_runner_raises_defensive(monkeypatch):
    """get_runner 抛异常：reset 块 try/except 静默吞掉，事件仍正常广播（usage=None）"""
    from niu_api import chat as chat_mod

    def boom():
        raise RuntimeError("runner down")

    monkeypatch.setattr(chat_mod, "get_runner", boom)
    monkeypatch.setattr(chat_mod, "_main_loop",
                        type("L", (), {"is_closed": lambda self: False,
                                       "call_soon_threadsafe": lambda self, fn, ev: fn(ev)})())
    captured = {}
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: captured.update(ev))

    chat_mod.notify_compact_status_sync("done", mode="sleep", reset_tokens=True)  # 不抛
    assert captured["status"] == "done"  # 事件仍广播，usage 透传 None
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_post_compress_usage_deleted(monkeypatch):
    """消息数减少（实际压缩）→ (True, 估算 usage)"""
    from niu_api import compat as compat_mod

    # 锁 call-site 契约：messages=after（压缩后列表）与 store 必须传入（防回归成二次全表扫描）
    calls = []

    async def fake_compute(*a, **kw):
        calls.append(kw)
        return 0.3

    # 辅助函数内部只调一次 get_messages 作为"压缩后"状态——恒返回 1 条（压缩后），
    # msgs_before=2 参数即代表压缩前计数
    msgs = [_Msg("user", "a"), _Msg("assistant", "b")]

    class _ShrunkStore(_FakeStore):
        async def get_messages(self):
            return [self._msgs[0]]

    monkeypatch.setattr(compat_mod, "compute_context_usage_estimate", fake_compute)

    async def _fake_get_store():
        return _ShrunkStore(msgs)

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)

    compressed, usage = await compat_mod._compute_post_compress_usage(store=None, msgs_before=2)
    assert compressed is True
    assert usage == 0.3
    assert calls, "compute_context_usage_estimate must be invoked on actual compression"
    assert calls[0]["messages"] == [msgs[0]]  # messages=after（压缩后列表），identity 成立
    assert calls[0]["store"]._msgs is msgs    # store 复用传入（未重新 get_message_store）


@pytest.mark.asyncio
async def test_post_compress_usage_unchanged(monkeypatch):
    """消息数不变（skip/未压缩）→ (False, None)"""
    from niu_api import compat as compat_mod

    async def _fake_get_store():
        return _FakeStore([_Msg("user", "a"), _Msg("assistant", "b")])

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)
    compressed, usage = await compat_mod._compute_post_compress_usage(store=None, msgs_before=2)
    assert compressed is False
    assert usage is None


@pytest.mark.asyncio
async def test_tidy_finally_no_reset_when_skipped(monkeypatch):
    """force+skip 未压缩：finally 调 notify done 不带 usage、不 reset_tokens"""
    from niu_api import compat as compat_mod
    from niu_api import chat as chat_mod

    calls = []
    monkeypatch.setattr(chat_mod, "notify_compact_status_sync",
                        lambda status, mode="", usage=None, reset_tokens=False:
                            calls.append((status, mode, usage, reset_tokens)))
    # 防真实 runner 创建 + dream-evolver 段 runner._ensure_session_chain 调用
    # （force 管道在 skip 判定前会跑 entity/dream/journal 三段；SUBAGENT_ERROR mock 使各段跳过推进）
    monkeypatch.setattr(chat_mod, "get_or_create_runner",
                        lambda: type("R", (), {"llm_config": {},
                                               "handler": None,
                                               "_ensure_session_chain": lambda self, max_days=10: None})())
    monkeypatch.setattr(chat_mod, "get_runner", lambda: None)
    monkeypatch.setattr("agent.subagent.call_subagent_with_auto_answer",
                        lambda *a, **kw: "SUBAGENT_ERROR: mocked")
    # 非空库（空库会提前 return "No messages to tidy"，测不到 skip 判定）
    async def _fake_get_store():
        return _FakeStore([_Msg("user", "a"), _Msg("assistant", "b")])

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)

    # force 分支读 request 的 skip_compress 键（sleep 分支不读——测试必须用 force）；
    # 管道各段对 SUBAGENT_ERROR 可能 return skipped/继续，无论哪条路径 finally 都执行
    # 且消息数不变 → done 无 usage——断言聚焦 done_calls，不依赖 result.status
    result = await compat_mod._tidy_context_impl({"mode": "force", "skip_compress": True})

    done_calls = [c for c in calls if c[0] == "done"]
    assert done_calls, f"expected done broadcast, got calls={calls} result={result}"
    assert done_calls[-1][2] is None      # 未推 usage
    assert done_calls[-1][3] is False     # 未 reset_tokens
