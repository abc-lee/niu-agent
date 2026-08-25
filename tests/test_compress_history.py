"""context-manager 模式二 history 构造测试。"""
import sys
from pathlib import Path

# 确保 niu_api 可 import
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_compress_history  # noqa: E402


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
