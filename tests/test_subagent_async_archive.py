"""T3 行为锁：异步子 Agent 完成态存档（T1）+ 同名续跑（T2）——19 条 spec 清单逐条。

覆盖（spec v0.6 §6 + 计划 P2-1b，条目号即测试后缀编号）：
 1. 异步 @end(EXITED)→落盘→同名续跑（档含末轮总结——last_reply append）
 2. TERMINATED_BY_SUPPLEMENT→落盘
 3. 忘 @end CURRENT_TASK_DONE→落盘（末轮在场）
 4. length 耗尽 CURRENT_TASK_DONE→落盘（档尾截断态半句 append）
 5. MAX_TURNS_EXCEEDED→落盘（messages 原样，不 append）
 6. CONTEXT_OVERFLOW→落盘（续跑保留 system + 原始任务）
 7. STOPPED mid-dispatch→落盘且续跑无悬空 assistant(tool_calls) 对
 8. LLM_ERROR（dict 无 messages）→不落盘 + warning 留痕
 9. 内层异常→不落盘（异常上抛，存档点不达）
10. 同步 @end→不落盘（is_sync=True 判定；同步路径代码上不可达存档块）
11. 无档同名→全新派发 + 实际名告知
12. 损坏档→全新 + warning
13. 跨类型→全新
14. 运行中同名→错误文案（非"同步路径"字样）
15. 空 task 续跑→effective_task="继续上次未完成的工作" 注入 + 通知不含 [错误]
16. 时序：完成通知组装（push）时 archive_written=True（档已就绪）
17. 写盘失败→通知不含续跑承诺（同名重调/24h 话术）+ 写盘失败留痕
18. 续跑完成后同名档被覆盖（续跑上下文 + 末轮补全在场）+ 完成通知再推
19. 存档路径不入消息内容（静态断言 + 行为级可见文本检查）

隔离纪律：禁真实 LLM（_run_agent_loop / _dispatch 启动全部 patch）、禁 messages.db 写
（call_subagent 走 hermetic 装配 + _run_agent_loop patch，真循环永不进入）、禁 ~/.niu 写
（tmp_dir.get_tmp_dir monkeypatch 到 tmp_path）、registry 用后必清（每测试 finally unregister）。
"""
import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest
from loguru import logger

from agent.main_agent_request_queue import get_main_agent_request_queue
from agent.subagent_registry import SubagentRegistry

_AGENT = "file-processor"
_LLM_CFG = {"model": "test-model", "apikey": "test-key", "apibase": "", "type": "openai"}
_SYSTEM_MSG = {"role": "system", "content": "You are a file-processing sub-agent."}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

class _FakeClient:
    """call_subagent 只写 client.backend.stop_check——backend 占位即可。"""

    class _Backend:
        def __init__(self):
            self.stop_check = None

    def __init__(self):
        self.backend = self._Backend()


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    """tmp_dir 目录函数全量重定向 tmp_path——归档读写禁写真实 ~/.niu/tmp。"""
    monkeypatch.setattr("agent.tmp_dir.get_tmp_dir", lambda: tmp_path)
    return tmp_path


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（agent 模块用 loguru，pytest caplog 捕获不到——项目既有模式）。"""
    msgs = []
    sink_id = logger.add(lambda m: msgs.append(str(m)), level=level)
    return msgs, sink_id


def _drain_queue():
    """取空主 Agent 请求队列（单例，防跨测试污染）。"""
    q = get_main_agent_request_queue()
    msgs = []
    while not q.is_empty():
        msgs.append(q.pop())
    return msgs


def _register(unique_name, agent_type=_AGENT, *, is_sync=False):
    """注册 RunningSubagent 实例；返回 (name, sq, mc)。"""
    from agent.subagent_memory import SubagentMemoryContext
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name=unique_name)
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register(
        agent_type=agent_type,
        supplement_queue=sq,
        memory_context=mc,
        is_sync=is_sync,
        force_unique_name=unique_name,
    )
    return name, sq, mc


def _read_archive(archive_dir, unique_name) -> dict:
    return json.loads((Path(archive_dir) / f"{unique_name}.json").read_text(encoding="utf-8"))


def _write_archive(archive_dir, unique_name, messages, agent_type=_AGENT) -> None:
    data = {
        "unique_name": unique_name,
        "agent_type": agent_type,
        "created_at": "2026-09-02T10:00:00",
        "last_activity": "2026-09-02T10:00:00",
        "messages": messages,
    }
    (Path(archive_dir) / f"{unique_name}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _mk_rv(result: str, messages: list, **extra) -> dict:
    rv = {"result": result, "messages": messages}
    rv.update(extra)
    return rv


def _assert_no_dangling_tool_calls(messages: list) -> None:
    """镜像 transform_history valid_tcs 语义：assistant 携带的 tool_calls 必须全部有配对 tool 响应。"""
    tool_ids = {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                assert tc.get("id") in tool_ids, f"悬空 assistant tool_calls 未剥离: {tc}"


def _hermetic_stack(loop_return=("", {"result": "EXITED", "messages": []}, "")):
    """call_subagent hermetic 装配：LLM 客户端 / 真循环 / 配置 / handler 全部 patch。

    返回 ExitStack——调用方 with 进入后调 call_subagent(unique_name=...)，真循环永不进入、
    无 db 写、无配置读取。返回的 _run_agent_loop 三连 = (result_text, return_value, last_reply)。
    """
    stack = ExitStack()
    stack.enter_context(mock.patch("agent.runner.create_client", return_value=_FakeClient()))
    stack.enter_context(mock.patch("agent.runner.get_runner", return_value=mock.MagicMock(_request_source="user")))
    stack.enter_context(mock.patch("agent.runner.is_stop_requested", return_value=False))
    stack.enter_context(mock.patch("agent.subagent._run_agent_loop", return_value=loop_return))
    stack.enter_context(mock.patch("agent.subagent.get_subagent_config", return_value={}))
    stack.enter_context(mock.patch("agent.subagent.build_subagent_system_segments", return_value=("sys-prompt", "")))
    stack.enter_context(mock.patch("agent.subagent._build_subagent_tools_schema", return_value=[]))
    stack.enter_context(mock.patch("agent.subagent._read_context_window_tokens", return_value=200000))
    stack.enter_context(mock.patch("agent.subagent._maybe_suspend_session"))
    stack.enter_context(mock.patch("agent.handler.NiuHandler", return_value=mock.MagicMock()))
    return stack


def _call_subagent_async(unique_name, sq, mc, task="归档 1 号文件"):
    """驱动 call_subagent 异步分支（unique_name 分支）到存档块。"""
    from agent.subagent import call_subagent

    return call_subagent(
        agent_name=_AGENT,
        task=task,
        llm_config=_LLM_CFG,
        mcp_client=None,
        supplement_queue=sq,
        memory_context=mc,
        unique_name=unique_name,
    )


def _dispatch_capture(unique_name, task, agent_name=_AGENT):
    """驱动 _dispatch_async_subagent，拦截真实异步启动（_run_subagent_async recorder + threadsafe stub）。

    Returns:
        (returned_name, confirmation, captured_kwargs_list)
    """
    from agent.subagent import _dispatch_async_subagent

    captured = []
    fake_future = mock.MagicMock()
    fake_loop = mock.MagicMock()
    fake_loop.is_closed.return_value = False
    fake_runner = mock.MagicMock()
    fake_runner._request_source = "user"

    def _fake_run_async(**kwargs):
        # 同步普通函数（非协程）：_run_subagent_async 是 async def，mock.patch 默认会造
        # AsyncMock（调用返回未 await 的协程）——用 new= 直换普通函数，调用即时记录
        captured.append(kwargs)
        return None

    def _fake_threadsafe(coro, loop):
        return fake_future

    with mock.patch("niu_api.chat._main_loop", fake_loop), \
         mock.patch("agent.subagent._run_subagent_async", new=_fake_run_async), \
         mock.patch("agent.runner.get_runner", return_value=fake_runner), \
         mock.patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_threadsafe):
        name, confirmation = _dispatch_async_subagent(
            agent_name=agent_name,
            task=task,
            llm_config=_LLM_CFG,
            unique_name=unique_name,
        )
    return name, confirmation, captured


# ===========================================================================
# T1 存档：各终态落盘 + 不落盘分支
# ===========================================================================

def test_01_async_end_exited_archives_with_final_summary_and_resumes(archive_dir):
    """1. 异步 @end(EXITED)→落盘→同名续跑：档含末轮总结（mock last_reply append）。"""
    name, sq, mc = _register("file-processor-t3-0001")
    try:
        msgs = [
            _SYSTEM_MSG,
            {"role": "user", "content": "整理照片目录"},
            {"role": "assistant", "content": "已完成照片分类"},
        ]
        rv = _mk_rv("EXITED", msgs, finish_reason="exited")
        with _hermetic_stack(loop_return=("", rv, "归档完成：3 张已分类 @end")):
            _call_subagent_async(name, sq, mc)

        inst = SubagentRegistry.get(name)
        assert inst is not None and inst.archive_written is True

        archive = _read_archive(archive_dir, name)
        assert archive["unique_name"] == name and archive["agent_type"] == _AGENT
        # 末轮总结 append 在场（纯文本终态不 append 进 messages，落盘前补）
        assert archive["messages"][-1] == {"role": "assistant", "content": "归档完成：3 张已分类 @end"}
        assert archive["messages"][0]["role"] == "system"

        # 同名续跑：档可直接清洗为续跑上下文，末轮总结是上下文的最后一条 assistant
        from agent.subagent import _prepare_resume_messages
        prepared = _prepare_resume_messages(archive["messages"])
        assert prepared is not None
        assert prepared[0]["role"] == "system"
        assert prepared[-1] == {"role": "assistant", "content": "归档完成：3 张已分类 @end"}
    finally:
        SubagentRegistry.unregister(name)


def test_02_terminated_by_supplement_archives(archive_dir):
    """2. 被叫停 TERMINATED_BY_SUPPLEMENT→落盘（supplement 收尾总结在场）。"""
    name, sq, mc = _register("file-processor-t3-0002")
    try:
        msgs = [
            _SYSTEM_MSG,
            {"role": "user", "content": "处理一批文件"},
            {"role": "assistant", "content": "第 1/3 批完成"},
        ]
        rv = _mk_rv("TERMINATED_BY_SUPPLEMENT", msgs, finish_reason="stop")
        with _hermetic_stack(loop_return=("", rv, "收到 /stop，已收尾：进度已保留")):
            _call_subagent_async(name, sq, mc)

        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        assert archive["messages"][-1] == {"role": "assistant", "content": "收到 /stop，已收尾：进度已保留"}
        # 中途 assistant 内容原样在场（不丢动作记忆）
        assert any(m.get("content") == "第 1/3 批完成" for m in archive["messages"])
    finally:
        SubagentRegistry.unregister(name)


def test_03_current_task_done_without_end_archives(archive_dir):
    """3. 忘 @end CURRENT_TASK_DONE→落盘（末轮在场——补全使续跑不丢最后陈述）。"""
    name, sq, mc = _register("file-processor-t3-0003")
    try:
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "总结本月文件"}, {"role": "assistant", "content": "中间轮输出"}]
        rv = _mk_rv("CURRENT_TASK_DONE", msgs, finish_reason="stop")
        with _hermetic_stack(loop_return=("", rv, "本月共处理 42 个文件，均已归档")):
            _call_subagent_async(name, sq, mc)

        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        assert archive["messages"][-1] == {"role": "assistant", "content": "本月共处理 42 个文件，均已归档"}
    finally:
        SubagentRegistry.unregister(name)


def test_04_length_truncation_current_task_done_archives_half_sentence(archive_dir):
    """4. length 耗尽 CURRENT_TASK_DONE→落盘（档尾截断态半句 append——不丢截断陈述）。"""
    name, sq, mc = _register("file-processor-t3-0004")
    try:
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "长报告任务"}, {"role": "assistant", "content": "已完成前两章"}]
        rv = _mk_rv("CURRENT_TASK_DONE", msgs, finish_reason="length")
        half = "报告第三章结论是：所有文件均已按规则归"
        with _hermetic_stack(loop_return=("", rv, half)):
            _call_subagent_async(name, sq, mc)

        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        # 档尾 = 截断态半句（不静默丢弃，续跑可从此续写）
        assert archive["messages"][-1] == {"role": "assistant", "content": half}
    finally:
        SubagentRegistry.unregister(name)


def test_05_max_turns_exceeded_archives_messages_asis(archive_dir):
    """5. MAX_TURNS_EXCEEDED→落盘（messages 原样，不在补全三类——不 append 中间话）。"""
    name, sq, mc = _register("file-processor-t3-0005")
    try:
        msgs = [
            _SYSTEM_MSG,
            {"role": "user", "content": "遍历全部目录"},
            {"role": "assistant", "content": "目录 A 处理完，进入 B…"},
        ]
        rv = _mk_rv("MAX_TURNS_EXCEEDED", msgs)
        with _hermetic_stack(loop_return=("", rv, "进行到一半的中间话")):
            _call_subagent_async(name, sq, mc)

        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        assert archive["messages"] == msgs  # 原样：不 append last_reply
        assert archive["messages"][-1] != {"role": "assistant", "content": "进行到一半的中间话"}
    finally:
        SubagentRegistry.unregister(name)


def test_06_context_overflow_archives_and_resume_keeps_system_and_task(archive_dir):
    """6. CONTEXT_OVERFLOW→落盘；续跑保留 system + 原始任务 user，无悬空对。"""
    name, sq, mc = _register("file-processor-t3-0006")
    try:
        msgs = [
            _SYSTEM_MSG,
            {"role": "user", "content": "大目录批量处理（原始任务）"},
            {"role": "assistant", "content": "已处理 120/500 个"},
        ]
        rv = _mk_rv(
            "CONTEXT_OVERFLOW", msgs,
            data={"overflow": True, "turns_completed": 6, "tokens_used": 180000, "tokens_limit": 200000},
        )
        with _hermetic_stack(loop_return=("", rv, "上下文超限前报告")):
            _call_subagent_async(name, sq, mc)

        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        assert archive["messages"] == msgs  # 原样落盘（overflow 不在补全三类）

        from agent.subagent import _prepare_resume_messages
        prepared = _prepare_resume_messages(archive["messages"])
        assert prepared is not None
        # system + 原始任务在场（resumed 分支直用 messages，system 头必须还原）
        assert prepared[0]["role"] == "system"
        assert any(m.get("role") == "user" and "原始任务" in m.get("content", "") for m in prepared)
        assert prepared[-1]["role"] == "assistant"  # 进度陈述保留
        _assert_no_dangling_tool_calls(prepared)
    finally:
        SubagentRegistry.unregister(name)


def test_07_stopped_mid_dispatch_archives_and_resume_no_dangling_pair(archive_dir):
    """7. STOPPED mid-dispatch→落盘（悬空 tool_calls 原样）；同名续跑剥离后无零配对对。"""
    name, sq, mc = _register("file-processor-t3-0007")
    try:
        dangling = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_mid_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        }
        msgs = [
            _SYSTEM_MSG,
            {"role": "user", "content": "处理文件并汇报"},
            dangling,
        ]
        rv = _mk_rv("STOPPED", msgs)
        with _hermetic_stack(loop_return=("", rv, "")):
            _call_subagent_async(name, sq, mc)

        # 落盘：档尾悬空 assistant(tool_calls) 原样保留（档文件不改）
        assert SubagentRegistry.get(name).archive_written is True
        archive = _read_archive(archive_dir, name)
        assert archive["messages"][-1]["tool_calls"][0]["id"] == "call_mid_1"

        # 生产语义：_run_subagent_async finally 注销实例后，主 Agent 才能同名重调——
        # 先注销本次实例再走 _dispatch 全链（占名 → 查档 → 剥离 → 携档启动）
        SubagentRegistry.unregister(name)

        # 同名续跑（_dispatch 全链）：剥离后首请求无零配对 tool_calls + 尾部 = 当前任务
        ret_name, confirmation, captured = _dispatch_capture(name, "继续处理剩余文件")
        try:
            assert ret_name == name and "上轮上下文续跑" in confirmation
            assert len(captured) == 1
            kwargs = captured[0]
            assert kwargs["unique_name"] == name
            resumed = kwargs["resumed_messages"]
            assert resumed is not None
            assert resumed[0]["role"] == "system"
            _assert_no_dangling_tool_calls(resumed)
            # 剥离后的历史末端是 user 当前任务（悬空 assistant 已被剥除，不会变成零配对对）
            assert resumed[-1] == {"role": "user", "content": "继续处理剩余文件"}
        finally:
            SubagentRegistry.unregister(ret_name)
    finally:
        SubagentRegistry.unregister(name)


def test_08_llm_error_no_messages_skips_archive_with_warning(archive_dir):
    """8. LLM_ERROR（dict 无 messages）→不落盘 + warning 留痕（含 unique_name/result）。"""
    name, sq, mc = _register("file-processor-t3-0008")
    warned, sink_id = _capture_loguru("WARNING")
    try:
        rv = {"result": "LLM_ERROR", "error_msg": "AuthError: bad key", "error_type": None}
        with _hermetic_stack(loop_return=("", rv, "")):
            out = _call_subagent_async(name, sq, mc)

        assert out.startswith("SUBAGENT_ERROR:")
        assert not (Path(archive_dir) / f"{name}.json").exists(), "LLM_ERROR 形态不得落盘"
        inst = SubagentRegistry.get(name)
        # 门控失败分支：archive_written 保持 None（未到写盘点）
        assert getattr(inst, "archive_written", "sentinel") is None
        joined = "\n".join(warned)
        assert name in joined and "完成态无 messages 可存档" in joined and "result=LLM_ERROR" in joined
    finally:
        logger.remove(sink_id)
        SubagentRegistry.unregister(name)


def test_09_inner_exception_no_archive(archive_dir):
    """9. 内层异常（_run_agent_loop 抛）→不落盘（异常上抛，存档点不达）。"""
    name, sq, mc = _register("file-processor-t3-0009")
    try:
        stack = ExitStack()
        stack.enter_context(mock.patch("agent.runner.create_client", return_value=_FakeClient()))
        stack.enter_context(mock.patch("agent.subagent._run_agent_loop", side_effect=RuntimeError("loop boom")))
        stack.enter_context(mock.patch("agent.subagent.get_subagent_config", return_value={}))
        stack.enter_context(mock.patch("agent.subagent.build_subagent_system_segments", return_value=("sys-prompt", "")))
        stack.enter_context(mock.patch("agent.subagent._build_subagent_tools_schema", return_value=[]))
        stack.enter_context(mock.patch("agent.subagent._read_context_window_tokens", return_value=200000))
        stack.enter_context(mock.patch("agent.handler.NiuHandler", return_value=mock.MagicMock()))
        with stack:
            with pytest.raises(RuntimeError, match="loop boom"):
                _call_subagent_async(name, sq, mc)

        assert not (Path(archive_dir) / f"{name}.json").exists(), "异常路径不得落盘"
        inst = SubagentRegistry.get(name)
        assert getattr(inst, "archive_written", "sentinel") is None
    finally:
        SubagentRegistry.unregister(name)


def test_10_sync_end_not_archived(archive_dir):
    """10. 同步 @end→不落盘：存档块 is_sync 守卫 + 同步路径结构性不可达，双面锁定。"""
    # 面 A：存档块守卫——unique_name 分支携带 is_sync=True 实例时不写盘
    name, sq, mc = _register("file-processor-t3-0010", is_sync=True)
    try:
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "同步任务"}]
        rv = _mk_rv("EXITED", msgs, finish_reason="exited")
        with _hermetic_stack(loop_return=("", rv, "同步完成")):
            _call_subagent_async(name, sq, mc)
        assert getattr(SubagentRegistry.get(name), "archive_written", "sentinel") is None
        assert not (Path(archive_dir) / f"{name}.json").exists()
    finally:
        SubagentRegistry.unregister(name)

    # 面 B：真实同步路径（unique_name=None）——归档文件绝不出现（防 <agent_name>.json 跨类型污染）
    warn_cap, sink_id = _capture_loguru("ERROR")
    try:
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "同步任务"}]
        rv = _mk_rv("EXITED", msgs, finish_reason="exited")
        with _hermetic_stack(loop_return=("", rv, "同步完成 @end")):
            from agent.subagent import call_subagent
            call_subagent(
                agent_name=_AGENT,
                task="同步任务",
                llm_config=_LLM_CFG,
                mcp_client=None,
            )
        assert not (Path(archive_dir) / f"{_AGENT}.json").exists()
        assert not any("存档" in m for m in warn_cap)  # 同步路径不应产生存档留痕
    finally:
        logger.remove(sink_id)


# ===========================================================================
# T2 派发矩阵：同名指定 → 查档三分支
# ===========================================================================

def test_11_no_archive_same_name_fresh_dispatch_with_actual_name(archive_dir):
    """11. 无档同名→全新派发 + 实际名告知（不占 agent_name 同步命名空间）。"""
    given = "file-processor-t3-none"
    warned, sink_id = _capture_loguru("WARNING")
    try:
        name, confirmation, captured = _dispatch_capture(given, "处理任务 X")
        try:
            # 回退全新派发：实际名 ≠ 指定名（自动 hex），注册在返回名下
            assert name != given and name.startswith(f"{_AGENT}-")
            assert "[续跑回退]" in confirmation and name in confirmation
            assert "已派出子 Agent" in confirmation
            assert SubagentRegistry.get(name) is not None
            assert SubagentRegistry.get(given) is None, "占名必须释放"
            # 全新派发：resumed_messages=None（不携档），task 原样
            assert len(captured) == 1 and captured[0]["resumed_messages"] is None
            assert captured[0]["task"] == "处理任务 X"
            assert captured[0]["unique_name"] == name
            joined = "\n".join(warned)
            assert given in joined and "回退全新派发" in joined
        finally:
            SubagentRegistry.unregister(name)
    finally:
        logger.remove(sink_id)


def test_12_corrupt_archive_fresh_dispatch_with_warning(archive_dir):
    """12. 损坏档→全新 + warning（回退实际名告知）。"""
    given = "file-processor-t3-corrupt"
    (Path(archive_dir) / f"{given}.json").write_text("{ not valid json !!!", encoding="utf-8")
    warned, sink_id = _capture_loguru("WARNING")
    try:
        name, confirmation, captured = _dispatch_capture(given, "任务")
        try:
            assert name != given and name.startswith(f"{_AGENT}-")
            assert "[续跑回退]" in confirmation
            assert captured[0]["resumed_messages"] is None
            joined = "\n".join(warned)
            assert f"指定续跑名 {given} 不可用" in joined
            assert "JSON 损坏" in joined and "回退全新派发" in joined
        finally:
            SubagentRegistry.unregister(name)
    finally:
        logger.remove(sink_id)


def test_13_cross_type_archive_fresh_dispatch(archive_dir):
    """13. 跨类型（档 agent_type 不匹配）→全新派发 + 跨类型 warning。"""
    given = "file-processor-t3-x-type"
    _write_archive(
        archive_dir, given,
        messages=[_SYSTEM_MSG, {"role": "user", "content": "旧任务"}],
        agent_type="journal-agent",  # 档属其他类型
    )
    warned, sink_id = _capture_loguru("WARNING")
    try:
        name, confirmation, captured = _dispatch_capture(given, "新任务", agent_name=_AGENT)
        try:
            assert name != given and name.startswith(f"{_AGENT}-")
            assert "[续跑回退]" in confirmation and "全新派发" in confirmation
            assert captured[0]["resumed_messages"] is None
            joined = "\n".join(warned)
            assert f"指定续跑名 {given} 不可用" in joined and "跨类型" in joined
            assert "journal-agent" in joined and _AGENT in joined
        finally:
            SubagentRegistry.unregister(name)
    finally:
        logger.remove(sink_id)


def test_14_running_same_name_error_not_sync_wording(archive_dir):
    """14. 运行中同名→错误文案（"仍在运行中"，非 registry 同步路径写死文案）。"""
    name, sq, mc = _register("file-processor-t3-running")
    captured = []
    fake_loop = mock.MagicMock()
    fake_loop.is_closed.return_value = False
    with mock.patch("niu_api.chat._main_loop", fake_loop), \
         mock.patch("agent.subagent._run_subagent_async", new=lambda **kw: captured.append(kw) or None):
        from agent.subagent import _dispatch_async_subagent
        ret_name, confirmation = _dispatch_async_subagent(
            agent_name=_AGENT, task="任务", llm_config=_LLM_CFG, unique_name=name,
        )
    try:
        assert ret_name is None
        assert f"[错误] {name} 仍在运行中" in confirmation
        assert "同步路径" not in confirmation, "registry 同步文案不得误用于异步冲突"
        assert "请等待完成或先 @" in confirmation
        # 不启动新协程、不释放原实例
        assert captured == []
        assert SubagentRegistry.get(name) is not None
    finally:
        SubagentRegistry.unregister(name)


def test_15_empty_task_resume_injects_effective_task(archive_dir):
    """15. 空 task 同名续跑→effective_task="继续上次未完成的工作" 注入 + 通知不含 [错误]。"""
    given = "file-processor-t3-empty-task"
    _write_archive(
        archive_dir, given,
        messages=[_SYSTEM_MSG, {"role": "user", "content": "第一轮：整理照片"}],
    )
    name, confirmation, captured = _dispatch_capture(given, "", agent_name=_AGENT)
    try:
        # 命中档续跑：同名保持注册（不是回退）
        assert name == given
        assert "已加载" in confirmation and "上轮上下文续跑" in confirmation and given in confirmation
        assert "[错误]" not in confirmation and "[续跑回退]" not in confirmation
        assert len(captured) == 1
        kwargs = captured[0]
        assert kwargs["unique_name"] == given
        # effective_task 注入 + call_subagent 入口闸门满足
        assert kwargs["task"] == "继续上次未完成的工作"
        resumed = kwargs["resumed_messages"]
        assert resumed is not None
        assert resumed[0]["role"] == "system"
        assert resumed[-1] == {"role": "user", "content": "继续上次未完成的工作"}
        # 上轮上下文在场（第一轮任务 user 保留）
        assert any(m.get("role") == "user" and "第一轮" in m.get("content", "") for m in resumed)
    finally:
        SubagentRegistry.unregister(name)


# ===========================================================================
# 时序 / 写盘失败 / 续跑覆盖 / 路径不入消息
# ===========================================================================

async def test_16_archive_written_before_completion_notification(archive_dir, monkeypatch):
    """16. 时序：完成通知组装（push 时刻）archive_written=True + 档文件已就绪。"""
    _drain_queue()
    name, sq, mc = _register("file-processor-t3-0016")
    q = get_main_agent_request_queue()
    observed = {}
    orig_push = q.push

    def _check_push(msg, msg_type="notify"):
        inst = SubagentRegistry.get(name)
        observed["inst_present"] = inst is not None
        observed["archive_written"] = getattr(inst, "archive_written", None) if inst else None
        observed["file_exists"] = (Path(archive_dir) / f"{name}.json").exists()
        observed["msg"] = msg
        orig_push(msg, msg_type)

    monkeypatch.setattr(q, "push", _check_push)
    try:
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "任务"}, {"role": "assistant", "content": "处理中"}]
        rv = _mk_rv("EXITED", msgs, finish_reason="exited")
        from agent.subagent import _run_subagent_async
        with _hermetic_stack(loop_return=("", rv, "任务完成总结 @end")):
            await _run_subagent_async(
                unique_name=name, agent_name=_AGENT, task="任务",
                llm_config=_LLM_CFG, memory_context=mc, supplement_queue=sq,
            )

        # 通知组装时刻：档已写盘、标志已置位（写盘先于通知——主 Agent 收到即同名重调必命中）
        assert observed.get("inst_present") is True
        assert observed.get("archive_written") is True
        assert observed.get("file_exists") is True
        assert "已完成" in observed.get("msg", "") and name in observed.get("msg", "")
        # 收尾：worker finally 注销
        assert SubagentRegistry.get(name) is None
    finally:
        SubagentRegistry.unregister(name)
        _drain_queue()


async def test_17_archive_write_failure_notification_no_resume_promise(archive_dir, monkeypatch):
    """17. 写盘失败→通知不含续跑承诺（同名重调/24h 话术）+ 写盘失败 error 留痕。"""
    _drain_queue()
    name, sq, mc = _register("file-processor-t3-0017")
    q = get_main_agent_request_queue()
    pushes = []
    orig_push = q.push
    monkeypatch.setattr(q, "push", lambda msg, msg_type="notify": (pushes.append(msg), orig_push(msg, msg_type)))
    errors, sink_id = _capture_loguru("WARNING")
    try:
        monkeypatch.setattr("agent.tmp_dir.write_archive", lambda unique_name, data: False)  # 写盘失败
        msgs = [_SYSTEM_MSG, {"role": "user", "content": "任务"}, {"role": "assistant", "content": "中途"}]
        rv = _mk_rv("STOPPED", msgs)
        from agent.subagent import _run_subagent_async
        with _hermetic_stack(loop_return=("", rv, "被打断的中间话")):
            await _run_subagent_async(
                unique_name=name, agent_name=_AGENT, task="任务",
                llm_config=_LLM_CFG, memory_context=mc, supplement_queue=sq,
            )

        # 写盘失败留痕 + 标志 False（通知组装方可据它抑制续跑承诺）
        assert SubagentRegistry.get(name) is None  # worker finally 注销
        joined = "\n".join(errors)
        assert name in joined and "存档写盘失败" in joined and "archive_written=False" in joined
        # 通知：STOPPED → incomplete 通知存在，但绝不带同名重调/24h 续跑承诺
        assert len(pushes) == 1
        msg = pushes[0]
        assert name in msg and "未完成" in msg
        for forbidden in ("重新调取", "原唯一名", "24h", "24 小时", "同名续跑"):
            assert forbidden not in msg, f"写盘失败通知不得承诺续跑: {msg}"
        assert not (Path(archive_dir) / f"{name}.json").exists()
    finally:
        logger.remove(sink_id)
        SubagentRegistry.unregister(name)
        _drain_queue()


async def test_18_resume_completion_overwrites_same_name_archive_and_notifies_again(archive_dir):
    """18. 续跑完成后同名档被覆盖（续跑上下文 + 末轮补全在场）+ 完成通知再推。"""
    _drain_queue()
    name = "file-processor-t3-0018"
    from agent.subagent import _prepare_resume_messages, _run_subagent_async

    # ---- 阶段 1：首跑完成 → 存档（含末轮总结）----
    name1, sq1, mc1 = _register(name)
    try:
        msgs1 = [_SYSTEM_MSG, {"role": "user", "content": "阶段一任务"}, {"role": "assistant", "content": "阶段一完成"}]
        rv1 = _mk_rv("EXITED", msgs1, finish_reason="exited")
        with _hermetic_stack(loop_return=("", rv1, "阶段一总结")):
            await _run_subagent_async(
                unique_name=name, agent_name=_AGENT, task="阶段一任务",
                llm_config=_LLM_CFG, memory_context=mc1, supplement_queue=sq1,
            )
    finally:
        SubagentRegistry.unregister(name)

    first_archive = _read_archive(archive_dir, name)
    assert first_archive["messages"][-1] == {"role": "assistant", "content": "阶段一总结"}
    notify1 = _drain_queue()
    assert any("已完成" in m and name in m for m in notify1), f"首跑完成通知缺失: {notify1}"

    # ---- 阶段 2：同名续跑（携档 + 追加新任务）→ 完成 → 同名档被覆盖 ----
    prepared = _prepare_resume_messages(first_archive["messages"])
    assert prepared is not None
    prepared.append({"role": "user", "content": "阶段二：处理剩余文件"})
    name2, sq2, mc2 = _register(name)
    try:
        # 续跑上下文 = 阶段一全量（系统 + 任务 + 总结）+ 新任务
        resumed_msgs = [dict(m) for m in prepared]
        resumed_msgs.append({"role": "assistant", "content": "阶段二完成"})
        rv2 = _mk_rv("EXITED", resumed_msgs, finish_reason="exited")
        with _hermetic_stack(loop_return=("", rv2, "阶段二总结")):
            await _run_subagent_async(
                unique_name=name, agent_name=_AGENT, task="阶段二：处理剩余文件",
                llm_config=_LLM_CFG, memory_context=mc2, supplement_queue=sq2,
                resumed_messages=[dict(m) for m in prepared],
            )
    finally:
        SubagentRegistry.unregister(name)

    second_archive = _read_archive(archive_dir, name)
    # 覆盖而非重置：续跑上下文全量在场（阶段一任务/总结）+ 新任务 + 本轮末轮补全
    text = json.dumps(second_archive["messages"], ensure_ascii=False)
    assert "阶段一任务" in text and "阶段一总结" in text
    assert "阶段二：处理剩余文件" in text
    assert second_archive["messages"][-1] == {"role": "assistant", "content": "阶段二总结"}
    assert len(second_archive["messages"]) > len(first_archive["messages"])
    # 完成通知再推（第二次）
    notify2 = _drain_queue()
    assert any("已完成" in m and name in m for m in notify2), f"续跑完成通知缺失: {notify2}"


def test_19_archive_path_not_leaked_into_messages(archive_dir):
    """19. 存档路径不入消息内容：路径常量知识唯一归属 tmp_dir.py + 可见文本零路径。"""
    base = Path(__file__).resolve().parent.parent
    # ---- 静态面：消息构造模块不得含 tmp 存档路径字面量（路径知识唯一归属 agent/tmp_dir.py）----
    for rel in ("agent/subagent.py", "agent/handler.py", "agent/subagent_registry.py", "agent/runner.py"):
        src = (base / rel).read_text(encoding="utf-8")
        assert ".niu/tmp" not in src and ".niu\\tmp" not in src, \
            f"{rel} 不得含 tmp 存档路径常量（路径知识唯一归属 agent/tmp_dir.py）"
    tmp_src = (base / "agent/tmp_dir.py").read_text(encoding="utf-8")
    assert '"tmp"' in tmp_src and '".niu"' in tmp_src, "tmp_dir.py 应持有存档路径常量"

    # ---- 行为面：全链可见文本（派单确认 + 完成通知 + 回退告知）不含任何路径形态 ----
    visible = []

    # (a) 全新派发回退确认（含 [续跑回退]）——不泄露指定/实际名对应的磁盘位置
    name_a, confirm_a, _ = _dispatch_capture("file-processor-t3-leak-none", "任务")
    visible.append(confirm_a)
    SubagentRegistry.unregister(name_a)

    # (b) 命中档续跑确认
    given = "file-processor-t3-leak-hit"
    _write_archive(archive_dir, given, messages=[_SYSTEM_MSG, {"role": "user", "content": "旧任务"}])
    name_b, confirm_b, _ = _dispatch_capture(given, "继续任务")
    visible.append(confirm_b)
    SubagentRegistry.unregister(name_b)

    # (c) 完成通知文本（EXITED 完整 + STOPPED 未完成）
    for rv_result, last_reply in (("EXITED", "完毕"), ("STOPPED", "")):
        name_c, sq_c, mc_c = _register(f"file-processor-t3-leak-{rv_result.lower()}")
        try:
            rv_c = _mk_rv(rv_result, [_SYSTEM_MSG, {"role": "user", "content": "任务"}])
            from agent.subagent import _run_subagent_async
            with _hermetic_stack(loop_return=("", rv_c, last_reply)):
                asyncio.run(_run_subagent_async(
                    unique_name=name_c, agent_name=_AGENT, task="任务",
                    llm_config=_LLM_CFG, memory_context=mc_c, supplement_queue=sq_c,
                ))
        finally:
            SubagentRegistry.unregister(name_c)
        pushed = _drain_queue()
        visible.extend(pushed)

    joined_visible = "\n".join(v for v in visible if v)
    archive_marker = str(Path(archive_dir))  # 本轮 tmp 目录实路径（行为级：任何路径形态都禁止）
    for tok in (archive_marker, "/.niu/tmp", "~/.niu/tmp", ".json", ".archive-"):
        assert tok not in joined_visible, f"可见消息泄露存档路径形态 {tok!r}: {joined_visible}"
