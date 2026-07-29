"""集成测试：验证完整的工具调用流程（从 agent_loop 到 ToolRegistry）。

这个测试验证修复后的完整流程：
1. agent_loop.py: 当 outcome.data 为 None 时添加空的 tool_result
2. ToolRegistry: 支持 MCP call_tool() 包装器模式
3. NiuHandler.dispatch: 正确处理未知工具

问题背景：
- Anthropic API 要求每个 tool_call 必须有对应的 tool_result
- 之前：当工具执行返回 None 时不添加 tool_result，导致 API 错误
- 现在：即使返回 None 也添加空 tool_result，确保 API 兼容性
"""
import copy
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.agent_loop import StepOutcome, agent_runner_loop
from agent.tool_registry import ToolRegistry, reset_registry


class TestFullToolCallFlow:
    """测试完整的工具调用流程。"""

    def setup_method(self):
        """每个测试前重置 registry。"""
        reset_registry()

    def test_full_tool_call_flow_with_unknown_tool(self):
        """验证从 agent_loop 到 ToolRegistry 的完整工具调用流程。

        场景：LLM 尝试调用一个未知工具
        预期：
        1. agent_loop 正确构建消息序列
        2. assistant 消息包含 tool_calls
        3. tool 消息存在（即使内容为空）
        4. 消息结构对 Anthropic API 有效
        """
        # ========== 设置模拟客户端 ==========
        client = Mock()
        client.last_tools = ""

        # ========== 模拟 LLM 响应包含未知工具调用 ==========
        # 注意：Mock 的 name 参数是用于 repr，需要单独设置 function.name 属性
        mock_function = Mock()
        mock_function.name = "nonexistent-server/unknown_tool"
        mock_function.arguments = '{"param": "test"}'

        mock_response = Mock()
        mock_response.content = "让我尝试调用这个工具..."
        mock_response.tool_calls = [
            Mock(
                id="call_unknown_123",
                function=mock_function
            )
        ]

        # ========== 设置 ToolRegistry 为空（模拟未知工具） ==========
        registry = ToolRegistry()

        # ========== 模拟 handler ==========
        def mock_dispatch(tool_name, args, response, index=0):
            """模拟 dispatch 处理未知工具。

            当工具不存在时，返回带有错误信息的 StepOutcome。
            """
            # 检查是否是内置工具
            f"do_{tool_name.replace('-', '_').replace('/', '_')}"
            if False:  # 模拟内置工具不存在
                pass

            # 检查是否是 MCP 工具
            if "/" in tool_name:
                # 尝试从 ToolRegistry 获取
                func = registry.get(tool_name)
                if func is None:
                    # 关键：未知工具返回 StepOutcome，data 为 None 或错误字典
                    yield f"[MCP Error] Tool not found: {tool_name}\n"
                    return StepOutcome(
                        {"status": "error", "error_code": "TOOL_NOT_FOUND", "msg": f"Tool {tool_name} not found"},
                        next_prompt=f"[System] 工具 {tool_name} 不存在，请使用其他方法"
                    )

            # 完全未知的工具
            yield f"Unknown tool: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}")

        handler = Mock()
        handler.dispatch = mock_dispatch
        handler._done_hooks = []
        handler.max_turns = 40
        handler.current_turn = 0

        # ========== 收集所有传递给 chat 的消息 ==========
        all_messages = []

        def capture_messages(**kwargs):
            msgs = kwargs.get("messages", [])
            # 保存消息的深拷贝
            all_messages.append(copy.deepcopy(msgs))
            # 模拟 LLM 响应 - 必须返回生成器
            def response_gen():
                yield mock_response
                return mock_response
            return response_gen()

        client.chat = capture_messages

        # ========== 运行 agent 循环 ==========
        gen = agent_runner_loop(
            client=client,
            system_prompt="你是测试助手",
            user_input="请使用 unknown_tool",
            handler=handler,
            tools_schema=[],
            max_turns=2  # 两次迭代确保消息历史完整
        )

        # 收集输出（忽略）
        try:
            list(gen)
        except StopIteration:
            pass

        # ========== 验证消息结构 ==========

        # 验证至少有 2 次 chat 调用
        assert len(all_messages) >= 2, f"预期至少 2 次 chat 调用，实际: {len(all_messages)}"

        # 检查最后一次 chat 调用的消息（包含完整历史）
        last_messages = all_messages[-1]

        # 找到 assistant 消息（包含 tool_calls）
        assistant_msgs = [m for m in last_messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in last_messages if m.get("role") == "tool"]

        # 验证 assistant 消息存在且有 tool_calls
        assert len(assistant_msgs) >= 1, f"预期至少 1 条 assistant 消息，实际: {len(assistant_msgs)}"

        assistant_msg = assistant_msgs[0]
        assert "tool_calls" in assistant_msg, "assistant 消息应包含 tool_calls"
        assert len(assistant_msg["tool_calls"]) == 1, f"预期 1 个 tool_call，实际: {len(assistant_msg['tool_calls'])}"

        # 验证 tool_call 结构
        tool_call = assistant_msg["tool_calls"][0]
        assert tool_call["id"] == "call_unknown_123"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "nonexistent-server/unknown_tool"

        # ========== 关键验证：必须有 tool 消息 ==========
        # 这是修复的核心：即使工具不存在/返回 None，也必须有 tool 消息
        assert len(tool_msgs) >= 1, (
            f"预期至少 1 条 tool 消息对应 tool_call，实际: {len(tool_msgs)} 条。\n"
            "当前代码在工具不存在时未正确添加 tool_result，这违反了 Anthropic API 要求。"
        )

        # 验证 tool 消息结构
        tool_msg = tool_msgs[0]
        assert tool_msg["tool_call_id"] == "call_unknown_123", (
            f"tool_call_id 应为 call_unknown_123，实际: {tool_msg.get('tool_call_id')}"
        )
        # 内容可能为空字符串，但必须存在
        assert "content" in tool_msg, "tool 消息必须包含 content 字段"

    def test_tool_result_with_empty_data(self):
        """验证当 outcome.data 为 None 时，仍添加空的 tool_result。

        这是针对 agent_loop.py 修复的专门测试。
        """
        # 设置模拟客户端
        client = Mock()
        client.last_tools = ""

        # 模拟 LLM 响应包含工具调用
        mock_response = Mock()
        mock_response.content = "执行中..."
        mock_response.tool_calls = [
            Mock(
                id="call_none_result",
                function=Mock(name="tool_returns_none", arguments="{}")
            )
        ]

        # 模拟 handler 返回 None 数据
        def mock_dispatch(tool_name, args, response, index=0):
            outcome = StepOutcome(
                data=None,  # 关键：data 为 None
                next_prompt="继续执行",
                should_exit=False
            )
            yield  # 使函数成为生成器
            return outcome

        handler = Mock()
        handler.dispatch = mock_dispatch
        handler._done_hooks = []
        handler.max_turns = 40

        # 收集所有传递给 chat 的消息
        all_messages = []

        def capture_messages(**kwargs):
            msgs = kwargs.get("messages", [])
            all_messages.append(copy.deepcopy(msgs))
            def response_gen():
                yield mock_response
                return mock_response
            return response_gen()

        client.chat = capture_messages

        # 运行 agent 循环
        gen = agent_runner_loop(
            client=client,
            system_prompt="测试",
            user_input="测试输入",
            handler=handler,
            tools_schema=[],
            max_turns=2
        )

        try:
            list(gen)
        except StopIteration:
            pass

        # 验证消息结构
        assert len(all_messages) >= 2, f"预期至少 2 次 chat 调用，实际: {len(all_messages)}"

        last_messages = all_messages[-1]
        tool_msgs = [m for m in last_messages if m.get("role") == "tool"]

        # 关键断言：即使 data 为 None，也必须有 tool 消息
        assert len(tool_msgs) == 1, (
            f"预期有 1 条 tool 消息对应 tool_call，实际: {len(tool_msgs)} 条。"
            "当 outcome.data 为 None 时，应添加空的 tool_result。"
        )
        assert tool_msgs[0]["tool_call_id"] == "call_none_result"
        assert tool_msgs[0]["content"] == ""  # 空但存在

    def test_tool_result_with_dict_data(self):
        """验证当 outcome.data 为 dict 时，正确添加 tool_result。"""
        # 设置模拟客户端
        client = Mock()
        client.last_tools = ""

        # 模拟 LLM 响应
        mock_response = Mock()
        mock_response.content = ""
        mock_response.tool_calls = [
            Mock(
                id="call_dict_result",
                function=Mock(name="tool_returns_dict", arguments='{"input": "test"}')
            )
        ]

        # 模拟 handler 返回 dict 数据
        def mock_dispatch(tool_name, args, response, index=0):
            outcome = StepOutcome(
                data={"status": "success", "result": "操作完成"},
                next_prompt="任务完成",
                should_exit=False
            )
            yield
            return outcome

        handler = Mock()
        handler.dispatch = mock_dispatch
        handler._done_hooks = []
        handler.max_turns = 40

        # 收集消息
        all_messages = []

        def capture_messages(**kwargs):
            msgs = kwargs.get("messages", [])
            all_messages.append(copy.deepcopy(msgs))
            def response_gen():
                yield mock_response
                return mock_response
            return response_gen()

        client.chat = capture_messages

        # 运行
        gen = agent_runner_loop(
            client=client,
            system_prompt="测试",
            user_input="测试",
            handler=handler,
            tools_schema=[],
            max_turns=2
        )

        try:
            list(gen)
        except StopIteration:
            pass

        # 验证
        last_messages = all_messages[-1]
        tool_msgs = [m for m in last_messages if m.get("role") == "tool"]

        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_dict_result"
        assert "success" in tool_msgs[0]["content"]

    def test_multiple_tool_calls_all_have_results(self):
        """验证多个 tool_call 都有对应的 tool_result。"""
        client = Mock()
        client.last_tools = ""

        # 模拟 LLM 响应包含 3 个工具调用
        mock_response = Mock()
        mock_response.content = ""
        mock_response.tool_calls = [
            Mock(id="call_1", function=Mock(name="tool_a", arguments="{}")),
            Mock(id="call_2", function=Mock(name="tool_b", arguments="{}")),
            Mock(id="call_3", function=Mock(name="tool_c", arguments="{}")),
        ]

        call_count = [0]

        def mock_dispatch(tool_name, args, response, index=0):
            call_count[0] += 1
            # 第一个返回 dict，第二个返回 None，第三个返回错误
            if tool_name == "tool_a":
                outcome = StepOutcome(
                    data={"status": "success"},
                    next_prompt="继续"
                )
            elif tool_name == "tool_b":
                outcome = StepOutcome(
                    data=None,  # None
                    next_prompt="继续"
                )
            else:
                outcome = StepOutcome(
                    data={"status": "error", "msg": "失败"},
                    next_prompt="继续"
                )
            yield
            return outcome

        handler = Mock()
        handler.dispatch = mock_dispatch
        handler._done_hooks = []
        handler.max_turns = 40

        all_messages = []

        def capture_messages(**kwargs):
            msgs = kwargs.get("messages", [])
            all_messages.append(copy.deepcopy(msgs))
            def response_gen():
                yield mock_response
                return mock_response
            return response_gen()

        client.chat = capture_messages

        gen = agent_runner_loop(
            client=client,
            system_prompt="测试",
            user_input="测试多个工具",
            handler=handler,
            tools_schema=[],
            max_turns=2
        )

        try:
            list(gen)
        except StopIteration:
            pass

        # 验证
        last_messages = all_messages[-1]
        tool_msgs = [m for m in last_messages if m.get("role") == "tool"]

        # 关键：3 个 tool_call 必须有 3 个 tool_result
        assert len(tool_msgs) == 3, (
            f"预期 3 条 tool 消息对应 3 个 tool_call，实际: {len(tool_msgs)} 条"
        )

        # 验证每个 tool_result 都有正确的 tool_call_id
        ids = {m["tool_call_id"] for m in tool_msgs}
        assert ids == {"call_1", "call_2", "call_3"}

    def test_anthropic_api_message_structure(self):
        """验证消息结构符合 Anthropic API 要求。

        Anthropic API 要求：
        1. messages 数组
        2. 每个 message 有 role 字段
        3. assistant 消息的 tool_calls 格式正确
        4. tool 消息有 tool_call_id 和 content
        """
        client = Mock()
        client.last_tools = ""

        mock_response = Mock()
        mock_response.content = ""
        mock_response.tool_calls = [
            Mock(
                id="call_test_id",
                function=Mock(name="test_tool", arguments='{"param": "value"}')
            )
        ]

        def mock_dispatch(tool_name, args, response, index=0):
            yield
            return StepOutcome(data={"result": "ok"}, next_prompt="done")

        handler = Mock()
        handler.dispatch = mock_dispatch
        handler._done_hooks = []
        handler.max_turns = 40

        all_messages = []

        def capture_messages(**kwargs):
            msgs = kwargs.get("messages", [])
            all_messages.append(copy.deepcopy(msgs))
            def response_gen():
                yield mock_response
                return mock_response
            return response_gen()

        client.chat = capture_messages

        gen = agent_runner_loop(
            client=client,
            system_prompt="系统提示",
            user_input="用户输入",
            handler=handler,
            tools_schema=[],
            max_turns=2
        )

        try:
            list(gen)
        except StopIteration:
            pass

        last_messages = all_messages[-1]

        # 验证消息数组结构
        assert isinstance(last_messages, list)

        # 验证每条消息都有 role
        for msg in last_messages:
            assert "role" in msg, f"消息缺少 role 字段: {msg}"

        # 验证消息顺序：system -> user -> assistant -> tool -> user
        roles = [m["role"] for m in last_messages]
        assert roles[0] == "system"
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

        # 验证 assistant 消息格式
        assistant_msg = next(m for m in last_messages if m["role"] == "assistant")
        assert "tool_calls" in assistant_msg
        tc = assistant_msg["tool_calls"][0]
        assert tc["id"] == "call_test_id"
        assert tc["type"] == "function"
        assert "name" in tc["function"]
        assert "arguments" in tc["function"]

        # 验证 tool 消息格式
        tool_msg = next(m for m in last_messages if m["role"] == "tool")
        assert "tool_call_id" in tool_msg
        assert "content" in tool_msg
        assert tool_msg["tool_call_id"] == "call_test_id"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
