"""测试轮次级刷新：on_turn_end 回调机制。

验证 agent_runner_loop 支持可选的 on_turn_end 回调，
在每轮循环末尾（组装好 next_prompt 之后）调用，
允许调用方动态更新 system_prompt 和 tools_schema。
"""
import pytest
import sys
import copy
from unittest.mock import Mock

sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.agent_loop import agent_runner_loop, StepOutcome


def _make_client(responses):
    """构建模拟客户端，按顺序返回 responses 中的响应。"""
    client = Mock()
    client.last_tools = ""
    call_idx = [0]
    all_messages = []

    def capture_messages(**kwargs):
        msgs = kwargs.get("messages", [])
        all_messages.append(copy.deepcopy(msgs))
        idx = min(call_idx[0], len(responses) - 1)
        mock_resp = responses[idx]
        call_idx[0] += 1

        def response_gen():
            yield mock_resp
            return mock_resp

        return response_gen()

    client.chat = capture_messages
    client._all_messages = all_messages
    return client


def _tool_call_response(tool_name="test_tool", tool_args="{}", call_id="call_001"):
    """创建包含单个工具调用的模拟响应。"""
    mock_response = Mock()
    mock_response.content = "执行中"
    mock_response.tool_calls = [
        Mock(id=call_id, function=Mock(name=tool_name, arguments=tool_args))
    ]
    return mock_response


def _no_tool_response(content="任务完成"):
    """创建不包含工具调用的模拟响应。"""
    mock_response = Mock()
    mock_response.content = content
    mock_response.tool_calls = None
    return mock_response


def _make_handler(outcomes):
    """构建模拟 handler，按顺序返回 outcomes 中的 StepOutcome。"""
    outcome_idx = [0]

    def mock_dispatch(tool_name, args, response, index=0):
        idx = min(outcome_idx[0], len(outcomes) - 1)
        outcome_idx[0] += 1
        yield
        return outcomes[idx]

    handler = Mock()
    handler.dispatch = mock_dispatch
    handler._done_hooks = []
    handler.max_turns = 40
    return handler


class TestOnTurnEndCallback:
    """测试 on_turn_end 回调机制。"""

    def test_system_prompt_updated_between_turns(self):
        """验证每轮循环后 system_prompt 通过 on_turn_end 回调更新。

        场景：
        1. 第一轮：使用初始 system_prompt
        2. on_turn_end 回调修改 messages[0] 的 system_prompt
        3. 第二轮：chat 调用时使用更新后的 system_prompt
        """
        # 准备两轮的响应：第一轮工具调用，第二轮无工具调用
        responses = [
            _tool_call_response("tool_a", '{}', "call_001"),
            _no_tool_response("完成"),
        ]
        outcomes = [
            StepOutcome(data={"ok": True}, next_prompt="继续"),
        ]

        client = _make_client(responses)
        handler = _make_handler(outcomes)

        # 记录回调调用
        callback_calls = []

        def on_turn_end(messages, tools_schema, turn):
            callback_calls.append({
                "turn": turn,
                "system_prompt": messages[0]["content"],
                "tools_schema_len": len(tools_schema),
            })
            # 修改 system_prompt
            messages[0]["content"] = f"Updated system prompt (turn {turn})"
            return tools_schema

        gen = agent_runner_loop(
            client=client,
            system_prompt="Initial system prompt",
            user_input="开始",
            handler=handler,
            tools_schema=[{"name": "tool_a"}],
            max_turns=3,
            on_turn_end=on_turn_end,
        )

        try:
            list(gen)
        except StopIteration:
            pass

        # 验证回调被调用
        assert len(callback_calls) >= 1, (
            f"on_turn_end 应至少被调用 1 次，实际: {len(callback_calls)} 次"
        )

        # 验证第一次回调时 system_prompt 是原始值
        first_call = callback_calls[0]
        assert first_call["system_prompt"] == "Initial system prompt", (
            f"第一次回调时 system_prompt 应为原始值，实际: {first_call['system_prompt']}"
        )

        # 验证第二轮 chat 调用时 system_prompt 已更新
        all_msgs = client._all_messages
        if len(all_msgs) >= 2:
            second_turn_system = all_msgs[1][0]["content"]
            assert "Updated system prompt" in second_turn_system, (
                f"第二轮 system_prompt 应包含更新内容，实际: {second_turn_system}"
            )

    def test_tools_schema_updated_between_turns(self):
        """验证每轮循环后 tools_schema 通过 on_turn_end 回调更新。

        场景：
        1. 第一轮：使用初始 tools_schema
        2. on_turn_end 回调返回更新后的 tools_schema
        3. 第二轮：chat 调用时使用更新后的 tools_schema
        """
        responses = [
            _tool_call_response("tool_a", '{}', "call_001"),
            _no_tool_response("完成"),
        ]
        outcomes = [
            StepOutcome(data={"ok": True}, next_prompt="继续"),
        ]

        client = _make_client(responses)
        handler = _make_handler(outcomes)

        # 记录传递给 chat 的 tools_schema
        chat_tools = []

        original_chat = client.chat

        def capturing_chat(**kwargs):
            chat_tools.append(copy.deepcopy(kwargs.get("tools", [])))
            return original_chat(**kwargs)

        client.chat = capturing_chat

        updated_schema = [{"name": "tool_a"}, {"name": "tool_b_new"}]

        def on_turn_end(messages, tools_schema, turn):
            # 返回更新后的 tools_schema
            return updated_schema

        gen = agent_runner_loop(
            client=client,
            system_prompt="测试",
            user_input="开始",
            handler=handler,
            tools_schema=[{"name": "tool_a"}],
            max_turns=3,
            on_turn_end=on_turn_end,
        )

        try:
            list(gen)
        except StopIteration:
            pass

        # 验证第二轮 chat 调用时 tools_schema 已更新
        if len(chat_tools) >= 2:
            second_turn_tools = chat_tools[1]
            assert len(second_turn_tools) == 2, (
                f"第二轮 tools_schema 应有 2 个工具，实际: {len(second_turn_tools)}"
            )
            tool_names = [t.get("name") for t in second_turn_tools]
            assert "tool_b_new" in tool_names, (
                f"第二轮 tools_schema 应包含 tool_b_new，实际: {tool_names}"
            )

    def test_on_turn_end_not_required(self):
        """验证不提供 on_turn_end 时行为与之前完全一致。

        这是一个回归测试：确保添加 on_turn_end 参数后，
        不传该参数时 agent_runner_loop 的行为与修改前完全一致。
        """
        responses = [
            _tool_call_response("tool_a", '{}', "call_001"),
            _no_tool_response("任务完成"),
        ]
        outcomes = [
            StepOutcome(data={"result": "ok"}, next_prompt="继续"),
        ]

        client = _make_client(responses)
        handler = _make_handler(outcomes)

        # 不传 on_turn_end，行为应与修改前一致
        gen = agent_runner_loop(
            client=client,
            system_prompt="测试系统",
            user_input="测试输入",
            handler=handler,
            tools_schema=[{"name": "tool_a"}],
            max_turns=3,
            # 注意：没有 on_turn_end 参数
        )

        results = []
        try:
            for chunk in gen:
                results.append(chunk)
        except StopIteration:
            pass

        # 验证循环正常运行完成（未抛出异常）
        all_msgs = client._all_messages
        assert len(all_msgs) >= 2, (
            f"不提供 on_turn_end 时应正常执行，预期至少 2 次 chat 调用，实际: {len(all_msgs)}"
        )

        # 验证 system_prompt 未被修改
        last_msgs = all_msgs[-1]
        assert last_msgs[0]["content"] == "测试系统", (
            f"system_prompt 应保持不变，实际: {last_msgs[0]['content']}"
        )

        # 验证消息结构完整（system -> user -> assistant -> tool -> user）
        roles = [m["role"] for m in last_msgs]
        assert roles[0] == "system"
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
