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
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

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
    """直接调 _tidy_context_impl sleep 分支（绕过 worker/CP0）。"""
    from niu_api.compat import _tidy_context_impl

    msgs = _messages(("m1", "user"), ("m2", "user"))
    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock):
            stack.enter_context(p)
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, call_mock


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
    def _caught_up(self, messages, protect, entity="", dream=""):
        """直接调 _cursors_caught_up，patch 游标读取为指定值。"""
        def _read(path, key):
            return {
                "last_entity_extract_id": entity,
                "last_dream_evolve_id": dream,
            }.get(key, "")

        with mock.patch("niu_api.compat._read_cursor_value", side_effect=_read):
            return compat._cursors_caught_up(messages, protect)

    def test_empty_messages_true(self):
        """空库无可压缩内容 → True（保护/游标不判）。"""
        assert compat._cursors_caught_up([], 10) is True

    def test_tool_msg_at_tail_not_naive_tail_cut(self):
        """tool 消息穿插尾部：_find_protected_range ≠ 朴素尾切（保护边界上移到 user 组起始）。

        messages = [u1, a1, u2, a2, tool]，protect=2：
        - 朴素尾切保护 [a2, tool]（idx 3-4），_find_protected_range 保护 [u2, a2, tool]（idx 2-4）
        - 游标钉在 protect_start-1（a1, idx 1）→ 追平 ✓；游标在 a1 之前（u1, idx 0）→ 不追平 ✗
        """
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "user"),
                         ("m4", "assistant"), ("m5", "tool"))
        # 判追平语义验证：protect_start == 2（保护含 tool 尾部 + 上移到 u2），非朴素尾切 len-2=3
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 2, f"期望保护边界 idx=2，实际 {protect_start}"
        # 游标 = protect_start-1（a1）→ 追平
        assert self._caught_up(msgs, 2, entity="m2", dream="m4") is True
        # 游标在保护区前（u1）→ 不追平
        assert self._caught_up(msgs, 2, entity="m1", dream="m4") is False

    def test_protect_zero_requires_cursor_at_true_tail(self):
        """protect=0：全部可压——游标须到真实尾部（含 tool 消息）才追平。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "tool"))
        # 游标 = 最后一条（tool 消息）→ 追平
        assert self._caught_up(msgs, 0, entity="m3", dream="m3") is True
        # 游标 = 倒数第二条 → 不追平（压缩会删掉未提炼消息）
        assert self._caught_up(msgs, 0, entity="m2", dream="m3") is False
        # 注：§4.3 伪代码 protect=0 分支按首游标（entity）early-return（idx == len-1），
        # dream 落后而 entity 在尾部时不再查 dream——逐字实现，行为如伪代码所示。

    def test_protect_start_zero_all_protected_passes(self):
        """protect_start==0（全保护=压缩不删任何消息）→ 提炼未做也安全，校验放行。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"))
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 0  # 少于 N 对 → 全保护
        assert self._caught_up(msgs, 2, entity="m1", dream="m2") is True
        # 游标任意位置都放行（idx >= -1 恒真）
        assert self._caught_up(msgs, 2, entity="m2", dream="m1") is True

    def test_cursor_before_protected_range_false(self):
        """游标在最后一条未保护消息之前 → 有未处理 → 不压。"""
        msgs = _messages(("m1", "user"), ("m2", "assistant"), ("m3", "user"), ("m4", "assistant"))
        protect_start = compat._find_protected_range(msgs, 2)
        assert protect_start == 2
        # 游标在 protect_start-2（u1, idx 0）→ 不追平
        assert self._caught_up(msgs, 2, entity="m1", dream="m4") is False
        # 游标恰好钉在 protect_start-1（a1, idx 1）→ 追平（entity 排除保护区，游标语义即此）
        assert self._caught_up(msgs, 2, entity="m2", dream="m4") is True

    def test_empty_cursor_false(self):
        """空游标（文件缺失/从未处理）→ 保守不压。"""
        msgs = _messages(("m1", "user"), ("m2", "user"))
        assert self._caught_up(msgs, 0, entity="", dream="m2") is False
        # dream 空游标：entity 不早退时（首游标非尾部）同样保守不压
        assert self._caught_up(msgs, 0, entity="m1", dream="") is False

    def test_stale_cursor_value_error_false(self):
        """游标指向已删消息（ValueError）→ 保守不压。"""
        msgs = _messages(("m1", "user"), ("m2", "user"))
        assert self._caught_up(msgs, 0, entity="deleted-id", dream="m2") is False
        # dream 游标失效：entity 通过首轮（protect=1 不早退，全保护 protect_start=0 仍逐游标查）→ 查 dream 时 ValueError
        assert self._caught_up(msgs, 1, entity="m2", dream="deleted-id") is False


# ---------------------------------------------------------------------------
# 2. sleep 调用点：CP3 同处（先状态机后游标）
# ---------------------------------------------------------------------------

def test_sleep_skipped_when_cursors_not_caught_up():
    """sleep：entity 失败（overflow）游标未追平 → CP3 后校验失败 → skipped + 中文 reason；cm 不执行。"""
    store = _CursorStore(entity="m1", dream="m2")  # 上一轮成功游标：entity 钉在 m1（保护区内）
    call_mock = _agent_keyed_call({"entity-extractor": OVERFLOW_JSON})  # 本轮失败 → 游标不推进
    result, call_mock = _run_sleep_tidy(store, call_mock)

    assert result == {"status": "skipped", "reason": SKIP_REASON}, f"实际: {result}"
    assert "context-manager" not in _called_agents(call_mock), "未追平不应调用 cm"


def test_sleep_caught_up_proceeds_to_compress():
    """sleep：entity/dream 真实推进到尾部（protect=0 游标=尾部）→ 校验通过 → 压缩执行。"""
    store = _CursorStore()  # 空游标 → entity/dream 全量处理并推进到 m2
    call_mock = _agent_keyed_call({})  # 全部 NORMAL_JSON
    result, call_mock = _run_sleep_tidy(store, call_mock)

    assert result.get("status") == "ok", f"追平后应正常压缩: {result}"
    agents = _called_agents(call_mock)
    assert agents == ["entity-extractor", "dream-evolver", "context-manager"], f"实际: {agents}"
    # 游标真实写回（内存 store）→ 校验读到 m2
    assert store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id") == "m2"
    assert store.read(Path.home() / ".niu" / "last_dream_evolve.json", "last_dream_evolve_id") == "m2"
    assert store.read(Path.home() / ".niu" / "last_compress.json", "last_compress_id") == "m2"


# ---------------------------------------------------------------------------
# 3. compat force 调用点：_compress_force 压缩段入口 + 降级同源
# ---------------------------------------------------------------------------

FORCE_MESSAGES = [("m1", "user"), ("m2", "assistant"), ("m3", "user"), ("m4", "assistant")]


def _run_force_tidy(store, call_mock, request_extra=None, protect_recent=0):
    """直接调 _tidy_context_impl force 分支（不 skip_compress，走到 _compress_force）。"""
    from niu_api.compat import _tidy_context_impl

    msgs = _messages(*FORCE_MESSAGES)
    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock, protect_recent=protect_recent):
            stack.enter_context(p)
        req = {"mode": "force", "session_id": "t"}
        if request_extra:
            req.update(request_extra)
        result = asyncio.run(_tidy_context_impl(req, chat_lock_already_held=True))
    return result, call_mock


def test_force_skipped_when_cursor_not_caught_up():
    """force：entity 失败游标空 → 压缩段入口校验失败 → skipped + 中文 reason；cm 不执行。"""
    store = _CursorStore(dream="m4")  # 仅 dream 追平；entity 空
    call_mock = _agent_keyed_call({"entity-extractor": OVERFLOW_JSON})
    result, call_mock = _run_force_tidy(store, call_mock)

    assert result.get("status") == "skipped", f"实际: {result}"
    assert result.get("reason") == SKIP_REASON
    assert "context-manager" not in _called_agents(call_mock), "未追平不应调用 cm"


def test_force_entity_real_advance_passes_check():
    """端到端：force entity 真实推进后校验通过（protect_start-1 判追平）→ cm 执行。

    protect=1：保护区 [m3, m4]（idx 2-3）；entity 排除保护区后处理 [m1, m2]，
    processed_up_to=2 → 游标钉在 protect_start-1（m2, idx 1）——校验接受。
    cm 返回 SUBAGENT_ERROR（证明 cm 被调=校验通过；LLM 错误自身跳过，不触 DB 写删）。
    """
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "entity-extractor": "处理完成 @end processed_up_to=2",
        "dream-evolver": "处理完成 @end processed_up_to=4",
        "journal-agent": "处理完成 @end processed_up_to=4",
        "context-manager": "SUBAGENT_ERROR:mock",
    })
    result, call_mock = _run_force_tidy(store, call_mock, protect_recent=1)

    # 校验通过 → cm 被调用；cm LLM 错误 → 返回 skipped LLM error（不写删）
    assert "context-manager" in _called_agents(call_mock), "校验通过后 cm 应被调用"
    assert result.get("status") == "skipped"
    assert "LLM error" in result.get("reason", ""), f"实际: {result}"
    # entity 游标钉在 protect_start-1（m2）
    entity_id = store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id")
    assert entity_id == "m2", f"entity 游标应钉在 protect_start-1=m2，实际 {entity_id}"


def test_force_degraded_protect_same_source():
    """降级同源（R4-B P1）：config protect=10 + force_protect_recent=5 → effective=5，
    entity 排除/校验/压缩三处 _find_protected_range 全用 5（不得出现 10）。"""
    import niu_api.llm_proxy as llm_proxy_module

    msgs = _messages(*[(f"m{i}", "user" if i % 2 else "assistant") for i in range(1, 13)])
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "entity-extractor": "处理完成 @end processed_up_to=6",
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

    assert "context-manager" in _called_agents(call_mock), "校验应通过（entity 钉在 5-1）"
    assert recorded, f"应发生 _find_protected_range 调用: {recorded}"
    assert all(v == 5 for v in recorded), f"entity 排除/校验/压缩三处应全用 effective=5，实际 {recorded}"
    # entity 游标钉在 effective-1（protect_start-1）
    entity_id = store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id")
    assert entity_id == "m6", f"entity 游标应钉在 protect_start-1=m6，实际 {entity_id}"
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
    monkeypatch.setattr(runner_module.NiuRunner, "_read_cursor_locked", staticmethod(store.read))
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


def test_runner_force_skipped_when_cursor_not_caught_up(monkeypatch):
    """runner-force：entity/dream/journal 全失败（overflow）→ 游标空 → 压缩段入口校验失败 → skipped。"""
    result, call_mock, _ = _run_runner_force(
        monkeypatch,
        {"entity-extractor": OVERFLOW_JSON, "dream-evolver": OVERFLOW_JSON, "journal-agent": OVERFLOW_JSON},
    )
    assert result == {"status": "skipped", "reason": SKIP_REASON}, f"实际: {result}"
    assert "context-manager" not in _called_agents(call_mock), "未追平不应调用 cm"


def test_runner_force_caught_up_proceeds(monkeypatch):
    """runner-force：三步真实推进到尾部（protect=0 游标=尾部）→ 校验通过 → 压缩执行（cm 被调）。"""
    result, call_mock, store = _run_runner_force(
        monkeypatch,
        {
            "entity-extractor": "处理完成 @end processed_up_to=2",
            "dream-evolver": "处理完成 @end processed_up_to=2",
            "journal-agent": "处理完成 @end processed_up_to=2",
            "context-manager": "SUBAGENT_ERROR:mock",
        },
    )
    assert "context-manager" in _called_agents(call_mock), "校验通过后 cm 应被调用"
    assert result.get("status") == "skipped" and "LLM error" in result.get("reason", ""), f"实际: {result}"
    entity_id = store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id")
    assert entity_id == "m2", f"entity 游标应推进到 m2，实际 {entity_id}"
