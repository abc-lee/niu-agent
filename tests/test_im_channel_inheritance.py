"""IM 通道继承机制测试。"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from agent.runner import NiuRunner


def _make_runner():
    runner = NiuRunner.__new__(NiuRunner)
    runner._current_channel_id = ""
    runner._im_channel_id = ""
    runner._im_force = False
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


class TestIMForceFlag:
    def test_set_get_im_force(self):
        runner = _make_runner()
        assert runner.get_im_force() is False
        runner.set_im_force(True)
        assert runner.get_im_force() is True

    def test_set_im_force_roundtrip(self):
        # setter 往返语义：True↔False 可切换（粘性转假由 Electron 入口分支负责，此处仅测 setter）
        runner = _make_runner()
        runner.set_im_force(True)
        assert runner.get_im_force() is True
        runner.set_im_force(False)
        assert runner.get_im_force() is False
        assert runner.get_im_channel() == ""


class TestChatQueueForceWiring:
    """规则 3/2 接线行为锁定：置位/清除分支必须存在（防 implementer 漏写或写反）。"""

    def test_electron_branch_clears_force(self):
        src = Path("niu_api/chat_queue.py").read_text(encoding="utf-8")
        idx = src.index('if channel == "electron":')
        seg = src[idx:idx + 150]
        assert "set_im_force(False)" in seg, "electron 分支必须清 force（规则 2 转假）"

    def test_scheduler_else_branch_sets_force(self):
        src = Path("niu_api/chat_queue.py").read_text(encoding="utf-8")
        # else 分支紧随 im 分支之后；从 else: 行锚定（固定窗口会因注释长度截断）
        idx = src.index('elif channel == "im":')
        idx_else = src.index("else:", idx)
        seg = src[idx_else:idx_else + 150]
        assert "set_im_force(True)" in seg, "else 分支必须置 force（规则 3 定时任务置位）"

    def test_compat_electron_branch_clears_force(self):
        # compat.py chat_session 的 Electron 转假入口
        src = Path("niu_api/compat.py").read_text(encoding="utf-8")
        idx = src.index('if request.source == "electron":')
        seg = src[idx:idx + 150]
        assert "set_im_force(False)" in seg, "compat electron 分支必须清 force（规则 2 转假）"


class TestShouldPushIM:
    """should_push_im() 单一判定入口——四组合真值表（用户拍板：全局只有一个 IM 推送判定）"""

    def test_channel_set_force_false(self):
        runner = _make_runner()
        runner.set_im_channel("oc_x")
        runner.set_im_force(False)
        assert runner.should_push_im() is True

    def test_channel_empty_force_true(self):
        runner = _make_runner()
        runner.set_im_channel("")
        runner.set_im_force(True)
        assert runner.should_push_im() is True

    def test_both_set(self):
        runner = _make_runner()
        runner.set_im_channel("oc_x")
        runner.set_im_force(True)
        assert runner.should_push_im() is True

    def test_both_empty(self):
        runner = _make_runner()
        runner.set_im_channel("")
        runner.set_im_force(False)
        assert runner.should_push_im() is False

    def test_returns_bool_not_string(self):
        # 必须返回 bool——_im_channel_id 是 str，直接 or 返回会透出 str 类型
        runner = _make_runner()
        runner.set_im_channel("oc_x")
        assert isinstance(runner.should_push_im(), bool)
