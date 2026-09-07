# tests/test_compression_compact.py
"""机械压实接入 + 单元感知重组：压实视图、三件套落库+内存追加双通道、原指令最后、工具轮无孤儿 tool。"""
from unittest.mock import patch, MagicMock

# 导入序守卫（同 test_compression_trigger_migration.py）：agent.context_assembler.compaction
# 顶层依赖 context_manager.ContextManager（既有循环依赖）——必须先完整加载 assembler 包，
# 否则先 import agent.context_manager 会命中部分初始化模块 ImportError。
import agent.context_assembler  # noqa: F401,E402

from agent.generic.agent_loop import (
    run_controlled_compression, _rebuild_messages_after_compact, _COMPRESSION_DONE_HINT,
    _SUMMARY_PROMPT,
)


def _mk_ctx():
    ctx = MagicMock()
    ctx._sync_get_messages = MagicMock(return_value=[])  # compact 助手 mock 掉时不用
    ctx._sync_add_message = MagicMock(return_value="msg-1")
    return ctx


def test_full_flow_compacts_and_rebuilds_messages():
    """完整受控压缩：提炼过 → 总结（仅内存）→ DB 压实 → 三件套落库 → 完成提示注入 → 原 user 指令最后。"""
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
         patch("agent.generic.agent_loop._persist_compression_triplet") as m_persist, \
         patch("agent.generic.agent_loop._compact_db_view", return_value=(True, {"usage": 0.1})) as m_compact, \
         patch("agent.generic.agent_loop._rebuild_messages_after_compact",
               side_effect=lambda m, ctx, s: [m[0]] + [{"role": "user", "content": "[历史索引] 共 3 块…"}] + [{"role": "user", "content": _SUMMARY_PROMPT}, {"role": "assistant", "content": s}, {"role": "user", "content": _COMPRESSION_DONE_HINT}] + [m[-1]]) as m_rebuild:
        ctx._last_compacted_view = compacted_view
        new_msgs, did = run_controlled_compression(list(orig_messages), ctx, client, 1)

    assert did is True
    m_persist.assert_called_once_with(ctx, "总结文本")
    m_compact.assert_called_once_with(ctx)
    assert m_rebuild.call_args[0][0] == orig_messages  # 原消息传入重组
    assert m_rebuild.call_args[0][2] == "总结文本"  # P3-2：第三参收到总结文本
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


def test_rebuild_triplet_single_copy_old_summary_in_window():
    """T2/T3 新形态锁·单代形态（跨代双套见 test_rebuild_second_generation_triplets_both_preserved）。
    （替代两个已删的总结双份用例——旧 fixture 形态"总结在压实视图/
    剥离段内"新设计下生产不可达：总结恒在压实后落库，_last_compacted_view 永不含
    本轮总结）：上轮压缩旧三件套在窗口主体（旧准备提示 + 旧总结，与本轮不同文本）
    + 待执行单元在上 → 重组后：窗口旧总结原样保留恰一份；本轮新总结恰一份
    （三件套内存通道）；完成提示恰一份；待执行单元最后。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "user", "content": _SUMMARY_PROMPT},          # 上轮准备提示（已落库，在窗口）
        {"role": "assistant", "content": "上轮旧总结文本"},     # 上轮旧总结（已落库，≠本轮文本）
        {"role": "user", "content": "指令B"},                   # 待执行单元起点
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "B 结果"},
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [dict(m) for m in compacted_view]  # 生产：messages=DB 视图
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "本轮新总结文本")

    # 窗口旧总结原样保留恰一份（不被剥离、不去重）
    old_count = sum(1 for m in new_msgs
                    if m.get("role") == "assistant" and (m.get("content") or "") == "上轮旧总结文本")
    assert old_count == 1
    # 本轮新总结恰一份（三件套内存通道）
    new_count = sum(1 for m in new_msgs
                    if m.get("role") == "assistant" and (m.get("content") or "") == "本轮新总结文本")
    assert new_count == 1
    # 完成提示恰一份
    hint_count = sum(1 for m in new_msgs
                     if (m.get("content") or "").startswith(_COMPRESSION_DONE_HINT))
    assert hint_count == 1
    # 顺序：旧总结 < 三件套（新总结）< 完成提示 < 待执行单元起点
    old_idx = next(i for i, m in enumerate(new_msgs) if (m.get("content") or "") == "上轮旧总结文本")
    new_idx = next(i for i, m in enumerate(new_msgs) if (m.get("content") or "") == "本轮新总结文本")
    hint_idx = next(i for i, m in enumerate(new_msgs)
                    if (m.get("content") or "").startswith(_COMPRESSION_DONE_HINT))
    unit_start = next(i for i, m in enumerate(new_msgs)
                      if m.get("role") == "user" and m.get("content") == "指令B")
    assert old_idx < new_idx < hint_idx < unit_start
    # 待执行单元最后（工具组尾）
    assert new_msgs[-1]["role"] == "tool"


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
    ctx._sync_add_message.assert_not_called()  # P2-2：压实失败零落库（三件套不落）
    m_gate.release.assert_called_once()  # 失败也 release 闸门（防永久失效）


def test_rebuild_exception_keeps_did_true_and_system():
    """P1/P2-1（A/B 独立同抓）+ P2-3：重组抛错 → did 固定 True（不翻转"未压缩"，
    防门误判 manual 重设致下轮重复压缩双份三件套）；降级基底保留 system 行在 [0]
    （门后重跑 on_before_llm 依赖 messages[0].role == "system"，剥掉则 _assemble_
    system_message 早退 → 该次及同 run 后续轮全无 system）；完成提示恰一份且在最后
    user 之前；done 通知两路径必推。"""
    ctx = _mk_ctx()
    client = MagicMock()
    orig_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "用户原指令"},
    ]
    with patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=True), \
         patch("agent.generic.agent_loop._run_summary_llm", return_value="总结文本"), \
         patch("agent.generic.agent_loop._compact_db_view", return_value=(True, {"usage": 0.1})), \
         patch("agent.generic.agent_loop._rebuild_messages_after_compact",
               side_effect=RuntimeError("rebuild boom")), \
         patch("agent.context_assembler.compaction.AUTO_GATE"), \
         patch("agent.context_assembler.compaction.reset_ratio", return_value=0.78), \
         patch("agent.generic.agent_loop._notify_compact_progress") as m_notify:
        ctx._last_compacted_view = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "指令B"},
        ]
        new_msgs, did = run_controlled_compression(list(orig_messages), ctx, client, 1)

    assert did is True  # 重组异常不翻转（压实+落库已成功）
    assert new_msgs[0]["role"] == "system"  # P1：降级基底保留 system 行
    hint_count = sum(1 for m in new_msgs if (m.get("content") or "") == _COMPRESSION_DONE_HINT)
    assert hint_count == 1  # 完成提示恰一份（降级手工插入）
    hint_idx = next(i for i, m in enumerate(new_msgs) if (m.get("content") or "") == _COMPRESSION_DONE_HINT)
    last_user_idx = max(i for i, m in enumerate(new_msgs)
                        if m.get("role") == "user" and (m.get("content") or "") != _COMPRESSION_DONE_HINT)
    assert hint_idx < last_user_idx  # 完成提示在最后一个 user（指令B）之前
    m_notify.assert_any_call("done", mode="auto")  # done 两路径必推


def test_rebuild_second_generation_triplets_both_preserved():
    """P3-1 跨代双套锁：窗口含上一代完整三件套（旧准备=同 _SUMMARY_PROMPT 文本 +
    旧总结 + 旧完成提示=同 _COMPRESSION_DONE_HINT 文本）+ 指令B → 重组后：旧总结恰
    一份原样保留、本轮总结恰一份、两代完成提示各一份（spec P3-1 有界双套设计内，
    不做跨代全局唯一断言）；排序 旧代 < 新代 < 指令B。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 5 块…"},
        {"role": "user", "content": _SUMMARY_PROMPT},        # 旧代准备提示（已落库，在窗口）
        {"role": "assistant", "content": "上代总结"},         # 旧代总结（已落库）
        {"role": "user", "content": _COMPRESSION_DONE_HINT},  # 旧代完成提示（已落库）
        {"role": "user", "content": "指令B"},                 # 待执行单元起点
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [dict(m) for m in compacted_view]  # 生产：messages=DB 视图
    new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "本轮总结")

    old_count = sum(1 for m in new_msgs
                    if m.get("role") == "assistant" and (m.get("content") or "") == "上代总结")
    assert old_count == 1  # 旧代总结原样保留恰一份（不被剥离、不去重）
    new_count = sum(1 for m in new_msgs
                    if m.get("role") == "assistant" and (m.get("content") or "") == "本轮总结")
    assert new_count == 1  # 本轮总结恰一份（三件套内存通道）
    hint_count = sum(1 for m in new_msgs
                     if (m.get("content") or "").startswith(_COMPRESSION_DONE_HINT))
    assert hint_count == 2  # 两代完成提示各一份（有界双套，非全局唯一）
    old_idx = next(i for i, m in enumerate(new_msgs) if (m.get("content") or "") == "上代总结")
    new_idx = next(i for i, m in enumerate(new_msgs) if (m.get("content") or "") == "本轮总结")
    unit_start = next(i for i, m in enumerate(new_msgs)
                      if m.get("role") == "user" and m.get("content") == "指令B")
    assert old_idx < new_idx < unit_start  # 排序：旧代 < 新代 < 指令B


# ---------------------------------------------------------------------------
# FinalReview A P2-1 / B P2-2：重组基底负向锁 + 条件解闩语义
# ---------------------------------------------------------------------------

def test_rebuild_base_is_last_compacted_view_not_db_rerun():
    """FinalReview A P2-1：_rebuild_messages_after_compact 基底必须是
    ctx._last_compacted_view——不得从 DB 重跑 assemble_view_sync/build_compact_view
    （若重跑，tool 原文会复活/三件套被二次归档）。spy 抛错锁负向 + 占位符化标记
    锁正向（基底=压实视图而非 in_flight 原文）。"""
    compacted_view = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "原用户指令"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[read_file 输出已裁剪…]"},  # 占位符化标记（压实视图形态）
    ]
    ctx = _mk_ctx()
    ctx._last_compacted_view = compacted_view
    in_flight = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[历史索引] 共 3 块…"},
        {"role": "user", "content": "原用户指令"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "文件原文内容…"},  # DB 原文（若走 DB 重跑会复活）
    ]
    with patch("agent.context_manager.ContextManager.assemble_view_sync",
               side_effect=AssertionError("重组不得走 DB 重跑（assemble_view_sync）")), \
         patch("agent.context_assembler.compaction.build_compact_view",
               side_effect=AssertionError("重组不得走 DB 重跑（build_compact_view）")):
        new_msgs = _rebuild_messages_after_compact(in_flight, ctx, "总结文本")
    # 未抛（未走 DB 重跑）+ 基底是占位符化压实视图（原文未复活）
    assert any("[read_file 输出已裁剪…]" in (m.get("content") or "") for m in new_msgs)
    assert not any("文件原文内容…" in (m.get("content") or "") for m in new_msgs)


def test_rebuild_has_no_standalone_summary_if_block():
    """FinalReview A P2-2：_rebuild_messages_after_compact 函数体内不得含独立
    `if summary_text:` 块——旧"从剥离段抽总结防双份"逻辑已删（spec 3.2 P2-1）。
    _triplet_messages 内的 if summary_text: 合法（空总结跳 assistant 行），故断言
    限定在本函数体源码段内（ast 提取，防误伤他函数）。"""
    import ast
    import pathlib
    import re

    src = (pathlib.Path(__file__).parent.parent / "agent" / "generic" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_rebuild_messages_after_compact"), None)
    assert fn is not None, "agent_loop.py 应存在 _rebuild_messages_after_compact"
    body_src = ast.get_source_segment(src, fn)
    assert body_src is not None
    assert not re.search(r"^\s*if\s+summary_text\s*:", body_src, re.M), (
        "_rebuild_messages_after_compact 不得含独立 `if summary_text:` 块"
        "（旧'从剥离段抽总结'逻辑已删；唯一合法引用在 _triplet_messages）"
    )


def test_failure_no_release_when_not_acquired():
    """FinalReview B P2-2：失败早退（提炼失败）+ release_on_failure=False →
    AUTO_GATE.release 不被调——本轮未 acquire 到闩锁（如他轮滞回期），不得误清
    他轮滞回闩锁。对照 test_compact_failure_keeps_original_messages（默认 True 失败解闩）。"""
    ctx = _mk_ctx()
    client = MagicMock()
    orig = [{"role": "system", "content": "sys"}, {"role": "user", "content": "原指令"}]
    with patch("agent.generic.agent_loop._extract_cooldown_active", return_value=False), \
         patch("agent.generic.agent_loop._extract_f1_before_compress", return_value=False), \
         patch("agent.generic.agent_loop._mark_extract_failed"), \
         patch("agent.context_assembler.compaction.AUTO_GATE") as m_gate:
        new_msgs, did = run_controlled_compression(list(orig), ctx, client, 0, release_on_failure=False)
    assert did is False
    assert new_msgs == orig  # 原样不丢
    m_gate.release.assert_not_called()  # 本轮未 acquire → 失败不解闩（防误清他轮滞回闩锁）
