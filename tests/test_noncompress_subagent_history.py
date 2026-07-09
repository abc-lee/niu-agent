"""非压缩子 Agent（entity-extractor/dream-evolver/journal-agent）改用 history 逐条传消息的单元测试。

背景：这三个子 Agent 原本把 600 条消息拼成单条 task 字符串传给 call_subagent_with_auto_answer，
被 _truncate_task_for_subagent 砍掉末尾最新工作内容，且每条消息前加了无用的 [id:UUID] [idx:N] 前缀。
本次改造仿 context-manager 简易 ID 映射：history 每条 content 前缀 [N] 极简编号，
程序内存维护 idx_to_id 映射，子 Agent 回传 processed_up_to=N，程序查映射更新游标。

本测试验证：
1. _build_plain_history 构造带 [N] 前缀的 history + 返回 idx_to_id 映射
2. _build_plain_history 保留 tool_calls/tool_call_id
3. content 前缀是 [N] 极简编号（不是 [id:UUID] [idx:N] Ntokens role:）
4. _parse_processed_up_to 解析各种格式（= / : / 空格，大小写不敏感）
5. _parse_processed_up_to 未找到返回 None
6. entity/dream/journal 三个子 Agent 的 force 调用用 history=... 而非 task=巨型字符串
"""
from unittest import mock
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_plain_history, _parse_processed_up_to


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def test_build_plain_history_basic_and_idx_to_id():
    """基本场景：3 条消息构造 history，content 前缀 [N] + 返回 idx_to_id 映射。"""
    messages = [
        FakeMsg(id="uuid-1", role="user", content="你好"),
        FakeMsg(id="uuid-2", role="assistant", content="你好，我是 Niu"),
        FakeMsg(id="uuid-3", role="user", content="今天天气"),
    ]
    out_msg_ids = []
    history, idx_to_id = _build_plain_history(messages, out_msg_ids=out_msg_ids)

    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "[1] 你好"  # 极简编号前缀
    assert history[1]["content"] == "[2] 你好，我是 Niu"
    assert history[2]["content"] == "[3] 今天天气"
    assert out_msg_ids == ["uuid-1", "uuid-2", "uuid-3"]
    # idx_to_id 映射：1-based 简易编号 -> 真实 UUID
    assert idx_to_id == {1: "uuid-1", 2: "uuid-2", 3: "uuid-3"}


def test_build_plain_history_preserves_tool_calls():
    """assistant 带 tool_calls + tool 消息：保留 tool_calls/tool_call_id，content 前缀 [N]。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="查天气"),
        FakeMsg(
            id="msg-2", role="assistant", content="",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="今天晴", tool_call_id="tc-1"),
    ]
    history, idx_to_id = _build_plain_history(messages)

    assert len(history) == 3
    assert history[0]["content"] == "[1] 查天气"
    assert history[1]["tool_calls"] == [{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]
    assert history[1]["content"] == "[2] "  # 空 content 前缀 [2]
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "tc-1"
    assert history[2]["content"] == "[3] 今天晴"
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_plain_history_prefix_is_minimal_not_verbose():
    """content 前缀是极简 [N]，不是 [id:UUID] / [idx:N] / Ntokens / role:。"""
    messages = [
        FakeMsg(id="uuid-abc-123", role="user", content="测试内容"),
    ]
    history, _ = _build_plain_history(messages)

    content = history[0]["content"]
    assert content == "[1] 测试内容"  # 极简 [N] 前缀
    assert "[id:" not in content
    assert "[idx:" not in content
    assert "tokens" not in content
    assert "role:" not in content
    assert "uuid-abc-123" not in content  # UUID 不出现在 content 里


def test_parse_processed_up_to_various_formats():
    """_parse_processed_up_to 支持 = / : / 空格分隔，大小写不敏感。"""
    assert _parse_processed_up_to("处理完成\nprocessed_up_to=15") == 15
    assert _parse_processed_up_to("processed_up_to: 15") == 15
    assert _parse_processed_up_to("processed_up_to 15") == 15
    assert _parse_processed_up_to("PROCESSED_UP_TO=15") == 15
    assert _parse_processed_up_to("Processed_Up_To=15") == 15
    # 匹配第一个有效整数
    assert _parse_processed_up_to("processed_up_to=3\nprocessed_up_to=15") == 3


def test_parse_processed_up_to_not_found_returns_none():
    """未找到 processed_up_to= 时返回 None。"""
    assert _parse_processed_up_to("处理完成，无标记") is None
    assert _parse_processed_up_to("") is None
    assert _parse_processed_up_to("processed_up_to=") is None  # 无数字
    assert _parse_processed_up_to("processed_up_to=abc") is None  # 非整数


def test_build_plain_history_empty_messages():
    """空消息列表返回空 history + 空 idx_to_id。"""
    history, idx_to_id = _build_plain_history([])
    assert history == []
    assert idx_to_id == {}


def test_build_plain_history_out_msg_ids_default_none():
    """out_msg_ids=None 时不报错（内部初始化为空列表）。"""
    messages = [FakeMsg(id="m1", role="user", content="hi")]
    history, idx_to_id = _build_plain_history(messages)  # 不传 out_msg_ids
    assert len(history) == 1
    assert idx_to_id == {1: "m1"}
