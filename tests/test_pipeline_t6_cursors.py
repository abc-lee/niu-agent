"""T6 测试：压缩前置游标追平校验（_cursors_caught_up 三处 + force 降级同源）。

方案 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.3 + §5 T6 + §6 T6。
覆盖：
1. _cursors_caught_up 单元测试（compat 版）：tool 消息穿插尾部（≠ 朴素尾切）/
   protect=0（游标=真实尾部才追平）/ protect_start==0 全保护放行 / 游标在保护区前 → False /
   空游标 → False / 游标失效（ValueError）→ False / 空 messages → True
2. sleep 调用点（CP3 同处，先状态机后游标）：entity 失败游标未追平 → skipped（中文 reason）+ cm 不执行；
   追平 → 压缩执行
3. compat force 调用点：entity 真实推进后校验通过（protect_start-1 判追平）→ cm 执行；
   未追平 → skipped；降级同源（effective_protect=5，entity 排除/校验/压缩三处全用 5）
4. runner _execute_force_pipeline 调用点：未追平 → skipped；追平 → 压缩继续（cm 被调）

全 mock：call_subagent_with_auto_answer / 游标文件（内存 _CursorStore 模拟真实文件往返）/
runner / TokenCalculator——禁真实 LLM、禁图谱写入、messages.db 零新增。
"""
import asyncio
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import niu_api.compat as compat

NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete / 非 failure 的正常返回
OVERFLOW_JSON = json.dumps({
    "overflow": True, "agent": "a", "turns_completed": 1,
    "tokens_used": 1, "tokens_limit": 2, "partial_result": "",
})
SKIP_REASON = "还有消息未提炼完，本次不压缩"


class _FakeCalc:
    def count_message_single(self, role, content, tool_calls=None):
        return 100


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        # dream 阶段收尾补链（真函数依赖 LightRAG，测试桩空操作）
        return None


class _Msg:
    def __init__(self, mid, role="user", content="hello", tool_call_id=""):
        self.id = mid
        self.role = role
        self.content = content
        self.tool_calls = None
        self.tool_call_id = tool_call_id


def _messages(*specs):
    """specs: list of (mid, role) tuples → [_Msg...]"""
    return [_Msg(mid, role) for mid, role in specs]


class _CursorStore:
    """内存游标文件：_write_cursor_with_lock 写入 → _read_cursor_value/Path 读取（模拟真实文件往返）。

    测试 hermetic：不触碰 ~/.niu 真实游标文件。
    """

    def __init__(self, entity="", dream="", compress="", journal=""):
        self.files: dict[str, dict] = {}
        if entity:
            self.files[str(Path.home() / ".niu" / "last_entity_extract.json")] = {"last_entity_extract_id": entity}
        if dream:
            self.files[str(Path.home() / ".niu" / "last_dream_evolve.json")] = {"last_dream_evolve_id": dream}
        if compress:
            self.files[str(Path.home() / ".niu" / "last_compress.json")] = {"last_compress_id": compress}
        if journal:
            self.files[str(Path.home() / ".niu" / "last_journal.json")] = {"last_journal_id": journal}

    def write(self, path, data):
        self.files[str(path)] = dict(data)

    def read(self, path, key):
        data = self.files.get(str(path))
        if not data:
            return ""
        return data.get(key, "")

    def exists(self, path):
        return str(path) in self.files

    def read_text(self, path, encoding="utf-8"):
        return json.dumps(self.files[str(path)])


def _patch_cursor_store(store, call_mock=None, protect_recent=0):
    """T6 通用 fixture：内存游标 store 读写 + 全 mock 依赖（模式同 T5 _cp_patches）。"""

    def _exists(path_obj):
        return str(path_obj) in store.files

    def _read_text(path_obj, encoding="utf-8"):
        return json.dumps(store.files[str(path_obj)])

    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=protect_recent),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        # 游标文件读取（Path.exists/read_text）与写入（_write_cursor_with_lock）全部走内存 store。
        # exists/read_text 用普通函数（类属性替换后仍走描述符绑定，Path 实例作首参）
        mock.patch("pathlib.Path.exists", _exists),
        mock.patch("pathlib.Path.read_text", _read_text),
        mock.patch("niu_api.compat._write_cursor_with_lock", side_effect=store.write),
        mock.patch("niu_api.compat._read_cursor_value", side_effect=store.read),
    ]


def _run_sleep_tidy(store, call_mock):
    """直接调 _tidy_context_impl sleep 分支（绕过 worker/CP0）。

    v2：种子记录写入隔离 F1（conftest tmp）使 entity 步真实执行；F2 patch 到
    测试专用 tmp（relay 剪切目标只允许落测试文件）。返回附 (f1_path, f2_path)。
    """
    import tempfile

    import agent.md_mirror as mdm
    from niu_api.compat import _tidy_context_impl

    msgs = _messages(("m1", "user"), ("m2", "user"))
    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    f2_path = os.path.join(tempfile.mkdtemp(prefix="t6_relay_"), "f2.md")
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock):
            stack.enter_context(p)
        stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
        block_m1 = mdm.format_message_record(
            msg_id="m1", created_at="t", role="user", content="种子一",
        )
        block_m2 = mdm.format_message_record(
            msg_id="m2", created_at="t", role="user", content="种子二",
        )
        assert mdm.append_record(block_m1, mdm.F1_PATH)
        assert mdm.append_record(block_m2, mdm.F1_PATH)
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, call_mock, mdm.F1_PATH, f2_path


def _agent_keyed_call(agent_results):
    """call_subagent_with_auto_answer mock：按 agent_name 返回对应结果。"""
    call_mock = mock.MagicMock()

    def side_effect(agent_name=None, task=None, **kwargs):
        return agent_results.get(agent_name, NORMAL_JSON)

    call_mock.side_effect = side_effect
    return call_mock


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


# ---------------------------------------------------------------------------
# 1. _cursors_caught_up 单元测试（compat 版）
# ---------------------------------------------------------------------------

class TestCursorsCaughtUp:
    """v2 门控分治：睡眠版 _cursors_caught_up = F1 空性 + dream 追平（委托 dream_only）；
    _cursors_caught_up_dream_only（force 入口共用）无 entity 腿、无 F1 判定。

    entity UUID 游标已退役——本类不再断言任何 last_entity_extract_id 行为。
    """

    def _caught_up(self, messages, protect, dream="", variant="dream_only"):
        """variant='dream_only' 直测 dream_only；'sleep' 测睡眠版（F1 由 conftest 隔离，缺省为空）。"""
        def _read(path, key):
            return {"last_dream_evolve_id": dream}.get(key, "")

        fn = compat._cursors_caught_up_dream_only if variant == "dream_only" else compat._cursors_caught_up
        with mock.patch("niu_api.compat._read_cursor_value", side_effect=_read):
            return fn(messages, protect)

    def test_empty_messages_true(self):
        """空库无可压缩内容 → True（保护/游标/F1 不判）。"""
        assert compat._cursors_caught_up([], 10) is True
        assert compat._cursors_caught_up_dream_only([], 10) is True

    def test_sleep_gate_f1_leg_behavioral(self, tmp_path, monkeypatch):
        """睡眠版 entity 腿=F1 空性（Task4-D 行为测试）：F1 非空→未追平；空→追平。

        前提：dream 游标钉在尾部（protect=0）；隔离 F1 指向 tmp_path（monkeypatch）。
        """
        import agent.md_mirror as mdm

        f1 = tmp_path / "f1.md"
        monkeypatch.setattr(mdm, "F1_PATH", str(f1))
        msgs = _messages(("m1", "user"), ("m2", "user"))
        f1.write_text('{"msg_id": "x"}\nbody\n\n', encoding="utf-8")
        assert self._caught_up(msgs, 0, dream="m2", variant="sleep") is False, "F1 非空=还有未提炼消息"
        f1.unlink()
        assert self._caught_up(msgs, 0, dream="m2", variant="sleep") is True, "F1 空=全部已提炼"

    def test_tool_msg_at_tail_not_naive_tail_cut(self):
        """tool 消息穿插尾部：_find_protected_range ≠ 朴素尾切（保护边界上移到 user 组起始）。

        messages = [u1, a1, u2, a2, tool]，protect=2：
        - 朴素尾切保护 [a2, tool]（idx 3-4），_find_protected_range 保护 [u2, a2, tool]（idx 2-4）
        - dream 游标钉在 protect_start-1（a1, idx 1）→ 追平 ✓；游标在 u1（idx 0）→ 不追平 ✗
        """
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "user"),
                         ("m4", "assistant"), ("m5", "tool"))
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 2, f"期望保护边界 idx=2，实际 {protect_start}"
        assert self._caught_up(msgs, 2, dream="m4") is True
        assert self._caught_up(msgs, 2, dream="m1") is False

    def test_protect_zero_requires_cursor_at_true_tail(self):
        """protect=0：全部可压——游标须到真实尾部（含 tool 消息）才追平。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "tool"))
        assert self._caught_up(msgs, 0, dream="m3") is True
        assert self._caught_up(msgs, 0, dream="m2") is False

    def test_protect_start_zero_all_protected_passes(self):
        """protect_start==0（全保护=压缩不删任何消息）→ 提炼未做也安全，校验放行。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"))
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 0  # 少于 N 对 → 全保护
        assert self._caught_up(msgs, 2, dream="m2") is True
        # 游标任意位置都放行（idx >= -1 恒真）
        assert self._caught_up(msgs, 2, dream="m1") is True

    def test_cursor_before_protected_range_false(self):
        """游标在最后一条未保护消息之前 → 有未处理 → 不压。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "user"), ("m4", "assistant"))
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 2
        assert self._caught_up(msgs, 2, dream="m1") is False
        assert self._caught_up(msgs, 2, dream="m4") is True

    def test_empty_cursor_false(self):
        """空游标（文件缺失/从未处理）→ 保守不压。"""
        msgs = _messages(("m1", "user"), ("m2", "user"))
        assert self._caught_up(msgs, 0, dream="") is False

    def test_stale_cursor_value_error_false(self):
        """游标指向已删消息（ValueError）→ 保守不压。"""
        msgs = _messages(("m1", "user"), ("m2", "user"))
        assert self._caught_up(msgs, 0, dream="deleted-id") is False
        assert self._caught_up(msgs, 1, dream="deleted-id") is False


# ---------------------------------------------------------------------------
# 2. sleep 调用点：CP3 同处（先状态机后游标）
# ---------------------------------------------------------------------------

def test_sleep_skipped_when_f1_not_caught_up():
    """sleep：F1 有未提炼内容（entity 收 overflow 未剪切）→ 门控 F1 腿不通过 → skipped；cm 不执行。"""
    store = _CursorStore()
    call_mock = _agent_keyed_call({"entity-extractor": OVERFLOW_JSON})
    result, call_mock, f1, _f2 = _run_sleep_tidy(store, call_mock)

    assert result == {"status": "skipped", "reason": SKIP_REASON}, f"实际: {result}"
    assert "context-manager" not in _called_agents(call_mock), "未追平不应调用 cm"
    # v2 契约：entity 失败 → F1 不剪切（数据保留，下次重跑）
    with open(f1, encoding="utf-8") as f:
        f1_content = f.read()
    assert '"msg_id": "m1"' in f1_content and '"msg_id": "m2"' in f1_content, "失败时 F1 不得被剪切"


def test_sleep_caught_up_proceeds_to_compress():
    """sleep：entity relay 剪切 F1 至空 + 梦境循环删空 F2 并推进游标到尾部 → 两腿齐 → 压缩执行。

    v3：F2 种子改用 store 真实消息 id（m1+m2）——drop 返回的末删 msg_id 经 fresh_ids
    校验后才写游标；dream mock 报 processed_line=6（两条记录共 6 行，全删）。
    """
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "entity-extractor": "处理完成 @end\nprocessed_line=999999",
        "dream-evolver": "处理完成 @end\nprocessed_line=6",
    })
    result, call_mock, f1, f2 = _run_sleep_tidy(store, call_mock)

    assert result.get("status") == "ok", f"追平后应正常压缩: {result}"
    agents = _called_agents(call_mock)
    assert agents == ["entity-extractor", "dream-evolver", "context-manager"], f"实际: {agents}"
    # 游标真实写回（内存 store）→ 校验读到 m2；entity UUID 游标退役零写
    assert store.read(Path.home() / ".niu" / "last_dream_evolve.json", "last_dream_evolve_id") == "m2"
    assert store.read(Path.home() / ".niu" / "last_compress.json", "last_compress_id") == "m2"
    assert store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id") == ""
    # v2：成功提炼后 F1 被剪切清空，剪下前缀落入 F2；v3：梦境循环删空 F2 前缀
    with open(f1, encoding="utf-8") as f:
        assert f.read() == "", "成功提炼后 F1 应为空"
    with open(f2, encoding="utf-8") as f:
        assert f.read() == "", "梦境循环 covered_all 后 F2 应已删空"


# ---------------------------------------------------------------------------
# 3. compat force 调用点：_compress_force 压缩段入口 + 降级同源
# ---------------------------------------------------------------------------

FORCE_MESSAGES = [("m1", "user"), ("m2", "assistant"), ("m3", "user"), ("m4", "assistant")]


def _run_force_tidy(store, call_mock, request_extra=None, protect_recent=0):
    """直接调 _tidy_context_impl force 分支（不 skip_compress，走到 _compress_force）。

    v2：种子记录写入隔离 F1——D3 后模式三无 entity 腿，F1 非空也不拦、不消费
    （用例据此断言 F1 原样）；F2 patch 到测试专用 tmp 以防意外 relay。
    """
    import tempfile

    import agent.md_mirror as mdm
    from niu_api.compat import _tidy_context_impl

    msgs = _messages(*FORCE_MESSAGES)
    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    f2_path = os.path.join(tempfile.mkdtemp(prefix="t6_relay_f_"), "f2.md")
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock, protect_recent=protect_recent):
            stack.enter_context(p)
        stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
        block = mdm.format_message_record(
            msg_id="t6-force-seed", created_at="t", role="user", content="种子",
        )
        assert mdm.append_record(block, mdm.F1_PATH)
        req = {"mode": "force", "session_id": "t"}
        if request_extra:
            req.update(request_extra)
        result = asyncio.run(_tidy_context_impl(req, chat_lock_already_held=True))
    return result, call_mock, mdm.F1_PATH


def test_force_proceeds_without_dream_leg_and_gating():
    """force（v3 spec §5）：梦境腿与门控一并摘除——dream 游标空/积压也照常压缩，只跑压缩对。

    journal 收 overflow（游标不动）、dream-evolver 不得被调；门控放行 → cm 被调
    （cm 返 SUBAGENT_ERROR 证明被调且 LLM 错误自身跳过）。
    """
    store = _CursorStore()
    call_mock = _agent_keyed_call({"journal-agent": OVERFLOW_JSON, "context-manager": "SUBAGENT_ERROR:mock"})
    result, call_mock, f1 = _run_force_tidy(store, call_mock)

    called = _called_agents(call_mock)
    assert "context-manager" in called, "无门控应直接进入压缩段"
    assert "dream-evolver" not in called and "entity-extractor" not in called, "模式三只跑压缩对"
    assert result.get("status") == "skipped"
    assert "LLM error" in result.get("reason", ""), f"实际: {result}"
    with open(f1, encoding="utf-8") as f:
        assert '"msg_id": "t6-force-seed"' in f.read(), "模式三不得剪切/改动 F1"


def test_force_dream_advance_passes_check_no_entity_leg():
    """端到端（D3）：模式三无 entity 腿——F1 非空也不拦、不消费；dream/journal 推进后 cm 执行。

    protect=1：保护区 [m3, m4]；dream 推进到 m4（尾部）→ dream_only 校验接受。
    cm 返回 SUBAGENT_ERROR（证明 cm 被调=校验通过；LLM 错误自身跳过，不触 DB 写删）。
    """
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "dream-evolver": "处理完成 @end processed_up_to=4",
        "journal-agent": "处理完成 @end processed_up_to=4",
        "context-manager": "SUBAGENT_ERROR:mock",
    })
    result, call_mock, f1 = _run_force_tidy(store, call_mock, protect_recent=1)

    called = _called_agents(call_mock)
    assert "context-manager" in called, "校验通过后 cm 应被调用"
    assert "entity-extractor" not in called, "模式三已摘除 entity 段"
    assert result.get("status") == "skipped"
    assert "LLM error" in result.get("reason", ""), f"实际: {result}"
    # F1 非空也放行且原样（模式三零消费）
    with open(f1, encoding="utf-8") as f:
        assert '"msg_id": "t6-force-seed"' in f.read(), "模式三不得剪切/改动 F1"


def test_force_degraded_protect_same_source():
    """降级同源（R4-B P1）：config protect=10 + force_protect_recent=5 → effective=5，
    校验/压缩两处 _find_protected_range 全用 5（不得出现 10）；v2 无 entity 排除处。"""

    msgs = _messages(*[(f"m{i}", "user" if i % 2 else "assistant") for i in range(1, 13)])
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "dream-evolver": "处理完成 @end processed_up_to=12",
        "journal-agent": "处理完成 @end processed_up_to=12",
        "context-manager": "SUBAGENT_ERROR:mock",
    })
    recorded = []
    real_find = compat._find_protected_range

    def spy(msgs_, n):
        recorded.append(n)
        return real_find(msgs_, n)

    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock):
            stack.enter_context(p)
        stack.enter_context(mock.patch("niu_api.compat._read_protect_recent_count", return_value=10))
        stack.enter_context(mock.patch("niu_api.compat._find_protected_range", side_effect=spy))
        result = asyncio.run(compat._tidy_context_impl(
            {"mode": "force", "session_id": "t", "force_protect_recent": 5},
            chat_lock_already_held=True,
        ))

    called = _called_agents(call_mock)
    assert "context-manager" in called, "校验应通过（dream 钉在尾部）"
    assert "entity-extractor" not in called, "模式三已摘除 entity 段"
    assert recorded, f"应发生 _find_protected_range 调用: {recorded}"
    assert all(v == 5 for v in recorded), f"校验/压缩各处应全用 effective=5，实际 {recorded}"
    assert result.get("status") == "skipped" and "LLM error" in result.get("reason", "")


# ---------------------------------------------------------------------------
# 4. runner _execute_force_pipeline 调用点
# ---------------------------------------------------------------------------

def _build_niu_runner_for_test():
    """构造 NiuRunner 实例（绕过 __init__，同 test_compress_quality 模式）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
    runner.handler = type("H", (), {"_last_prompt_tokens": 0})()
    return runner


def _run_runner_force(monkeypatch, agent_results, store=None):
    import niu_api.llm_proxy as llm_proxy_module
    from agent import runner as runner_module
    from agent import subagent as subagent_module

    store = store or _CursorStore()
    messages = _messages(("m1", "user"), ("m2", "user"))

    call_mock = mock.MagicMock()
    call_mock.side_effect = lambda agent_name=None, task=None, **kw: agent_results.get(agent_name, NORMAL_JSON)

    monkeypatch.setattr(runner_module, "is_stop_requested", lambda: False)
    monkeypatch.setattr(runner_module.NiuRunner, "_sync_get_messages", lambda self, limit=None: messages)
    monkeypatch.setattr(runner_module.NiuRunner, "_read_cursor", staticmethod(store.read))
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda path, data: store.write(path, data))
    monkeypatch.setattr("agent.token_calculator.TokenCalculator.get", lambda: _FakeCalc())
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    monkeypatch.setattr(subagent_module, "_read_context_window_tokens", lambda: 8000)
    monkeypatch.setattr(subagent_module, "_read_protect_recent_count", lambda: 0)
    monkeypatch.setattr(subagent_module, "_read_compress_target_tokens", lambda: 60000)
    monkeypatch.setattr(subagent_module, "_read_max_output_tokens", lambda: 32000)
    monkeypatch.setattr(llm_proxy_module, "get_llm_config", lambda use_lightrag_config=False: {
        "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
        "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
    })

    runner = _build_niu_runner_for_test()
    result = runner._execute_force_pipeline()
    return result, call_mock, store


def test_runner_force_proceeds_without_dream_step_and_gating(monkeypatch):
    """runner-force（v3）：dream 步与门控摘除——journal overflow 后仍直接进入压缩段。

    判别力：cm 返 SUBAGENT_ERROR 证明被调；dream-evolver 零调用、dream 游标零写
    （force 不再推进游标，压缩保护边界用入口读取值）。
    """
    result, call_mock, store = _run_runner_force(
        monkeypatch,
        {
            "journal-agent": OVERFLOW_JSON,
            "context-manager": "SUBAGENT_ERROR:mock",
        },
    )
    called = _called_agents(call_mock)
    assert "context-manager" in called, "无门控应直接进入压缩段（cm 被调）"
    assert "dream-evolver" not in called and "entity-extractor" not in called, "模式三只跑压缩对"
    assert result.get("status") == "skipped" and "LLM error" in result.get("reason", ""), f"实际: {result}"
    dream_id = store.read(Path.home() / ".niu" / "last_dream_evolve.json", "last_dream_evolve_id")
    assert dream_id == "", f"runner force 不再推进 dream 游标，实际 {dream_id}"
