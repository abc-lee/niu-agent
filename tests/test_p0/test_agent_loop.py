"""P0-6: 测试 JSON 解析异常处理"""
import json
import sys

import pytest

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


@pytest.mark.p0
class TestE401ParseFailureHandling:
    """E4-01：参数解析失败 → 错误进工具结果 + next_prompts 注入（循环续行）+ 同一轮连续 3 次失败终止 + 轮起点重置。

    不再 append {"args": {}} 空参继续（空参调用产生误导性结果）：
    - 解析失败 → 构建错误工具结果 [工具参数解析失败: <err>]（截断保尾 ≤500）直接进 tool_results（跳过 dispatch）
    - 同时注入 next_prompts（循环续行——防全失败轮 len(next_prompts)==0 走 CURRENT_TASK_DONE 退出）
    - 同一轮连续 3 次解析失败 → 第 3 次不再注入 next_prompts + 显式退出（yield ⚠️ system + chat_idle + return CURRENT_TASK_DONE）
    - 每轮解析循环起点重置计数（保留解析成功清零）——触发严格限定"同一轮连续 3 次"，跨轮散点失败不累计（LLM 自纠不截断）
    """

    @staticmethod
    def _run_loop(rounds):
        """构造 FakeClient（按轮次返回响应）+ dispatch 即抛错的 handler，跑完 agent_runner_loop。

        rounds: [(content, [(id, name, arguments_str), ...]), ...]——超出轮数时重复最后一轮。
        dispatch 抛 AssertionError：任何解析失败的工具若进入 dispatch 即测试失败（跳过 dispatch 契约）。
        """
        from types import SimpleNamespace

        from agent.generic.agent_loop import StepOutcome, agent_runner_loop

        class FakeClient:
            def __init__(self):
                self._call_count = 0

            def chat(self, **kw):
                self._call_count += 1
                idx = min(self._call_count, len(rounds)) - 1
                content, tool_calls = rounds[idx]
                yield
                return SimpleNamespace(
                    content=content,
                    tool_calls=[
                        SimpleNamespace(
                            id=tc_id,
                            function=SimpleNamespace(name=name, arguments=args_str),
                        )
                        for tc_id, name, args_str in tool_calls
                    ],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    finish_reason="stop",
                )

        class FakeHandler:
            current_turn = 0
            max_turns = 40

            def dispatch(self, tool_name, args, response, index=0):
                if tool_name == "ok_tool":
                    yield  # 生成器：agent_loop 用 exhaust 消费
                    return StepOutcome(data="ok", next_prompt="", should_exit=False)
                raise AssertionError(
                    f"E4-01: 解析失败的工具不应进入 dispatch（被调 tool_name={tool_name} args={args}）"
                )

            def tool_before_callback(self, *a, **kw):
                return
                yield

            def tool_after_callback(self, *a, **kw):
                return
                yield

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        client = FakeClient()
        gen = agent_runner_loop(
            client=client,
            system_prompt="test",
            user_input="test",
            handler=FakeHandler(),
            tools_schema=[{"type": "function", "function": {"name": "fail_tool", "parameters": {"type": "object", "properties": {}}}}],
            verbose=False,
        )
        events = []
        final_return = None
        try:
            while True:
                events.append(next(gen))
        except StopIteration as e:
            final_return = e.value
        return client, final_return, events

    def test_parse_failure_error_into_tool_result_and_loop_continues(self):
        """解析失败 → 错误工具结果进 tool 消息 + next_prompts 非空循环续行（LLM 下一轮可见可自纠）。"""
        client, rv, _ = self._run_loop([
            ("", [("tc1", "fail_tool", "{invalid json}")]),   # 轮 1：参数解析失败
            ("done", []),                                      # 轮 2：纯文本退出
        ])
        # 循环续行断言：next_prompts 注入保证第 2 次 chat 调用（未走 CURRENT_TASK_DONE 退出）
        assert client._call_count == 2, f"循环应续行（next_prompts 非空），实际 chat 调用 {client._call_count} 次"
        assert rv["result"] == "CURRENT_TASK_DONE"
        # 错误进工具结果断言：tool 消息含 [工具参数解析失败: ...]（截断保尾 ≤500 格式）
        tool_msgs = [m for m in rv.get("messages", []) if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, f"预期 1 条错误工具结果，实际 {len(tool_msgs)}"
        assert tool_msgs[0]["tool_call_id"] == "tc1"
        assert tool_msgs[0]["content"].startswith("[工具参数解析失败:"), tool_msgs[0]["content"]
        assert len(tool_msgs[0]["content"]) <= 500, "错误文本应截断保尾 ≤500"

    def test_three_consecutive_parse_failures_terminate(self):
        """同一轮连续 3 次解析失败 → 第 3 次不再注入 next_prompts + ⚠️ system 提示 + 显式退出 CURRENT_TASK_DONE。"""
        client, rv, events = self._run_loop([
            ("", [("tc1", "fail_tool", "x"), ("tc2", "fail_tool", "x"), ("tc3", "fail_tool", "x")]),
        ])
        # 第 3 次失败显式退出：只发生 1 次 chat 调用（无续行）
        assert client._call_count == 1, f"连续 3 次失败应终止，实际 chat 调用 {client._call_count} 次"
        assert rv["result"] == "CURRENT_TASK_DONE"
        assert rv.get("data") is None
        # ⚠️ system 事件（对齐截断强制退出格式——退出路径用户侧可见不静默，yield 顺序 system → chat_idle）
        system_events = [e for e in events if e.type == "system"]
        assert any("⚠️ 工具参数连续 3 次解析失败，已强制退出" in e.content for e in system_events), (
            f"强制退出前应 yield ⚠️ system 提示，实际 system 事件={[e.content for e in system_events]}"
        )
        assert any(e.content == "chat_idle" for e in system_events), (
            f"强制退出应 yield chat_idle，实际 system 事件={[e.content for e in system_events]}"
        )
        # 退出轮不残留半成品消息（assistant tool_calls / tool 结果均不落——错误工具结果丢弃不落库）
        roles = [m.get("role") for m in rv.get("messages", [])]
        assert "assistant" not in roles, f"退出轮不应残留 assistant 消息，实际 roles={roles}"
        assert "tool" not in roles, f"退出轮不应残留 tool 消息，实际 roles={roles}"

    def test_scattered_parse_failure_resets_count(self):
        """散点失败重置：解析成功清零计数，失败不跨成功轮累计（防提前误终止）。"""
        client, rv, _ = self._run_loop([
            ("", [("tc1", "fail_tool", "bad"), ("tc2", "ok_tool", '{"a": 1}')]),  # 轮 1：失败→成功（清零）
            ("", [("tc3", "fail_tool", "bad"), ("tc4", "fail_tool", "bad")]),     # 轮 2：连续 2 次失败（<3 不终止）
            ("done", []),                                                          # 轮 3：纯文本退出
        ])
        # 若计数未在成功时清零：轮 1 失败 1 + 轮 2 连续 2 = 3 → 提前终止 → chat 调用仅 2 次
        assert client._call_count == 3, (
            f"散点失败应重置计数（成功清零），实际 chat 调用 {client._call_count} 次"
        )
        assert rv["result"] == "CURRENT_TASK_DONE"
        # 错误工具结果按轮落消息（tc1/tc3/tc4 错误，tc2 正常）
        tool_msgs = [m for m in rv.get("messages", []) if m.get("role") == "tool"]
        assert len(tool_msgs) == 4, f"预期 4 条 tool 消息（3 错误 + 1 正常），实际 {len(tool_msgs)}"
        by_id = {m["tool_call_id"]: m["content"] for m in tool_msgs}
        assert by_id["tc1"].startswith("[工具参数解析失败:")
        assert by_id["tc3"].startswith("[工具参数解析失败:")
        assert by_id["tc4"].startswith("[工具参数解析失败:")
        assert by_id["tc2"] == "ok"

    def test_cross_round_scattered_failures_do_not_trigger(self):
        """跨轮散点不触发：轮起点重置——失败不跨轮累计（每轮 <3 次失败，LLM 自纠不截断）。

        轮 1 失败 2 次 + 轮 2 失败 1 次：旧实现函数级计数累计 2+1=3 → 轮 2 误强制退出；
        新实现每轮解析循环起点重置 → 轮 2 计数从 0 起 1 次 <3 → 续行，轮 3 纯文本正常退出。
        （真实循环中纯文本轮即终止轮——next_prompts 为空直接 CURRENT_TASK_DONE 退出，无法夹在
        两失败轮之间；故以"每轮各自 <3 次、散点分布不同轮"复现跨轮累计语义，锁轮起点重置。）
        """
        client, rv, _ = self._run_loop([
            ("", [("tc1", "fail_tool", "bad"), ("tc2", "fail_tool", "bad")]),  # 轮 1：2 次失败（<3）
            ("", [("tc3", "fail_tool", "bad")]),                                # 轮 2：1 次失败（轮起点重置后 <3）
            ("done", []),                                                       # 轮 3：纯文本退出
        ])
        # 若计数未在轮起点重置：轮 1 失败 2 + 轮 2 失败 1 = 3 → 轮 2 强制退出 → chat 调用仅 2 次
        assert client._call_count == 3, (
            f"跨轮散点失败应在轮起点重置（不跨轮累计），实际 chat 调用 {client._call_count} 次"
        )
        assert rv["result"] == "CURRENT_TASK_DONE"
        # 3 次失败均正常进错误工具结果（LLM 每轮可见可自纠）
        tool_msgs = [m for m in rv.get("messages", []) if m.get("role") == "tool"]
        assert len(tool_msgs) == 3, f"预期 3 条错误 tool 消息，实际 {len(tool_msgs)}"
        by_id = {m["tool_call_id"]: m["content"] for m in tool_msgs}
        assert by_id["tc1"].startswith("[工具参数解析失败:")
        assert by_id["tc2"].startswith("[工具参数解析失败:")
        assert by_id["tc3"].startswith("[工具参数解析失败:")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
