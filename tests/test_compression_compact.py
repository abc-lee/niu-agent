# tests/test_compression_compact.py
"""机械压实接入 + 单元感知重组：压实视图、完成提示纯内存、原指令最后、工具轮无孤儿 tool。"""
from unittest.mock import patch, MagicMock
from agent.generic.agent_loop import (
    run_controlled_compression, _rebuild_messages_after_compact, _COMPRESSION_DONE_HINT,
)


def _mk_ctx():
    ctx = MagicMock()
    ctx._sync_get_messages = MagicMock(return_value=[])  # compact 助手 mock 掉时不用
    ctx._sync_add_message = MagicMock(return_value="msg-1")
    return ctx


def test_full_flow_compacts_and_rebuilds_messages():
    """完整受控压缩：提炼过 → 总结落库 → DB 压实 → 完成提示注入 → 原 user 指令最后。"""
    ctx = _mk_ctx()
    client = MagicMock()

    orig_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "用户原指令"},
    ]
    # 压实视图（模拟 build_compact_view 返回：保留最近单元 → 含原指令）
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "用户原指令"},
    ]

    with patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=True), \
         patch("agent.generic.agent_loop._run_summary_llm", return_value="总结文本"), \
         patch("agent.generic.agent_loop._persist_summary_without_extract") as m_persist, \
         patch("agent.generic.agent_loop._compact_db_view", return_value=(True, {"usage": 0.1})) as m_compact, \
         patch("agent.generic.agent_loop._rebuild_messages_after_compact",
               side_effect=lambda m, ctx, s: [m[0]] + [{"role": "user", "content": "[历史索引] 共 3 块…"}] + [{"role": "assistant", "content": s}] + [{"role": "user", "content": _COMPRESSION_DONE_HINT}] + [m[-1]]) as m_rebuild:
        ctx._last_compacted_view = compacted_view
        new_msgs, did = run_controlled_compression(list(orig_messages), ctx, client, 1)

    assert did is True
    m_persist.assert_called_once_with(ctx, "总结文本")
    m_compact.assert_called_once_with(ctx)
    assert m_rebuild.call_args[0][0] == orig_messages  # 原消息传入重组
    assert new_msgs[-1]["role"] == "user"
    assert new_msgs[-1]["content"] == "用户原指令"  # 原指令最后一条
    assert any(_COMPRESSION_DONE_HINT in m.get("content", "") for m in new_msgs)


def test_rebuild_pending_unit_last_hint_before():
    """整体替换 + 待执行单元置最后（R4-A P0-1 用户不变式）：重组后原指令（待执行
    单元）是最后一条；完成提示在它前；未落库尾引导在完成提示后、待执行单元前。"""
    # 压实视图（_compact_db_view 产出：system + 索引 + 占位符化窗口，含待执行单元）
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "user", "content": "原用户指令"},
        {"role": "assistant", "content": "[read_file 输出已裁剪…]", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[read_file 输出已裁剪…]"},
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    # 发送前 messages：DB 视图（含原指令单元）+ 未落库引导（工具超时提示）——注意
    # 引导 append 在消息尾，但它语义上是"系统提示"，应排在待执行单元前
    in_flight = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "user", "content": "原用户指令"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "文件内容…"},
        {"role": "user", "content": "[系统提示] 你上一轮的工具调用已超时，请重试"},  # 未落库引导
    ]
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "总结文本")
    # 用户拍板不变式：待执行单元（原用户指令起的组）最后一条
    assert new_msgs[-1]["role"] == "tool"  # 单元尾（assistant+tool 组）在最后
    assert new_msgs[-2]["role"] == "assistant"
    assert any(m.get("content") == "原用户指令" for m in new_msgs[-4:])
    # 完成提示在待执行单元前
    hint_idx = next(i for i, m in enumerate(new_msgs)
                    if (m.get("content") or "").startswith(_COMPRESSION_DONE_HINT))
    unit_start = next(i for i, m in enumerate(new_msgs)
                      if m.get("content") == "原用户指令" and m.get("role") == "user"
                      and i > hint_idx)
    assert hint_idx < unit_start
    # 未落库尾引导保留（在完成提示后、待执行单元前）
    guide_idx = next(i for i, m in enumerate(new_msgs)
                     if "[系统提示] 你上一轮的工具调用已超时" in (m.get("content") or ""))
    assert hint_idx < guide_idx < unit_start
    # 无孤儿 tool
    seen = set()
    for m in new_msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen.add(tc["id"])
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in seen


def test_rebuild_first_turn_user_last():
    """R4-A P0-1 回归：首轮场景（当前 user 指令已 persist 在视图）——重组后当前
    user 指令是最后一条，不是完成提示。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "用户原指令"},  # keep 窗口保留的当前指令
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "用户原指令"},
    ]
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "总结文本")
    # 原指令最后一条（用户拍板层级：完成提示在其上）
    assert new_msgs[-1]["role"] == "user"
    assert new_msgs[-1]["content"] == "用户原指令"
    assert any((m.get("content") or "").startswith(_COMPRESSION_DONE_HINT) for m in new_msgs[:-1])


def test_rebuild_summary_not_duplicated_when_in_view():
    """R2-A P2-1：总结已在压实视图（先落库后读 DB）→ 不重复 append。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "assistant", "content": "总结文本"},
        {"role": "user", "content": "原指令"},
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "assistant", "content": "总结文本"},
        {"role": "user", "content": "原指令"},
    ]
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "总结文本")
    # 总结只出现一次
    sum_count = sum(1 for m in new_msgs
                    if m.get("role") == "assistant" and (m.get("content") or "").strip() == "总结文本")
    assert sum_count == 1
    assert any(_COMPRESSION_DONE_HINT in m.get("content", "") for m in new_msgs)


def test_rebuild_tool_unit_no_guide():
    """R11 P2-B：轮间无尾引导——messages 尾 = 工具链（tool 组，无未落库 user 引导）；
    重组后工具单元在最后、完成提示在其前。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "指令A"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "A 结果"},
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "指令A"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "A 结果"},
    ]
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "总结")
    # 工具单元（assistant+tool）在最后，完成提示在它前
    assert new_msgs[-1]["role"] == "tool"
    assert new_msgs[-2]["role"] == "assistant"
    hint_idx = next(i for i, m in enumerate(new_msgs)
                    if (m.get("content") or "").startswith(_COMPRESSION_DONE_HINT))
    assert hint_idx < len(new_msgs) - 2
    # 无孤儿 tool
    seen = set()
    for m in new_msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen.add(tc["id"])
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in seen


def test_compact_success_keeps_latch_when_not_reset():
    """R11 P2-B：auto 滞回——压实成功但压后 usage 未回落 < 复位线 → 保持闩锁不 release
    （防本 loop 每 send 重压）；回落 < 复位线才 release。"""
    ctx = _mk_ctx()
    client = MagicMock()
    orig = [{"role": "system", "content": "sys"}, {"role": "user", "content": "原指令"}]
    # stats usage=0.85（≥ 复位线 0.78）→ 成功但保持闩锁
    with patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=True), \
         patch("agent.generic.agent_loop._run_summary_llm", return_value=""), \
         patch("agent.generic.agent_loop._compact_db_view",
               return_value=(True, {"usage": 0.85})), \
         patch("agent.context_assembler.compaction.AUTO_GATE") as m_gate, \
         patch("agent.context_assembler.compaction.reset_ratio", return_value=0.78):
        m_gate.try_acquire.return_value = True  # 门已置闩
        ctx._last_compacted_view = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[历史索引] 共 1 块…"},
            {"role": "user", "content": "原指令"},
        ]
        new_msgs, did = run_controlled_compression(list(orig), ctx, client, 1)
    assert did is True
    m_gate.release.assert_not_called()  # 未回落 → 保持闩锁（R6-A P2-1 滞回）


def test_compact_success_release_when_reset():
    """R11 P2-B：压实成功且压后 usage 回落 < 复位线 → release（下轮可再触发）。"""
    ctx = _mk_ctx()
    client = MagicMock()
    orig = [{"role": "system", "content": "sys"}, {"role": "user", "content": "原指令"}]
    with patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=True), \
         patch("agent.generic.agent_loop._run_summary_llm", return_value=""), \
         patch("agent.generic.agent_loop._compact_db_view",
               return_value=(True, {"usage": 0.60})), \
         patch("agent.context_assembler.compaction.AUTO_GATE") as m_gate, \
         patch("agent.context_assembler.compaction.reset_ratio", return_value=0.78):
        m_gate.try_acquire.return_value = True
        ctx._last_compacted_view = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[历史索引] 共 1 块…"},
            {"role": "user", "content": "原指令"},
        ]
        new_msgs, did = run_controlled_compression(list(orig), ctx, client, 1)
    assert did is True
    m_gate.release.assert_called_once()  # 已回落 → 解除闩锁


def test_compact_failure_keeps_original_messages():
    """DB 压实失败 → 不重组、不丢消息，返回 (原样, False)；闸门 release（R1-B P3-1：patch compaction.AUTO_GATE）。"""
    ctx = _mk_ctx()
    client = MagicMock()
    orig = [{"role": "system", "content": "sys"}, {"role": "user", "content": "原指令"}]
    with patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=True), \
         patch("agent.generic.agent_loop._run_summary_llm", return_value=""), \
         patch("agent.generic.agent_loop._compact_db_view", return_value=(False, {})), \
         patch("agent.context_assembler.compaction.AUTO_GATE") as m_gate:
        new_msgs, did = run_controlled_compression(list(orig), ctx, client, 1)
    assert did is False
    assert new_msgs == orig  # 原样
    m_gate.release.assert_called_once()  # 失败也 release 闸门（防永久失效）
