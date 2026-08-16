"""测试 subagent.py 的基础工具白名单过滤逻辑（allowBaseTools 白名单制）。

白名单语义：缺省零基础工具，allowBaseTools 声明哪个有哪个。
与 MCP 工具（mcpServers/mcpToolFilter）的白名单模型一致。
"""
from agent import subagent


def _make_base_tools():
    """构造 6 个基础工具的 schema（与 agent/generic/assets/tools_schema.json 一致）。"""
    return [
        {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
        for n in ["bash", "code_run", "read", "write", "edit", "grep"]
    ]


def _run_filter(agent_config):
    """跑一遍 subagent._filter_base_tools 真实函数。"""
    from agent.subagent import _filter_base_tools

    tools_schema = _make_base_tools()
    filtered, _ = _filter_base_tools(agent_config, tools_schema)
    return [t["function"]["name"] for t in filtered]


def test_default_no_base_tools():
    """缺省（未配置 allowBaseTools）：没有任何基础工具（白名单缺省为零）。"""
    assert _run_filter({}) == []


def test_allowBaseTools_whitelist():
    """allowBaseTools 声明的工具保留，其余全部移除。"""
    result = _run_filter({"allowBaseTools": ["read", "write"]})
    assert sorted(result) == ["read", "write"]


def test_allowBaseTools_all_six():
    """allowBaseTools 声明全部 6 个基础工具。"""
    result = _run_filter(
        {"allowBaseTools": ["bash", "code_run", "read", "write", "edit", "grep"]}
    )
    assert sorted(result) == ["bash", "code_run", "edit", "grep", "read", "write"]


def test_allowBaseTools_unknown_name_ignored_with_warning():
    """allowBaseTools 中不存在的工具名（笔误）：忽略并打 warning 日志。

    注意：agent/subagent.py 用 loguru（不是标准 logging），
    pytest caplog 捕获不到，必须用 loguru sink 捕获。
    """
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        result = _run_filter({"allowBaseTools": ["reed", "read"]})
    finally:
        logger.remove(sink_id)
    assert result == ["read"]
    assert any("unknown tool names" in m and "reed" in m for m in messages)


def test_allowBaseTools_empty_list_means_no_tools():
    """allowBaseTools: [] 显式空列表 = 零基础工具（与缺省一致）。"""
    assert _run_filter({"allowBaseTools": []}) == []


def test_context_manager_config():
    """context-manager 白名单配置：只有 read。"""
    assert _run_filter({"allowBaseTools": ["read"]}) == ["read"]


def test_dream_evolver_config():
    """dream-evolver 白名单配置：read/write/edit/bash（无 code_run、无 grep）。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "bash"]})
    assert sorted(result) == ["bash", "edit", "read", "write"]
    assert "code_run" not in result
    assert "grep" not in result


def test_journal_agent_config():
    """journal-agent 白名单配置：read/write/edit/grep。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "grep"]})
    assert sorted(result) == ["edit", "grep", "read", "write"]


def test_mcp_only_agent_no_base_tools():
    """纯 MCP 子 Agent（entity-extractor/event-manager/file-processor 形态）：
    只有 mcpServers 没有 allowBaseTools → 零基础工具。"""
    assert _run_filter({"mcpServers": ["lightrag-server"]}) == []


# ── 以下 4 个测试与白名单改造无关，是既有职责边界注入契约，
#    仅本文件覆盖，全文重写时必须原样保留 ──────────────────────────


def test_boundary_section_template_exists():
    """确认 _BOUNDARY_SECTION_TEMPLATE 常量已定义。"""
    from agent.subagent import _BOUNDARY_SECTION_TEMPLATE
    assert "## 职责边界" in _BOUNDARY_SECTION_TEMPLATE
    assert "不要猜测含义" in _BOUNDARY_SECTION_TEMPLATE
    assert "直接退出" in _BOUNDARY_SECTION_TEMPLATE


def test_build_subagent_system_segments_injects_boundary_when_missing(monkeypatch):
    """子 Agent 正文没有"直接退出"语义时，自动注入通用模板。"""
    from agent.subagent import build_subagent_system_segments
    # mock get_subagent_prompt 返回不含"直接退出"的正文
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "你是测试子 Agent。")
    # mock _build_user_info_section 返回空
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("test-agent")
    assert "## 职责边界" in static_system
    assert "不要猜测含义" in static_system


def test_build_subagent_system_segments_skips_injection_when_present(monkeypatch):
    """子 Agent 正文已含"直接退出"语义时，不重复注入。"""
    from agent.subagent import build_subagent_system_segments
    # 模拟 dream-evolver 场景：正文已有"## 职责边界"段且含"直接退出"语义
    custom_boundary = "## 职责边界\n\n这是子 Agent 自定义的边界规则，无法确认职责范围就要直接退出，回复主 Agent。"
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: f"你是测试子 Agent。\n\n{custom_boundary}")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("test-agent")
    # 自定义边界保留
    assert custom_boundary in static_system
    # 通用模板的"不要猜测含义"不应出现（因为已跳过自动注入）
    assert "不要猜测含义" not in static_system
    # "## 职责边界"标题只出现 1 次（不重复注入）
    assert static_system.count("## 职责边界") == 1, "should not inject twice"


def test_build_subagent_system_segments_injects_for_dream_evolver_existing_section(monkeypatch):
    """dream-evolver 场景：正文已有"## 职责边界"段但不含"直接退出"语义，应触发注入追加退出语义。"""
    from agent.subagent import build_subagent_system_segments
    # 模拟 dream-evolver.md:32 现状：有"## 职责边界"标题但内容是职责声明，无"直接退出"
    existing_section = "## 职责边界\n\n- 你负责精加工实体\n- 你不负责从零提取新实体"
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: f"你是 dream-evolver。\n\n{existing_section}")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("dream-evolver")
    # 通用模板被追加（因为原文不含"直接退出"）
    assert "不要猜测含义" in static_system
    assert "直接退出" in static_system
    # 原"## 职责边界"段保留
    assert "你负责精加工实体" in static_system
    # 标题出现 2 次：原文 1 次 + 模板 1 次（这是预期行为，dream-evolver 的旧段不含退出语义需追加）
    assert static_system.count("## 职责边界") == 2


# ── E4-09：MCP 工具加载失败——[] 返回 + task 标注（LLM 输入可见，不进 tools_schema） ──


def test_get_subagent_mcp_tools_schema_exception_returns_empty_list(monkeypatch):
    """E4-09：get_subagent_mcp_tools_schema 内部异常 → 返回 [] + logger.error（不返回裸字符串）。

    返回裸字符串会导致 _build_subagent_tools_schema 的 tools_schema + mcp_tools_schema 抛
    list+str TypeError——必须返回空列表。
    """
    from loguru import logger

    from agent import subagent

    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {"mcpServers": ["lightrag-server"]})

    def _boom():
        raise RuntimeError("registry boom")

    monkeypatch.setattr("agent.tool_registry.get_registry", _boom)

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        schema = subagent.get_subagent_mcp_tools_schema("test-agent")
    finally:
        logger.remove(sink_id)
    assert schema == []  # 空列表而非裸字符串——防 list+str TypeError
    assert any("MCP 工具" in m and "registry boom" in m for m in messages), (
        f"应记录 error 含异常文本，实际: {messages}"
    )


def _call_subagent_capture(monkeypatch, agent_config, mcp_schema):
    """hermetic call_subagent：capture user_input + tools_schema（范式照抄 test_subagent_current_time）。"""
    from unittest.mock import Mock

    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured["user_input"] = user_input
        captured["tools_schema"] = tools_schema
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: agent_config)
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: mcp_schema)
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    return captured


def test_call_subagent_annotates_mcp_tools_load_failure(monkeypatch):
    """E4-09：mcpServers 非空 + 0 工具可用 → task 附加 ⚠ 标注（LLM 输入可见）；
    标注绝不进入 tools_schema（tools_schema 仍是空列表）。"""
    from agent import subagent

    captured = _call_subagent_capture(
        monkeypatch,
        agent_config={"mcpServers": ["lightrag-server"]},
        mcp_schema=[],
    )
    subagent.call_subagent(
        agent_name="test-agent",
        task="处理文件",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert captured["user_input"].startswith(
        "⚠ 配置的 MCP 工具加载失败——0 工具可用（预期能力不完整）"
    ), captured["user_input"]
    assert "处理文件" in captured["user_input"]
    # 标注在 task 文本而非 tools_schema
    assert captured["tools_schema"] == []
    assert all("MCP 工具加载失败" not in str(t) for t in captured["tools_schema"])


def test_call_subagent_no_annotation_when_mcp_not_configured(monkeypatch):
    """E4-09：mcpServers 配置为空 → 不标注（纯基础工具有意设计）。"""
    from agent import subagent

    captured = _call_subagent_capture(monkeypatch, agent_config={}, mcp_schema=[])
    subagent.call_subagent(
        agent_name="test-agent",
        task="处理文件",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert "MCP 工具加载失败" not in captured["user_input"]


def test_call_subagent_no_annotation_when_mcp_tools_loaded(monkeypatch):
    """E4-09：MCP 工具正常加载 → 不标注（正常路径零变化）。"""
    from agent import subagent

    mcp_tools = [{
        "type": "function",
        "function": {"name": "lightrag_query", "description": "q", "parameters": {}},
    }]
    captured = _call_subagent_capture(
        monkeypatch,
        agent_config={"mcpServers": ["lightrag-server"]},
        mcp_schema=mcp_tools,
    )
    subagent.call_subagent(
        agent_name="test-agent",
        task="处理文件",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert "MCP 工具加载失败" not in captured["user_input"]
    assert [t["function"]["name"] for t in captured["tools_schema"]] == ["lightrag_query"]
