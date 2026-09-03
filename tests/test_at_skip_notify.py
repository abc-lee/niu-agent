"""@指令跳过提示 loop 级测试（2026-09-03 D1-D4）。

@指令与工具调用同轮时工具优先执行、@指令被静默跳过——注入点挂 next_prompts，
随下轮 LLM 请求的引导块送达（截断免疫）。本文件验证注入点真实送达下轮请求
messages（防注入点错位回归），及负向用例：_bypass_at_prefix=True 子 Agent /
主 Agent 不注入提示。

全 mock 禁真实 LLM / 禁写图谱；整循环 mock client/_Handler 模式先例
test_at_prefix_full_content.py（L270-320）。
"""
from agent.generic import agent_loop
from agent.generic.agent_loop import StepOutcome
from agent.generic.llmcore import MockResponse, MockToolCall

_SKIP_MARKER = "未送达主 Agent"  # @niu-agent 跳过提示文案关键句（断言用，防文案漂移）


class _Client:
    """整循环 mock client：按序返回 responses，记录每次请求的 messages。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []  # 每次请求的 messages 快照（按序）

    def chat(self, messages, tools=None):
        self.calls.append(list(messages))
        resp = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

        def gen():
            yield from ()
            return resp

        return gen()


def _make_handler(is_subagent=True, bypass=False):
    """构造 loop 级 mock handler（先例 test_at_prefix_full_content.py _Handler）。

    is_subagent=True：子 Agent 路径（_is_sync_subagent=True——@end EXIT 分支可达）；
    is_subagent=False：主 Agent 路径（memory_context=None + 非同步子 Agent）。
    bypass=True：_bypass_at_prefix=True（该标志子 Agent 不用 @ 指令）。
    """

    class _Handler:
        _is_subagent = is_subagent
        _is_sync_subagent = is_subagent
        _subagent_unique_name = "test-agent-abc1"
        _program_triggered = False
        current_turn = 0
        _done_hooks = []

        def __init__(self):
            self.dispatches = []  # [(tool_name, args)]——工具实际执行记录

        def dispatch(self, tool_name, args, response, index=0):
            self.dispatches.append((tool_name, dict(args)))

            def gen():
                yield  # 生成器（agent_loop 经 exhaust 消费）
                return StepOutcome(data=None, next_prompt="工具执行成功")

            return gen()

        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    if bypass:
        _Handler._bypass_at_prefix = True
    return _Handler()


def _run_loop(handler, client, max_turns=5):
    """消费 agent_runner_loop 整循环，返回 (events, return_value)。"""
    events = []
    gen = agent_loop.agent_runner_loop(
        client=client,
        system_prompt="你是子 Agent",
        user_input="任务",
        handler=handler,
        tools_schema=[],
        verbose=False,
        enable_supplement=False,
        max_turns=max_turns,
        stop_predicate=lambda: False,  # 确定性：不读全局停止标志（与其他测试隔离）
    )
    while True:
        try:
            events.append(next(gen))
        except StopIteration as e:
            return events, e.value


def _tool_call_response(content):
    """第 1 轮响应：content 含 @niu-agent + 同轮 tool_calls（000043 实证场景）。"""
    tc = MockToolCall(name="grep", args={"pattern": "2026-08-27|2026-08-30"}, id="call_1")
    return MockResponse(
        thinking="", content=content, tool_calls=[tc], raw=content,
        finish_reason="tool_calls", usage={},
    )


def _plain_response(content):
    """无工具调用的纯文本响应。"""
    return MockResponse(
        thinking="", content=content, tool_calls=[], raw=content,
        finish_reason="end_turn", usage={},
    )


def test_skip_notify_injected_into_next_round_messages():
    """T8: 子 Agent 第 1 轮 @niu-agent+tool_calls → 工具正常执行、不 EXIT，
    下轮请求 messages 含跳过提示（user 消息含"未送达主 Agent"）。"""
    handler = _make_handler(is_subagent=True)
    client = _Client([
        _tool_call_response("@niu-agent 请确认是否先补充 8/27、8/30 记录？"),
        _plain_response("工作已完成 @end"),
    ])

    events, rv = _run_loop(handler, client)

    # 第 1 轮不 EXIT（工具优先执行，循环继续到第 2 轮经 @end 正常退出）
    assert rv["result"] == "EXITED"
    assert len(client.calls) == 2  # 确有第 2 轮请求
    # 工具正常执行（NO_INTERCEPTION 语义不变——@ 拦截层对 tool_calls 轮不介入）
    assert handler.dispatches == [("grep", {"pattern": "2026-08-27|2026-08-30"})]
    # 下轮请求 messages 含跳过提示（注入点真实送达——防注入点错位回归）
    second_round = client.calls[1]
    assert any(
        m.get("role") == "user" and _SKIP_MARKER in m.get("content", "") and "@niu-agent" in m.get("content", "")
        for m in second_round
    ), f"第 2 轮请求 messages 缺跳过提示: {second_round}"


def test_skip_notify_not_injected_when_bypass_at_prefix():
    """B3a: _bypass_at_prefix=True 子 Agent 同场景 → 下轮请求 messages 无提示（该标志子 Agent 不用 @ 指令，避免噪声）。"""
    handler = _make_handler(is_subagent=True, bypass=True)
    client = _Client([
        _tool_call_response("@niu-agent 请确认是否先补充 8/27、8/30 记录？"),
        _plain_response("工作已完成 @end"),
    ])

    events, rv = _run_loop(handler, client)

    # bypass 子 Agent 的 @end 同样被拦截层放行（NO_INTERCEPTION——该标志子 Agent 不用 @ 指令），
    # 经纯文本路径 CURRENT_TASK_DONE 退出（非 EXITED）
    assert rv["result"] == "CURRENT_TASK_DONE"
    assert len(client.calls) == 2
    assert handler.dispatches == [("grep", {"pattern": "2026-08-27|2026-08-30"})]  # 工具照常执行
    second_round = client.calls[1]
    assert not any(_SKIP_MARKER in m.get("content", "") for m in second_round), \
        f"_bypass_at_prefix=True 不应注入提示: {second_round}"


def test_skip_notify_not_injected_for_main_agent():
    """B3b: 主 Agent（_is_subagent=False）同场景 → 下轮请求 messages 无提示（主 Agent 的 @ 是出站消息，语义不同）。"""
    handler = _make_handler(is_subagent=False)
    client = _Client([
        _tool_call_response("@niu-agent 请确认是否先补充 8/27、8/30 记录？"),
        _plain_response("完成"),
    ])

    events, rv = _run_loop(handler, client)

    assert rv["result"] == "CURRENT_TASK_DONE"
    assert len(client.calls) == 2
    assert handler.dispatches == [("grep", {"pattern": "2026-08-27|2026-08-30"})]  # 工具照常执行
    second_round = client.calls[1]
    assert not any(_SKIP_MARKER in m.get("content", "") for m in second_round), \
        f"主 Agent 路径不应注入提示: {second_round}"
