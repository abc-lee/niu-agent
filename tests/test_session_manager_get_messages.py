"""get_messages 扩展单测（journal Task 1 清单，计划 §4.1）。

覆盖：after_id 严格大于过滤 / invalid_after_id / transient / limit 三态
（缺省 200、封顶 1000、无 after_id 取末尾 N 条）/ has_more 语义 /
next_after_id / tool 折叠三例（超限折叠+<已精简>、full_tool_output=true 不折叠、
user/assistant 不折叠）/ created_at 在场。
全 mock store，无真实 DB / LLM / 图谱写入，messages.db 零写入。
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(
    0, os.path.join(_PROJECT_ROOT, "mcp-servers", "session-manager", "src")
)

from niu_session_manager import TOOL_SCHEMAS, get_messages  # noqa: E402

# ============== 构造工具 ==============


def _msg(i, role="user", content=None, created_at=None):
    return SimpleNamespace(
        id=f"m{i}",
        role=role,
        content=content if content is not None else f"content {i}",
        created_at=created_at or f"2026-08-26T00:{i % 60:02d}:00",
        tool_calls=None,
    )


def _store_with(messages):
    class _FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return list(messages)

    return _FakeStore()


def _call(messages, **kwargs):
    with patch("niu_session_manager._get_store", return_value=_store_with(messages)):
        return get_messages("default", **kwargs)


_LONG = "a" * 3000  # > 2000 字节折叠阈值


# ============== Schema 断言 ==============


def test_schema_has_new_params():
    props = TOOL_SCHEMAS["get_messages"]["input_schema"]["properties"]
    assert {"session_id", "after_id", "limit", "full_tool_output"} <= set(props)
    assert TOOL_SCHEMAS["get_messages"]["input_schema"]["required"] == ["session_id"]
    # session_id 占位说明
    assert "default" in props["session_id"]["description"]


# ============== after_id 过滤与错误语义 ==============


def test_after_id_strictly_greater():
    msgs = [_msg(i) for i in range(1, 11)]
    result = _call(msgs, after_id="m4")
    assert "error" not in result
    ids = [m["id"] for m in result["messages"]]
    assert ids == ["m5", "m6", "m7", "m8", "m9", "m10"]


def test_after_id_invalid_reason():
    msgs = [_msg(i) for i in range(1, 6)]
    result = _call(msgs, after_id="nonexistent")
    assert result.get("reason") == "invalid_after_id"
    assert "error" in result


def test_store_exception_transient_reason():
    class _BrokenStore:
        async def get_messages(self, limit=None, before_id=None):
            raise RuntimeError("db locked")

    with patch("niu_session_manager._get_store", return_value=_BrokenStore()):
        result = get_messages("default")
    assert result.get("reason") == "transient"
    assert "error" in result


# ============== limit 三态 ==============


def test_limit_default_200_tail_without_after_id():
    msgs = [_msg(i) for i in range(1, 206)]  # 205 条
    result = _call(msgs)
    ids = [m["id"] for m in result["messages"]]
    assert len(ids) == 200
    # 无 after_id 取末尾最新 N 条：m6..m205
    assert ids[0] == "m6" and ids[-1] == "m205"
    # 本批已含最新消息 → has_more False
    assert result["has_more"] is False
    assert result["next_after_id"] == "m205"


def test_limit_capped_at_1000():
    msgs = [_msg(i) for i in range(1, 1201)]  # 1200 条
    result = _call(msgs, limit=99999)
    ids = [m["id"] for m in result["messages"]]
    assert len(ids) == 1000
    assert ids[0] == "m201" and ids[-1] == "m1200"


def test_limit_no_after_id_takes_tail_n():
    msgs = [_msg(i) for i in range(1, 11)]
    result = _call(msgs, limit=3)
    ids = [m["id"] for m in result["messages"]]
    assert ids == ["m8", "m9", "m10"]


# ============== has_more / next_after_id 语义 ==============


def test_has_more_true_only_when_newer_exist():
    msgs = [_msg(i) for i in range(1, 11)]
    page1 = _call(msgs, after_id="m4", limit=3)
    assert [m["id"] for m in page1["messages"]] == ["m5", "m6", "m7"]
    assert page1["has_more"] is True
    assert page1["next_after_id"] == "m7"

    page2 = _call(msgs, after_id="m7", limit=3)
    assert [m["id"] for m in page2["messages"]] == ["m8", "m9", "m10"]
    # 已到最新消息 → 无更新消息存在
    assert page2["has_more"] is False
    assert page2["next_after_id"] == "m10"


def test_empty_batch_next_after_id_none():
    msgs = [_msg(i) for i in range(1, 6)]
    result = _call(msgs, after_id="m5")
    assert result["messages"] == []
    assert result["has_more"] is False
    assert result["next_after_id"] is None


# ============== tool 折叠三例 + created_at ==============


def test_oversized_tool_content_folded():
    msgs = [
        _msg(1, role="user"),
        _msg(2, role="assistant", content="tool call"),
        _msg(3, role="tool", content=_LONG),
    ]
    result = _call(msgs)
    folded = result["messages"][2]
    content = folded["content"]
    assert "<已精简>" in content
    # 头部在场
    assert content.startswith(_LONG[:100])
    # 尾部在场
    assert content.endswith(_LONG[-100:])
    assert len(content.encode("utf-8")) < len(_LONG.encode("utf-8"))


def test_full_tool_output_true_not_folded():
    msgs = [_msg(1, role="tool", content=_LONG)]
    result = _call(msgs, full_tool_output=True)
    assert result["messages"][0]["content"] == _LONG


def test_user_assistant_never_folded():
    msgs = [
        _msg(1, role="user", content=_LONG),
        _msg(2, role="assistant", content=_LONG),
    ]
    result = _call(msgs)
    assert result["messages"][0]["content"] == _LONG
    assert result["messages"][1]["content"] == _LONG


def test_created_at_present_on_every_message():
    msgs = [
        _msg(1, created_at="2026-08-26T09:00:00"),
        _msg(2, role="tool", content=_LONG, created_at="2026-08-26T09:00:05"),
    ]
    result = _call(msgs)
    assert result["messages"][0]["created_at"] == "2026-08-26T09:00:00"
    # 折叠不影响 created_at
    assert result["messages"][1]["created_at"] == "2026-08-26T09:00:05"


def test_created_at_missing_attr_tolerated():
    msg = SimpleNamespace(id="m1", role="user", content="hi")  # 无 created_at
    result = _call([msg])
    assert result["messages"][0]["created_at"] == ""
