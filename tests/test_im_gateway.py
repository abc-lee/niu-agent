"""IM Gateway 单元测试"""
import asyncio
import json

import pytest


def _encode(msg: dict) -> bytes:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


async def _read_one(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict | None:
    try:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = int.from_bytes(header, "big")
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(data.decode("utf-8"))
    except (TimeoutError, asyncio.IncompleteReadError):
        return None


@pytest.mark.asyncio
async def test_gateway_accepts_connection():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert not reader.at_eof()
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_dispatches_msg():
    from niu_api.channel.gateway import IMGateway
    received = []
    class FakeRouter:
        def route_in_sync(self, message, session_id=None, message_override=None):
            received.append({"session_id": session_id, "content": message_override, "channel": message.channel})
    gw = IMGateway(channel_router=FakeRouter(), port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_encode({"type": "MSG", "session_id": "im:123", "content": "hello",
                              "channel_id": "ch1", "sender_id": "u1", "is_group": False, "reply_to_id": None}))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0]["session_id"] == "im:123"
        assert received[0]["channel"] == "im"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_send():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.sleep(0.1)
        await gw.send("ch1", "reply text")
        cmd = await _read_one(reader)
        assert cmd["type"] == "SEND"
        assert cmd["channel_id"] == "ch1"
        assert cmd["content"] == "reply text"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_ready_sets_push_target():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_encode({"type": "READY", "adapter": "test", "push_target": "oc_target"}))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert gw.push_target == "oc_target"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_on_msg_intercepts_main_agent_answer(monkeypatch):
    """R2：main-agent ask_user 等待中且 channel_id 匹配 → IM 回答直接 set_answer + 持久化 + 不 route_in_sync。"""
    from niu_api.channel.gateway import IMGateway
    from agent.ask_user import get_user_ask_registry

    registry = get_user_ask_registry()
    registry.unregister("main-agent")
    future = registry.register("main-agent")
    try:
        routed = []
        persisted = []
        pushed = []

        class FakeRunner:
            _current_channel_id = "ch1"  # 与消息 channel_id 匹配

        class FakeRouter:
            def route_in_sync(self, message, session_id=None, message_override=None):
                routed.append((session_id, message_override))

        class FakeStore:
            async def add_message(self, role, content):
                persisted.append((role, content))
                return "im-msg-1"

        async def _get_store():
            return FakeStore()

        async def _notify(msg_id, role, content, source="electron"):
            pushed.append((msg_id, role, content, source))

        monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
        monkeypatch.setattr("agent.runner.get_runner", lambda: FakeRunner())
        monkeypatch.setattr("agent.session.get_message_store", _get_store)
        monkeypatch.setattr("niu_api.chat.notify_new_message", _notify)

        gw = IMGateway(channel_router=FakeRouter(), port=0)
        await gw._on_msg({"type": "MSG", "session_id": "im:123", "content": "答案是 42",
                          "channel_id": "ch1", "sender_id": "u1", "is_group": False, "reply_to_id": None})

        assert future.wait(timeout=1) == "答案是 42"  # set_answer 注入成功
        assert not registry.is_waiting("main-agent")
        assert routed == []  # 不 route_in_sync（不 enqueue）
        assert persisted == [("user", "答案是 42")]  # 持久化 user 消息（对话历史可见）
        assert pushed and pushed[0][2] == "答案是 42"  # SSE 推送
    finally:
        registry.unregister("main-agent")


@pytest.mark.asyncio
async def test_on_msg_other_channel_not_intercepted(monkeypatch):
    """P1：channel_id 与 runner._current_channel_id 不匹配 → 不注入，落入原 route_in_sync（多会话隔离）。"""
    from niu_api.channel.gateway import IMGateway
    from agent.ask_user import get_user_ask_registry

    registry = get_user_ask_registry()
    registry.unregister("main-agent")
    future = registry.register("main-agent")
    try:
        routed = []

        class FakeRunner:
            _current_channel_id = "ch1"  # 当前会话通道

        class FakeRouter:
            def route_in_sync(self, message, session_id=None, message_override=None):
                routed.append((session_id, message_override))

        monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
        monkeypatch.setattr("agent.runner.get_runner", lambda: FakeRunner())

        gw = IMGateway(channel_router=FakeRouter(), port=0)
        await gw._on_msg({"type": "MSG", "session_id": "im:999", "content": "别的会话回答",
                          "channel_id": "ch2", "sender_id": "u9", "is_group": False, "reply_to_id": None})

        assert future.wait(timeout=0.05) is None  # 未被注入（future 无回答）
        assert registry.is_waiting("main-agent")  # 等待未被劫持
        assert routed and routed[0][0] == "im:999"  # 落入原 route_in_sync
    finally:
        registry.unregister("main-agent")


@pytest.mark.asyncio
async def test_on_msg_normal_route_when_not_waiting(monkeypatch):
    """R2：main-agent 未等待 → 走原 route_in_sync（不回归）。"""
    from niu_api.channel.gateway import IMGateway
    from agent.ask_user import get_user_ask_registry

    registry = get_user_ask_registry()
    registry.unregister("main-agent")
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)

    routed = []

    class FakeRouter:
        def route_in_sync(self, message, session_id=None, message_override=None):
            routed.append((session_id, message_override, message.channel))

    gw = IMGateway(channel_router=FakeRouter(), port=0)
    await gw._on_msg({"type": "MSG", "session_id": "im:123", "content": "hello",
                      "channel_id": "ch1", "sender_id": "u1", "is_group": False, "reply_to_id": None})
    assert routed and routed[0][0] == "im:123" and routed[0][2] == "im"
    assert not registry.is_waiting("main-agent")
