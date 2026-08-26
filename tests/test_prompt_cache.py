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
    """静态段不含 Current Time/disk_desc/memory 段，仅含 niu.md 正文。

    memory 段由 _on_before_llm 每轮从 memory.json 重读，不在 static_system_prompt 里。
    """
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
    """非 Claude 模型：system content 是字符串静态区（static+disk+memory），
    不含 Current Time/injection；动态内容以动态块文本返回。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)  # 绕过 __init__ 的重资源加载
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = "ark-code-latest"


    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    dynamic = runner._assemble_system_message(messages, "", injection, model="ark-code-latest")

    content = messages[0]["content"]
    assert isinstance(content, str), "非 Claude 模型 content 应为字符串"
    assert content.startswith("STATIC_PART"), "静态区应在开头"
    assert content.endswith("...disk desc..."), "静态区应含 disk_desc"
    assert "Current Time" not in content, "system 静态区不应含 Current Time（D17）"
    assert "skill1" not in content, "system 静态区不应含 injection（D17）"
    assert "skill1" in dynamic and "Current Time" in dynamic, "动态块文本应含注入与时间"


def test_assemble_system_message_claude():
    """Claude 模型：system content 是 list，静态段末尾打 cache_control breakpoint。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = "claude-sonnet-4-6"


    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    dynamic = runner._assemble_system_message(messages, "", injection, model="claude-sonnet-4-6")

    content = messages[0]["content"]
    assert isinstance(content, list), "Claude 模型 content 应为 list"
    # D17：Claude 单 text 块（static+disk+memory），cache_control 打在块上
    assert len(content) == 1, "应为单块静态区"

    static_block = content[0]
    assert static_block["type"] == "text"
    assert static_block["text"] == "STATIC_PART\n\n### [虚拟磁盘工具]\n...disk desc..."
    assert static_block.get("cache_control") == {"type": "ephemeral"}, \
        "静态区必须打 cache_control breakpoint"

    assert "Current Time" not in static_block["text"], "静态区不含时间（D17）"
    assert "skill1" in dynamic and "Current Time" in dynamic, "动态块文本独立返回"



def test_assemble_system_message_memory_goes_to_static_region():
    """memory_section 拼入 system 静态区末尾（D17），不进动态块。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = ""
    runner.default_model = "ark-code-latest"

    messages = [{"role": "system", "content": ""}]
    dynamic = runner._assemble_system_message(messages, "MEM_SECTION", "INJ", model="ark-code-latest")

    content = messages[0]["content"]
    assert content == "STATIC\n\nMEM_SECTION"
    assert "MEM_SECTION" not in dynamic
    assert "INJ" in dynamic
    assert dynamic.index("INJ") < dynamic.index("Current Time"), "时间在动态块最后"


def test_dynamic_block_carrier_is_user_role_all_providers():
    """D19：全 provider 动态块载体统一 role=user，插在最后一个 user 消息之前。"""
    from agent.runner import NiuRunner

    for model in ("ark-code-latest", "deepseek-chat", "claude-sonnet-4-6"):
        runner = NiuRunner.__new__(NiuRunner)
        runner.static_system_prompt = "STATIC"
        runner.dynamic_system_prefix = ""
        runner.default_model = model

        messages = [
            {"role": "system", "content": "STATIC"},
            {"role": "user", "content": "历史输入"},
            {"role": "assistant", "content": "历史回复"},
            {"role": "user", "content": "当前输入"},
        ]
        dynamic = runner._assemble_system_message(messages, "", "INJ", model=model)
        runner._refresh_dynamic_user_block(messages, dynamic)

        block = messages[-2]
        assert messages[-1]["content"] == "当前输入"
        assert block["role"] == "user", f"{model}: 动态块载体必须是 user"
        assert block["content"].startswith("[系统动态信息]")
        assert "INJ" in block["content"]
        assert "Current Time" in block["content"]


def test_dynamic_block_idempotent_removal():
    """每轮刷新先移除上一轮旧块，多轮不叠加。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = ""
    runner.default_model = "ark-code-latest"

    messages = [
        {"role": "system", "content": "STATIC"},
        {"role": "user", "content": "输入1"},
    ]
    d1 = runner._build_dynamic_block("第一轮注入")
    runner._refresh_dynamic_user_block(messages, d1)
    assert sum(1 for m in messages if m["content"].startswith("[系统动态信息]")) == 1

    d2 = runner._build_dynamic_block("第二轮注入")
    runner._refresh_dynamic_user_block(messages, d2)
    blocks = [m for m in messages if m["content"].startswith("[系统动态信息]")]
    assert len(blocks) == 1, "旧动态块必须被移除，不得叠加"
    assert "第二轮注入" in blocks[0]["content"]
    assert messages[messages.index(blocks[0]) + 1] == {"role": "user", "content": "输入1"} or \
        messages[-1]["role"] == "user"


def test_dynamic_block_never_deletes_user_input_with_marker():
    """对抗场景：用户输入原文以「[系统动态信息]」开头时不被误删。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = ""
    runner.default_model = "ark-code-latest"

    user_text = "[系统动态信息] 帮我查这个标记是什么意思"
    messages = [
        {"role": "system", "content": "STATIC"},
        {"role": "user", "content": user_text},
    ]
    d1 = runner._build_dynamic_block("")
    runner._refresh_dynamic_user_block(messages, d1)

    d2 = runner._build_dynamic_block("")
    runner._refresh_dynamic_user_block(messages, d2)

    contents = [m.get("content") for m in messages]
    assert user_text in contents, "用户原文必须保留"


def test_dynamic_block_mid_turn_tool_tail_insert_position():
    """轮中工具循环：尾部是 tool 结果时，动态块仍插在最后一个 user 输入之前。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = ""
    runner.default_model = "ark-code-latest"

    messages = [
        {"role": "system", "content": "STATIC"},
        {"role": "user", "content": "当前输入"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "工具结果"},
    ]
    dynamic = runner._build_dynamic_block("轮中刷新")
    runner._refresh_dynamic_user_block(messages, dynamic)

    assert messages[1]["content"].startswith("[系统动态信息]"), "动态块紧贴本轮 user 输入语义位"
    assert messages[2]["content"] == "当前输入"
    assert messages[-1]["role"] == "tool", "tool 结果保持在尾部之后"


def test_first_turn_skeleton_has_no_dynamic_text():
    """chat() 入口空骨架：首条 system 只含静态指令+disk_desc，无 Current Time。"""
    from datetime import datetime as _real_datetime
    from unittest.mock import patch

    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = "ark-code-latest"

    fixed = _real_datetime(2026, 8, 13, 18, 30, 0)
    with patch("agent.runner.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        system_message = {"role": "system", "content": ""}
        runner._assemble_system_message([system_message], "", "", model="ark-code-latest")

    content = system_message["content"]
    assert content == "STATIC\n\n### [虚拟磁盘工具]\n...disk desc...", \
        "空骨架只含静态指令+disk_desc，不含任何动态文本"
    assert "Current Time" not in content


def test_supplement_and_dynamic_block_coexist_order():
    """supplement 由 agent_loop 在动态块之后追加：相对顺序=索引…→动态块→supplement/输入。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = ""
    runner.default_model = "ark-code-latest"

    messages = [
        {"role": "system", "content": "STATIC"},
        {"role": "user", "content": "当前任务"},
    ]
    dynamic = runner._build_dynamic_block("注入")
    runner._refresh_dynamic_user_block(messages, dynamic)
    # agent_loop 见缝插针：supplement 拼在 next_prompt 前、整体 append 在尾部
    messages.append({"role": "user", "content": "补充消息\n当前任务"})

    roles_contents = [(m["role"], m["content"]) for m in messages]
    assert ("user", "补充消息\n当前任务") == roles_contents[-1], "supplement 在最末"

    idx_dyn = next(i for i, m in enumerate(messages) if m["content"].startswith("[系统动态信息]"))
    assert idx_dyn < len(messages) - 1, "动态块在 supplement 之前，语义顺序稳定"


def test_assemble_system_message_non_system_first_msg():
    """messages[0] 不是 system 时应跳过（不抛异常）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    messages = [{"role": "user", "content": "hello"}]
    runner._assemble_system_message(messages, "", "inj", model="ark-code-latest")

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
    import inspect

    from agent.generic.agent_loop import agent_runner_loop

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
    import inspect

    from agent.subagent import _run_agent_loop

    sig = inspect.signature(_run_agent_loop)
    params = sig.parameters
    assert "system_message" in params, "_run_agent_loop 应新增 system_message 可选参数"
