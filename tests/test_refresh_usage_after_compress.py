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
