"""端到端验证：ask_main_agent 阻塞期间收 /stop 不死锁。

场景：
  1. 异步子 Agent 调 ask_main_agent 问"我应该用 OCR 吗" → future.wait() 阻塞
  2. 主 Agent 发 @子名 /stop（用 route_message 同步路由）
  3. db_monitor route_message /stop：cancel_pending_ask + 推 supplement queue (is_terminate=True)
  4. ask_main_agent 工具解除阻塞，返回 terminated 状态
  5. 子 Agent 下一轮 drain 到 /stop，调 LLM 生成总结，退出

用真实 LLM + 真实 route_message（同步路由，绕过 db 写入）。
"""
import os
import asyncio
import threading
import time
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
        "apibase": llm.get("apiBase") or llm.get("apibase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_ask_main_agent_during_stop_no_deadlock(llm_config, tmp_path):
    """ask_main_agent 阻塞期间收 /stop 不死锁。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    # 阶段二关键：需要设置 _main_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe）
    from niu_api.chat import set_main_event_loop
    from niu_api import db_monitor
    from agent.subagent import _dispatch_async_subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.ask_main_agent import get_pending_ask_registry
    from agent.main_agent_request_queue import get_main_agent_request_queue

    # 清空队列
    q = get_main_agent_request_queue()
    while not q.is_empty():
        q.pop()

    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)

    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    try:
        # 派异步子 Agent，任务设计成"必须问主 Agent"才能完成
        # _dispatch_async_subagent 是同步函数，内部用 run_coroutine_threadsafe 提交到 _main_loop（即 test_loop）
        # 所以 test_loop 必须 run_forever 中，否则提交会失败
        confirmation = _dispatch_async_subagent(
            agent_name="file-processor",
            task=(
                "你需要处理一个文件，但不确定是否需要 OCR。"
                "请用 ask_main_agent 工具询问主 Agent：'这个 PDF 是扫描件吗？需要 OCR 吗？'"
                "然后根据主 Agent 的回答决定下一步。"
            ),
            llm_config=llm_config,
        )

        # 提取子 Agent 唯一名
        unique_name = None
        for r in SubagentRegistry.list_running():
            if r.agent_type == "file-processor":
                unique_name = r.unique_name
                break
        assert unique_name is not None

        # 等子 Agent 进入 ask_main_agent 阻塞（最多 30 秒）
        reg = get_pending_ask_registry()

        entered_ask = False
        for _ in range(60):
            time.sleep(0.5)
            with reg._lock:
                if unique_name in reg._futures:
                    entered_ask = True
                    break

        if not entered_ask:
            # 子 Agent 没调 ask_main_agent（LLM 行为不确定），跳过
            # 但必须先清理：取消异步子 Agent 的 task future，避免残留线程污染后续测试
            instance = SubagentRegistry.get(unique_name)
            if instance is not None and instance.task is not None:
                try:
                    instance.task.cancel()
                except Exception:
                    pass
            # 直接调 route_message 路由 /stop（模拟主 Agent 发 /stop，绕过 db 写入）
            try:
                db_monitor.route_message(target=unique_name, sender="主Agent", content="/stop")
            except Exception:
                pass
            # 等子 Agent 退出（最多 30 秒）
            for _ in range(60):
                time.sleep(0.5)
                if not any(r.unique_name == unique_name for r in SubagentRegistry.list_running()):
                    break
            pytest.skip("子 Agent 没调 ask_main_agent，无法测试死锁场景")

        # 直接调 route_message 路由 /stop（模拟主 Agent 发 /stop，绕过 db 写入）
        db_monitor.route_message(target=unique_name, sender="主Agent", content="/stop")

        # route_message 已同步路由 /stop（cancel_pending_ask + 推 supplement queue），不需要 _poll_messages

        # 验证子 Agent 不死锁：在 60 秒内退出
        for _ in range(120):
            time.sleep(0.5)
            if not any(r.unique_name == unique_name for r in SubagentRegistry.list_running()):
                break
        else:
            pytest.fail("子 Agent 死锁——ask_main_agent 阻塞期间收 /stop 后未退出")

        # 验证子 Agent 已注销
        assert SubagentRegistry.get(unique_name) is None

        # 验证 MainAgentRequestQueue 收到子 Agent 终止/完成/异常通知（不写 db，走内存队列）
        queued = []
        while not q.is_empty():
            queued.append(q.pop())

        # 子 Agent ask_main_agent 被 cancel 后走终止总结退出，_run_subagent_async 推完成或异常通知
        assert any(unique_name in m for m in queued), f"MainAgentRequestQueue 应含子 Agent 通知：{queued}"
    finally:
        # 清空 MainAgentRequestQueue 避免污染后续测试
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        while not q.is_empty():
            q.pop()
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
