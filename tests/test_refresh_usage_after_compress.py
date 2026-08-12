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
