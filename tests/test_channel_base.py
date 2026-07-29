"""Tests for channel abstraction layer: UnifiedMessage + ChannelRouter."""
from unittest.mock import AsyncMock

from niu_api.channel import ChannelRouter, get_channel_router
from niu_api.channel.base import ChannelAdapter, UnifiedMessage


# ---------------------------------------------------------------------------
# Helper: a concrete ChannelAdapter for testing
# ---------------------------------------------------------------------------
class MockChannelAdapter(ChannelAdapter):
    async def send(self, channel_id: str, content: str) -> None:
        pass

    async def push(self, channel_id: str, content: str) -> None:
        pass


def _make_mock_adapter():
    """Create a MockChannelAdapter with AsyncMock send/push for assertion."""
    adapter = MockChannelAdapter()
    adapter.send = AsyncMock()
    adapter.push = AsyncMock()
    return adapter


# ---------------------------------------------------------------------------
# 1. UnifiedMessage construction
# ---------------------------------------------------------------------------
class TestUnifiedMessage:
    def test_default_values(self):
        msg = UnifiedMessage(
            content="hello",
            channel="electron",
            channel_id="c1",
            sender_id="u1",
            message_type="text",
        )
        assert msg.content == "hello"
        assert msg.channel == "electron"
        assert msg.channel_id == "c1"
        assert msg.sender_id == "u1"
        assert msg.message_type == "text"
        assert msg.resources == []
        assert msg.raw == {}

    def test_resources_and_raw_populated(self):
        msg = UnifiedMessage(
            content="photo",
            channel="im",
            channel_id="c2",
            sender_id="u2",
            message_type="image",
            resources=["file_key_123"],
            raw={"event_id": "evt_abc"},
        )
        assert msg.resources == ["file_key_123"]
        assert msg.raw == {"event_id": "evt_abc"}

    def test_default_factory_isolation(self):
        """Two instances must not share the same list/dict object."""
        m1 = UnifiedMessage(
            content="a", channel="x", channel_id="x", sender_id="x", message_type="text"
        )
        m2 = UnifiedMessage(
            content="b", channel="y", channel_id="y", sender_id="y", message_type="text"
        )
        m1.resources.append("shared?")
        assert m2.resources == []
        m1.raw["key"] = "val"
        assert m2.raw == {}


# ---------------------------------------------------------------------------
# 2. ChannelRouter registration
# ---------------------------------------------------------------------------
class TestChannelRouterRegister:
    def test_register_and_has_channel(self):
        router = ChannelRouter()
        adapter = _make_mock_adapter()
        router.register("electron", adapter)
        assert router.has_channel("electron") is True
        assert router.has_channel("im") is False

    def test_register_multiple_channels(self):
        router = ChannelRouter()
        electron = _make_mock_adapter()
        im = _make_mock_adapter()
        router.register("electron", electron)
        router.register("im", im)
        assert router.has_channel("electron") is True
        assert router.has_channel("im") is True


# ---------------------------------------------------------------------------
# 3. ChannelRouter route_in without runner raises RuntimeError
# ---------------------------------------------------------------------------
class TestChannelRouterRouteIn:
    async def test_route_in_returns_enqueue_result(self):
        """route_in enqueues to ChatQueue and returns result message"""
        router = ChannelRouter()
        msg = UnifiedMessage(
            content="hi",
            channel="electron",
            channel_id="c1",
            sender_id="u1",
            message_type="text",
        )
        result = await router.route_in(msg)
        # route_in now returns EnqueueResult.message from ChatQueue
        assert isinstance(result, str)
        assert result != ""


# ---------------------------------------------------------------------------
# 4. ChannelRouter route_out calls adapter.send
# ---------------------------------------------------------------------------
class TestChannelRouterRouteOut:
    async def test_route_out_calls_send(self):
        router = ChannelRouter()
        adapter = _make_mock_adapter()
        router.register("electron", adapter)

        await router.route_out("reply text", "electron", "c1")
        adapter.send.assert_awaited_once_with("c1", "reply text")

    async def test_route_out_unknown_channel_no_error(self):
        """If channel not registered, route_out silently does nothing."""
        router = ChannelRouter()
        await router.route_out("reply", "im", "c1")


# ---------------------------------------------------------------------------
# 5. ChannelRouter push calls adapter.push
# ---------------------------------------------------------------------------
class TestChannelRouterPush:
    async def test_push_calls_adapter_push(self):
        router = ChannelRouter()
        adapter = _make_mock_adapter()
        router.register("im", adapter)

        await router.push("reminder text", "im", "c1")
        adapter.push.assert_awaited_once_with("c1", "reminder text")

    async def test_push_unknown_channel_no_error(self):
        router = ChannelRouter()
        await router.push("msg", "electron", "c1")


# ---------------------------------------------------------------------------
# 6. get_channel_router global singleton
# ---------------------------------------------------------------------------
class TestGetChannelRouter:
    def test_returns_same_instance(self):
        import niu_api.channel as mod
        mod._router = None
        r1 = get_channel_router()
        r2 = get_channel_router()
        assert r1 is r2

    def test_reset_produces_new_instance(self):
        import niu_api.channel as mod
        mod._router = None
        r1 = get_channel_router()
        mod._router = None
        r2 = get_channel_router()
        assert r1 is not r2
