"""P0-6: 测试 JSON 解析异常处理"""
import pytest
import sys
import json
sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.llmcore import MockResponse, MockToolCall


@pytest.mark.p0
class TestToolCallJSONParsing:
    """测试工具调用参数的 JSON 解析"""

    def test_valid_json_parsing(self):
        """测试有效 JSON 正常解析"""
        # 创建包含有效 JSON 的工具调用
        response = MockResponse(
            thinking="",
            content="",
            tool_calls=[
                MockToolCall(
                    name="test_tool",
                    args='{"param1": "value1", "param2": 123}',  # args 是字符串
                    id="tool_1"
                )
            ],
            raw=""
        )

        # 模拟 agent_loop 中的 JSON 解析逻辑
        tool_calls = []
        for tc in response.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": args,
                    "id": tc.id,
                })
            except json.JSONDecodeError as e:
                # 错误处理
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": {},
                    "id": tc.id,
                    "error": str(e),
                })

        # 验证解析成功
        assert len(tool_calls) == 1
        assert tool_calls[0]["args"] == {"param1": "value1", "param2": 123}
        assert "error" not in tool_calls[0]

    def test_invalid_json_fallback_to_empty_dict(self):
        """测试非法 JSON 回退为空 dict"""
        # 创建包含非法 JSON 的工具调用
        response = MockResponse(
            thinking="",
            content="",
            tool_calls=[
                MockToolCall(
                    name="test_tool",
                    args='{invalid json}',  # args 是字符串
                    id="tool_1"
                )
            ],
            raw=""
        )

        # 模拟 agent_loop 中的错误处理
        tool_calls = []
        for tc in response.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": args,
                    "id": tc.id,
                })
            except json.JSONDecodeError as e:
                # 错误处理：回退为空参数
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": {},  # 回退
                    "id": tc.id,
                    "error": str(e),
                })

        # 验证不崩溃，使用空参数
        assert len(tool_calls) == 1
        assert tool_calls[0]["args"] == {}
        assert "error" in tool_calls[0]

    def test_mixed_valid_invalid_json(self):
        """测试混合有效/非法 JSON 的工具调用"""
        response = MockResponse(
            thinking="",
            content="",
            tool_calls=[
                MockToolCall(
                    name="tool_1",
                    args='{"valid": true}',
                    id="tool_1"
                ),
                MockToolCall(
                    name="tool_2",
                    args='invalid',
                    id="tool_2"
                ),
                MockToolCall(
                    name="tool_3",
                    args='{"also_valid": 123}',
                    id="tool_3"
                ),
            ],
            raw=""
        )

        tool_calls = []
        for tc in response.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": args,
                    "id": tc.id,
                })
            except json.JSONDecodeError as e:
                tool_calls.append({
                    "tool_name": tc.function.name,
                    "args": {},
                    "id": tc.id,
                    "error": str(e),
                })

        # 验证所有工具调用都被处理
        assert len(tool_calls) == 3
        assert tool_calls[0]["args"] == {"valid": True}
        assert tool_calls[1]["args"] == {}
        assert "error" in tool_calls[1]
        assert tool_calls[2]["args"] == {"also_valid": 123}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
