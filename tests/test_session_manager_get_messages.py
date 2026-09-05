"""get_messages 扩展单测（journal Task 1 清单，计划 §4.1 + browse-trim T2）。

覆盖：after_id 严格大于过滤 / invalid_after_id / transient / limit 三态
（缺省 200、封顶 1000、无 after_id 取末尾 N 条）/ has_more 语义 /
next_after_id / created_at 在场 /
after_time 时间水位（严格大于边界、空格/T 跨界归一、>limit 最旧 N 条分页、
与 after_id 共存、游标被过滤空批、无匹配空批、dispatch 转发断言）/
2000 字符裁剪契约（2000/2001 边界产物形状、CJK 多字节安全、全 role 受裁、
tool 两道折叠交互：ASCII tool 显示 <已折叠> / CJK tool 显示 <已精简>）/
message_id 单查（完整原文不受裁、分页参数忽略、invalid_message_id、
too_large 显式报错含 content_chars、通道线下完整返回）/
预算自管收缩（分页排空不重不漏 + 各页单形态 ≤29000 + _truncate_dict_result
恒等、尾取路径收缩后 idx 与存储序一致、remaining_in_batch 数值钉死）/
schema 双副本一致 / dispatch 转发 message_id。
全 mock store，无真实 DB / LLM / 图谱写入，messages.db 零写入。
"""

import asyncio
import itertools
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
    assert {"session_id", "after_id", "limit"} <= set(props)
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
    # 无收缩兼容守卫（AC5）：窗口内无超限消息 → 不触发体积自管，输出逐字节兼容
    assert "output_budget_truncated" not in result


def test_limit_capped_at_1000_budget_shrink():
    # 1200 条默认 content：limit 封顶仍取末尾 1000 条（m201..m1200），
    # 但整批序列化 ~120K > 29000 → 体积自管从页尾收缩，实际返回少于 1000。
    msgs = [_msg(i) for i in range(1, 1201)]  # 1200 条
    result = _call(msgs, limit=99999)
    ids = [m["id"] for m in result["messages"]]
    assert len(ids) < 1000
    # 批首不动（封顶窗口最旧端），丢的是最新端
    assert ids[0] == "m201"
    # 收缩元数据：remaining_in_batch = 收缩前批条数 − 返回条数
    assert result["output_budget_truncated"] is True
    assert result["remaining_in_batch"] == 1000 - len(ids)
    assert result["has_more"] is True
    assert result["next_after_id"] == ids[-1]
    # 单形态口径 ≤ 29000
    assert len(json.dumps(result, ensure_ascii=False)) <= 29000


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


# ============== 2000 字符裁剪契约 + tool 两道折叠（T2） ==============


def test_trim_boundary_2000_passthrough():
    """≤2000 字符原样；2001 字符 → 前 1200 + <已折叠> + 后 800（总长 2005）。"""
    exact = "b" * 2000
    over = "c" * 2001
    result = _call([_msg(1, content=exact), _msg(2, content=over)])
    assert result["messages"][0]["content"] == exact
    c = result["messages"][1]["content"]
    assert c == over[:1200] + "<已折叠>" + over[-800:]
    assert len(c) == 2005  # 1200 + marker(5) + 800


def test_trim_cjk_multibyte_char_level():
    """CJK 多字节安全：按字符级（非字节级）裁剪，产物形状同样 2005 字符。"""
    cjk = "中" * 3000  # 9000 UTF-8 字节、3000 字符
    result = _call([_msg(1, content=cjk)])
    c = result["messages"][0]["content"]
    assert len(c) == 2005
    assert c == cjk[:1200] + "<已折叠>" + cjk[-800:]


def test_all_roles_trimmed_at_2000():
    """所有 role 均受 2000 字符裁剪约束（旧「user/assistant 永不折叠」契约退役）。"""
    msgs = [
        _msg(1, role="user", content=_LONG),
        _msg(2, role="assistant", content=_LONG),
    ]
    result = _call(msgs)
    for m in result["messages"]:
        assert m["content"] == _LONG[:1200] + "<已折叠>" + _LONG[-800:]
        assert len(m["content"]) == 2005


def test_ascii_tool_double_fold_shows_trimmed_marker():
    """ASCII tool 两道折叠：第一道字节级折叠产物 2005 字符（1200+<已精简>+800）> 2000
    → 第二道字符裁剪再折为前 1200 + <已折叠> + 后 800，<已精简> 被切掉。"""
    msgs = [
        _msg(1, role="user"),
        _msg(2, role="assistant", content="tool call"),
        _msg(3, role="tool", content=_LONG),
    ]
    result = _call(msgs)
    content = result["messages"][2]["content"]
    assert "<已折叠>" in content
    assert "<已精简>" not in content
    assert len(content) == 2005
    # 头尾仍来自原文（全 'a' → 形状精确钉死）
    assert content == _LONG[:1200] + "<已折叠>" + _LONG[-800:]


def test_cjk_tool_first_fold_only():
    """CJK tool 两道折叠交互：9000 字节 > 2000 → 第一道折为 400+<已精简>+266=671 字符，
    < 2000 → 第二道 no-op——显示 <已精简> 而非 <已折叠>。"""
    cjk = "中" * 3000
    result = _call([_msg(1, role="tool", content=cjk)])
    c = result["messages"][0]["content"]
    assert "<已精简>" in c
    assert "<已折叠>" not in c
    assert len(c) < 2000


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
    # 无收缩兼容守卫（AC5）：窗口内无超限消息 → 两页均不触发体积自管
    assert "output_budget_truncated" not in result
    assert "output_budget_truncated" not in page2


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


# ============== message_id 单查（T2） ==============


def test_message_id_single_full_content():
    """单查返回完整原文（>2000 不受裁剪）；元素与遍历同型、不含 idx。"""
    long_cjk = "字" * 3000
    msgs = [_msg(1, content="short"), _msg(2, role="assistant", content=long_cjk)]
    result = _call(msgs, message_id="m2")
    assert result["status"] == "ok"
    m = result["message"]
    assert m["content"] == long_cjk
    assert m["id"] == "m2" and m["role"] == "assistant"
    assert set(m) == {"id", "tokens", "role", "content", "created_at"}


def test_message_id_single_tool_full_unfolded():
    """role=tool 超长 content 单查：返回存储原文（不受遍历两道折叠影响），
    含遍历 <已精简> 省略的中段——journal「需完整原文时 message_id 单查」契约。"""
    # 三段可区分（9000 字节 > 2000 触发第一道折叠）：头甲/中乙/尾丙——
    # 均匀字符串会让「中段被省略」前置断言恒假（任意子串都在头尾里）
    cjk = "甲" * 1400 + "乙" * 1200 + "丙" * 400
    msgs = [_msg(1, role="tool", content=cjk)]
    # 遍历形态：第一道字节级折叠 → 前 400（全甲）+ <已精简> + 后 266（全丙），中段乙省略
    folded = _call(msgs)["messages"][0]["content"]
    assert "<已精简>" in folded and len(folded) < 2000
    mid = cjk[1500:1600]  # 全乙（乙段 [1400,2600)）
    assert mid not in folded, "前置确认：中段确被遍历折叠省略"
    # 单查：完整原文，含被省略的中段
    result = _call(msgs, message_id="m1")
    assert result["status"] == "ok"
    m = result["message"]
    assert m["role"] == "tool"
    assert m["content"] == cjk and len(m["content"]) == 3000
    assert mid in m["content"]


def test_message_id_ignores_paging_params():
    """单查语义：after_time/after_id/limit 一律忽略。"""
    msgs = [_msg(i, created_at=_t(i)) for i in range(1, 6)]
    # 水位排除 m2（_t(3) > _t(2)）、after_id 越过 m2、limit=1——均不生效
    result = _call(msgs, message_id="m2", after_time=_t(3), after_id="m4", limit=1)
    assert result["status"] == "ok"
    assert result["message"]["id"] == "m2"


def test_message_id_invalid_reason():
    msgs = [_msg(1)]
    result = _call(msgs, message_id="nope")
    assert result.get("reason") == "invalid_message_id"
    assert "error" in result


def test_message_id_too_large_explicit_error():
    """单形态序列化 > 30000 → error+reason=too_large（含 content_chars），绝不静默截断。"""
    huge = "x" * 30000  # 单形态 ≈ 30122 > 30000（实测）
    result = _call([_msg(1, content=huge)], message_id="m1")
    assert result.get("reason") == "too_large"
    assert result["content_chars"] == 30000
    assert "error" in result and "30000" in result["error"]


def test_message_id_under_channel_limit_full():
    """对照：29000 ASCII（单形态 ≈ 29122 < 30000，实测）→ 完整返回不报错。"""
    ok = "y" * 29000
    result = _call([_msg(1, content=ok)], message_id="m1")
    assert result["status"] == "ok"
    assert result["message"]["content"] == ok


# ============== 预算自管收缩（T2） ==============


def test_budget_shrink_paging_drains_window():
    """混合 90 条（含 5000 字符 ASCII tool + 4000 字符 CJK）：全窗口序列化远超 29000 →
    首页必收缩；分页排空不重不漏，各页单形态 ≤ 29000（_truncate_dict_result 恒等——关口不触发截断）。"""
    from agent.generic.agent_loop import _truncate_dict_result

    msgs = []
    for i in range(1, 91):
        if i % 5 == 0:
            msgs.append(_msg(i, role="tool", content="x" * 5000, created_at=_t(i)))
        elif i % 7 == 0:
            msgs.append(_msg(i, role="user", content="中" * 4000, created_at=_t(i)))
        else:
            msgs.append(_msg(i, created_at=_t(i)))
    window_ids = [f"m{i}" for i in range(1, 91)]

    page = _call(msgs, after_time="2026-09-04T00:00:00", limit=200)
    pages = [page]
    while page["has_more"]:
        page = _call(
            msgs, after_time="2026-09-04T00:00:00",
            after_id=page["next_after_id"], limit=200,
        )
        pages.append(page)

    for p in pages:
        # 单形态口径 ≤ 29000 → 通道关口 _truncate_dict_result 原样返回（恒等）
        assert len(json.dumps(p, ensure_ascii=False)) <= 29000
        assert _truncate_dict_result(p) is p

    page_ids = [[m["id"] for m in p["messages"]] for p in pages]
    # 各页两两不相交 + union == 窗口 id 集（不重不漏，存储序）
    for a, b in itertools.combinations(page_ids, 2):
        assert not (set(a) & set(b))
    assert [i for ids in page_ids for i in ids] == window_ids
    assert pages[-1]["has_more"] is False

    # 首页收缩元数据：remaining_in_batch = 收缩前批条数 − 返回条数
    first = pages[0]
    assert first["output_budget_truncated"] is True
    assert first["remaining_in_batch"] == 90 - len(first["messages"])
    assert first["next_after_id"] == page_ids[0][-1]


def test_tail_path_shrink_idx_matches_storage_order():
    """尾取路径收缩：120 条大 content → selected=全部 120（base_index=0），
    收缩丢页尾（最新端）→ 返回最旧 k 条；idx 必须与存储序 1-based 位置一致
    （独立推导，不与实现共用公式）。"""
    big = "z" * 3000
    msgs = [_msg(i, content=big) for i in range(1, 121)]
    pos = {f"m{i}": i for i in range(1, 121)}  # 独立推导：存储序位置
    result = _call(msgs)
    ids = [m["id"] for m in result["messages"]]
    assert len(ids) < 120
    assert result["output_budget_truncated"] is True
    # 批首不动、丢的是最新端 → 返回恰为最旧 len(ids) 条
    assert ids == [f"m{i}" for i in range(1, len(ids) + 1)]
    for m in result["messages"]:
        assert m["idx"] == pos[m["id"]]
    # remaining_in_batch = 收缩前批条数（120）− 返回条数
    assert result["remaining_in_batch"] == 120 - len(ids)
    assert result["has_more"] is True
    assert result["next_after_id"] == ids[-1]
    assert len(json.dumps(result, ensure_ascii=False)) <= 29000


# ============== schema 双副本 + dispatch 转发（T2） ==============


def test_schema_dual_copy_consistent():
    """TOOL_SCHEMAS 与 list_tools 手写副本：description 逐字一致、inputSchema 逐属性深比较
    （type/description/required 全等，防单侧改 type 或 desc 另一侧静默分叉）；
    message_id 在场、已退役参数双副本均已移除（字符串拼接避免退役字面量污染全仓 grep——
    验收项 full_tool*output 源码树零命中，同 test_journal_daily_scheduler 先例）。"""
    from niu_session_manager import list_tools

    tools = asyncio.run(list_tools())
    gm = next(t for t in tools if t.name == "get_messages")
    src = TOOL_SCHEMAS["get_messages"]
    assert gm.description == src["description"]
    assert gm.inputSchema == src["input_schema"], \
        "inputSchema 双副本必须逐属性深比较一致（type/description/required）"
    assert "message_id" in gm.inputSchema["properties"]
    retired = "full_" + "tool_output"
    assert retired not in gm.inputSchema["properties"]
    assert retired not in src["input_schema"]["properties"]


def test_dispatch_forwards_message_id():
    """dispatch 层：message_id 必须转发——漏转发则走遍历裁剪，完整原文断言必挂。"""
    from niu_session_manager import call_tool as dispatch

    long_cjk = "字" * 3000
    msgs = [_msg(1, content="short"), _msg(2, role="assistant", content=long_cjk)]
    with patch("niu_session_manager._get_store", return_value=_store_with(msgs)):
        out = asyncio.run(dispatch(
            "get_messages",
            {"session_id": "default", "message_id": "m2"},
        ))
    payload = json.loads(out[0].text)
    assert payload["status"] == "ok"
    assert payload["message"]["content"] == long_cjk
