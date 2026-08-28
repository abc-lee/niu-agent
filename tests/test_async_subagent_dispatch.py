"""验证 _dispatch_async_subagent 立即返回派单确认 + _run_subagent_async 后台跑完推完成通知到 MainAgentRequestQueue。

用离线 fake LLM 客户端（mock agent.runner.create_client）跑简短任务（"直接回复 OK"），不触网、不读真实配置。
"""
import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from agent.main_agent_request_queue import get_main_agent_request_queue
from agent.subagent_registry import SubagentRegistry


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
    """确定性离线 LLM 客户端——每次 chat() 直接返回 @end 终结的 final 回复（无工具调用）。

    子 Agent 协议：纯文本最终回复必须带 @end 标记，否则被拦截层判 FORMAT_ERROR 无限重试；
    这里用 "@end OK" 确定性地走 EXIT 结束路径（_run_subagent_async 内部 call_subagent
    不传 bypass_at_prefix，无法绕过 @end 协议）。
    """

    def __init__(self, reply_text="@end OK"):
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


def test_dispatch_async_subagent_returns_immediately_with_unique_name(llm_config):
    """_dispatch_async_subagent 立即返回，返回值含唯一名 + 使用说明。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    # 阶段二关键：_dispatch_async_subagent 依赖 niu_api.chat._main_loop（主 asyncio loop）
    # 测试必须设置 _main_loop，否则 _dispatch_async_subagent 返回错误
    from agent.subagent import _dispatch_async_subagent
    from niu_api.chat import set_main_event_loop

    # 清空 MainAgentRequestQueue
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)

    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    try:
        with patch("agent.runner.create_client", return_value=_FakeSubagentLLMClient()):
            _, result = _dispatch_async_subagent(
                agent_name="file-processor",
                task="直接回复 OK，不要调用任何工具",
                llm_config=llm_config,
            )

            assert "已派出子 Agent" in result or "file-processor-" in result
            assert "check_subagent_progress" in result
            assert "/stop" in result

            # 等子 Agent 跑完（避免影响下一个测试）——fake client 毫秒级完成，轮询等待上限 30s
            deadline = time.time() + 30
            while time.time() < deadline:
                if not [r for r in SubagentRegistry.list_running() if r.agent_type == "file-processor"]:
                    break
                time.sleep(0.1)

        # 子 Agent 应已注销
        running = [r for r in SubagentRegistry.list_running() if r.agent_type == "file-processor"]
        assert len(running) == 0, f"子 Agent 应已注销，但还有：{running}"
    finally:
        # 清空队列避免污染
        while q.pop() is not None:
            pass
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)


def test_run_subagent_async_pushes_completion_to_queue(llm_config):
    """_run_subagent_async 跑完后推 [子名] 已完成 到 MainAgentRequestQueue（不写 db）。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    from agent.subagent import _run_subagent_async
    from agent.subagent_memory import SubagentMemoryContext
    from agent.subagent_supplement import SubagentSupplementQueue
    from niu_api.chat import set_main_event_loop

    # 清空队列
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)

    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    sq = SubagentSupplementQueue("test-run-0001")
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)

    try:
        # 在 test_loop 里跑 _run_subagent_async（create_client 已 mock——离线 fake client）
        with patch("agent.runner.create_client", return_value=_FakeSubagentLLMClient()):
            future = asyncio.run_coroutine_threadsafe(
                _run_subagent_async(
                    unique_name=name,
                    agent_name="file-processor",
                    task="直接回复 OK，不要调用任何工具",
                    llm_config=llm_config,
                    memory_context=mc,
                    supplement_queue=sq,
                ),
                test_loop,
            )
            future.result(timeout=120)  # 等子 Agent 跑完

        # 验证 MainAgentRequestQueue 里有完成通知（不写 db，走内存队列）
        queued_msgs = []
        while not q.is_empty():
            queued_msgs.append(q.pop())

        # 应该有完成通知（content 格式 "[子名] 已完成，结果：..."）
        completion_found = any(
            "已完成" in m and name in m
            for m in queued_msgs
        )
        assert completion_found, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"

        # 子 Agent 应已注销
        assert SubagentRegistry.get(name) is None
    finally:
        # 清空队列避免污染
        while q.pop() is not None:
            pass
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
