"""验证 _dispatch_async_subagent 立即返回派单确认 + _run_subagent_async 后台跑完推完成通知到 MainAgentRequestQueue。

用真实 LLM 调一个简短任务（"直接回复 OK"）。
"""
import os
import asyncio
import threading
import time
import pytest
from agent.subagent_registry import SubagentRegistry
from agent.main_agent_request_queue import get_main_agent_request_queue


@pytest.fixture
def llm_config():
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apiKey") or llm.get("apikey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_dispatch_async_subagent_returns_immediately_with_unique_name(llm_config):
    """_dispatch_async_subagent 立即返回，返回值含唯一名 + 使用说明。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    # 阶段二关键：_dispatch_async_subagent 依赖 niu_api.chat._main_loop（主 asyncio loop）
    # 测试必须设置 _main_loop，否则 _dispatch_async_subagent 返回错误
    from niu_api.chat import set_main_event_loop
    from agent.subagent import _dispatch_async_subagent

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
        result = _dispatch_async_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
        )

        assert "已派出子 Agent" in result or "file-processor-" in result
        assert "check_subagent_progress" in result
        assert "/stop" in result

        # 等子 Agent 跑完（避免影响下一个测试）
        time.sleep(20)

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

    from niu_api.chat import set_main_event_loop
    from agent.subagent import _run_subagent_async
    from agent.subagent_memory import SubagentMemoryContext
    from agent.subagent_supplement import SubagentSupplementQueue

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
        # 在 test_loop 里跑 _run_subagent_async
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
