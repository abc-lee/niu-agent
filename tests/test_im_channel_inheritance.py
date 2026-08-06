"""IM 通道继承机制测试。"""
import pytest
from unittest.mock import MagicMock, patch
from agent.runner import NiuRunner


def _make_runner():
    runner = NiuRunner.__new__(NiuRunner)
    runner._current_channel_id = ""
    runner._im_channel_id = ""
    runner._first_turn_extra_injection = ""
    runner.last_return_value = None
    runner._persisted_msgs = []
    runner.handler = MagicMock()
    runner.client = MagicMock()
    runner.disk_engine = MagicMock()
    runner.disk_engine.get_schema.return_value = {"type": "function", "function": {"name": "disk", "parameters": {"type": "object", "properties": {}}}}
    runner.base_tools_schema = []
    runner.default_model = "test"
    runner._refresh_base_tools_schema_if_dirty = MagicMock()
    runner._assemble_system_message = MagicMock()
    return runner


class TestSetGetIMChannel:
    def test_set_im_channel_records_channel_id(self):
        runner = _make_runner()
        runner.set_im_channel("im_chat_123")
        assert runner.get_im_channel() == "im_chat_123"

    def test_set_im_channel_clears_with_empty_string(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        runner.set_im_channel("")
        assert runner.get_im_channel() == ""

    def test_get_im_channel_returns_empty_by_default(self):
        runner = _make_runner()
        assert runner.get_im_channel() == ""


class TestChatInheritance:
    def test_chat_with_im_channel_id_sets_im_channel_id(self):
        runner = _make_runner()
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id="im_chat_123"))
        assert runner._im_channel_id == "im_chat_123"

    def test_chat_with_empty_channel_id_inherits_im_channel_id(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id=""))
        assert runner._im_channel_id == "im_chat_123"

    def test_chat_with_empty_channel_id_and_empty_im_channel_id_stays_empty(self):
        runner = _make_runner()
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "test", channel_id=""))
        assert runner._im_channel_id == ""

    def test_im_channel_id_survives_across_multiple_chat_calls(self):
        runner = _make_runner()
        runner._im_channel_id = "im_chat_123"
        with patch("agent.runner.agent_runner_loop", return_value=iter([])):
            list(runner.chat("default", "first", channel_id=""))
            list(runner.chat("default", "second", channel_id=""))
        assert runner._im_channel_id == "im_chat_123"
