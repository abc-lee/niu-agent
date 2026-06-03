"""Tests for Feishu group chat features (F1-F5)."""
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from niu_api.channel.base import UnifiedMessage


# ---------------------------------------------------------------------------
# Fake InboundMessage — 模拟飞书 SDK 消息对象
# ---------------------------------------------------------------------------
class FakeInboundMessage:
    """模拟飞书 SDK 的 InboundMessage 对象，用于测试"""

    def __init__(
        self,
        message_id: str = "om_default",
        chat_type: str = "p2p",
        chat_id: str = "oc_default",
        sender_id: str = "ou_default",
        sender_name: str = "测试用户",
        text: str = "测试消息",
        mentioned_bot: bool = False,
        resources: list | None = None,
        raw: dict | None = None,
    ):
        self.id = message_id
        self.message_id = message_id
        self.content_text = text
        self.chat_type = chat_type
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.mentioned_bot = mentioned_bot  # F1: 群聊 @bot 过滤
        self.raw_content_type = "text"
        self.resources = resources or []
        self.raw = raw or {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_feishu_channel():
    """Mock FeishuChannel SDK"""
    mock_channel = MagicMock()
    mock_channel.on = MagicMock()
    mock_channel.send = AsyncMock()
    mock_channel.connect_until_ready = AsyncMock()
    mock_channel.is_ready = True
    # Mock client for cardkit API
    mock_client = MagicMock()
    mock_channel.client = mock_client
    return mock_channel


@pytest.fixture
def adapter(mock_feishu_channel):
    """创建 FeishuChannelAdapter 实例用于测试"""
    with patch("lark_oapi.channel.FeishuChannel", return_value=mock_feishu_channel):
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        from niu_api.channel import ChannelRouter

        router = ChannelRouter()
        adapter = FeishuChannelAdapter(
            app_id="cli_test",
            app_secret="test_secret",
            channel_router=router,
        )
    return adapter


@pytest.fixture
def mock_route():
    """mock route_in_sync 方法"""
    with patch("niu_api.channel.ChannelRouter.route_in_sync") as mock:
        # 返回一个有 queued 属性的对象
        result = MagicMock()
        result.queued = True
        result.message = "queued"
        mock.return_value = result
        yield mock


# ---------------------------------------------------------------------------
# F1: @Bot 过滤 — 群聊中仅 @bot 消息触发 Agent
# ---------------------------------------------------------------------------
class TestF1BotFilter:
    """F1: 群聊中仅 @bot 消息触发 Agent"""

    def test_group_message_without_mention_early_return(self, adapter, mock_route):
        """群聊非@bot消息 -> early return，不调用 route_in_sync"""
        msg = FakeInboundMessage(
            message_id="om_test1",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="大家好",
            mentioned_bot=False,
        )
        adapter._on_message(msg)
        mock_route.assert_not_called()

    def test_group_message_with_mention_proceeds(self, adapter, mock_route):
        """群聊@bot消息 -> 正常进入处理流程，调用 route_in_sync"""
        msg = FakeInboundMessage(
            message_id="om_test2",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="@bot 你好",
            mentioned_bot=True,
        )
        adapter._on_message(msg)
        mock_route.assert_called_once()

    def test_p2p_message_always_proceeds(self, adapter, mock_route):
        """单聊消息不受 @bot 过滤影响"""
        msg = FakeInboundMessage(
            message_id="om_test3",
            chat_type="p2p",
            chat_id="oc_p2p1",
            sender_id="ou_user1",
            sender_name="张三",
            text="你好",
            mentioned_bot=False,  # p2p 消息 mentioned_bot 可能为 False
        )
        adapter._on_message(msg)
        mock_route.assert_called_once()

    def test_p2p_message_without_mentioned_bot_attr(self, adapter, mock_route):
        """单聊消息即使没有 mentioned_bot 属性也正常处理"""
        msg = FakeInboundMessage(
            message_id="om_test4",
            chat_type="p2p",
            chat_id="oc_p2p1",
            sender_id="ou_user1",
            sender_name="张三",
            text="你好",
        )
        # 删除 mentioned_bot 属性模拟旧版 SDK
        if hasattr(msg, "mentioned_bot"):
            delattr(msg, "mentioned_bot")
        adapter._on_message(msg)
        mock_route.assert_called_once()

    def test_group_message_without_mentioned_bot_attr_early_return(self, adapter, mock_route):
        """群聊消息没有 mentioned_bot 属性时默认为 False，early return"""
        msg = FakeInboundMessage(
            message_id="om_test5",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="大家好",
        )
        # 删除 mentioned_bot 属性模拟旧版 SDK
        if hasattr(msg, "mentioned_bot"):
            delattr(msg, "mentioned_bot")
        adapter._on_message(msg)
        mock_route.assert_not_called()


# ---------------------------------------------------------------------------
# F2: 群聊消息注入发送者前缀 + @bot 文本清理
# ---------------------------------------------------------------------------
class TestF2GroupMessagePrefix:
    """F2: 群聊消息注入发送者前缀 + @bot 文本清理"""

    def test_group_message_has_sender_prefix(self, adapter, mock_route):
        """群聊@bot消息 → message_content 带 [群聊] sender_name: 前缀"""
        msg = FakeInboundMessage(
            message_id="om_f2_1",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="@_user_1 你好",
            mentioned_bot=True,
        )
        adapter._on_message(msg)
        # 验证 route_in_sync 被调用时的 message_override 参数
        call_args = mock_route.call_args
        message_override = call_args.kwargs.get("message_override") or call_args[1].get("message_override")
        assert "[群聊] 张三:" in message_override

    def test_group_message_at_mention_cleaned(self, adapter, mock_route):
        """群聊@bot消息 → @_user_N mention 标记被清理"""
        msg = FakeInboundMessage(
            message_id="om_f2_2",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="李四",
            text="@_user_1 帮我查一下",
            mentioned_bot=True,
        )
        adapter._on_message(msg)
        call_args = mock_route.call_args
        message_override = call_args.kwargs.get("message_override") or call_args[1].get("message_override")
        assert "@_user_1" not in message_override

    def test_p2p_message_no_prefix(self, adapter, mock_route):
        """单聊消息不添加 [群聊] 前缀"""
        msg = FakeInboundMessage(
            message_id="om_f2_3",
            chat_type="p2p",
            chat_id="oc_p2p1",
            sender_id="ou_user1",
            sender_name="张三",
            text="你好",
            mentioned_bot=False,
        )
        adapter._on_message(msg)
        call_args = mock_route.call_args
        message_override = call_args.kwargs.get("message_override") or call_args[1].get("message_override")
        assert "[群聊]" not in message_override
        assert "张三:" not in message_override


# ---------------------------------------------------------------------------
# F3: 群聊使用 reply API 回复，单聊使用 create API
# ---------------------------------------------------------------------------
class TestF3GroupReplyAPI:
    """F3: 群聊使用 reply API 回复，单聊使用 create API"""

    def test_group_sets_reply_to_id(self, adapter, mock_route):
        """群聊@bot消息 → _stream_reply_to_id 被设为 msg.message_id"""
        msg = FakeInboundMessage(
            message_id="om_reply1",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="@_user_1 你好",
            mentioned_bot=True,
        )
        adapter._on_message(msg)
        assert adapter._stream_reply_to_id == "om_reply1"

    def test_p2p_no_reply_to_id(self, adapter, mock_route):
        """单聊消息 → _stream_reply_to_id 保持 None"""
        msg = FakeInboundMessage(
            message_id="om_reply2",
            chat_type="p2p",
            chat_id="oc_p2p1",
            sender_id="ou_user1",
            sender_name="张三",
            text="你好",
            mentioned_bot=False,
        )
        adapter._on_message(msg)
        assert adapter._stream_reply_to_id is None

    def test_on_message_resets_reply_to_id(self, adapter, mock_route):
        """新消息进入时 _stream_reply_to_id 被重置"""
        # 先设置一个旧值
        adapter._stream_reply_to_id = "om_old_msg"
        # 发送新的群聊消息
        msg = FakeInboundMessage(
            message_id="om_reply3",
            chat_type="group",
            chat_id="oc_group1",
            sender_id="ou_user1",
            sender_name="张三",
            text="@_user_1 新消息",
            mentioned_bot=True,
        )
        adapter._on_message(msg)
        # 应该被设为新消息的 ID（而不是旧值）
        assert adapter._stream_reply_to_id == "om_reply3"
