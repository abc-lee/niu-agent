"""验证 call_subagent 传 memory_context 时，子 Agent 每轮 LLM 调用前后更新 memory_context。

用离线 fake LLM 客户端（mock agent.runner.create_client）跑简短任务（"直接回复 OK"），不触网、不读真实配置，
验证 memory_context.current_turn >= 1 且 last_llm_response 非空。
"""
from unittest.mock import patch

import pytest

from agent.subagent import call_subagent
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue


# ---------------------------------------------------------------------------
# 离线 fake LLM 客户端（mock agent.runner.create_client；不触网、不读真实配置）
# ---------------------------------------------------------------------------

class _FakeBackend:
    """client.backend 占位——call_subagent 会向其设置 stop_check。"""

    def __init__(self):
        self.stop_check = None


class _FakeSubagentResponse:
    """纯文本响应（无工具调用），满足 agent_runner_loop 的响应属性契约。"""

    def __init__(self, content="OK"):
        self.content = content
        self.tool_calls = []
        self.finish_reason = "stop"
        self.stream_error = False
        self.usage = None


class _OneShotResponseGen:
    """单步迭代器：首次 next() 立即抛 StopIteration(response)。

    对齐 agent_runner_loop 的消费契约——exhaust() 循环 next() 直到 StopIteration 并取其 value。
    """

    def __init__(self, response):
        self._response = response

    def __iter__(self):
        return self

    def __next__(self):
        raise StopIteration(self._response)


class _FakeSubagentLLMClient:
    """确定性离线 LLM 客户端——每次 chat() 直接返回纯文本回复（无工具调用）。"""

    def __init__(self, reply_text="OK"):
        self._reply_text = reply_text
        self.last_tools = ""
        self.backend = _FakeBackend()

    def chat(self, messages=None, tools=None, **kwargs):
        response = _FakeSubagentResponse(content=self._reply_text)
        return _OneShotResponseGen(response)


@pytest.fixture
def llm_config():
    """离线占位 LLM 配置——create_client 已被 mock（见 _FakeSubagentLLMClient），不读真实配置文件。"""
    return {
        "apikey": "offline-test-key",
        "apibase": "",
        "model": "fake-offline-model",
        "type": "openai",
    }


def test_call_subagent_updates_memory_context(llm_config):
    """call_subagent 传 memory_context 时，子 Agent 跑完后 memory_context 有数据。

    bypass_at_prefix=True：子 Agent 纯文本回复默认被 @end 协议拦截为 FORMAT_ERROR（无限重试）；
    bypass 让本测试走确定性单轮正常回复路径——该路径是更新 last_llm_response 的唯一路径，
    也是本测试要验证的"LLM 调用后更新 memory_context"钩子所在。
    """
    sq = SubagentSupplementQueue("test-mem-0001")
    mc = SubagentMemoryContext()

    with patch("agent.runner.create_client", return_value=_FakeSubagentLLMClient()):
        result = call_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
            supplement_queue=sq,
            memory_context=mc,
            bypass_at_prefix=True,
        )

    assert result and len(result) > 0, "子 Agent 应有非空回复"
    # 不强断言 "OK" in result——LLM 可能调 read 等工具后回复，内容可能不含字面 "OK"
    snap = mc.snapshot()
    assert snap["current_turn"] >= 1
    assert snap["last_llm_response"] is not None
    assert len(snap["last_llm_response"]) > 0


def test_call_subagent_without_memory_context_unchanged(llm_config):
    """call_subagent 不传 memory_context 时，行为与阶段一一致（不报错）。

    bypass_at_prefix=True：同测试1——让纯文本回复走正常路径而非 @end FORMAT_ERROR 重试。
    """
    sq = SubagentSupplementQueue("test-nomem-0001")

    with patch("agent.runner.create_client", return_value=_FakeSubagentLLMClient()):
        result = call_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
            supplement_queue=sq,
            # 不传 memory_context
            bypass_at_prefix=True,
        )

    assert result and len(result) > 0, "子 Agent 应有非空回复"
