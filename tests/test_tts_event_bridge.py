"""tts_event 事件桥测试：事件形状（含 source）与 final 标志。"""
import asyncio
import pytest


def test_tts_event_shape(monkeypatch):
    import niu_api.chat as chat
    q = asyncio.Queue()
    monkeypatch.setattr(chat, "_event_subscribers", [q])
    async def run():
        ok = await chat.notify_tts_event("m1", "assistant", "hello", final=True)
        return ok, await q.get()
    ok, event = asyncio.run(run())
    assert ok is True
    assert event == {"type": "tts_event", "id": "m1", "role": "assistant", "content": "hello", "final": True, "source": "electron"}
