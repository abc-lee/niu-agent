"""Tests for Electron and Feishu channel adapters."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from niu_api.channel.base import ChannelAdapter, UnifiedMessage
from niu_api.channel.electron_channel import ElectronChannelAdapter


# ---------------------------------------------------------------------------
# ElectronChannelAdapter tests
# ---------------------------------------------------------------------------
class TestElectronChannelAdapter:
    async def test_send_does_nothing(self):
        """Electron send is a no-op — replies already pushed via SSE."""
        adapter = ElectronChannelAdapter()
        await adapter.send("session1", "reply text")

    async def test_push_calls_notify(self):
        """Electron push delegates to notify_new_message_sync."""
        adapter = ElectronChannelAdapter()
        with patch("niu_api.chat.notify_new_message_sync") as mock_notify:
            await adapter.push("session1", "reminder text")
            mock_notify.assert_called_once()
            args = mock_notify.call_args
            assert args[0][1] == "assistant"
            assert args[0][2] == "reminder text"

    async def test_is_channel_adapter(self):
        """ElectronChannelAdapter implements ChannelAdapter."""
        adapter = ElectronChannelAdapter()
        assert isinstance(adapter, ChannelAdapter)


# ---------------------------------------------------------------------------
# FeishuChannelAdapter tests (mocked SDK)
# ---------------------------------------------------------------------------
class TestFeishuChannelAdapter:
    def _make_adapter(self):
        """Create FeishuChannelAdapter with mocked FeishuChannel SDK."""
        mock_channel = MagicMock()
        mock_channel.on = MagicMock()
        mock_channel.send = AsyncMock()
        mock_channel.connect_until_ready = AsyncMock()

        with patch("lark_oapi.channel.FeishuChannel", return_value=mock_channel):
            from niu_api.channel.feishu_channel import FeishuChannelAdapter
            from niu_api.channel import ChannelRouter

            router = ChannelRouter()
            adapter = FeishuChannelAdapter(
                app_id="cli_test",
                app_secret="test_secret",
                channel_router=router,
            )
        return adapter, mock_channel

    def test_registers_event_handlers(self):
        """FeishuChannelAdapter registers message/card/reconnect handlers."""
        adapter, mock_channel = self._make_adapter()
        assert mock_channel.on.call_count == 4
        event_names = [call.args[0] for call in mock_channel.on.call_args_list]
        assert "message" in event_names
        assert "cardAction" in event_names
        assert "reconnecting" in event_names
        assert "reconnected" in event_names

    def test_initial_p2p_chat_id_is_none(self):
        """P2P chat_id starts as None."""
        adapter, _ = self._make_adapter()
        assert adapter.user_p2p_chat_id is None

    async def test_send_calls_channel_send(self):
        """send() delegates to FeishuChannel.send with markdown format."""
        adapter, mock_channel = self._make_adapter()
        await adapter.send("chat_123", "hello")
        mock_channel.send.assert_awaited_once_with("chat_123", {"markdown": "hello"})

    async def test_push_with_explicit_chat_id(self):
        """push() with explicit chat_id sends to that chat."""
        adapter, mock_channel = self._make_adapter()
        await adapter.push("chat_456", "reminder")
        mock_channel.send.assert_awaited_once_with("chat_456", {"markdown": "reminder"})

    async def test_push_with_p2p_chat_id_fallback(self):
        """push() without chat_id falls back to stored P2P chat_id."""
        adapter, mock_channel = self._make_adapter()
        adapter._user_p2p_chat_id = "chat_p2p"
        await adapter.push(None, "reminder")
        mock_channel.send.assert_awaited_once_with("chat_p2p", {"markdown": "reminder"})

    async def test_push_with_no_chat_id_skips(self):
        """push() with no chat_id and no P2P id does nothing."""
        adapter, mock_channel = self._make_adapter()
        await adapter.push(None, "reminder")
        mock_channel.send.assert_not_awaited()

    async def test_on_message_records_p2p_chat_id(self):
        """First message sets P2P chat_id for future push."""
        adapter, mock_channel = self._make_adapter()

        mock_msg = MagicMock()
        mock_msg.content_text = "hello from feishu"
        mock_msg.chat_id = "chat_p2p_001"
        mock_msg.sender_id = "user_001"
        mock_msg.raw_content_type = "text"
        mock_msg.resources = None
        mock_msg.raw = None

        with patch.object(adapter.router, "route_in", new_callable=AsyncMock, return_value="reply"):
            await adapter._on_message(mock_msg)

        assert adapter._user_p2p_chat_id == "chat_p2p_001"

    async def test_on_message_skips_empty_content(self):
        """Empty messages are skipped."""
        adapter, mock_channel = self._make_adapter()

        mock_msg = MagicMock()
        mock_msg.content_text = ""
        mock_msg.chat_id = "chat_001"
        mock_msg.sender_id = "user_001"
        mock_msg.raw_content_type = "text"
        mock_msg.resources = None
        mock_msg.raw = None

        await adapter._on_message(mock_msg)

    async def test_is_channel_adapter(self):
        """FeishuChannelAdapter implements ChannelAdapter."""
        adapter, _ = self._make_adapter()
        assert isinstance(adapter, ChannelAdapter)