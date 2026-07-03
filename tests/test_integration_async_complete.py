"""端到端验证：异步派子 Agent → 跑完 → MainAgentRequestQueue 收到完成通知。

用真实 LLM + 真实 db_monitor（部分 mock 前端触发，因为完整前端链路在 Task 15 手动验证）。
"""
import os
import asyncio
import time
import threading
import pytest


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


def test_async_dispatch_and_completion_notification(llm_config, tmp_path):
    """异步派子 Agent → 跑完 → MainAgentRequestQueue 收到完成通知。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    # 阶段二关键：需要设置 _main_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe）
    from niu_api.chat import set_main_event_loop
    from agent.subagent import _dispatch_async_subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.main_agent_request_queue import get_main_agent_request_queue

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

    try:
        # 派异步子 Agent
        confirmation = _dispatch_async_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
        )

        assert "file-processor-" in confirmation

        # 等子 Agent 跑完（最多 60 秒）
        for _ in range(120):
            time.sleep(0.5)
            if SubagentRegistry.list_running() == []:
                break

        assert SubagentRegistry.list_running() == [], "子 Agent 应已注销"

        # 完成通知走 MainAgentRequestQueue 内存队列（不写 db，不需要 _poll_messages）
        # _run_subagent_async 完成时直接 push 到队列，测试主动 drain 验证
        queued_msgs = []
        while not q.is_empty():
            queued_msgs.append(q.pop())

        assert len(queued_msgs) >= 1, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"
        found = any("已完成" in s or "OK" in s for s in queued_msgs)
        assert found, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"
    finally:
        # 清空队列避免污染后续测试
        while q.pop() is not None:
            pass
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
