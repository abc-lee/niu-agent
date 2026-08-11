"""主 Agent ask_user 主输入框回答测试（见缝插针分支直接注入 set_answer，不走补充队列）。"""
import pytest


class _FakeRegistry:
    def __init__(self):
        self.answers = []
        self.waiting = True

    def is_waiting(self, name):
        return self.waiting

    def set_answer(self, name, answer):
        self.answers.append((name, answer))


class _FakeStore:
    def __init__(self):
        self.added = []

    async def add_message(self, role, content):
        self.added.append((role, content))
        return f"msg-{len(self.added)}"


class _FakeLock:
    def locked(self):
        return True


class _FakeLLM:
    api_key = "test-key"


class _FakeConfig:
    llm = _FakeLLM()


def _patch_common(monkeypatch, registry, store, enqueued, notify_pushed):
    """公共 mock：锁、config、消息存储、SSE、补充队列。"""
    from niu_api import compat

    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("niu_api.compat._chat_lock", _FakeLock())
    monkeypatch.setattr("niu_api.config.get_config", lambda: _FakeConfig())

    async def _get_store():
        return store

    monkeypatch.setattr("niu_api.compat.get_message_store", _get_store)

    async def _notify(msg_id, role, content, source="electron"):
        notify_pushed.append((msg_id, role, content, source))

    monkeypatch.setattr("niu_api.chat.notify_new_message", _notify)
    monkeypatch.setattr("agent.runner.enqueue_supplement", lambda m: enqueued.append(m))
    return compat


@pytest.mark.asyncio
async def test_chat_session_answer_injects_set_answer(monkeypatch):
    """见缝插针分支：main-agent 等待中 → 用户消息直接 set_answer + 持久化 + SSE + 返回已收到。"""
    registry = _FakeRegistry()
    store = _FakeStore()
    enqueued = []
    notify_pushed = []
    compat = _patch_common(monkeypatch, registry, store, enqueued, notify_pushed)

    req = compat.ChatRequest(message="答案是 42")
    res = await compat.chat_session(req)

    assert registry.answers == [("main-agent", "答案是 42")]  # 回答直接注入 do_ask_user
    assert store.added == [("user", "答案是 42")]  # 以 user 角色持久化（前端可见）
    assert notify_pushed and notify_pushed[0][1] == "user"
    assert notify_pushed[0][2] == "答案是 42"
    assert enqueued == []  # 不走补充队列
    assert res.reply == "已收到"
    assert res.message_id == "msg-1"


@pytest.mark.asyncio
async def test_chat_session_not_waiting_uses_supplement_queue(monkeypatch):
    """见缝插针分支：main-agent 未等待 → 走原有补充队列逻辑（不回归）。"""
    registry = _FakeRegistry()
    registry.waiting = False
    store = _FakeStore()
    enqueued = []
    notify_pushed = []
    compat = _patch_common(monkeypatch, registry, store, enqueued, notify_pushed)

    req = compat.ChatRequest(message="补充信息")
    res = await compat.chat_session(req)

    assert registry.answers == []  # 不注入 set_answer
    assert enqueued == ["补充信息"]  # 原有补充队列逻辑保留
    assert res.reply == "已收到"
