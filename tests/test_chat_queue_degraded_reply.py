"""ChatQueue runner.chat 异常时降级回复测试
用真实 persist_agent_reply + _FakeStore 验证降级回复真的写入 DB。
不 patch persist_agent_reply（避免假测试）。
"""
import asyncio
import uuid
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from niu_api.chat_queue import ChatQueue


class _FakeStore:
    def __init__(self):
        self.messages = []
    async def add_message(self, role, content, **kwargs):
        msg_id = str(uuid.uuid4())
        self.messages.append({"id": msg_id, "role": role, "content": content})
        return msg_id


@pytest.mark.asyncio
async def test_runner_chat_exception_writes_degraded_reply_to_db():
    """runner.chat 抛异常时，降级回复 [系统繁忙，请重试] 真的写入 DB"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    # mock runner.chat 抛异常
    def _raise(*args, **kwargs):
        raise RuntimeError("LLM timeout")
    q._runner.chat = MagicMock(side_effect=_raise)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()

    # 用真实 persist_agent_reply（不 patch），验证 rv=None 走 elif 分支写入 DB
    # patch get_message_store 返回 _FakeStore
    # patch notify_new_message 避免实际 SSE 推送（只验证 DB 写入）
    # patch get_context_manager 返回 AsyncMock（避免 await MagicMock 抛 TypeError）
    with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
        with patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                result = await asyncio.wait_for(
                    q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                    timeout=5
                )

    # 验证降级回复被写入 DB
    assert result == "[系统繁忙，请重试]", f"Expected degraded reply, got {result!r}"
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, f"Expected 1 assistant message, got {len(assistant_msgs)}"
    assert assistant_msgs[0]["content"] == "[系统繁忙，请重试]"

    await q.stop()


@pytest.mark.asyncio
async def test_normal_path_unchanged():
    """正常路径不受影响——用真实 persist_agent_reply + _FakeStore 验证"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    # mock runner.chat 正常返回（生成器 yield 一条回复）
    def _ok(*args, **kwargs):
        yield "正常回复"
    q._runner.chat = MagicMock(side_effect=_ok)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()

    # 不 patch persist_agent_reply——用真实函数 + _FakeStore 验证 DB 写入
    with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
        with patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                result = await asyncio.wait_for(
                    q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                    timeout=5
                )

    assert result == "正常回复"
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "正常回复"

    await q.stop()
