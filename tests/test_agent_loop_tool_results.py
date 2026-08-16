"""测试每个 tool_call 都有对应的 tool_result 消息。

这是修复 LiteLLM API 错误 2013 的失败测试。

问题背景：
- Anthropic API 要求：每个 tool_call 必须有对应的 tool_result
- 当前 bug：agent_loop.py:190-196 当 outcome.data is None 时不添加 tool_result
- 导致错误：LiteLLM API Error 2013 - 每个工具调用必须有工具结果
"""
import copy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.agent_loop import StepOutcome, agent_runner_loop


def test_tool_result_for_none_data():
    """当 outcome.data 为 None 时，仍应添加中性占位的 tool_result。

    E4-03 契约反转：data=None 不再落空串 ""，而是落中性占位
    "（工具已执行，无返回值）"（全角括号中性占位——无错误前缀语义）。

    预期行为：
    - 即使 outcome.data 为 None
    - 也应该添加 tool_result 消息
    - content 为中性占位 "（工具已执行，无返回值）"，但消息必须存在
    """
    # 设置模拟客户端
    client = Mock()
    client.last_tools = ""

    # 模拟 LLM 响应包含工具调用
    mock_response = Mock()
    mock_response.content = "测试中"
    mock_response.stream_error = False  # MagicMock 自动真值陷阱：不显式置 False 会走 LLM_ERROR 退出
    mock_response.context_overflow = False  # 同上：不显式置 False 会走 CONTEXT_OVERFLOW 退出
    mock_response.tool_calls = [
        Mock(
            id="call_123",
            function=SimpleNamespace(name="unknown_tool", arguments="{}")  # Mock(name=) 是显示名配置非属性——用 SimpleNamespace 防 .name 返回自动 Mock
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
    handler.next_prompt_patcher = lambda next_prompt, outcome, turn: next_prompt

    # 收集所有传递给 chat 的消息
    all_messages = []
    call_count = {"n": 0}

    def capture_messages(**kwargs):
        msgs = kwargs.get("messages", [])
        # 保存消息的深拷贝，因为 agent_loop 会继续修改原列表
        all_messages.append(copy.deepcopy(msgs))
        call_count["n"] += 1

        def response_gen():
            if call_count["n"] == 1:
                yield mock_response
                return mock_response
            # 第 2 次：纯文本回复（LLM 停止调用工具）→ CURRENT_TASK_DONE 退出
            done = Mock()
            done.content = "完成"
            done.stream_error = False
            done.context_overflow = False
            done.tool_calls = []
            yield done
            return done

        return response_gen()

    client.chat = capture_messages

    # 运行 agent 循环
    gen = agent_runner_loop(
        client=client,
        system_prompt="测试",
        user_input="测试输入",
        handler=handler,
        tools_schema=[],
        max_turns=3  # 三轮边界：确保第 2 次 chat 调用包含完整消息历史（max_turns=3 而非 2 的原因：旧 response_gen 恒重复返回同一工具响应导致 assistant 消息重复、断言不可达——max_turns=3 保证循环至少执行 2 次 chat，末次含完整历史）
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

    # 关键断言：即使 data 为 None，也必须有 tool 消息（E4-03 契约反转）
    assert len(tool_msgs) == 1, (
        f"预期有 1 条 tool 消息对应 tool_call，实际: {len(tool_msgs)} 条。"
        f"当前代码在 outcome.data 为 None 时不添加 tool_result，这违反了 Anthropic API 要求。"
    )
    assert tool_msgs[0]["tool_call_id"] == "call_123"
    assert tool_msgs[0]["content"] == "（工具已执行，无返回值）"  # 中性占位（非空串）


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
