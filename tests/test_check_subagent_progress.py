"""验证 check_subagent_progress 工具读 SubagentMemoryContext.snapshot() 返回进度。"""
from agent.handler import NiuHandler
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue


def _consume_generator(gen):
    """消费 dispatch 生成器，返回 StepOutcome。"""
    ret = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        ret = e.value
    return ret


def setup_function():
    """每个测试前清空注册表。"""
    SubagentRegistry._instances.clear()


def test_check_subagent_progress_returns_snapshot():
    """工具返回子 Agent 的最近一轮 LLM 对话进度。"""
    sq = SubagentSupplementQueue("test-progress-0001")
    mc = SubagentMemoryContext()
    mc.update(
        last_llm_request="请处理这个文件",
        last_llm_response="好的，我开始读取文件",
        current_turn=3,
        last_tool_name="read",
    )
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)

    try:
        handler = NiuHandler(mcp_client=None)
        gen = handler.dispatch("check_subagent_progress", {"subagent_name": name}, response=None, index=0)
        ret = _consume_generator(gen)

        assert ret is not None
        result = ret.data if hasattr(ret, 'data') else ret
        assert isinstance(result, str)
        assert "3" in result  # current_turn
        assert "read" in result or "读取" in result  # last_tool_name
    finally:
        SubagentRegistry.unregister(name)


def test_check_subagent_progress_unknown_name():
    """未知子 Agent 名返回提示。"""
    handler = NiuHandler(mcp_client=None)
    gen = handler.dispatch("check_subagent_progress", {"subagent_name": "nonexistent-xxxx"}, response=None, index=0)
    ret = _consume_generator(gen)

    result = ret.data if hasattr(ret, 'data') else ret
    assert "不在运行中" in str(result) or "不存在" in str(result)


def test_check_subagent_progress_sync_subagent_no_memory():
    """同步子 Agent（memory_context=None）返回提示无进度数据。"""
    sq = SubagentSupplementQueue("test-sync-prog-0001")
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, is_sync=True)

    try:
        handler = NiuHandler(mcp_client=None)
        gen = handler.dispatch("check_subagent_progress", {"subagent_name": name}, response=None, index=0)
        ret = _consume_generator(gen)

        result = ret.data if hasattr(ret, 'data') else ret
        assert "同步" in str(result) or "无进度" in str(result)
    finally:
        SubagentRegistry.unregister(name)
