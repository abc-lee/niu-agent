"""飞书通道修复测试"""
import asyncio
import pytest
from unittest.mock import MagicMock, Mock, patch
from niu_api.channel import ChannelRouter


def _create_msg(content="你好", chat_id="chat_123", sender_id="user_001",
                chat_type="p2p", resources=None, raw_content_type="text"):
    """创建模拟的飞书 InboundMessage"""
    msg = Mock()
    msg.content_text = content
    msg.chat_id = chat_id
    msg.sender_id = sender_id
    msg.chat_type = chat_type
    msg.resources = resources or []
    msg.raw_content_type = raw_content_type
    msg.raw = {}
    return msg


def _create_adapter():
    """创建 FeishuChannelAdapter 实例（跳过 __init__）"""
    from niu_api.channel.feishu_channel import FeishuChannelAdapter
    adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
    adapter.channel = MagicMock()
    adapter.channel.on = MagicMock()
    adapter.channel.schedule = MagicMock()
    adapter.channel.is_ready = True
    adapter.router = ChannelRouter()
    adapter._user_p2p_chat_id = None
    adapter._user_open_id = None
    adapter._prefs_path = Mock()
    adapter._feishu_prefs = {}
    return adapter


class TestImageMessageNotSkipped:
    """Task 1: 纯图片/文件消息不应被丢弃"""

    def test_image_message_not_skipped(self):
        """纯图片消息（content 为空但有 resources）不应被丢弃"""
        adapter = _create_adapter()
        msg = _create_msg(
            content="",
            resources=[{"type": "image", "file_key": "img_001"}],
            raw_content_type="image"
        )
        with patch('threading.Thread') as mock_thread, \
             patch('asyncio.get_running_loop', return_value=Mock()):
            adapter._on_message(msg)
            # 纯图片消息不应被丢弃，应该启动线程处理
            assert mock_thread.called

    def test_empty_message_with_no_resources_skipped(self):
        """content 和 resources 都为空的消息应被跳过"""
        adapter = _create_adapter()
        msg = _create_msg(content="", resources=[])
        with patch('threading.Thread') as mock_thread:
            adapter._on_message(msg)
            # 空消息应被跳过，不应启动线程
            assert not mock_thread.called

    def test_text_message_not_skipped(self):
        """普通文字消息不应被丢弃"""
        adapter = _create_adapter()
        msg = _create_msg(content="你好")
        with patch('threading.Thread') as mock_thread, \
             patch('asyncio.get_running_loop', return_value=Mock()):
            adapter._on_message(msg)
            assert mock_thread.called
