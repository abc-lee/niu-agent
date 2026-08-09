"""_extract_context_from_messages 单元测试。

纯函数：直接构造 NiuRunner.__new__ 实例调用，不触 LLM/LightRAG。
核心回归：最近 2 条**对话**消息（跳过 tool 消息），assistant 附带工具名。
"""
import pytest

from agent.runner import NiuRunner


@pytest.fixture
def runner():
    """最小 NiuRunner 实例（不执行 __init__）。

    _extract_context_from_messages 仅用模块级 _smart_truncate，
    无需 patch（R1-P3：with 块内 return 的 mock.patch 是死补丁）。
    """
    return NiuRunner.__new__(NiuRunner)


def _msg(role, content="", tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def _tc(name):
    return {"function": {"name": name}}


def test_skip_tool_messages_keeps_user_intent(runner):
    """核心回归：尾部 tool 消息不占对话名额——user 意图保留在 query 中。

    Minimax H3 场景：user 说「你的Minimax H三提示词技能…」→ assistant
    「让我确认一下」（调 bash）→ tool 输出。旧实现取 assistant+tool，
    user 被挤出；修复后取 user+assistant+工具名。
    """
    messages = [
        _msg("user", "你的Minimax H三提示词技能应该正常加载进来了吧？"),
        _msg("assistant", "让我确认一下：", tool_calls=[_tc("bash")]),
        _msg("tool", '{"status": "success", "stdout": "total 24"}'),
    ]
    result = runner._extract_context_from_messages(messages)

    assert "Minimax H三" in result          # user 意图在场
    assert "assistant: 让我确认一下：" in result
    assert "tool: bash" in result            # 工具调用名附带
    assert "total 24" not in result          # 工具输出不占对话名额


def test_no_tool_messages_takes_last_two_dialogues(runner):
    """无工具调用时取最近 2 条对话（user/assistant 交替）。"""
    messages = [
        _msg("user", "第一轮问题"),
        _msg("assistant", "第一轮回答"),
        _msg("user", "第二轮问题"),
        _msg("assistant", "第二轮回答"),
    ]
    result = runner._extract_context_from_messages(messages)

    assert "第二轮问题" in result
    assert "第二轮回答" in result
    assert "第一轮问题" not in result
    assert "第一轮回答" not in result


def test_mixed_tool_trail_multiple_tools(runner):
    """多轮工具循环（≥2 条 assistant）仍保证 user 意图 + 最近 assistant 工具名。

    收集策略=各角色至多 1 条：最近 assistant（read+grep）+ 最近 user，
    旧 assistant（bash）不取——否则 2 条 assistant 会挤掉 user（R1-P1）。
    """
    messages = [
        _msg("user", "H3 相关请求"),
        _msg("assistant", "先看目录", tool_calls=[_tc("bash")]),
        _msg("tool", "ls 输出"),
        _msg("assistant", "再读文件", tool_calls=[_tc("read"), _tc("grep")]),
        _msg("tool", "文件内容"),
    ]
    result = runner._extract_context_from_messages(messages)

    assert "H3 相关请求" in result          # user 意图跨工具链保留
    assert "再读文件" in result
    assert "tool: read" in result
    assert "tool: grep" in result
    assert "tool: bash" not in result        # 旧 assistant 不取（各角色至多一次）
    assert "先看目录" not in result
    assert "ls 输出" not in result


def test_tool_call_succeeded_prefix_truncated(runner):
    """user 消息「工具调用成功」前缀取首行摘要（现状逻辑保留）。"""
    messages = [
        _msg("user", "工具调用成功\n写入 3 个文件\n全部完成"),
        _msg("assistant", "好的"),
    ]
    result = runner._extract_context_from_messages(messages)

    assert "工具调用成功" in result
    assert "全部完成" not in result


def test_single_message(runner):
    """只有 1 条对话消息时取 1 条。"""
    messages = [_msg("user", "唯一问题")]
    result = runner._extract_context_from_messages(messages)
    assert result == "user: 唯一问题"


def test_empty_messages(runner):
    """空消息列表返回空串。"""
    assert runner._extract_context_from_messages([]) == ""


def test_assistant_empty_content_still_contributes_tool_names(runner):
    """assistant content 为空但带 tool_calls：仍贡献工具名（现状行为保留）。"""
    messages = [
        _msg("user", "问题"),
        _msg("assistant", "", tool_calls=[_tc("bash")]),
        _msg("tool", "输出"),
    ]
    result = runner._extract_context_from_messages(messages)

    assert "tool: bash" in result
    assert "问题" in result
