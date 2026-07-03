"""验证异步子 Agent 的 tools_schema 包含 ask_main_agent；同步子 Agent 不包含。"""
from agent.subagent import _build_subagent_tools_schema


def test_async_subagent_includes_ask_main_agent():
    """file-processor（allowAsync=true）异步调用时 tools_schema 包含 ask_main_agent。"""
    # memory_context 非 None → 异步路径 → 应包含 ask_main_agent
    schema = _build_subagent_tools_schema("file-processor", memory_context=object())

    tool_names = [t.get("function", {}).get("name", "") for t in schema]
    assert "ask_main_agent" in tool_names


def test_sync_subagent_excludes_ask_main_agent():
    """同步调用时（memory_context None）tools_schema 不含 ask_main_agent（避免死锁）。"""
    schema = _build_subagent_tools_schema("file-processor", memory_context=None)

    tool_names = [t.get("function", {}).get("name", "") for t in schema]
    assert "ask_main_agent" not in tool_names
