"""LLM 调用前停止检查测试。

覆盖：on_before_llm 后、client.chat 前，stop_predicate True → STOPPED（不发起 LLM 调用）。
R1-B 修正：agent_runner_loop 签名无 messages 参数（用 system_prompt/user_input，消息函数内构造）；
MockResponse thinking 必选；stop 须在 on_before_llm 回调内置位（否则轮起始检查 L753 先拦截，测不到新检查点）。
参照 tests/test_agent_loop_stream_events.py 的既有 handler/client mock 模式。
"""
import pytest

from agent.generic import agent_loop as al
from agent.generic.llmcore import MockResponse


class _MinHandler:
    """最小 handler：满足 agent_runner_loop 的属性访问（参照既有测试 mock）。"""

    def __init__(self):
        self._is_subagent = False
        self._current_messages = []
        self.current_turn = 0
        self._last_prompt_tokens = 0
        self.last_tools = ""
        self._done_hooks = []  # R11-B P2-1：agent_loop L739 会重置，但 mock 必须自带

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass


class _Client:
    """记录 chat 是否被调用。"""

    def __init__(self):
        self.chat_called = 0
        self.last_tools = ""


def test_stop_before_llm_exits_without_chat():
    """stop 在 on_before_llm 回调内置位：LLM 前新检查点捕获 → STOPPED，client.chat 不被调用。"""
    handler = _MinHandler()
    client = _Client()
    stop_flag = {"v": False}

    def _on_before_llm(messages, turn):
        stop_flag["v"] = True  # 模拟动态注入（可中断化后放弃）后 stop 已置位

    gen = al.agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        verbose=False,
        stop_predicate=lambda: stop_flag["v"],
        on_before_llm=_on_before_llm,
    )
    result = al.exhaust(gen)
    assert result["result"] == "STOPPED"
    assert client.chat_called == 0


def test_no_stop_calls_chat():
    """stop 恒 False：正常发起 client.chat。"""
    handler = _MinHandler()
    client = _Client()

    def _fake_chat(messages, tools=None):
        client.chat_called += 1
        resp = MockResponse(thinking="", content="ok", tool_calls=[], raw="ok", usage={})
        def _gen():
            yield from ()
            return resp
        return _gen()

    client.chat = _fake_chat
    gen = al.agent_runner_loop(
        client=client,
        system_prompt="sys",
        user_input="hi",
        handler=handler,
        verbose=False,
        stop_predicate=lambda: False,
        on_before_llm=lambda m, t: None,
    )
    result = al.exhaust(gen)
    assert client.chat_called == 1
    assert result["result"] not in ("STOPPED", "LLM_ERROR")
