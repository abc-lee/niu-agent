"""Task 2（视图渲染）：窗口头行 + 折叠占位符共享 helper。

覆盖 spec §4/§9：
- 头行渲染（有/无 pct），格式 `[输出#{rowid} · {tool_name} · 占上下文 {pct}%]`
- 折叠完成态占位符（含"已由 fold_tool_output 折叠：工具名(参数摘要≤80字符，无配对
  unknown)"+原占约 X%；pct=None 变体省略占比分句），以「获取]」收尾兼容 _is_tool_placeholder 识别
- 同一消息两轮渲染逐字节一致（固化不变式）
- tc_map=None（历史回放路径）/ 迁移失败降级 → 原样返回
- 压实路径一致性：build_compact_view 窗口段与常规组装同制式（R1 交叉 P1 回归锁）
"""

from types import SimpleNamespace

import pytest

from agent.context_assembler.compaction import build_compact_view
from agent.context_manager import ContextManager, build_tc_map, render_tool_content
from agent.session import MessageStore

CW = 5000  # 测试用上下文窗口


def msg(role, content, mid, rowid, tool_calls=None, tool_call_id=None,
        folded=0, output_pct=None):
    return SimpleNamespace(
        id=mid, rowid=rowid, role=role, content=content,
        tool_calls=tool_calls, tool_call_id=tool_call_id,
        created_at="2026-09-02T10:00:00",
        folded=folded, output_pct=output_pct,
    )


def assistant_with_tc(tc_id, name, arguments):
    return msg("assistant", "", f"a-{tc_id}", 1,
               tool_calls=[{"id": tc_id, "type": "function",
                            "function": {"name": name, "arguments": arguments}}])


TOOL_105 = msg("tool", "ORIGINAL_BODY", "t-105", 105,
               tool_call_id="tc1", folded=0, output_pct=4.2)
TC_MAP = build_tc_map([
    assistant_with_tc("tc1", "read_file", '{"path": "/x.py"}'),
    TOOL_105,
])


def test_header_line_with_pct():
    assert render_tool_content(TOOL_105, TC_MAP) == \
        "[输出#105 · read_file · 占上下文 4.2%]\nORIGINAL_BODY"


def test_header_line_without_pct():
    m = msg("tool", "BODY", "t-7", 7, tool_call_id="tc1", output_pct=None)
    assert render_tool_content(m, TC_MAP) == "[输出#7 · read_file]\nBODY"


def test_placeholder_with_pct():
    m = msg("tool", "SECRET_BODY", "t-105", 105,
            tool_call_id="tc1", folded=1, output_pct=4.2)
    assert render_tool_content(m, TC_MAP) == \
        '[输出#105 已由 fold_tool_output 折叠：read_file({"path": "/x.py"})，本条已移出上下文（原占约 4.2%）。如需原文请重新调用原工具获取]'


def test_placeholder_without_pct_omits_ratio_clause():
    m = msg("tool", "SECRET_BODY", "t-7", 7, tool_call_id="tc1",
            folded=1, output_pct=None)
    assert render_tool_content(m, TC_MAP) == \
        '[输出#7 已由 fold_tool_output 折叠：read_file({"path": "/x.py"})，本条已移出上下文。如需原文请重新调用原工具获取]'


def test_folded_placeholder_truncates_args_summary():
    # 占位符含参数摘要（LLM 重调原工具的通道）但截断 ≤80 字符——超长 args 不膨胀占位符
    long_args = '{"q": "' + "a" * 200 + '"}'
    m = assistant_with_tc("tc9", "search", long_args)
    _, args = build_tc_map([m])["tc9"]
    assert len(args) <= 80 and args == long_args[:80]
    folded = msg("tool", "B", "t-9", 9, tool_call_id="tc9", folded=1)
    rendered = render_tool_content(folded, build_tc_map([m]))
    assert f"search({args})，本条已移出上下文" in rendered and "a" * 80 not in rendered


def test_no_pairing_still_renders():
    # 无配对 assistant tool_calls 时工具名/参数摘要用 unknown 照常渲染
    m = msg("tool", "B", "t-3", 3, tool_call_id="tc-missing",
            folded=1, output_pct=1.5)
    assert render_tool_content(m, TC_MAP) == \
        "[输出#3 已由 fold_tool_output 折叠：unknown()，本条已移出上下文（原占约 1.5%）。如需原文请重新调用原工具获取]"


def test_tc_map_none_no_render_history_replay():
    # load_history 路径（tc_map=None）：不渲染头行/占位符，原样返回
    assert render_tool_content(TOOL_105, None) == "ORIGINAL_BODY"
    m = msg("tool", "B", "t-3", 3, tool_call_id="tc1", folded=1, output_pct=1.5)
    assert render_tool_content(m, None) == "B"


def test_fold_columns_unavailable_passthrough(monkeypatch):
    # 迁移失败降级（spec §8）：无头行无占位符，原样返回
    import agent.session as session_mod
    monkeypatch.setattr(session_mod, "_fold_columns_available", False)
    assert render_tool_content(TOOL_105, TC_MAP) == "ORIGINAL_BODY"


def test_non_tool_and_no_rowid_untouched():
    a = assistant_with_tc("tc1", "read_file", "{}")
    assert render_tool_content(a, TC_MAP) == ""
    u = msg("user", "hello", "u-1", 1)
    assert render_tool_content(u, TC_MAP) == "hello"
    no_rowid = msg("tool", "B", "t-x", 0, tool_call_id="tc1")
    assert render_tool_content(no_rowid, TC_MAP) == "B"


def test_fixed_invariant_two_rounds_identical():
    # 固化不变式：编号/工具名/pct 全部稳定来源，两轮渲染逐字节一致；DB 原文不动
    r1 = ContextManager._message_to_dict(TOOL_105, TC_MAP)
    r2 = ContextManager._message_to_dict(TOOL_105, TC_MAP)
    assert r1 == r2 and r1["content"].startswith("[输出#105 · read_file")
    assert TOOL_105.content == "ORIGINAL_BODY"  # 真相源 content 永不动


def test_is_tool_placeholder_recognizes_fold():
    # Global Constraint：占位符以「获取]」收尾，复用 _PLACEHOLDER_SUFFIX 既有判定（识别集不扩展）
    from agent.generic.agent_loop import _is_tool_placeholder
    folded = msg("tool", "B", "t-105", 105, tool_call_id="tc1",
                 folded=1, output_pct=4.2)
    assert _is_tool_placeholder(render_tool_content(folded, TC_MAP)) is True
    no_pct = msg("tool", "B", "t-7", 7, tool_call_id="tc1", folded=1)
    assert _is_tool_placeholder(render_tool_content(no_pct, TC_MAP)) is True


def test_header_plus_body_ending_suffix_not_misjudged():
    # T2 P3：头行使窗口 tool 消息 startswith("[") 恒真——原文恰好以「获取]」收尾的未折叠
    # 多行消息不得被误判为占位符（否则应急裁剪 _placeholderize_tool_outputs 会跳过它）
    from agent.generic.agent_loop import _is_tool_placeholder
    m = msg("tool", "正文…如需原文请重新调用该工具获取]", "t-105", 105,
            tool_call_id="tc1", folded=0, output_pct=4.2)
    rendered = render_tool_content(m, TC_MAP)
    assert rendered.startswith("[") and rendered.endswith("获取]") and "\n" in rendered
    assert _is_tool_placeholder(rendered) is False


@pytest.fixture
def ratio_one():
    """倍率固定 1.0，隔离真实持久化状态。"""
    import agent.context_assembler.calibration as cal
    old = cal._cached_ratio
    cal._cached_ratio = 1.0
    yield
    cal._cached_ratio = old


def test_compact_view_window_same_format(ratio_one, tmp_path):
    """R1 交叉 P1 回归锁：压实路径不经过 get_context_for_chat——窗口段必须与
    常规组装同制式（头行/占位符），否则 folded 内容全文复活、视图抖动再破缓存。"""
    messages = [
        msg("user", "question 0", "u0", 1),
        msg("assistant", "answer 0", "a0", 2),
        msg("user", "question 1", "u1", 3),
        assistant_with_tc("tc1", "read_file", '{"path": "/x.py"}'),  # rowid=4
        msg("tool", "OLD_BODY", "t1", 5, tool_call_id="tc1", output_pct=2.0),
        msg("user", "question 2", "u2", 6),
        assistant_with_tc("tc2", "search", '{"q": "niu"}'),          # rowid=7
        msg("tool", "FOLDED_BODY", "t2", 8, tool_call_id="tc2",
            folded=1, output_pct=3.0),
    ]
    view, _stats = build_compact_view(
        messages, system_msg=None, keep_turns=2,
        blocks_db_path=tmp_path / "b.db", context_window_tokens=CW,
    )
    window = view[1:]  # 首条为索引前导 user 消息
    by_tc = {e.get("tool_call_id"): e for e in window if e.get("role") == "tool"}
    assert set(by_tc) == {"tc1", "tc2"}
    # 折叠条：占位符（原文 FOLDED_BODY 不得复活）
    assert by_tc["tc2"]["content"] == \
        '[输出#8 已由 fold_tool_output 折叠：search({"q": "niu"})，本条已移出上下文（原占约 3.0%）。如需原文请重新调用原工具获取]'
    # 未折叠条：头行 + 原文
    assert by_tc["tc1"]["content"] == "[输出#5 · read_file · 占上下文 2.0%]\nOLD_BODY"
    # 与常规路径共享 helper 逐字节一致（同制式锁）
    tc_map = build_tc_map(messages)
    for m in messages:
        if m.role == "tool":
            assert by_tc[m.tool_call_id]["content"] == render_tool_content(m, tc_map)


async def test_get_context_for_chat_renders_header(tmp_path):
    """常规组装路径接线：get_context_for_chat 窗口 tool 消息带头行（tc_map 已传）。"""
    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    await store.add_message(role="user", content="question")
    await store.add_message(role="assistant", content="",
                            tool_calls=[{"id": "tc1", "type": "function",
                                         "function": {"name": "read_file",
                                                      "arguments": "{}"}}])
    await store.add_message(role="tool", content="TOOLBODY",
                            tool_call_id="tc1", output_pct=2.5)
    cm = ContextManager(store, max_tokens=CW, blocks_db_path=tmp_path / "b.db")
    view = await cm.get_context_for_chat(exclude_last=False)
    tool_entries = [e for e in view if e.get("role") == "tool"]
    assert len(tool_entries) == 1
    assert tool_entries[0]["content"] == "[输出#3 · read_file · 占上下文 2.5%]\nTOOLBODY"
