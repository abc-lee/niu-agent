"""停止穿透工程单文件测试：可中断流式等待 + 子 Agent 停止隔离。全 mock，不调真实 LLM。

覆盖：
- T1: LiteLLMSession._interruptible_iter 可中断等待（正常/中途停止/流错误/停止+错误竞态/默认 stop_check）
- T2/T4: request_stop_all_subagents 的 source 门控（user 收 /stop，program/scheduler 跳过）
- T5: agent_runner_loop 的 stop 谓词与 clear_stop 门控（子 Agent 不清全局标志）
- T6: _tidy_context_impl 的 stop_aware（sleep 不 abort，force abort）
- T3: chat_queue 来源归一化表达式
"""
import threading

import pytest
from unittest import mock


# ---- T1: _interruptible_iter 可中断等待 ----

def _make_session():
    from agent.generic.litellm_adapter import LiteLLMSession
    return LiteLLMSession({"apikey": "x", "apibase": "http://x", "model": "m"})


class _Chunk:
    """最小 chunk 桩：choices[0].delta.content / finish_reason / usage。"""

    def __init__(self, content="", finish_reason=None):
        delta = type("Delta", (), {"content": content, "reasoning_content": "", "tool_calls": None})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()
        # 无内容无 finish_reason 的 chunk 视为空 choices（与真实流一致）
        self.choices = [choice] if (content or finish_reason) else []


def _chunk_content(chunk):
    return chunk.choices[0].delta.content


def test_interruptible_iter_complete():
    """正常消费完整 response 生成器，yield 全部 chunk。

    _interruptible_iter 产出原始 chunk 对象（由 _do_streaming_completion 消费），
    这里按 chunk 内容断言。
    """
    session = _make_session()
    response = iter([_Chunk("a"), _Chunk("b"), _Chunk("c", finish_reason="stop")])
    got = list(session._interruptible_iter(response))
    assert [_chunk_content(c) for c in got] == ["a", "b", "c"]


def test_interruptible_iter_stop_midstream():
    """stop_check 置位后迭代在 ≤0.2s 内终止（不依赖 response 吐完）。"""
    import time

    session = _make_session()
    state = {"stop": False}
    session.stop_check = lambda: state["stop"]

    def slow_gen():
        yield _Chunk("a")
        yield _Chunk("b")
        time.sleep(1.0)  # 模拟 LLM 挂起（无 chunk）
        yield _Chunk("c")

    it = session._interruptible_iter(slow_gen())
    assert _chunk_content(next(it)) == "a"
    assert _chunk_content(next(it)) == "b"
    state["stop"] = True
    start = time.monotonic()
    with pytest.raises(StopIteration):
        next(it)
    assert time.monotonic() - start < 0.5  # 0.2s 轮询 + 余量


def test_interruptible_iter_error():
    """response 中途 raise → 原异常上抛给调用方。"""
    session = _make_session()

    def bad_gen():
        yield _Chunk("a")
        raise RuntimeError("boom")

    it = session._interruptible_iter(bad_gen())
    assert _chunk_content(next(it)) == "a"
    with pytest.raises(RuntimeError, match="boom"):
        next(it)


def test_interruptible_iter_stop_then_error():
    """stop 置位 + 后台同时抛错 → 无死锁；error 不被 stop 吞掉。

    与计划初稿的差异：实码 _pull 的 except 分支"先无条件尝试一次：stop 与流错误
    竞态时 error 不能被吞"（P3），因此 stop 已置位时流错误仍会原样上抛，
    而非返回 StopIteration。此处以实码为准断言 RuntimeError 传播。
    """
    session = _make_session()
    session.stop_check = lambda: True

    def bad_gen():
        raise RuntimeError("boom")
        yield  # 使函数成为 generator：调用不抛错，首次 next() 才抛

    it = session._interruptible_iter(bad_gen())
    with pytest.raises(RuntimeError, match="boom"):
        next(it)


def test_stop_check_default_global():
    """LiteLLMSession 默认 stop_check 是全局 is_stop_requested，且 call-time 解析（monkeypatch 生效）。"""
    from agent.generic.litellm_adapter import LiteLLMSession, is_stop_requested
    session = LiteLLMSession({"apikey": "x", "apibase": "http://x", "model": "m"})
    assert session.stop_check() == is_stop_requested()
    with mock.patch("agent.generic.litellm_adapter.is_stop_requested", return_value=True):
        assert session.stop_check() is True  # call-time 解析（T1-P1 修复验证）


# ---- T2/T4: request_stop_all_subagents source 门控 ----

def test_request_stop_all_skips_program_scheduler():
    """仅 user 实例收到 push + terminate_event.set；program/scheduler 跳过。

    与计划初稿的差异：request_stop_all_subagents 内部 `from agent.ask_main_agent
    import get_pending_ask_registry`（调用时解析），因此 patch 目标是
    agent.ask_main_agent.get_pending_ask_registry，而非 agent.runner 上的同名属性。
    """
    from agent.runner import request_stop_all_subagents
    from agent.subagent_registry import SubagentRegistry

    calls = []
    evs = {}
    for name, src in (("user-1", "user"), ("prog-1", "program"), ("sched-1", "scheduler")):
        ev = threading.Event()
        evs[name] = ev
        inst = mock.MagicMock(unique_name=name, source=src, supplement_queue=mock.MagicMock(),
                              state="running", terminate_event=ev)
        calls.append(inst)
    with mock.patch.object(SubagentRegistry, "list_running", return_value=calls), \
         mock.patch("agent.ask_main_agent.get_pending_ask_registry") as _p:
        _p.return_value.cancel_pending_ask = mock.MagicMock()
        request_stop_all_subagents()
    assert calls[0].supplement_queue.push.called  # user-1 收到
    assert evs["user-1"].is_set()
    assert not calls[1].supplement_queue.push.called  # program 跳过
    assert not calls[2].supplement_queue.push.called  # scheduler 跳过
    assert not evs["prog-1"].is_set()
    assert not evs["sched-1"].is_set()


# ---- T5: agent_loop 谓词与 clear_stop 门控 ----

def _make_loop_response():
    """非 verbose 路径需要的响应对象。显式置假值，避免 MagicMock 自动真值误入 LLM_ERROR 分支。"""
    resp = mock.MagicMock()
    resp.stream_error = False
    resp.error_msg = None
    resp.content = ""
    resp.tool_calls = []
    resp.finish_reason = "stop"
    resp.usage = None
    resp.context_overflow = False
    resp.thinking = None
    return resp


def _make_loop_client():
    """client.chat 返回 generator：yield 一个空 chunk + return MockResponse（exhaust 取 return 值）。"""
    client = mock.MagicMock()
    resp = _make_loop_response()

    def _chat_gen(messages, tools):
        def gen():
            yield  # exhaust 消费；StopIteration.value = resp 被 exhaust 返回
            return resp

        return gen()

    client.chat.side_effect = _chat_gen
    return client


def _run_loop(handler, global_stop, stop_predicate=None):
    """跑一轮 agent_runner_loop（空流 → FORMAT_ERROR → continue → max_turns 耗尽退出）。

    与计划初稿的差异：agent_runner_loop 内部 `from agent.runner import clear_stop,
    is_stop_requested`（调用时解析），因此必须 patch agent.runner.*，而不是
    agent.generic.agent_loop.* 上的同名属性。
    """
    from agent.generic.agent_loop import agent_runner_loop
    client = _make_loop_client()
    with mock.patch("agent.runner.clear_stop") as cs, \
         mock.patch("agent.runner.is_stop_requested", return_value=global_stop), \
         mock.patch("agent.generic.agent_loop.count_messages_tokens", return_value=0), \
         mock.patch("agent.generic.agent_loop._read_warning_threshold", return_value=0.8):
        list(agent_runner_loop(client=client, system_prompt="", system_message={"role": "system", "content": ""},
                               user_input="hi", handler=handler, tools_schema=[], max_turns=1,
                               verbose=False, stop_predicate=stop_predicate))
    return cs


def test_agent_loop_subagent_no_clear_stop():
    """子 Agent（_is_subagent=True）退出不清全局标志。"""
    handler = mock.MagicMock()
    handler._is_subagent = True
    cs = _run_loop(handler, global_stop=False)
    assert not cs.called  # 子 Agent 不清


def test_agent_loop_main_agent_clears_stop():
    """主 Agent（_is_subagent=False）退出照清。"""
    handler = mock.MagicMock()
    handler._is_subagent = False
    cs = _run_loop(handler, global_stop=False)
    assert cs.called  # 主 Agent 照清


def test_agent_loop_subagent_stop_predicate():
    """异步子 Agent 谓词 terminate-only：全局置位但谓词 False → 轮起始检查不退出（继续跑）。"""
    handler = mock.MagicMock()
    handler._is_subagent = True
    terminate = threading.Event()
    cs = _run_loop(handler, global_stop=True, stop_predicate=lambda: terminate.is_set())
    assert not cs.called  # 全局 True 但谓词 False → 不退出不清标志


# ---- T6: sleep tidy stop_aware ----

def _tidy_messages():
    msgs = []
    for i, mid in enumerate(["m1", "m2"]):
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {i}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


class _FakeCalc:
    def count_message_single(self, role, content, tool_calls=None):
        return 100


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0


def _tidy_common_patches():
    """_tidy_context_impl 共用的模块级/调用时解析 seam。

    与计划初稿的差异：run_entity_extractor 等是函数内局部闭包，patch 不到；
    这里 patch 真实 seam——agent.token_calculator.TokenCalculator.get、
    niu_api.chat.get_or_create_runner、agent.subagent.call_subagent_with_auto_answer
    （均为函数内 `from X import Y` 调用时解析）、niu_api.compat 模块级函数。
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", return_value=""),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=0),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        # 增量区间恒空 → 各阶段"no new messages"，不调子 Agent、不写游标
        mock.patch("niu_api.compat._build_incremental_msg_text", return_value=""),
        # 防 ~/.niu 既有游标文件触发真实游标写入（测试保持 hermetic）
        mock.patch("niu_api.compat._write_cursor_with_lock"),
    ]


def test_sleep_tidy_stop_aware_false():
    """sleep 模式（stop_aware=False）：is_stop_requested=True 时阶段间检查不 abort（程序任务隔离）。"""
    import asyncio
    from contextlib import ExitStack

    from niu_api.compat import _tidy_context_impl

    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in _tidy_common_patches():
            stack.enter_context(p)
        stack.enter_context(mock.patch("agent.runner.is_stop_requested", return_value=True))
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    assert result.get("status") == "ok"  # sleep 完整跑完，不 abort
    assert result.get("status") != "aborted"


def test_force_tidy_stop_aware_true():
    """force 模式（stop_aware=True）：is_stop_requested=True 时第一阶段后 abort。"""
    import asyncio
    from contextlib import ExitStack

    from niu_api.compat import _tidy_context_impl

    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in _tidy_common_patches():
            stack.enter_context(p)
        stack.enter_context(mock.patch("agent.runner.is_stop_requested", return_value=True))
        clear_stop_mock = stack.enter_context(mock.patch("agent.runner.clear_stop"))
        result = asyncio.run(_tidy_context_impl({"mode": "force", "session_id": "t"}, chat_lock_already_held=True))
    assert result.get("status") == "aborted"  # force 阶段间检查 abort
    assert clear_stop_mock.called


# ---- T3: source 归一化 ----

def test_request_source_norm():
    """chat_queue 来源归一化表达式：scheduler/ha-watcher→scheduler；frontend/im→user。

    直接提取仓库中的真实归一化表达式验证（避免测试复制逻辑产生漂移）。
    """
    import inspect
    import re

    from niu_api import chat_queue

    src = inspect.getsource(chat_queue)
    m = re.search(r"_norm_source = (.+)$", src, re.MULTILINE)
    assert m, "chat_queue.py 应包含 _norm_source 归一化表达式"
    norm = eval("lambda source: " + m.group(1).strip(), {})
    assert norm("scheduler") == "scheduler"
    assert norm("ha-watcher") == "scheduler"
    assert norm("frontend") == "user"
    assert norm("im") == "user"
