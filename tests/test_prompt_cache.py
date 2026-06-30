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


def test_count_messages_tokens_handles_list_content():
    """count_messages_tokens 应兼容 list 格式 content（Claude cache_control 模式）。"""
    from agent.generic.agent_loop import count_messages_tokens

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "static content here"},
                {"type": "text", "text": "dynamic content here"},
            ],
        },
        {"role": "user", "content": "hello"},
    ]
    tokens = count_messages_tokens(messages)
    assert isinstance(tokens, int)
    assert tokens > 0


def test_agent_runner_loop_accepts_system_message_param():
    """agent_runner_loop 应支持可选 system_message 参数。"""
    from agent.generic.agent_loop import agent_runner_loop
    import inspect

    sig = inspect.signature(agent_runner_loop)
    params = sig.parameters
    assert "system_message" in params, "agent_runner_loop 应新增 system_message 可选参数"
    assert params["system_message"].default is None, "system_message 默认 None"
    assert "system_prompt" in params, "system_prompt 保留（向后兼容）"


def test_subagent_builds_static_and_dynamic_segments():
    """子 Agent 应构建静态段（agent.md + user_info）+ 动态段（Current Time）。"""
    from agent.subagent import build_subagent_system_segments

    static, dynamic = build_subagent_system_segments("file-processor")
    assert "Current Time" not in static, "静态段不应含 Current Time"
    assert "Current Time" in dynamic, "动态段应含 Current Time"
    assert len(static) > 100, "静态段应含 agent.md 正文"


def test_run_agent_loop_accepts_system_message_param():
    """_run_agent_loop 应支持可选 system_message 参数。"""
    from agent.subagent import _run_agent_loop
    import inspect

    sig = inspect.signature(_run_agent_loop)
    params = sig.parameters
    assert "system_message" in params, "_run_agent_loop 应新增 system_message 可选参数"


def test_refresh_user_memories_updates_static_and_recomputes_base():
    """memory 变化时 _refresh_user_memories 应同步更新 static_system_prompt
    并重算 base_system_prompt = static + dynamic_system_prefix。"""
    # niu_memory_server 不在默认 sys.path，需手动添加
    # （参考 tests/test_user_memory.py:10 的做法）
    import sys
    from pathlib import Path
    mem_src = Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"
    if str(mem_src) not in sys.path:
        sys.path.insert(0, str(mem_src))

    import threading
    from agent.runner import NiuRunner
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC <!--USER_MEMORY_START-->old<!--USER_MEMORY_END-->"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.base_system_prompt = runner.static_system_prompt + runner.dynamic_system_prefix
    runner._memory_dirty = threading.Event()
    runner._memory_dirty.set()

    # 直接调用 _refresh_user_memories，mock 内部读取
    import unittest.mock as mock
    new_memory_json = '{"permanent": [{"type": "memory", "content": "new memory"}]}'

    # runner.py 内 from niu_memory_server import _memory_file_lock 是函数内局部 import
    # 必须patch源模块属性，import时才能拿到patched引用
    fake_lock = type('FakeLock', (), {
        '__enter__': lambda self: None,
        '__exit__': lambda self, *a: None,
    })()
    with mock.patch('niu_memory_server._memory_file_lock', fake_lock), \
         mock.patch('pathlib.Path.read_text', return_value=new_memory_json), \
         mock.patch('agent.runner._render_permanent_section', return_value="<!--USER_MEMORY_START-->new memory<!--USER_MEMORY_END-->"):
        runner._refresh_user_memories([])

    # static_system_prompt 应已更新（old → new memory）
    assert "new memory" in runner.static_system_prompt, \
        f"static_system_prompt 应含 new memory，实际: {runner.static_system_prompt}"
    assert "<!--USER_MEMORY_START-->old<!--USER_MEMORY_END-->" not in runner.static_system_prompt, \
        "static_system_prompt 不应再含 old memory"

    # base_system_prompt 应等于 static + dynamic（重算后）
    assert runner.base_system_prompt == runner.static_system_prompt + runner.dynamic_system_prefix, \
        "base_system_prompt 应重算为 static + dynamic"
