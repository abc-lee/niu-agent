"""测试每个 tool_call 都有对应的 tool_result 消息。

这是修复 LiteLLM API 错误 2013 的失败测试。

问题背景：
- Anthropic API 要求：每个 tool_call 必须有对应的 tool_result
- 当前 bug：agent_loop.py:190-196 当 outcome.data is None 时不添加 tool_result
- 导致错误：LiteLLM API Error 2013 - 每个工具调用必须有工具结果
"""
import pytest
import sys
import copy
from unittest.mock import Mock

sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.agent_loop import agent_runner_loop, StepOutcome


def test_tool_result_for_none_data():
    """当 outcome.data 为 None 时，仍应添加空的 tool_result。

    这是失败测试，证明当前代码存在 bug：
    - 当工具执行结果 outcome.data 为 None 时
    - 代码不会添加 tool_result 消息
    - 这违反了 Anthropic API 的要求

    预期行为（修复后）：
    - 即使 outcome.data 为 None
    - 也应该添加一个空的 tool_result 消息
    - 内容为空字符串 ""，但消息必须存在
    """
    # 设置模拟客户端
    client = Mock()
    client.last_tools = ""

    # 模拟 LLM 响应包含工具调用
    mock_response = Mock()
    mock_response.content = "测试中"
    mock_response.tool_calls = [
        Mock(
            id="call_123",
            function=Mock(name="unknown_tool", arguments="{}")
        )
    ]

    # 模拟 handler 返回 None 数据
    # dispatch 必须返回生成器，因为 agent_runner_loop 使用 yield from gen
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
        # 保存消息的深拷贝，因为 agent_loop 会继续修改原列表
        all_messages.append(copy.deepcopy(msgs))
        # 模拟 LLM 响应 - 必须返回生成器以支持 yield from
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
        max_turns=2  # 两次迭代，确保第二次 chat 调用包含完整消息历史
    )

    try:
        list(gen)
    except StopIteration:
        pass

    # 验证消息结构
    assert len(all_messages) >= 2, f"预期至少 2 次 chat 调用，实际: {len(all_messages)}"

    # 检查最后一次 chat 调用的消息（应该包含完整的对话历史）
    last_messages = all_messages[-1]

    assistant_msgs = [m for m in last_messages if m.get("role") == "assistant"]
    tool_msgs = [m for m in last_messages if m.get("role") == "tool"]

    # 验证 assistant 消息存在且有 tool_calls
    assert len(assistant_msgs) == 1, f"预期 1 条 assistant 消息，实际: {len(assistant_msgs)}"
    assert len(assistant_msgs[0].get("tool_calls", [])) == 1

    # 关键断言：即使 data 为 None，也必须有 tool 消息
    # 这是当前代码的 bug：当 outcome.data 为 None 时不添加 tool_result
    # 此断言会失败，证明 bug 存在
    assert len(tool_msgs) == 1, (
        f"预期有 1 条 tool 消息对应 tool_call，实际: {len(tool_msgs)} 条。"
        f"当前代码在 outcome.data 为 None 时不添加 tool_result，这违反了 Anthropic API 要求。"
    )
    assert tool_msgs[0]["tool_call_id"] == "call_123"
    assert tool_msgs[0]["content"] == ""  # 空但存在


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
