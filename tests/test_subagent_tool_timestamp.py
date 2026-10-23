"""工具消息时间戳：格式、截断公告保留、幂等、两处构造点、纯前缀、主 Agent 不受影响。"""
import copy

from agent.generic import agent_loop as al
from agent.generic.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    StepOutcome,
    _append_tool_timestamp,
    _truncate_tool_content,
)
from agent.generic.llmcore import MockResponse, MockToolCall


def test_append_format():
    out = _append_tool_timestamp("RESULT")
    assert out.startswith("RESULT\n\n[")
    stamp = out[out.rindex("[") + 1:-1]
    assert len(stamp) == 8 and stamp.count(":") == 2


def test_append_preserves_truncation_notice():
    gate_out = _truncate_tool_content("x" * (MAX_TOOL_RESULT_CHARS + 5000), "bash")
    out = _append_tool_timestamp(gate_out)
    assert len(out) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in out and out.endswith("]")


def test_append_exact_boundary_without_notice_skips_marker():
    out = _append_tool_timestamp("y" * MAX_TOOL_RESULT_CHARS)
    assert out == "y" * MAX_TOOL_RESULT_CHARS  # 宁可无标记，也不切正文


def test_append_room_boundary():
    room = MAX_TOOL_RESULT_CHARS - len("\n\n[00:00:00]")
    assert _append_tool_timestamp("z" * room).rstrip().endswith("]")
    over = "z" * (room + 1)
    assert _append_tool_timestamp(over) == over


def test_truncate_idempotent_after_append():
    gate_out = _truncate_tool_content("x" * (MAX_TOOL_RESULT_CHARS + 5000))
    out = _append_tool_timestamp(gate_out)
    assert out.rstrip().endswith("]")
    assert _truncate_tool_content(out) == out


def test_pipeline_order_is_distinguishable():
    """先截断后追加 vs 先追加后截断，产物必须不同（防用例锁不住顺序）。"""
    raw = "x" * (MAX_TOOL_RESULT_CHARS + 10000)
    good = _append_tool_timestamp(_truncate_tool_content(raw, "bash"))
    bad = _truncate_tool_content(_append_tool_timestamp(raw))
    assert good != bad
    assert good.rstrip().endswith("]")
    assert not bad.rstrip().endswith("]")


class _CaptureClient:
    def __init__(self, tool_rounds=2):
        self.snapshots = []
        self.n = 0
        self.tool_rounds = tool_rounds

    def chat(self, messages, tools=None):
        self.snapshots.append(copy.deepcopy(messages))
        self.n += 1
        if self.n <= self.tool_rounds:
            resp = MockResponse(thinking="", content="", usage={},
                                tool_calls=[MockToolCall(name="noop", args={}, id=f"call_{self.n}")], raw="")
        else:
            resp = MockResponse(thinking="", content="完成", usage={}, tool_calls=[], raw="")

        def _gen():
            yield from ()
            return resp

        return _gen()


class _Handler:
    def __init__(self, should_exit=False, payload="TOOL-OUT"):
        self._is_subagent = False
        self._subagent_unique_name = None
        self._current_messages = []
        self.current_turn = 0
        self._last_prompt_tokens = 0
        self.last_tools = ""
        self.max_turns = 20
        self._done_hooks = []
        self.should_exit = should_exit
        self.payload = payload

    def tool_after_callback(self, *a, **k):
        pass

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def dispatch(self, tool_name, args, response, index=0):
        def _gen():
            yield
            return StepOutcome(data=self.payload, next_prompt="继续", should_exit=self.should_exit)

        return _gen()


def _run(tool_timestamp=True, rounds=2, should_exit=False, payload="TOOL-OUT"):
    client = _CaptureClient(tool_rounds=rounds if not should_exit else 1)
    handler = _Handler(should_exit=should_exit, payload=payload)
    gen = al.agent_runner_loop(
        client=client, system_prompt="S", user_input="task", handler=handler,
        tools_schema=[], max_turns=6, context_window_tokens=0,
        enable_supplement=False, stop_predicate=lambda: False,
        tool_timestamp=tool_timestamp,
    )
    return al.exhaust(gen), client.snapshots


def test_long_tool_output_marker_survives_gate():
    """构造点必须「先截断、后追加」：40k 输出经关口与构造点后仍带标记与公告。"""
    _, snaps = _run(tool_timestamp=True, rounds=1, payload="x" * 40000)
    tool_msgs = [m for m in snaps[-1] if m.get("role") == "tool"]
    assert tool_msgs
    content = tool_msgs[-1]["content"]
    assert len(content) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in content and content.rstrip().endswith("]")


def test_normal_path_marks_tool_messages():
    _, snaps = _run(tool_timestamp=True)
    tool_msgs = [m for m in snaps[-1] if m.get("role") == "tool"]
    assert tool_msgs and all(m["content"].rstrip().endswith("]") for m in tool_msgs)


def test_should_exit_path_marks_tool_messages():
    ret, _ = _run(tool_timestamp=True, should_exit=True)
    tool_msgs = [m for m in ret["messages"] if m.get("role") == "tool"]
    assert tool_msgs and all(m["content"].rstrip().endswith("]") for m in tool_msgs)


def test_default_no_marker():
    _, snaps = _run(tool_timestamp=False)
    tool_msgs = [m for m in snaps[-1] if m.get("role") == "tool"]
    assert tool_msgs and all("[" not in m["content"] for m in tool_msgs)


def test_messages_are_prefix_extension():
    _, snaps = _run(tool_timestamp=True, rounds=3)
    assert len(snaps) >= 3
    for prev, cur in zip(snaps, snaps[1:]):
        assert cur[:len(prev)] == prev, "上一轮 messages 必须是下一轮的前缀（否则缓存失效）"


def test_main_agent_call_site_does_not_pass_flag():
    from pathlib import Path
    assert "tool_timestamp=" not in (Path(__file__).resolve().parents[1] / "agent/runner.py").read_text(encoding="utf-8")
