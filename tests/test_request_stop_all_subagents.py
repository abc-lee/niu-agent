"""验证 request_stop_all_subagents 同时 cancel @niu-agent 拦截阻塞，避免双击停止死锁。"""
from agent.ask_main_agent import TERMINATED_SIGNAL, get_pending_ask_registry
from agent.runner import request_stop_all_subagents
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue


def test_request_stop_all_subagents_cancels_pending_ask():
    """双击停止时 request_stop_all_subagents 同时 cancel @niu-agent 拦截阻塞，避免死锁。"""
    sq = SubagentSupplementQueue("test-stop-all-0001")
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register(
        "file-processor",
        supplement_queue=sq,
        memory_context=mc,
        is_sync=False,
    )

    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)

        request_stop_all_subagents()

        answer = future.wait(timeout=1.0)
        assert answer == TERMINATED_SIGNAL

        items = sq.drain()
        assert len(items) == 1
        assert items[0].is_terminate is True
        assert items[0].content == "/stop"
    finally:
        SubagentRegistry.unregister(name)
