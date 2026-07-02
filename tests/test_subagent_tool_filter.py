"""测试 subagent.py 的基础工具过滤逻辑（disableBaseTools + allowBaseTools + 默认黑名单）。"""
import pytest
from unittest.mock import patch
from agent import subagent


def _make_base_tools():
    """构造 6 个基础工具的 schema（模拟 get_tools_schema 返回）。"""
    names = ["bash", "code_run", "read", "write", "edit", "grep"]
    return [{"type": "function", "function": {"name": n}} for n in names]


def _run_filter(agent_config):
    """跑一遍 subagent._filter_base_tools 真实函数。

    调用 Task 1 Step 2a 抽取的真实函数，避免复制逻辑导致测试与代码失同步。
    """
    from agent.subagent import _filter_base_tools

    tools_schema = _make_base_tools()
    filtered, disabled_set, _, _ = _filter_base_tools(agent_config, tools_schema)
    return [t["function"]["name"] for t in filtered]


def test_default_blacklist_disables_bash_and_grep():
    """未配置任何字段时，默认禁用 bash 和 grep。"""
    result = _run_filter({})
    assert "bash" not in result, f"bash should be disabled by default, got {result}"
    assert "grep" not in result, f"grep should be disabled by default, got {result}"
    assert "code_run" in result
    assert "read" in result
    assert "write" in result
    assert "edit" in result


def test_disableBaseTools_adds_to_blacklist():
    """disableBaseTools 追加禁用到默认黑名单。"""
    result = _run_filter({"disableBaseTools": ["read", "write"]})
    assert "bash" not in result  # 默认黑名单
    assert "grep" not in result  # 默认黑名单
    assert "read" not in result  # 追加禁用
    assert "write" not in result  # 追加禁用
    assert "code_run" in result
    assert "edit" in result


def test_allowBaseTools_unblocks_default_blacklist():
    """allowBaseTools 从默认黑名单中解禁 bash。"""
    result = _run_filter({"allowBaseTools": ["bash"]})
    assert "bash" in result, f"bash should be allowed, got {result}"
    assert "grep" not in result  # 默认黑名单仍禁用 grep
    assert "code_run" in result
    assert "read" in result


def test_allowBaseTools_priority_over_disableBaseTools():
    """allowBaseTools 优先级高于 disableBaseTools（同时配置时 allow 胜出）。"""
    result = _run_filter({
        "disableBaseTools": ["bash", "read"],
        "allowBaseTools": ["bash"],
    })
    assert "bash" in result, f"bash should be allowed (allow wins), got {result}"
    assert "read" not in result  # 被 disableBaseTools 禁用，不在 allow 里
    assert "grep" not in result  # 默认黑名单


def test_dream_evolver_config_unblocks_bash():
    """dream-evolver 的预期配置：allowBaseTools 解禁 bash（skill 删除需要 mv 命令）。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "bash"]})
    assert "bash" in result
    assert "read" in result
    assert "write" in result
    assert "edit" in result
    assert "grep" not in result  # 默认黑名单仍禁用
    assert "code_run" in result  # 不在默认黑名单也不在 allowBaseTools，保留


def test_journal_agent_config():
    """journal-agent 的预期配置：allowBaseTools 解禁 read/write/edit/grep。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "grep"]})
    assert "read" in result
    assert "write" in result
    assert "edit" in result
    assert "grep" in result
    assert "bash" not in result  # 默认黑名单仍禁用
    assert "code_run" in result  # 不在默认黑名单也不在 allowBaseTools，保留


def test_event_manager_config_all_disabled():
    """event-manager 的预期配置：disableBaseTools 全禁。"""
    result = _run_filter({"disableBaseTools": ["bash", "code_run", "read", "write", "edit", "grep"]})
    assert result == [], f"event-manager should have no base tools, got {result}"


def test_default_blacklist_constant_exists():
    """确认 DEFAULT_DISABLED_BASE_TOOLS 常量已定义。"""
    from agent.subagent import DEFAULT_DISABLED_BASE_TOOLS
    assert "bash" in DEFAULT_DISABLED_BASE_TOOLS
    assert "grep" in DEFAULT_DISABLED_BASE_TOOLS


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
