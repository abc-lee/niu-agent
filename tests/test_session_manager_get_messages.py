"""get_messages 扩展单测（journal Task 1 清单，计划 §4.1）。

覆盖：after_id 严格大于过滤 / invalid_after_id / transient / limit 三态
（缺省 200、封顶 1000、无 after_id 取末尾 N 条）/ has_more 语义 /
next_after_id / tool 折叠三例（超限折叠+<已精简>、full_tool_output=true 不折叠、
user/assistant 不折叠）/ created_at 在场 /
after_time 时间水位（严格大于边界、空格/T 跨界归一、>limit 最旧 N 条分页、
与 after_id 共存、游标被过滤空批、无匹配空批、dispatch 转发断言）。
全 mock store，无真实 DB / LLM / 图谱写入，messages.db 零写入。
"""

import asyncio
import json
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


def _t(i):
    """单调递增 'T' 分隔时间戳（2026-09-04T00:00:00 起第 i 秒）。

    现 _msg 默认 created_at 用 i%60 分钟回绕，>60 条时非单调；
    after_time >limit 分页用例必须用本夹具保证严格单调。
    """
    return f"2026-09-04T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}"


def _t_us(i, us):
    """_t(i) 的微秒版——真实库 created_at 恒带 6 位微秒（P1 边界用例）。"""
    return f"{_t(i)}.{us:06d}"


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


# ============== after_time 时间水位（journal 游标改造 T1） ==============


def test_after_time_strictly_greater():
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 7)]
    # 水位等于 m3 的时间戳 → 严格大于，m3 被排除
    result = _call(msgs, after_time=_t(3))
    assert [m["id"] for m in result["messages"]] == ["m4", "m5", "m6"]


def test_after_time_microsecond_boundary_excluded():
    """P1 边界锁：真实库 created_at 恒带微秒（...21:49:12.726768），落款水位截断到秒
    （...21:49:12）——全精度比较会使边界消息自身（前缀相等、更长者大）落入下轮窗口
    （空批不可达/跨天重复条目）。秒粒度比较必须排除同秒边界消息。"""
    msgs = [
        _msg(1, created_at=_t_us(5, 123456)),  # 边界消息自身：水位同秒 + 微秒尾
        _msg(2, created_at=_t_us(6, 0)),       # 严格之后
    ]
    # 水位 = _t(5)（秒级，等价于落款截断值）
    result = _call(msgs, after_time=_t(5))
    assert [m["id"] for m in result["messages"]] == ["m2"], (
        "同秒微秒尾消息必须被秒粒度水位排除——否则边界消息恒被下轮重拉"
    )


def test_after_time_space_separator_normalized():
    # 跨界格式：空格分隔水位 × 'T' 分隔库值。
    # 裸比较时第 10 字节 ' '(0x20) < 'T'(0x54)，全部库值都会误判为"之后"；
    # 归一化后只剩严格大于水位的 m3。
    msgs = [
        _msg(1, created_at="2026-09-04T10:59:59"),
        _msg(2, created_at="2026-09-04T11:00:00"),
        _msg(3, created_at="2026-09-04T11:00:01"),
    ]
    result = _call(msgs, after_time="2026-09-04 11:00:00")
    assert [m["id"] for m in result["messages"]] == ["m3"]


def test_after_time_first_page_oldest_n_and_paging():
    # >limit 场景：过滤集 250 条取 200 → 首页最旧 200 条 + has_more；续页拉完剩余
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 251)]
    result = _call(msgs, after_time="2026-09-04T00:00:00", limit=200)
    ids = [m["id"] for m in result["messages"]]
    assert len(ids) == 200 and ids[0] == "m1" and ids[-1] == "m200"
    assert result["has_more"] is True
    assert result["next_after_id"] == "m200"
    # total_messages / idx 基于过滤后序列（同参照系）
    assert result["total_messages"] == 250
    assert [m["idx"] for m in result["messages"]] == list(range(1, 201))

    page2 = _call(msgs, after_time="2026-09-04T00:00:00", after_id="m200", limit=200)
    assert [m["id"] for m in page2["messages"]] == [f"m{i}" for i in range(201, 251)]
    assert page2["has_more"] is False
    # next_after_id = 本批最后一条 id（与存量约定一致，仅空批为 None）
    assert page2["next_after_id"] == "m250"
    assert page2["total_messages"] == 250
    assert [m["idx"] for m in page2["messages"]] == list(range(201, 251))


def test_after_time_with_after_id_coexist():
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 7)]
    # 水位保留 m3-m6，after_id 再约束到 m3 之后 → 同时满足取交集
    result = _call(msgs, after_time=_t(2), after_id="m3")
    assert [m["id"] for m in result["messages"]] == ["m4", "m5", "m6"]

    # after_id 比水位更靠后 → 取更严的一侧
    result2 = _call(msgs, after_time=_t(1), after_id="m4")
    assert [m["id"] for m in result2["messages"]] == ["m5", "m6"]


def test_after_time_cursor_filtered_out_empty_batch():
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 7)]
    # after_id=m3 在全量存在但被水位排除（_t(3) ≤ _t(5)）；filtered=[m6]（_t(5) 自身
    # 严格大于排除 m5）→ 游标在 filtered 中定位失败 → 空批。
    # total_messages=len(filtered)=1 钉过滤后参照系（与 no_match 空批 total=0 对照）
    result = _call(msgs, after_time=_t(5), after_id="m3")
    assert result["messages"] == []
    assert result["has_more"] is False
    assert result["next_after_id"] is None
    assert result["total_messages"] == 1


def test_after_time_no_match_empty_batch():
    # 水位在未来 → 无匹配，空批语义
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 6)]
    result = _call(msgs, after_time="2099-01-01T00:00:00")
    assert result["messages"] == []
    assert result["has_more"] is False
    assert result["next_after_id"] is None
    assert result["total_messages"] == 0


def test_dispatch_forwards_after_time():
    # dispatch 层断言：call_tool 必须转发 after_time。
    # 若漏转发，get_messages 走缺省尾取返回全部 10 条，与期望的过滤后 5 条不符。
    from niu_session_manager import call_tool as dispatch

    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 11)]
    with patch("niu_session_manager._get_store", return_value=_store_with(msgs)):
        out = asyncio.run(dispatch(
            "get_messages",
            {"session_id": "default", "after_time": _t(5)},
        ))
    payload = json.loads(out[0].text)
    assert [m["id"] for m in payload["messages"]] == ["m6", "m7", "m8", "m9", "m10"]


def test_mcp_tool_schema_after_time():
    # MCPTool list_tools 手写 schema 副本与 TOOL_SCHEMAS 同步（四处同步之②）
    from niu_session_manager import list_tools

    tools = asyncio.run(list_tools())
    gm = next(t for t in tools if t.name == "get_messages")
    assert "after_time" in gm.inputSchema["properties"]
    assert gm.inputSchema["properties"]["after_time"]["type"] == "string"


def test_schema_after_time_property():
    props = TOOL_SCHEMAS["get_messages"]["input_schema"]["properties"]
    assert "after_time" in props
    assert props["after_time"]["type"] == "string"
    desc = props["after_time"]["description"]
    assert "严格大于" in desc and "可共存" in desc
