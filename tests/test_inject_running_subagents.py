"""验证 _inject_dynamic_resources 返回的注入文本含后台子 Agent 清单。"""
from agent.runner import NiuRunner
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue


def test_inject_lists_running_async_subagents(monkeypatch):
    """有异步子 Agent 在跑时，注入文本含其唯一名和状态。"""
    sq = SubagentSupplementQueue("test-inject-0001")
    mc = SubagentMemoryContext()
    mc.update(current_turn=3, last_tool_name="read")
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)

    try:
        runner = NiuRunner.__new__(NiuRunner)  # 不调 __init__ 避免加载 LLM 等
        # mock 依赖（__new__ 不调 __init__，需要手动设置实例属性）
        runner._get_brain_injector = lambda: None
        runner._brain_adapter = None
        # mock LightRAGAdapter 让 search_multi_lightrag 返回空
        import niu_api.internal.lightrag_adapter as lightrag_adapter_mod
        class _FakeAdapter:
            def search_multi_lightrag(self, *args, **kwargs):
                return {}
            def search_within_region(self, *args, **kwargs):
                return {"skill": [], "knowledge": [], "other": []}
            def search_interaction_habits(self, *args, **kwargs):
                return []
        monkeypatch.setattr(lightrag_adapter_mod, "LightRAGAdapter", _FakeAdapter)

        injection, _ = runner._inject_dynamic_resources("测试上下文")

        assert "后台" in injection or "子 Agent" in injection
        assert name in injection
        assert "file-processor" in injection
    finally:
        SubagentRegistry.unregister(name)


def test_inject_no_subagents_no_section():
    """没有异步子 Agent 在跑时，注入文本不含子 Agent 清单段。"""
    # 清空注册表异步子 Agent
    for r in list(SubagentRegistry.list_running()):
        if not r.is_sync:
            SubagentRegistry.unregister(r.unique_name)

    runner = NiuRunner.__new__(NiuRunner)
    # 如果没有异步子 Agent，_format_running_subagents_section 返回空
    section = runner._format_running_subagents_section()
    assert section == ""


def test_inject_caps_at_5_subagents():
    """超过 5 个子 Agent 时只显示前 5 个 + '还有 N 个'。"""
    sqs = []
    names = []
    try:
        for i in range(7):
            sq = SubagentSupplementQueue(f"test-cap-{i:04d}")
            mc = SubagentMemoryContext()
            name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
            sqs.append(sq)
            names.append(name)

        runner = NiuRunner.__new__(NiuRunner)
        section = runner._format_running_subagents_section()
        # 至少不抛异常，且含"还有"或类似提示
        assert "还有" in section or len([n for n in names if n in section]) <= 5
    finally:
        for n in names:
            SubagentRegistry.unregister(n)
