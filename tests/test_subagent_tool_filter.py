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
