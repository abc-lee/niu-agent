"""fold 成功结果照常落库测试（2026-09-02 循环折叠修复：回滚"不落库"）。

机制：agent_loop persist 循环对每条 tool 结果 yield persist 事件——fold_tool_output
成功结果照常落库。理由（raw_http 实证）：不落库时 LLM 每轮 rebuild 后看不到
"我折过了"的记录，反复折叠同一批编号；落库后 assistant 行的 fold tool_call 与
配对 tool 结果在 transform_history 中成对保留，LLM 从历史看到折叠确认即停止重折。
失败（status≠ok）同样照常落库（LLM 需见错误自纠）。

harness 约定同 test_fold_view_refresh.py：_is_subagent=True 避免全局停止标志；
mock client / 禁真实 LLM。
"""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.generic.agent_loop import (  # noqa: E402
    StepOutcome,
    agent_runner_loop,
    transform_history,
)


# ---------------------------------------------------------------------------
# harness（同 test_fold_view_refresh.py / test_agent_loop_tool_results.py 约定）
# ---------------------------------------------------------------------------

class _FakeHandler:
    """驱动 agent_runner_loop 的最小 handler。"""
    _is_subagent = True

    def __init__(self, dispatches):
        self.dispatches = dispatches  # {tool_name: StepOutcome | callable(args) -> StepOutcome}
        self._done_hooks = []
        self.max_turns = None
        self.current_turn = 0
        self._subagent_unique_name = ""

    def next_prompt_patcher(self, np, outcome, turn):
        return np

    def dispatch(self, tool_name, args, response, index=0):
        def gen():
            yield  # 生成器（agent_loop 经 yield from 消费）
            d = self.dispatches[tool_name]
            return d(args) if callable(d) else d
        return gen()


def _resp(content="", tool_calls=()):
    r = mock.Mock()
    r.content = content
    r.stream_error = False
    r.context_overflow = False
    r.tool_calls = list(tool_calls)
    r.usage = None
    r.finish_reason = "stop"
    return r


def _tc(name, tc_id, args="{}"):
    return mock.Mock(id=tc_id, function=SimpleNamespace(name=name, arguments=args))


class _FakeClient:
    """按序返回响应；记录每次 LLM 请求的 messages deepcopy（LLM 视图断言用）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.last_tools = ""
        self.requests = []

    def chat(self, messages=None, tools=None):
        self.requests.append(copy.deepcopy(messages))
        r = self.responses.pop(0)

        def gen():
            yield r
            return r
        return gen()


def _run_loop(client, handler, **kw):
    """驱动 agent_runner_loop 至完成；返回 (persisted, events, result)。"""
    gen = agent_runner_loop(
        client=client, system_prompt="SYS", user_input="Q", handler=handler,
        tools_schema=[], verbose=False, max_turns=10, enable_supplement=False, **kw)
    persisted, events = [], []
    result = None
    try:
        while True:
            ev = next(gen)
            events.append(ev)
            if ev.type == "persist":
                persisted.append(json.loads(ev.content))
    except StopIteration as e:
        result = e.value
    return persisted, events, result


def _fold_ok():
    return StepOutcome(
        data={"status": "ok", "folded": [3], "freed_pct": 8.0,
              "errors": [], "notes": [], "message": "已折叠 1 条输出（#3）"},
        next_prompt="继续", should_exit=False)


def _client_fold_then_read():
    """turn-1 并行调 fold + read_file；turn-2 纯文本退出。"""
    return _FakeClient([
        _resp("", [
            _tc("fold_tool_output", "call_fold", '{"output_ids": [3]}'),
            _tc("read_file", "call_read", '{"path": "/tmp/x"}'),
        ]),
        _resp("完成"),
    ])


def _client_fold_only():
    """turn-1 只调 fold；turn-2 纯文本退出。"""
    return _FakeClient([
        _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
        _resp("完成"),
    ])


# ---------------------------------------------------------------------------
# 1. persist 行为：成功照常落库 / 失败照常落库
# ---------------------------------------------------------------------------

class TestFoldPersistBehavior:
    def test_fold_success_tool_result_persisted(self):
        """回滚"不落库"（2026-09-02）：fold 成功结果照常 persist——LLM rebuild 后
        需见"我折过了"的记录，否则循环折叠同一批编号（raw_http 实证）。"""
        handler = _FakeHandler({
            "fold_tool_output": _fold_ok(),
            "read_file": StepOutcome(data="read ok", next_prompt="继续", should_exit=False),
        })
        client = _client_fold_then_read()
        persisted, events, result = _run_loop(client, handler)

        assert result["result"] == "CURRENT_TASK_DONE"
        tool_persists = [m for m in persisted if m.get("role") == "tool"]
        # fold 成功结果照常落库；同轮其他工具同样落库
        fold_persist = [m for m in tool_persists if m.get("tool_call_id") == "call_fold"]
        assert len(fold_persist) == 1
        assert "已折叠 1 条输出（#3）" in fold_persist[0]["content"]
        assert any(m.get("tool_call_id") == "call_read" for m in tool_persists)
        # assistant(tool_calls) 行落库（含 fold tool_call，与配对 tool 结果成对入库）
        asst = [m for m in persisted if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(asst) == 1
        assert {tc["id"] for tc in asst[0]["tool_calls"]} == {"call_fold", "call_read"}

    def test_fold_failure_still_persisted(self):
        handler = _FakeHandler({
            "fold_tool_output": StepOutcome(
                data={"status": "error", "error": "输出#9 不存在"},
                next_prompt="继续", should_exit=False),
        })
        client = _client_fold_only()
        persisted, events, result = _run_loop(client, handler)

        assert result["result"] == "CURRENT_TASK_DONE"
        tool_persists = [m for m in persisted if m.get("role") == "tool"]
        # 失败照常落库（LLM 需见错误自纠）
        fold_persist = [m for m in tool_persists if m.get("tool_call_id") == "call_fold"]
        assert len(fold_persist) == 1
        assert "输出#9 不存在" in fold_persist[0]["content"]

    def test_fold_non_dict_result_still_persisted(self):
        handler = _FakeHandler({
            "fold_tool_output": StepOutcome(data="plain string", next_prompt="继续", should_exit=False),
        })
        client = _client_fold_only()
        persisted, events, result = _run_loop(client, handler)

        tool_persists = [m for m in persisted if m.get("role") == "tool"]
        assert any(m.get("tool_call_id") == "call_fold" for m in tool_persists)


# ---------------------------------------------------------------------------
# 2. fold 确认既进 LLM 视图也落库（内存 append 与 persist 事件内容一致）
# ---------------------------------------------------------------------------

class TestFoldResultInViewAndPersist:
    def test_fold_confirmation_in_llm_view_and_persisted(self):
        handler = _FakeHandler({
            "fold_tool_output": _fold_ok(),
            "read_file": StepOutcome(data="read ok", next_prompt="继续", should_exit=False),
        })
        client = _client_fold_then_read()
        persisted, events, result = _run_loop(client, handler)

        # LLM 当轮可见 fold 确认（tool 消息在场）
        req2 = client.requests[1]
        fold_msg = [m for m in req2 if m.get("tool_call_id") == "call_fold"]
        assert len(fold_msg) == 1
        assert "已折叠 1 条输出（#3）" in fold_msg[0]["content"]

        # 同一确认落库（persist 事件在场，内容含折叠文案）
        fold_persist = [m for m in persisted if m.get("tool_call_id") == "call_fold"]
        assert len(fold_persist) == 1
        assert "已折叠 1 条输出（#3）" in fold_persist[0]["content"]

        # tool_results 内部键不外泄——所有 LLM 请求与最终 messages 均无该键
        for req in client.requests:
            for m in req:
                assert "_skip_persist" not in m
        for m in result["messages"]:
            assert "_skip_persist" not in m


# ---------------------------------------------------------------------------
# 3. 组装配对：落库行（assistant 含 fold tool_call + 配对 tool 结果）过
#    transform_history → fold tool_call 与结果成对保留（LLM 下轮见折叠记录不重折）
# ---------------------------------------------------------------------------

class TestAssemblyKeepsFoldResult:
    def test_transform_history_keeps_fold_pair(self):
        handler = _FakeHandler({
            "fold_tool_output": _fold_ok(),
            "read_file": StepOutcome(data="read ok", next_prompt="继续", should_exit=False),
        })
        client = _client_fold_then_read()
        persisted, events, result = _run_loop(client, handler)

        # 模拟下轮入口组装的 history：user + 落库行（fold tool 结果在场）
        history = [{"role": "user", "content": "Q"}] + persisted
        out = transform_history(history)

        asst_entries = [e for e in out if e["role"] == "assistant" and e.get("tool_calls")]
        assert len(asst_entries) == 1
        # fold tool_call 有配对 tool 结果 → 保留（read_file 同样保留）
        assert [tc["id"] for tc in asst_entries[0]["tool_calls"]] == ["call_fold", "call_read"]
        # fold 的 tool 结果成对在场（非孤儿，不被跳过）
        fold_tool = [e for e in out if e.get("tool_call_id") == "call_fold"]
        assert len(fold_tool) == 1
        assert "已折叠 1 条输出（#3）" in fold_tool[0]["content"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
