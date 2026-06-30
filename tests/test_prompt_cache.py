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
    """静态段不含 Current Time/disk_desc，含 niu.md 正文 + memory 段。"""
    from agent.runner import NiuRunner

    static = NiuRunner._build_static_system_prompt()

    # 不含动态段内容
    assert "Current Time" not in static, \
        f"静态段不应包含 Current Time，但找到: {static[-200:]}"

    # 含 niu.md 正文（身份描述关键词——niu.md 以"你是一个全能型个人AI助理"开头）
    assert "全能型" in static or "Role: Niu" in static or "Niu Agent" in static, \
        f"静态段应含 niu.md 正文关键词，实际开头: {static[:100]}"

    assert len(static) > 500, \
        f"静态段应包含 niu.md 正文，长度应 > 500，实际 {len(static)}"


def test_assemble_system_message_non_claude():
    """非 Claude 模型：system content 是字符串，静态段在开头且稳定。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)  # 绕过 __init__ 的重资源加载
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    runner._assemble_system_message(messages, injection, model="ark-code-latest")

    content = messages[0]["content"]
    assert isinstance(content, str), "非 Claude 模型 content 应为字符串"
    assert content.startswith("STATIC_PART"), "静态段应在开头"
    assert "Current Time" in content
    assert "skill1" in content


def test_assemble_system_message_claude():
    """Claude 模型：system content 是 list，静态段末尾打 cache_control breakpoint。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "claude-sonnet-4-6"

    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    runner._assemble_system_message(messages, injection, model="claude-sonnet-4-6")

    content = messages[0]["content"]
    assert isinstance(content, list), "Claude 模型 content 应为 list"
    assert len(content) == 2, "应为两段：静态段 + 动态段"

    static_block = content[0]
    assert static_block["type"] == "text"
    assert static_block["text"] == "STATIC_PART"
    assert static_block.get("cache_control") == {"type": "ephemeral"}, \
        "静态段末尾必须有 cache_control breakpoint"

    dynamic_block = content[1]
    assert dynamic_block["type"] == "text"
    assert "Current Time" in dynamic_block["text"]
    assert "skill1" in dynamic_block["text"]
    assert "cache_control" not in dynamic_block, "动态段不应有 cache_control"


def test_assemble_system_message_empty_injection():
    """injection 为空时动态段只含 Current Time + disk_desc。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    messages = [{"role": "system", "content": ""}]
    runner._assemble_system_message(messages, "", model="ark-code-latest")

    content = messages[0]["content"]
    assert content == "STATIC\n\nCurrent Time: 2026-06-30 10:51:00"


def test_assemble_system_message_non_system_first_msg():
    """messages[0] 不是 system 时应跳过（不抛异常）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    messages = [{"role": "user", "content": "hello"}]
    runner._assemble_system_message(messages, "inj", model="ark-code-latest")

    assert messages[0]["content"] == "hello"
