"""P2-1: 测试工具重复调用检测"""
import sys

import pytest

sys.path.insert(0, "E:/tools/ai-bot")

from agent.handler import NiuHandler


@pytest.mark.p2
class TestToolDuplicateDetection:
    """测试工具重复调用检测功能"""

    @pytest.fixture
    def handler(self):
        """创建 NiuHandler 实例"""
        return NiuHandler(mcp_client=None)

    def test_recent_tool_calls_initialization(self, handler):
        """测试工具调用历史初始化"""
        assert hasattr(handler, '_recent_tool_calls')
        assert isinstance(handler._recent_tool_calls, list)
        assert len(handler._recent_tool_calls) == 0

    def test_track_tool_calls(self, handler):
        """测试工具调用追踪"""
        # 模拟工具调用
        handler.tool_after_callback("tool1", {"arg": "value1"}, None, {"status": "success"})
        assert len(handler._recent_tool_calls) == 1
        assert handler._recent_tool_calls[0][0] == "tool1"

        # 再次调用
        handler.tool_after_callback("tool2", {"arg": "value2"}, None, {"status": "success"})
        assert len(handler._recent_tool_calls) == 2

    def test_keep_only_recent_10_calls(self, handler):
        """测试只保留最近 10 次调用"""
        # 模拟 15 次工具调用
        for i in range(15):
            handler.tool_after_callback(f"tool{i}", {"arg": f"value{i}"}, None, {"status": "success"})

        # 验证只保留 10 条
        assert len(handler._recent_tool_calls) == 10
        # 验证保留的是最近的 10 条
        assert handler._recent_tool_calls[0][0] == "tool5"  # 第 6 次调用
        assert handler._recent_tool_calls[-1][0] == "tool14"  # 第 15 次调用

    def test_detect_repeated_calls(self, handler):
        """测试检测重复调用"""
        # 添加 3 次相同的工具调用
        for _i in range(3):
            handler.tool_after_callback("same_tool", {"arg": "same_value"}, None, {"status": "error"})

        # 更新轮数
        handler.current_turn = 4

        # 调用 next_prompt_patcher
        next_prompt = "Continue with next step"
        result = handler.next_prompt_patcher(next_prompt, None, turn=4)

        # 验证检测到重复调用
        assert "⚠️" in result or "警告" in result
        assert "重复工具调用" in result or "same_tool" in result
        assert "建议行动" in result

    def test_no_warning_for_different_tools(self, handler):
        """测试不同工具调用不触发警告"""
        # 添加 3 次不同的工具调用
        handler.tool_after_callback("tool1", {"arg": "value1"}, None, {"status": "success"})
        handler.tool_after_callback("tool2", {"arg": "value2"}, None, {"status": "success"})
        handler.tool_after_callback("tool3", {"arg": "value3"}, None, {"status": "success"})

        # 调用 next_prompt_patcher
        next_prompt = "Continue with next step"
        result = handler.next_prompt_patcher(next_prompt, None, turn=4)

        # 验证没有警告
        assert "重复工具调用" not in result

    def test_no_warning_for_same_tool_different_args(self, handler):
        """测试相同工具名但不同参数不触发警告（本次修复的核心场景）

        修复前：args_preview 截断到 50 字符，同文件不同位置的 edit
        会因预览字符串相同而被误判为重复。
        修复后：使用完整参数哈希，只有参数完全相同才算重复。
        """
        # 3 次相同工具名但不同参数
        handler.tool_after_callback("edit", {"file_path": "/tmp/a.py", "old": "x"}, None, {"status": "success"})
        handler.tool_after_callback("edit", {"file_path": "/tmp/a.py", "old": "y"}, None, {"status": "success"})
        handler.tool_after_callback("edit", {"file_path": "/tmp/a.py", "old": "z"}, None, {"status": "success"})

        # 调用 next_prompt_patcher
        next_prompt = "Continue with next step"
        result = handler.next_prompt_patcher(next_prompt, None, turn=4)

        # 验证没有警告（参数不同，不算重复）
        assert "重复工具调用" not in result

    def test_no_warning_for_low_turns(self, handler):
        """测试低轮次不触发检测"""
        # 添加重复调用
        for _i in range(3):
            handler.tool_after_callback("same_tool", {"arg": "value"}, None, {"status": "error"})

        # 低轮次（turn <= 3）
        next_prompt = "Continue"
        result = handler.next_prompt_patcher(next_prompt, None, turn=2)

        # 验证没有警告（turn <= 3 不检测）
        assert "重复工具调用" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p2"])
