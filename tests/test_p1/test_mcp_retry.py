"""P1-3: 测试 MCP 工具调用重试机制"""
import pytest
import sys
import time
sys.path.insert(0, "E:/tools/ai-bot")

from agent.mcp_sync_bridge import MCPSyncBridge
from unittest.mock import Mock, patch, MagicMock


@pytest.mark.p1
class TestMCPToolRetry:
    """测试 MCP 工具重试机制"""

    @pytest.fixture
    def bridge(self):
        """创建 MCPSyncBridge 实例"""
        bridge = MCPSyncBridge()
        yield bridge
        # 清理
        if bridge._loop and bridge._loop.is_running():
            bridge.stop()

    def test_call_tool_success_no_retry(self, bridge):
        """测试成功调用无需重试"""
        # Mock call_tool 返回成功
        with patch.object(bridge, 'call_tool') as mock_call:
            mock_call.return_value = {"status": "success", "result": "OK"}

            result = bridge.call_tool_with_retry(
                "test-server",
                "test_tool",
                {"param": "value"},
                max_retries=2
            )

            # 验证只调用一次
            assert mock_call.call_count == 1
            assert result["status"] == "success"

    def test_call_tool_retry_on_failure(self, bridge):
        """测试失败后重试"""
        call_count = 0

        def mock_call_tool(server_name, tool_name, args, timeout=60.0):
            nonlocal call_count
            call_count += 1

            if call_count < 3:  # 前 2 次失败
                return {"status": "error", "msg": "Temporary failure"}
            else:  # 第 3 次成功
                return {"status": "success", "result": "OK"}

        with patch.object(bridge, 'call_tool', side_effect=mock_call_tool):
            result = bridge.call_tool_with_retry(
                "test-server",
                "test_tool",
                {"param": "value"},
                max_retries=2,
                retry_delay=0.1  # 加速测试
            )

            # 验证重试了 2 次
            assert call_count == 3
            assert result["status"] == "success"

    def test_call_tool_max_retries_exceeded(self, bridge):
        """测试超过最大重试次数"""
        with patch.object(bridge, 'call_tool') as mock_call:
            # 总是失败
            mock_call.return_value = {"status": "error", "msg": "Persistent failure"}

            result = bridge.call_tool_with_retry(
                "test-server",
                "test_tool",
                {"param": "value"},
                max_retries=2,
                retry_delay=0.1
            )

            # 验证调用了 3 次（初始 + 2 次重试）
            assert mock_call.call_count == 3
            assert result["status"] == "error"
            assert "Failed after 3 attempts" in result["msg"]
            assert result["retries"] == 2

    def test_call_tool_retry_delay_exponential_backoff(self, bridge):
        """测试指数退避"""
        delays = []

        original_sleep = time.sleep
        def mock_sleep(delay):
            delays.append(delay)
            # 不实际sleep，加速测试

        with patch.object(time, 'sleep', side_effect=mock_sleep):
            with patch.object(bridge, 'call_tool') as mock_call:
                # 前 2 次失败，第 3 次成功
                mock_call.side_effect = [
                    {"status": "error", "msg": "Fail 1"},
                    {"status": "error", "msg": "Fail 2"},
                    {"status": "success", "result": "OK"}
                ]

                result = bridge.call_tool_with_retry(
                    "test-server",
                    "test_tool",
                    {},
                    max_retries=2,
                    retry_delay=1.0
                )

                # 验证指数退避
                assert len(delays) == 2  # 2 次重试
                assert delays[0] == 1.0  # 初始延迟
                assert delays[1] == 1.5  # 指数增长 (1.0 * 1.5)

    def test_call_tool_zero_retries(self, bridge):
        """测试 0 次重试（只调用一次）"""
        with patch.object(bridge, 'call_tool') as mock_call:
            mock_call.return_value = {"status": "error", "msg": "Fail"}

            result = bridge.call_tool_with_retry(
                "test-server",
                "test_tool",
                {},
                max_retries=0
            )

            # 验证只调用一次
            assert mock_call.call_count == 1
            assert result["status"] == "error"
            assert "Failed after 1 attempts" in result["msg"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p1"])
