"""工具执行可中断化测试。

覆盖：工具 dispatch generator 慢速挂起 + stop → 放弃等待，立即 STOPPED（后台继续跑）；
正常路径 outcome 一致。复用 Task 3 的最小 handler/client 模式。
"""
import time

from agent.generic import agent_loop as al
from agent.generic.llmcore import MockResponse, MockToolCall


class _SlowToolHandler:
    """dispatch 返回慢速 generator（模拟长耗时工具，如 LightRAG 检索类）。

    R2-B P1-1：agent_runner_loop 在工具结果后无条件调用 next_prompt_patcher——缺则
    test_normal_tool_completes AttributeError（T3 _MinHandler 已有，此处补齐）。
    """

    def __init__(self, delay=0.5):
        self._is_subagent = False
        self._current_messages = []
        self.current_turn = 0
        self._last_prompt_tokens = 0
        self.last_tools = ""
        self.delay = delay

    def dispatch(self, tool_name, args, response, index=0):
        def _gen():
            time.sleep(self.delay)  # 慢速工具：停止应放弃等待
            yield from ()
            return al.StepOutcome(data={"ok": True}, next_prompt="done")
        return _gen()

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass


class _ToolClient:
    """client.chat 返回带单个 tool_call 的响应（触发工具执行路径）。"""

    def __init__(self):
        self.chat_called = 0

    def chat(self, messages, tools=None):
        self.chat_called += 1
        resp = MockResponse(
            thinking="",  # R1-B：thinking 必选
            content="",
            tool_calls=[MockToolCall(name="slow_tool", args={}, id="call_1")],
            raw="",
            usage={},
        )
        def _gen():
            yield from ()
            return resp
        return _gen()


def test_slow_tool_abandoned_on_stop():
    """工具执行中 stop 置位：放弃等待，STOPPED，且耗时 < 工具时长。"""
    handler = _SlowToolHandler(delay=0.5)
    client = _ToolClient()
    stop_flag = {"v": False}

    import threading
    threading.Timer(0.05, lambda: stop_flag.__setitem__("v", True)).start()

    started = time.monotonic()
    gen = al.agent_runner_loop(
        client=client,
        system_prompt="sys",  # R1-B：无 messages 参数，用 system_prompt/user_input
        user_input="hi",
        handler=handler,
        verbose=False,
        max_turns=2,  # R6-B P0-1：防御（放弃路径先返回，但防意外）
        stop_predicate=lambda: stop_flag["v"],
        on_before_llm=lambda m, t: None,
    )
    result = al.exhaust(gen)
    elapsed = time.monotonic() - started
    assert result["result"] == "STOPPED"
    assert elapsed < 0.4  # 放弃等待：< 工具 0.5s 时长


def test_normal_tool_completes():
    """无停止：工具正常执行，结果进入消息上下文。

    R7 P2-1 修正：agent_runner_loop 默认 max_turns=40（非 None——R6-B 叙事"挂死"不准确，
    但每轮返 tool_calls 时 40 轮循环才 MAX_TURNS_EXCEEDED，测试慢且意图模糊）——
    显式 max_turns=2 让测试快速聚焦（既有惯例：test_agent_loop_tool_results.py L86
    同场景显式 max_turns=2）。
    """
    handler = _SlowToolHandler(delay=0.0)
    client = _ToolClient()
    gen = al.agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        verbose=False,
        max_turns=2,  # R6-B P0-1：防无限循环（结果 MAX_TURNS_EXCEEDED）
        stop_predicate=lambda: False,
        on_before_llm=lambda m, t: None,
    )
    result = al.exhaust(gen)
    assert result["result"] not in ("STOPPED", "LLM_ERROR")
    # 工具结果应已进入 messages（tool 消息存在）
    assert any(m.get("role") == "tool" for m in result["messages"])


def test_chat_with_subagent_terminated_on_abandon(monkeypatch):
    """R1-P1-1：放弃 chat-with-* 工具时，同步子 Agent 实例 terminate_event 被置位
    （否则 clear_stop 清全局后子 Agent 谓词只剩 terminate → 逃逸单击停止）。"""
    import threading
    from agent.generic.llmcore import MockResponse, MockToolCall

    class _ChatWithClient:
        def __init__(self):
            self.last_tools = ""

        def chat(self, messages, tools=None):
            resp = MockResponse(
                thinking="",
                content="",
                tool_calls=[MockToolCall(name="chat-with-testagent", args={}, id="call_1")],
                raw="",
                usage={},
            )
            def _gen():
                yield from ()
                return resp
            return _gen()

    handler = _SlowToolHandler(delay=0.5)  # 慢速 dispatch（模拟卡在同步子 Agent loop）
    client = _ChatWithClient()
    stop_flag = {"v": False}
    threading.Timer(0.05, lambda: stop_flag.__setitem__("v", True)).start()

    # mock SubagentRegistry.get 返回带 terminate_event 的实例
    ev = threading.Event()
    from agent.subagent_registry import SubagentRegistry
    monkeypatch.setattr(SubagentRegistry, "get", lambda name: type("I", (), {"terminate_event": ev})())

    gen = al.agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        verbose=False,
        max_turns=2,  # R6-B P0-1：防御（放弃路径先返回）
        stop_predicate=lambda: stop_flag["v"],
        on_before_llm=lambda m, t: None,
    )
    result = al.exhaust(gen)
    assert result["result"] == "STOPPED"
    assert ev.is_set()  # 子 Agent 实例被 terminate
