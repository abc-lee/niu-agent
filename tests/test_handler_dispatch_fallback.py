"""测试 handler.dispatch 统一异常包裹（E1 Task 1，2026-08-15）。

背景：主/子 Agent 工具调用抛未捕获异常（do_* 参数畸形 / disk_engine.execute 裸调用 /
chat-with 异步分支 / 回调穿透）→ 工具循环死亡 / 会话中止，Agent 看不到错误。
修复：dispatch 外壳统一 `except Exception` → TOOL_ERROR error dict 进 StepOutcome.data
（tool 消息 → LLM 可见可自纠）；BaseException（KeyboardInterrupt 等）保留穿透。

TDD: 先写测试确认红（当前实现崩溃），再改实现跑绿。
"""
import pytest
from unittest.mock import Mock, patch

from agent.generic.agent_loop import StepOutcome, StreamEvent
from agent.generic.interruptible import run_interruptibly
from agent.handler import NiuHandler


class BadStr(Exception):
    """__str__ 抛异常的坏异常——测试错误文本构造兜底（防二次抛异常杀循环）。"""

    def __str__(self):
        raise RuntimeError("bad __str__")


def _make_handler(**kwargs):
    """创建一个 NiuHandler 实例，带默认 mock 依赖。"""
    return NiuHandler(
        cwd="/tmp",
        mcp_client=kwargs.get("mcp_client", Mock()),
        disk_engine=kwargs.get("disk_engine", None),
    )


def _run_dispatch(handler, tool_name, args, response=None, index=0):
    """消费 dispatch 生成器，返回 (yield 事件列表, 最终 StepOutcome)。"""
    events = []
    gen = handler.dispatch(tool_name, args, response, index=index)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value
    return events, None


def _consume_dispatch(gen):
    """非 verbose 消费语义（同 exhaust 循环）+ 捕获生成器返回值。"""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _system_events(events):
    return [e for e in events if isinstance(e, StreamEvent) and e.type == "system"]


def test_do_tool_exception_returns_error_stepoutcome():
    """do_* 抛 ValueError（do_read int(raw_offset) 畸形参数）→ StepOutcome error dict TOOL_ERROR。"""
    handler = _make_handler()
    # offset='abc' → do_read 内 int('abc') 抛 ValueError（真实参数畸形路径）
    events, outcome = _run_dispatch(handler, "read", {"path": "/tmp/foo.txt", "offset": "abc"})

    assert isinstance(outcome, StepOutcome), f"应返回 StepOutcome，实际: {outcome!r}"
    data = outcome.data if isinstance(outcome.data, dict) else {}
    assert data.get("status") == "error"
    assert data.get("error_code") == "TOOL_ERROR"
    assert "Tool read failed: ValueError" in data.get("msg", ""), f"msg 应含错误类型与消息，实际: {data.get('msg')}"
    assert "invalid literal" in data.get("msg", "")
    sys_events = _system_events(events)
    assert any("[Tool Error]" in e.content for e in sys_events), \
        f"应有 [Tool Error] system 事件，实际: {sys_events}"


def test_do_tool_exception_loop_survives():
    """非 verbose 路径（run_interruptibly 消费）——dispatch 异常 → 循环继续不死亡（error dict 返回）。"""
    handler = _make_handler()
    gen = handler.dispatch("read", {"path": "/tmp/foo.txt", "offset": "abc"}, None, index=0)
    completed, outcome = run_interruptibly(_consume_dispatch, lambda: False, args=(gen,))

    assert completed is True, "run_interruptibly 应正常完成（异常未上抛——循环不死亡）"
    assert isinstance(outcome, StepOutcome), f"应返回 StepOutcome，实际: {outcome!r}"
    data = outcome.data if isinstance(outcome.data, dict) else {}
    assert data.get("status") == "error"
    assert data.get("error_code") == "TOOL_ERROR"


def test_disk_execute_exception_returns_error():
    """mock disk_engine.execute 抛 RuntimeError → StepOutcome error dict（disk 裸调用链兜底）。"""
    mock_engine = Mock()
    mock_engine.execute.side_effect = RuntimeError("disk boom")
    handler = _make_handler(disk_engine=mock_engine)

    events, outcome = _run_dispatch(handler, "disk", {"command": "ls"})

    assert isinstance(outcome, StepOutcome), f"应返回 StepOutcome，实际: {outcome!r}"
    data = outcome.data if isinstance(outcome.data, dict) else {}
    assert data.get("status") == "error"
    assert data.get("error_code") == "TOOL_ERROR"
    assert "Tool disk failed: RuntimeError: disk boom" in data.get("msg", ""), \
        f"msg 应含 'Tool disk failed: RuntimeError: disk boom'，实际: {data.get('msg')}"
    sys_events = _system_events(events)
    assert any("[Tool Error]" in e.content for e in sys_events), \
        f"应有 [Tool Error] system 事件，实际: {sys_events}"


def test_base_exception_not_caught():
    """契约锁定：KeyboardInterrupt（BaseException）仍穿透——外层 except Exception 不捕获。"""
    handler = _make_handler()
    with patch.object(handler, "do_read", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _run_dispatch(handler, "read", {})


def test_mcp_error_does_not_trigger_outer():
    """契约锁定：MCP 分支内层 except 优先——system 事件不含 '[Tool Error]' 前缀。"""
    handler = _make_handler()
    mock_registry = Mock()
    mock_func = Mock(side_effect=RuntimeError("mcp boom"))
    mock_registry.get.return_value = mock_func

    with patch("agent.tool_registry.get_registry", return_value=mock_registry):
        events, outcome = _run_dispatch(handler, "some-server/some_tool", {})

    assert isinstance(outcome, StepOutcome), f"应返回 StepOutcome，实际: {outcome!r}"
    sys_events = _system_events(events)
    assert not any("[Tool Error]" in e.content for e in sys_events), \
        f"外层包裹不应触发（MCP 内层 except 已处理并 return），实际: {sys_events}"
    assert any("[MCP Error]" in e.content for e in sys_events), \
        f"应有 [MCP Error] system 事件（内层 except 优先），实际: {sys_events}"


def test_bad_str_exception_safe():
    """坏 __str__ 异常 → StepOutcome error dict（msg 含 <unprintable>）+ 无二次异常。"""
    handler = _make_handler()
    with patch.object(handler, "do_read", side_effect=BadStr("boom")):
        events, outcome = _run_dispatch(handler, "read", {})

    assert isinstance(outcome, StepOutcome), f"应返回 StepOutcome，实际: {outcome!r}"
    data = outcome.data if isinstance(outcome.data, dict) else {}
    assert data.get("status") == "error"
    assert data.get("error_code") == "TOOL_ERROR"
    assert "BadStr: <unprintable>" in data.get("msg", ""), \
        f"msg 应含 '<unprintable>' 兜底文本，实际: {data.get('msg')}"
    sys_events = _system_events(events)
    assert any("[Tool Error]" in e.content and "<unprintable>" in e.content for e in sys_events), \
        f"system 事件应复用 err_detail（含 <unprintable>），实际: {sys_events}"


def test_failed_tool_status_end_pushed():
    """失败工具 → 状态 end 推送（前端不滞留）——主 Agent notify_tool_status_sync + 子 Agent _push_subagent_event 双断言。"""
    # 主 Agent 路径：notify_tool_status_sync(short_name, 'end')
    handler = _make_handler()
    with patch.object(handler, "do_read", side_effect=ValueError("boom")), \
         patch("niu_api.chat.notify_tool_status_sync") as mock_notify:
        events, outcome = _run_dispatch(handler, "read", {"path": "/tmp/foo.txt"})

    end_calls = [c for c in mock_notify.call_args_list if c.args == ("read", "end")]
    assert len(end_calls) == 1, \
        f"主 Agent 失败工具应推送 ('read', 'end')，实际调用: {mock_notify.call_args_list}"

    # 子 Agent 路径：_push_subagent_event(unique_name, 'tool_status', {'status': 'end', ...})
    sub_handler = _make_handler()
    sub_handler._is_subagent = True
    sub_handler._subagent_unique_name = "test-agent"
    with patch.object(sub_handler, "do_read", side_effect=ValueError("boom")), \
         patch("agent.handler._push_subagent_event") as mock_push:
        events, outcome = _run_dispatch(sub_handler, "read", {"path": "/tmp/foo.txt"})

    end_calls = [
        c for c in mock_push.call_args_list
        if len(c.args) == 3 and c.args[1] == "tool_status" and c.args[2].get("status") == "end"
    ]
    assert len(end_calls) == 1, \
        f"子 Agent 失败工具应推送一次 end，实际调用: {mock_push.call_args_list}"
    unique_name, event_type, data = end_calls[0].args
    assert unique_name == "test-agent"
    assert event_type == "tool_status"
    assert data["tool_name"] == "read"
    assert data["summary"] == "tool error"
