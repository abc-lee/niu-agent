"""Prompt cache 实施测试。"""


def test_claude_tools_get_cache_control():
    """Claude 模型的 tools_schema 末尾应打 cache_control breakpoint。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
        {"type": "function", "function": {"name": "tool2", "parameters": {}}},
    ]

    converted = _convert_tools_schema(tools, model="claude-sonnet-4-6")
    assert len(converted) == 2
    assert converted[-1].get("cache_control") == {"type": "ephemeral"}, \
        "Claude tools 末尾应有 cache_control breakpoint"
    assert "cache_control" not in converted[0]


def test_non_claude_tools_no_cache_control():
    """非 Claude 模型的 tools 不应有 cache_control。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
    ]

    converted = _convert_tools_schema(tools, model="ark-code-latest")
    assert len(converted) == 1
    assert "cache_control" not in converted[0]


def test_convert_tools_schema_backward_compatible():
    """不传 model 参数时应向后兼容（不给 tools 加 cache_control）。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
    ]

    # 不传 model（老调用方式）
    converted = _convert_tools_schema(tools)
    assert len(converted) == 1
    assert "cache_control" not in converted[0]


def test_build_static_system_prompt_excludes_current_time():
    """静态段不应包含 Current Time（Current Time 每分钟变化，会切断前缀 cache）。"""
    from agent.runner import NiuRunner

    static = NiuRunner._build_static_system_prompt()
    assert "Current Time" not in static, \
        f"静态段不应包含 Current Time，但找到: {static[-200:]}"
    assert len(static) > 500, \
        f"静态段应包含 niu.md 正文，长度应 > 500，实际 {len(static)}"
