"""折叠视图刷新修复测试（docs/superpowers/plans/2026-09-02-fold-view-refresh-fix.md v1.5）。

Task 1：assemble_view_sync——无压实的纯视图组装（水位线切分 + fold 统计 + 折叠渲染 + usage 估算）。
Task 2：transform_history 抽取 + agent_loop 折叠 hook + runner._on_fold_applied。
        四层测试（R3-B P2）：transform_history 行为零变化锁 / hook 契约层（stub 回调）/
        生产接线断言（runner.chat 传真方法引用）/ _on_fold_applied DB 重建语义层 + 组合级回归锁。

入口行为零变化回归锁在 test_get_context_for_chat_v2.py + test_compaction.py +
test_calibration.py（Step 4 合跑）。mock store / 校准倍率 / token 计数，
禁真实 LLM；tmp DB 禁碰 ~/.niu。
"""

import copy
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.context_assembler.calibration as calibration
import agent.session as session_mod
import agent.context_manager as cm_mod
from agent.context_assembler.blocks import load_all
from agent.context_assembler.compaction import AUTO_GATE
from agent.context_manager import ContextManager
from agent.generic.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    StepOutcome,
    agent_runner_loop,
    transform_history,
)
from agent.runner import NiuRunner
from agent.session import Message

sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "session-manager" / "src"))
from niu_session_manager import fold_tool_output  # noqa: E402


# ---------------------------------------------------------------------------
# 测试基建：FakeStore + 确定性 token 计数 + 校准/闸门隔离（同 v2 测试约定）
# ---------------------------------------------------------------------------

class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages: list[Message]):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。"""
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _msg(idx, role, content, tool_calls=None, tool_call_id="",
         folded=0, output_pct=None):
    return Message(
        id=f"m{idx:03d}",
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_call_id=tool_call_id,
        folded=folded,
        output_pct=output_pct,
        created_at="2026-09-02T10:00:00",
        rowid=idx + 1,
    )


def _conversation():
    """一段对话：Q1 → assistant(read_file) → tool(未折叠, pct=5.0)
    → assistant(grep) → tool(已折叠, pct=7.5) → Q2（当前输入）。"""
    return [
        _msg(0, "user", "Q1"),
        _msg(1, "assistant", "", tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}]),
        _msg(2, "tool", "RAW_BODY_1", tool_call_id="c1", output_pct=5.0),
        _msg(3, "assistant", "", tool_calls=[
            {"id": "c2", "type": "function",
             "function": {"name": "grep", "arguments": "{}"}}]),
        _msg(4, "tool", "RAW_BODY_2", tool_call_id="c2", folded=1, output_pct=7.5),
        _msg(5, "user", "Q2"),
    ]


@pytest.fixture
def fold_env(monkeypatch, tmp_path):
    """校准倍率 1.5 + fold 列标志 True + 确定性计数 + AUTO_GATE 复位。"""
    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.5
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))
    AUTO_GATE.release()
    yield tmp_path
    calibration._cached_ratio = old_ratio
    AUTO_GATE.release()


def _make_cm(store, max_tokens, blocks_db):
    return ContextManager(store, max_tokens=max_tokens, blocks_db_path=blocks_db)


# ---------------------------------------------------------------------------
# 1. 折叠占位符/头行渲染 + _fold_stats（usage=校准值）
# ---------------------------------------------------------------------------

class TestAssembleViewSyncRendering:
    def test_folded_placeholder_and_header_line(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=True)

        # 无块 → 无索引前导；history=前 5 条（Q2 被排除）
        assert [e["role"] for e in view] == ["user", "assistant", "tool", "assistant", "tool"]
        # 未折叠 tool：头行 + 原文（编号=rowid，pct 落库固化值）
        assert view[2]["content"] == "[输出#3 · read_file · 占上下文 5.0%]\nRAW_BODY_1"
        # 折叠 tool：占位符含 pct 快照，以「获取]」收尾（agent_loop 识别契约）
        assert view[4]["content"] == (
            "[输出#5 已折叠：grep({})，产生时占上下文 7.5%。如需原文请重新调用该工具获取]"
        )

    def test_fold_stats_and_calibrated_usage(self, fold_env):
        msgs = _conversation()
        cm = _make_cm(FakeStore(msgs), 10_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(msgs, exclude_last=True)

        # n=窗口未折叠 tool 数（仅 RAW_BODY_1）；m/p=有快照者条数与合计
        assert cm._fold_stats["n"] == 1
        assert cm._fold_stats["m"] == 1
        assert cm._fold_stats["p"] == 5.0
        # usage=校准值：raw × 倍率 1.5 ÷ max_tokens（非 raw）
        base = _fake_count_tokens(view)
        assert cm._fold_stats["usage"] == pytest.approx(base * 1.5 / 10_000)


# ---------------------------------------------------------------------------
# 2. exclude_last 双向语义
# ---------------------------------------------------------------------------

class TestExcludeLast:
    def test_exclude_last_true_drops_current_input(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=True)
        assert all(e.get("content") != "Q2" for e in view)

    def test_exclude_last_false_keeps_current_input(self, fold_env):
        cm = _make_cm(FakeStore(_conversation()), 1_000_000, fold_env / "blocks.db")
        view = cm.assemble_view_sync(_conversation(), exclude_last=False)
        assert view[-1] == {"role": "user", "content": "Q2"}

    def test_empty_messages(self, fold_env):
        cm = _make_cm(FakeStore([]), 1_000_000, fold_env / "blocks.db")
        assert cm.assemble_view_sync([], exclude_last=True) == []


# ---------------------------------------------------------------------------
# 3. 不触发压实（深审 P1：rebuild 不得触发归档）
# ---------------------------------------------------------------------------

class TestNoCompactionInAssemble:
    def test_high_usage_does_not_compact(self, fold_env):
        msgs = _conversation()
        blocks_db = fold_env / "blocks.db"
        # max_tokens=10 → usage 远超任何触发线，assemble_view_sync 仍不得压实
        cm = _make_cm(FakeStore(msgs), 10, blocks_db)
        view = cm.assemble_view_sync(msgs, exclude_last=True)

        # 原始视图原样返回：未折叠 tool 全文在场（非压实产物）
        assert any("RAW_BODY_1" in e.get("content", "") for e in view)
        # 无新块归档（压实是唯一归档者，assemble_view_sync 不触发）
        assert load_all(blocks_db) == []


# ---------------------------------------------------------------------------
# Task 2 测试基建：驱动 agent_runner_loop 的最小 handler/client
# （约定同 test_agent_loop_tool_results.py：_is_subagent=True 避免全局停止标志/
#   SubagentRegistry 查询；Mock(name=...) 是显示名配置项——function 用 SimpleNamespace）
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
    """按序返回响应；记录每次 LLM 请求的 messages deepcopy（事后断言）。"""

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
    """驱动 agent_runner_loop 至完成；返回 (events, result)。"""
    gen = agent_runner_loop(
        client=client, system_prompt="SYS", user_input="Q", handler=handler,
        tools_schema=[], verbose=False, max_turns=10, enable_supplement=False, **kw)
    events = []
    result = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        result = e.value
    return events, result


def _fold_outcome(data):
    return StepOutcome(data=data, next_prompt="继续", should_exit=False)


# ---------------------------------------------------------------------------
# 4. transform_history 行为零变化锁（R4-B P3-1）：5 子场景断言原逻辑全保留
# ---------------------------------------------------------------------------

class TestTransformHistoryZeroChange:
    def _history(self):
        return [
            {"role": "subagent_msg", "content": "@user sub"},          # ① 跳过
            {"role": "user", "content": ""},                           # ② 空消息丢弃
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "ok1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "dangling", "type": "function",
                 "function": {"name": "grep", "arguments": "{}"}}]},   # ④ 悬空剥离（留 ok1）
            {"role": "tool", "content": "ORPHAN_BODY", "tool_call_id": "orphan"},  # ③ 孤儿跳过
            {"role": "tool", "content": "OK_BODY", "tool_call_id": "ok1"},         # 配对 tool 保留
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "dangling2", "type": "function",
                 "function": {"name": "x", "arguments": "{}"}}]},      # 全悬空 → 纯文本（无 content）
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "ok1b", "type": "function",
                 "function": {"name": "big_tool", "arguments": "{}"}}]},
            {"role": "tool", "content": "LONG_" + "z" * 40000, "tool_call_id": "ok1b"},  # ⑤ 截断
        ]

    def test_five_branches(self):
        out = transform_history(self._history())
        # ① subagent_msg 跳过
        assert all(e["role"] != "subagent_msg" for e in out)
        # ② 空消息丢弃：无空 content 的 user 条目
        assert not any(e["role"] == "user" and e.get("content") == "" for e in out)
        # ③ 孤儿 tool 跳过
        assert all(e.get("content") != "ORPHAN_BODY" for e in out)
        # ④ 悬空剥离：read_file assistant 只留 ok1；全悬空 → 纯文本（无 tool_calls 键）
        with_tcs = [e for e in out if e["role"] == "assistant" and e.get("tool_calls")]
        assert [tc["id"] for tc in with_tcs[0]["tool_calls"]] == ["ok1"]
        assert {"role": "assistant", "content": ""} in out
        # ⑤ 截断：长度 ≤30000 带标记，name 还原
        long_e = [e for e in out if e.get("tool_call_id") == "ok1b"][0]
        assert len(long_e["content"]) <= MAX_TOOL_RESULT_CHARS
        assert "[截断]" in long_e["content"]
        assert long_e["name"] == "big_tool"
        # 配对 tool 保留 + name 还原
        ok = [e for e in out if e.get("tool_call_id") == "ok1"][0]
        assert ok["content"] == "OK_BODY" and ok["name"] == "read_file"


# ---------------------------------------------------------------------------
# 5. hook 契约层（stub 回调）：触发条件 / 挂载点 / 每轮初始化 / 子 Agent None 不崩
# ---------------------------------------------------------------------------

class TestFoldHookContract:
    def test_fold_success_triggers_once_after_persist_before_next_llm(self):
        order = []
        hook_calls = []

        def on_fold(messages):
            hook_calls.append(messages)
            order.append("hook")

        handler = _FakeHandler({"fold_tool_output": _fold_outcome(
            {"status": "ok", "folded": [3], "freed_pct": 8.0,
             "errors": [], "notes": [], "message": "已折叠 1 条输出（#3）"})})
        client = _FakeClient([
            _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
            _resp("完成"),
        ])
        orig_chat = client.chat

        def chat_tracked(messages=None, tools=None):
            order.append("llm")
            return orig_chat(messages=messages, tools=tools)
        client.chat = chat_tracked

        gen = agent_runner_loop(
            client=client, system_prompt="SYS", user_input="Q", handler=handler,
            tools_schema=[], verbose=False, max_turns=10, enable_supplement=False,
            on_fold_applied=on_fold)
        result = None
        try:
            while True:
                ev = next(gen)
                if ev.type == "persist":
                    order.append("persist")
        except StopIteration as e:
            result = e.value

        assert result["result"] == "CURRENT_TASK_DONE"
        assert len(hook_calls) == 1
        # 挂载点锁：turn-1 LLM → assistant persist → tool persist → hook → turn-2 LLM
        # （末尾多一个 persist=纯文本退出轮的 V4 persist，与 hook 无关）
        assert order[:5] == ["llm", "persist", "persist", "hook", "llm"]

    @pytest.mark.parametrize("data", [
        {"status": "ok", "folded": [], "notes": ["输出#3 已折叠（幂等）"], "errors": [], "message": "m"},
        {"status": "error", "error": "输出#9 不存在"},
        "plain string result",
    ], ids=["idempotent", "error", "non_dict"])
    def test_no_trigger_on_idempotent_error_nondict(self, data):
        hook_calls = []
        handler = _FakeHandler({"fold_tool_output": _fold_outcome(data)})
        client = _FakeClient([
            _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
            _resp("完成"),
        ])
        events, result = _run_loop(client, handler, on_fold_applied=lambda m: hook_calls.append(m))
        assert result["result"] == "CURRENT_TASK_DONE"
        assert hook_calls == []

    def test_subagent_path_none_no_crash(self):
        # 不传 on_fold_applied（子 Agent 路径默认 None）：折叠成功也不崩、正常完成
        handler = _FakeHandler({"fold_tool_output": _fold_outcome(
            {"status": "ok", "folded": [3], "message": "m"})})
        client = _FakeClient([
            _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
            _resp("完成"),
        ])
        events, result = _run_loop(client, handler)
        assert result["result"] == "CURRENT_TASK_DONE"

    def test_no_retrigger_on_nonfold_turn_after_fold(self):
        # 每轮初始化锁（R1-B P3）：折叠轮后的无折叠轮不重复触发
        hook_calls = []
        handler = _FakeHandler({
            "fold_tool_output": _fold_outcome({"status": "ok", "folded": [3], "message": "m"}),
            "read_file": StepOutcome(data="read ok", next_prompt="继续", should_exit=False),
        })
        client = _FakeClient([
            _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
            _resp("", [_tc("read_file", "call_read", '{"path": "/tmp/x"}')]),
            _resp("完成"),
        ])
        events, result = _run_loop(client, handler, on_fold_applied=lambda m: hook_calls.append(m))
        assert result["result"] == "CURRENT_TASK_DONE"
        assert len(hook_calls) == 1


# ---------------------------------------------------------------------------
# 6. 生产接线断言（R4-B P2-3）：runner.chat 传 on_fold_applied=self._on_fold_applied
# ---------------------------------------------------------------------------

class TestProductionWiring:
    def test_runner_chat_passes_on_fold_applied(self):
        from agent import runner as runner_mod

        captured = {}

        def fake_loop(**kwargs):
            captured.update(kwargs)

            def gen():
                if False:
                    yield
                return {"result": "CURRENT_TASK_DONE", "messages": []}
            return gen()

        self_mock = mock.MagicMock()
        self_mock._im_channel_id = ""
        self_mock.default_model = "test-model"
        self_mock.base_tools_schema = []
        self_mock._assemble_tools_schema.return_value = []
        self_mock.should_push_im.return_value = False

        with mock.patch.object(runner_mod, "cleanup_suspended_sync_subagents"), \
             mock.patch.object(runner_mod, "agent_runner_loop", side_effect=fake_loop), \
             mock.patch.object(runner_mod, "is_stop_requested", return_value=False), \
             mock.patch.object(runner_mod, "clear_stop"), \
             mock.patch("agent.subagent._read_context_window_tokens", return_value=0):
            list(NiuRunner.chat(self_mock, session_id="s1", user_input="hi"))

        assert captured.get("on_fold_applied") is self_mock._on_fold_applied


# ---------------------------------------------------------------------------
# 7. _on_fold_applied DB 重建语义层（R3-B P2-4）：真方法 + monkeypatched
#    _sync_get_messages / peek_context_manager——原地替换/占位符/同制式三防线
# ---------------------------------------------------------------------------

class TestOnFoldAppliedRebuild:
    def _make_runner(self, monkeypatch, db_msgs, cm):
        r = NiuRunner.__new__(NiuRunner)
        monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: list(db_msgs))
        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        return r

    def _stale_view(self):
        """内存陈旧视图（折叠前原文仍在——bug 态；与入口组装同制式）。"""
        return [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}]},
            {"role": "tool", "content": "[输出#3 · read_file · 占上下文 5.0%]\nRAW_BODY_1",
             "tool_call_id": "c1", "name": "read_file"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]},
            {"role": "tool", "content": "[输出#5 · grep · 占上下文 7.5%]\nRAW_BODY_2",
             "tool_call_id": "c2", "name": "grep"},
            {"role": "user", "content": "Q2"},
        ]

    def test_inplace_rebuild_with_placeholder(self, fold_env, monkeypatch):
        cm = _make_cm(FakeStore(_conversation()), 100_000, fold_env / "blocks.db")
        r = self._make_runner(monkeypatch, _conversation(), cm)
        messages = self._stale_view()
        sys_entry = messages[0]
        r._on_fold_applied(messages)
        # 原地替换：同列表对象，system 保留（同一性不变——含 cache_control 不重建）
        assert messages[0] is sys_entry
        assert [e["role"] for e in messages] == ["system", "user", "assistant", "tool", "assistant", "tool", "user"]
        # 折叠行 → 占位符；折叠前原文从视图消失（exclude_last=False 含 Q2）
        assert messages[5]["content"] == (
            "[输出#5 已折叠：grep({})，产生时占上下文 7.5%。如需原文请重新调用该工具获取]"
        )
        assert not any("RAW_BODY_2" in (e.get("content") or "") for e in messages)
        # 未折叠行保持头行 + 原文（与入口同制式）
        assert messages[3]["content"] == "[输出#3 · read_file · 占上下文 5.0%]\nRAW_BODY_1"

    def test_subagent_msg_rows_dropped(self, fold_env, monkeypatch):
        db_msgs = _conversation() + [_msg(9, "subagent_msg", "@user hi")]
        cm = _make_cm(FakeStore(db_msgs), 100_000, fold_env / "blocks.db")
        r = self._make_runner(monkeypatch, db_msgs, cm)
        messages = self._stale_view()
        r._on_fold_applied(messages)
        assert all(e["role"] != "subagent_msg" for e in messages)

    def test_dangling_tool_calls_stripped(self, fold_env, monkeypatch):
        # STOPPED-at-dispatch 场景：assistant tool_calls 无对应 tool 响应 → 剥离（R3-A P1）
        db_msgs = [
            _msg(0, "user", "Q"),
            _msg(1, "assistant", "", tool_calls=[
                {"id": "cx", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
        ]
        cm = _make_cm(FakeStore(db_msgs), 100_000, fold_env / "blocks.db")
        r = self._make_runner(monkeypatch, db_msgs, cm)
        messages = [{"role": "system", "content": "SYS"}] + [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "cx", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        ]
        r._on_fold_applied(messages)
        assert all("cx" not in [tc.get("id") for tc in e.get("tool_calls", [])] for e in messages)

    def test_long_tool_content_still_truncated(self, fold_env, monkeypatch):
        # R3-A P1：不经截断会把已 cap 输出去截断回全量（缓存破口扩大+膨胀）
        db_msgs = [
            _msg(0, "user", "Q"),
            _msg(1, "assistant", "", tool_calls=[
                {"id": "cb", "type": "function", "function": {"name": "big", "arguments": "{}"}}]),
            _msg(2, "tool", "L" * 40000, tool_call_id="cb"),
        ]
        cm = _make_cm(FakeStore(db_msgs), 100_000, fold_env / "blocks.db")
        r = self._make_runner(monkeypatch, db_msgs, cm)
        messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q"}]
        r._on_fold_applied(messages)
        tool_e = [e for e in messages if e.get("tool_call_id") == "cb"][0]
        assert len(tool_e["content"]) <= MAX_TOOL_RESULT_CHARS
        assert "[截断]" in tool_e["content"]


# ---------------------------------------------------------------------------
# 8. 组合级回归锁（R3-B P2-5）：harness 消费 persist 事件写真实 tmp sqlite，
#    dispatch 调真 fold_tool_output 写 folded=1，_on_fold_applied 为真方法——
#    断言折叠轮后第二轮 LLM 请求含占位符、不含折叠前原文
# ---------------------------------------------------------------------------

class TestCombinedRegressionLock:
    def _init_db(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE messages (
            id TEXT PRIMARY KEY, role TEXT NOT NULL, content TEXT,
            tool_calls TEXT, tool_results TEXT, created_at TEXT NOT NULL,
            tool_call_id TEXT DEFAULT '', degraded_reason TEXT DEFAULT '',
            folded INTEGER DEFAULT 0, output_pct REAL)""")
        conn.commit()
        return conn

    def _insert(self, conn, n, role, content, tool_calls=None, tool_call_id="", output_pct=None):
        conn.execute(
            "INSERT INTO messages (id, role, content, tool_calls, tool_results, created_at,"
            " tool_call_id, degraded_reason, folded, output_pct)"
            " VALUES (?,?,?,?,?,?,?,?,0,?)",
            (f"m{n}", role, content, json.dumps(tool_calls or []), "[]",
             "2026-09-02T10:00:00", tool_call_id, "", output_pct))
        conn.commit()

    def test_fold_then_next_turn_sees_placeholder(self, tmp_path, fold_env, monkeypatch):
        db_path = tmp_path / "messages.db"
        conn = self._init_db(db_path)
        RAW_FULL = "RAWBODY_" + "x" * 40000
        self._insert(conn, 1, "user", "Q0")
        self._insert(conn, 2, "assistant", "", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}])
        self._insert(conn, 3, "tool", RAW_FULL, tool_call_id="c1", output_pct=8.0)
        self._insert(conn, 4, "user", "Q")  # 当前用户输入（模拟入口端预 persist 行为）

        def read_db():
            c = sqlite3.connect(str(db_path))
            rows = c.execute(
                "SELECT id, role, content, tool_calls, tool_call_id, folded, output_pct,"
                " created_at, rowid FROM messages ORDER BY rowid").fetchall()
            c.close()
            return [Message(id=r[0], role=r[1], content=r[2] or "", tool_calls=json.loads(r[3] or "[]"),
                            tool_call_id=r[4] or "", folded=r[5] or 0, output_pct=r[6],
                            created_at=r[7], rowid=r[8]) for r in rows]

        cm = _make_cm(FakeStore([]), 100_000, tmp_path / "blocks.db")
        monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: read_db())
        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        r = NiuRunner.__new__(NiuRunner)

        def fold_dispatch(args):
            # 真工具写 folded=1 进 tmp DB（**kwargs 测试注入通道）
            return StepOutcome(data=fold_tool_output([3], messages_db_path=str(db_path)),
                               next_prompt="继续", should_exit=False)

        handler = _FakeHandler({"fold_tool_output": fold_dispatch})
        client = _FakeClient([
            _resp("", [_tc("fold_tool_output", "call_fold", '{"output_ids": [3]}')]),
            _resp("完成"),
        ])
        history = [
            {"role": "user", "content": "Q0"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            # 入口视图：头行 + 全文（agent_runner_loop 的 transform_history 自行截断）
            {"role": "tool", "content": "[输出#3 · read_file · 占上下文 8.0%]\n" + RAW_FULL,
             "tool_call_id": "c1"},
        ]

        gen = agent_runner_loop(
            client=client, system_prompt="SYS", user_input="Q", handler=handler,
            tools_schema=[], verbose=False, max_turns=10, enable_supplement=False,
            history=history, on_fold_applied=r._on_fold_applied)
        sink_n = [10]

        def persist_to_db(msg):
            # 模拟 runner.chat 的 persist 处理：每条落 tmp DB（yield 即落库语义）
            sink_n[0] += 1
            self._insert(conn, sink_n[0], msg.get("role", ""), msg.get("content") or "",
                         tool_calls=msg.get("tool_calls"), tool_call_id=msg.get("tool_call_id", ""))

        result = None
        try:
            while True:
                ev = next(gen)
                if ev.type == "persist":
                    persist_to_db(json.loads(ev.content))
        except StopIteration as e:
            result = e.value

        assert result["result"] == "CURRENT_TASK_DONE"
        # DB 确实被折叠（真工具写效果）
        assert conn.execute("SELECT folded FROM messages WHERE rowid=3").fetchone()[0] == 1
        # bug 态：turn-1 LLM 请求仍含折叠前原文（陈旧视图）
        assert any("RAWBODY_" in (e.get("content") or "") for e in client.requests[0])
        # 修复效果：turn-2 LLM 请求含占位符、无折叠前原文
        req2 = json.dumps(client.requests[1], ensure_ascii=False)
        assert "[输出#3 已折叠：read_file({})，产生时占上下文 8.0%。如需原文请重新调用该工具获取]" in req2
        assert "RAWBODY_" not in req2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
