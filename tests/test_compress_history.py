"""context-manager 模式二 history 构造测试。"""
import sys
from pathlib import Path

# 确保 niu_api 可 import
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_compress_history


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def test_build_compress_history_basic():
    """基本场景：3 条消息（user/assistant/user）构造 history，每条 content 加 idx 前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
        FakeMsg(id="msg-3", role="user", content="今天天气"),
    ]
    msg_tokens = [10, 20, 15]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"].startswith("[idx:1] 10tokens ")
    assert "你好" in history[0]["content"]
    assert history[1]["role"] == "assistant"
    assert history[1]["content"].startswith("[idx:2] 20tokens ")
    assert history[2]["role"] == "user"
    assert history[2]["content"].startswith("[idx:3] 15tokens ")
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_with_tool_calls():
    """assistant 带 tool_calls + tool 消息：保留 tool_calls/tool_call_id，content 加前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="查天气"),
        FakeMsg(
            id="msg-2", role="assistant", content="",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="今天晴", tool_call_id="tc-1"),
    ]
    msg_tokens = [5, 8, 12]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"] == messages[1].tool_calls
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "tc-1"
    assert history[2]["content"].startswith("[idx:3] 12tokens ")


def test_build_compress_history_protected_excludes_orphan_tool():
    """PROTECTED 排除 assistant(tool_calls) 后，其 tool 消息也同步排除（避免孤立 tool 导致 idx 错位）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端消息"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="user", content="近端消息"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant 受保护 → msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # 关键：msg-2(assistant, tool_calls) 不在保护集（protect_recent 只数最后1条 user/assistant = msg-4）
    # 所以 msg-2 不被排除，msg-3(tool) 也不被排除（父 assistant 在）
    # 此场景下 history 应含 msg-1, msg-2, msg-3（msg-4 排除）
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    # msg-4 被排除，其余 3 条保留，idx 连续 1,2,3
    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_protected_assistant_excludes_its_tool():
    """PROTECTED 排除 assistant(tool_calls) 时，其 tool 消息也同步排除（孤立 tool 检测）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="assistant", content="近端回复"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant = msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # msg-2(assistant, tool_calls) 不在保护集，保留
    # msg-3(tool, tc-1) 父 assistant msg-2 在，保留
    # 此场景 history 应含 msg-1, msg-2, msg-3
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}

    # 现在构造另一个场景：protect_recent=2，msg-2 和 msg-4 都受保护
    # msg-2 被排除 → msg-3(tool, tc-1) 父 assistant 不在 → 孤立 tool，必须同步排除
    history2, idx_to_id2 = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=2,
        exclude_protected=True,
    )
    # msg-2 和 msg-4 被排除，msg-3 孤立 tool 同步排除，只剩 msg-1
    assert len(history2) == 1
    assert idx_to_id2 == {1: "msg-1"}


def test_build_compress_history_out_msg_ids():
    """out_msg_ids 收集保留消息的真实 ID（与 idx 顺序一致，含孤立 tool 同步排除）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="a"),
        FakeMsg(id="msg-2", role="assistant", content="b"),
    ]
    out_msg_ids = []

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=[10, 20],
        out_msg_ids=out_msg_ids,
    )

    assert out_msg_ids == ["msg-1", "msg-2"]
    assert idx_to_id == {1: "msg-1", 2: "msg-2"}


def test_build_compress_history_no_tokens():
    """msg_tokens 为 None 时不加 tokens 前缀，只加 idx。"""
    messages = [FakeMsg(id="msg-1", role="user", content="你好")]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=None,
        out_msg_ids=None,
    )

    # 前缀格式 [idx:1] 内容（无 tokens）
    assert history[0]["content"].startswith("[idx:1] ")
    assert "你好" in history[0]["content"]
