"""Task 2（视图渲染）：窗口头行 + 折叠占位符共享 helper。

覆盖 spec §4/§9：
- 头行渲染（有/无 pct），格式 `[可折叠编号 {rowid} · {tool_name} · 占上下文 {pct}%]`
  （可折叠编号 = messages.db rowid，非序号）
- 折叠完成态占位符单行 `[已折叠：工具名(参数摘要≤80字符，无配对 unknown)，原占约 X%。]`
  （pct=None 变体省略占比分句；**不带编号**——用户定案），以「[已折叠：」前缀
  被 _is_tool_placeholder 识别（旧带编号 `[输出#N 已折叠：…]` 与更早旧文案作兼容锚照认）
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
        "[可折叠编号 105 · read_file · 占上下文 4.2%]\nORIGINAL_BODY"


def test_header_line_without_pct():
    m = msg("tool", "BODY", "t-7", 7, tool_call_id="tc1", output_pct=None)
    assert render_tool_content(m, TC_MAP) == "[可折叠编号 7 · read_file]\nBODY"


def test_placeholder_with_pct():
    m = msg("tool", "SECRET_BODY", "t-105", 105,
            tool_call_id="tc1", folded=1, output_pct=4.2)
    assert render_tool_content(m, TC_MAP) == \
        '[已折叠：read_file({"path": "/x.py"})，原占约 4.2%。]'


def test_placeholder_without_pct_omits_ratio_clause():
    m = msg("tool", "SECRET_BODY", "t-7", 7, tool_call_id="tc1",
            folded=1, output_pct=None)
    assert render_tool_content(m, TC_MAP) == \
        '[已折叠：read_file({"path": "/x.py"})。]'


def test_folded_placeholder_truncates_args_summary():
    # 占位符含参数摘要（LLM 重调原工具的通道）但截断 ≤80 字符——超长 args 不膨胀占位符
    long_args = '{"q": "' + "a" * 200 + '"}'
    m = assistant_with_tc("tc9", "search", long_args)
    _, args = build_tc_map([m])["tc9"]
    assert len(args) <= 80 and args == long_args[:80]
    folded = msg("tool", "B", "t-9", 9, tool_call_id="tc9", folded=1)
    rendered = render_tool_content(folded, build_tc_map([m]))
    assert f"search({args})" in rendered and "a" * 80 not in rendered


def test_no_pairing_still_renders():
    # 无配对 assistant tool_calls 时工具名/参数摘要用 unknown 照常渲染
    m = msg("tool", "B", "t-3", 3, tool_call_id="tc-missing",
            folded=1, output_pct=1.5)
    assert render_tool_content(m, TC_MAP) == \
        "[已折叠：unknown()，原占约 1.5%。]"


@pytest.mark.parametrize("rowid", [3, 105, 1_234_567])
def test_folded_placeholder_carries_no_number(rowid):
    """已折叠占位符**不带编号**（用户定案：占位符回到无编号原形态）：
    模型只需看到「已折叠」形态即知不必重复折叠；
    取回原文仍靠参数摘要重调原工具（编号不是取回通道）。"""
    m = msg("tool", "SECRET_BODY", f"t-{rowid}", rowid,
            tool_call_id="tc1", folded=1, output_pct=4.2)
    rendered = render_tool_content(m, TC_MAP)
    assert rendered.startswith("[已折叠：")
    assert not rendered.startswith("[输出#") and f"#{rowid}" not in rendered  # legacy 兼容锚形态不得出现
    assert 'read_file({"path": "/x.py"})' in rendered          # 工具名 + 参数摘要（重调通道）
    assert rendered == '[已折叠：read_file({"path": "/x.py"})，原占约 4.2%。]'
    assert "SECRET_BODY" not in rendered                        # 原文不复活


def test_is_tool_placeholder_round_trip_with_rendered_fold():
    """渲染→判定往返锁：无编号折叠占位符 True（防应急 ToolCrop 重复折叠），
    未折叠渲染（头行+换行+原文）恒多行 → False，旧带编号/无编号兼容锚仍 True。"""
    from agent.generic.agent_loop import _is_tool_placeholder
    folded = msg("tool", "B", "t-105", 105, tool_call_id="tc1",
                 folded=1, output_pct=4.2)
    rendered = render_tool_content(folded, TC_MAP)
    assert rendered.startswith("[已折叠：") and "\n" not in rendered
    assert _is_tool_placeholder(rendered) is True
    # 未折叠渲染：头行+换行+原文恒多行，不误判为占位符
    assert _is_tool_placeholder(render_tool_content(TOOL_105, TC_MAP)) is False
    # 旧带编号形态（兼容锚，恢复会话兼容）：legacy [输出#N 已折叠：…] 仍 True
    assert _is_tool_placeholder("[输出#105 已折叠：read_file({})，原占约 4.2%。]") is True  # legacy 兼容锚


def test_new_header_label_not_misjudged_as_placeholder():
    """新标签未折叠头行（`[可折叠编号 ` 开头、含换行）不得被判成占位符：
    占位符通道恒为「[已折叠：」前缀，兼容锚恒以「[输出#」开头——两条都不吃新头行。"""
    from agent.generic.agent_loop import _is_tool_placeholder
    m = msg("tool", "正文：已折叠：这是普通输出", "t-105", 105,
            tool_call_id="tc1", folded=0, output_pct=4.2)
    rendered = render_tool_content(m, TC_MAP)
    assert rendered.startswith("[可折叠编号 105 · read_file · 占上下文 4.2%]\n")
    assert _is_tool_placeholder(rendered) is False
    # 单行截断（无换行）同样不误判——新标签既非「[已折叠：」前缀也非「[输出#」兼容锚
    assert _is_tool_placeholder("[可折叠编号 105 · read_file · 占上下文 4.2%]") is False


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
    assert r1 == r2 and r1["content"].startswith("[可折叠编号 105 · read_file")
    assert TOOL_105.content == "ORIGINAL_BODY"  # 真相源 content 永不动


def test_is_tool_placeholder_recognizes_fold():
    # 折叠族当前形态按「[已折叠：」前缀识别；旧带编号 `[输出#N 已折叠：…]` 作兼容锚照认：
    # 折叠占位符恒单行，未折叠渲染头行+换行+原文恒多行不误判
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
        '[已折叠：search({"q": "niu"})，原占约 3.0%。]'
    # 未折叠条：头行 + 原文
    assert by_tc["tc1"]["content"] == "[可折叠编号 5 · read_file · 占上下文 2.0%]\nOLD_BODY"
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
    assert tool_entries[0]["content"] == "[可折叠编号 3 · read_file · 占上下文 2.5%]\nTOOLBODY"
