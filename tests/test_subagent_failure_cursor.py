"""T7 测试：子 Agent 程序化失败（'[错误]' / 'SUBAGENT_ERROR:'）游标不推进。

方案 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.4 + §6 T7：
判定扩展 overflow or incomplete or failure（11 决策点）——[错误]（注册冲突）/ SUBAGENT_ERROR:（LLM 错误）
非 JSON 不命中 overflow/incomplete 判定，现状落 else 全量推进 → 游标假推进。本 Task 修复。

覆盖：
1. _is_subagent_failure 单元测试（前缀判定 + 非 str/正常 JSON 不命中）
2. compat sleep 代表点：全链（entity/dream）+ L3546 mode1 压缩点 → 游标全不动
3. compat force 代表点：entity/dream/journal（skip_compress=True）→ 游标全不动
4. runner nap 代表点：entity + dream 分支顺序（failure 优先于 overflow / else 推进）
5. handler._update_journal_cursor（journal 游标：failure 前缀零写入，正常推进判别力）

fixture 数学同 test_incomplete_cursor（R6-B 验证）：2 条消息 × _FakeCalc 100 token/条 = 200 tokens，
_read_context_window_tokens=8000 → 2.5% usage：sleep journal 跳过（<50%）、mode1 压缩执行（<70% 不 skip）。
"""
import asyncio
import json
from contextlib import ExitStack
from unittest import mock

from niu_api.compat import _is_subagent_failure


# ---------------------------------------------------------------------------
# 1. _is_subagent_failure 单元测试
# ---------------------------------------------------------------------------

class TestIsSubagentFailure:
    def test_error_bracket_prefix_true(self):
        """注册冲突返回 '[错误]' 前缀 → failure。"""
        assert _is_subagent_failure("[错误]同名子 Agent 已在运行") is True

    def test_subagent_error_prefix_true(self):
        """LLM 错误返回 'SUBAGENT_ERROR:' 前缀 → failure。"""
        assert _is_subagent_failure("SUBAGENT_ERROR:provider connection failed") is True

    def test_normal_json_false(self):
        """正常 JSON 结果不命中。"""
        assert _is_subagent_failure(json.dumps({"ok": True})) is False

    def test_overflow_json_false(self):
        """overflow JSON 不命中（由 _is_subagent_overflow 负责）。"""
        assert _is_subagent_failure(
            '{"overflow": true, "agent": "a", "turns_completed": 5, "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}'
        ) is False

    def test_incomplete_json_false(self):
        """incomplete JSON 不命中（由 _is_subagent_incomplete 负责）。"""
        assert _is_subagent_failure(
            '{"incomplete": true, "agent": "a", "reason": "MAX_TURNS_EXCEEDED", "partial_result": ""}'
        ) is False

    def test_plain_text_not_prefixed_false(self):
        """普通文本（processed_up_to 正常返回）不命中。"""
        assert _is_subagent_failure("处理完成 @end processed_up_to=5") is False

    def test_non_str_false(self):
        """非 str 一律 False（None / dict / bytes）。"""
        assert _is_subagent_failure(None) is False
        assert _is_subagent_failure({"error": "x"}) is False
        assert _is_subagent_failure("[错误]".encode("utf-8")) is False

    def test_empty_string_false(self):
        assert _is_subagent_failure("") is False


# ---------------------------------------------------------------------------
# 2. compat _tidy_context_impl sleep 模式一代表点（独立 fixture）
# ---------------------------------------------------------------------------

NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete / 非 failure 的正常返回
ERROR_BRACKET = "[错误]同名子 Agent 已在运行（注册冲突）"
SUBAGENT_ERROR_STR = "SUBAGENT_ERROR:provider connection failed"


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


def _tidy_failure_patches(subagent_result, call_mock):
    """T7 独立 fixture（同 test_incomplete_cursor._tidy_incomplete_patches 模式，各 Task fixture 独立）。

    - 四个游标文件 READ 强制 cursor=''：Path.exists→False（缺失文件 → 游标留空）
    - _read_protect_recent_count→0 / _read_warning_threshold→0.8 / 窗口 8000 / _FakeCalc
    - 保留真实 _build_incremental_msg_text（不 patch）→ 范围非空
    - _write_cursor_with_lock → MagicMock（记录调用，测试 hermetic）
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        # builder refetch lightrag 段——mock 隔离，不读真实用户配置
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=0),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        # T5：sleep 管道测试保持睡眠态（CP1-CP3 检查点需 is_sleeping=True 才不打断）
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        # 四个游标文件 READ 强制 cursor=''：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock"),
    ]


class TestTidySleepFailureCursor:
    def _run_sleep_tidy(self, subagent_result):
        from niu_api.compat import _tidy_context_impl

        store = mock.MagicMock()
        store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
        call_mock = mock.MagicMock()
        call_mock.return_value = subagent_result
        with ExitStack() as stack:
            stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
            for p in _tidy_failure_patches(subagent_result, call_mock):
                stack.enter_context(p)
            write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
            result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
        return result, write_mock, call_mock

    @staticmethod
    def _cursor_writes(write_mock):
        return [call.args[1] for call in write_mock.call_args_list]

    def test_error_bracket_does_not_advance_any_cursor(self):
        """sleep 全链收 '[错误]' → entity/dream 游标零写（L2953/L3039 决策点）。"""
        result, write_mock, call_mock = self._run_sleep_tidy(ERROR_BRACKET)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents == ["entity-extractor", "dream-evolver", "context-manager"], (
            f"期望 entity/dream/cm 依次调用，实际 {called_agents}"
        )
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"'[错误]' 结果不应写任何游标: {writes}"

    def test_subagent_error_does_not_advance_compress_cursor(self):
        """sleep mode1 压缩点（L3546）收 'SUBAGENT_ERROR:' → 压缩游标不动。

        判别力：cm 真实被调用（called_agents 断言），压缩游标零写——
        若 failure 退化成 else 兜底会写 last_compress_id ∈ (m1, m2)。
        """
        result, write_mock, call_mock = self._run_sleep_tidy(SUBAGENT_ERROR_STR)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert "context-manager" in called_agents, f"cm 应被调用: {called_agents}"
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"'SUBAGENT_ERROR:' 结果不应写任何游标: {writes}"

    def test_normal_result_advances_compress_cursor(self):
        """对照：正常返回时压缩游标必须推进（证明 fixture 非空洞、断言有判别力）。"""
        result, write_mock, _ = self._run_sleep_tidy(NORMAL_JSON)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        compress_writes = [
            d for d in self._cursor_writes(write_mock)
            if d.get("last_compress_id")
        ]
        assert compress_writes, "正常返回应推进压缩游标"
        assert compress_writes[-1]["last_compress_id"] in ("m1", "m2")


# ---------------------------------------------------------------------------
# 3. compat _tidy_context_impl force 代表点（skip_compress=True）
# ---------------------------------------------------------------------------

class TestTidyForceFailureCursor:
    def _run_force_tidy(self, subagent_result):
        from niu_api.compat import _tidy_context_impl

        store = mock.MagicMock()
        store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
        call_mock = mock.MagicMock()
        call_mock.return_value = subagent_result
        with ExitStack() as stack:
            stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
            for p in _tidy_failure_patches(subagent_result, call_mock):
                stack.enter_context(p)
            write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
            result = asyncio.run(_tidy_context_impl(
                {"mode": "force", "skip_compress": True, "session_id": "t"},
                chat_lock_already_held=True,
            ))
        return result, write_mock, call_mock

    @staticmethod
    def _cursor_writes(write_mock):
        return [call.args[1] for call in write_mock.call_args_list]

    def test_error_bracket_does_not_advance_force_cursors(self):
        """force 全链收 '[错误]' → entity/dream/journal 三游标零写（L3726/L3811/L3896 决策点）。

        force 模式 journal 始终调用（区别于 sleep 的 usage>=50% 门控）。
        """
        result, write_mock, call_mock = self._run_force_tidy(ERROR_BRACKET)
        assert result.get("status") == "ok", f"force tidy 应正常结束: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents == ["entity-extractor", "dream-evolver", "journal-agent"], (
            f"期望 entity/dream/journal 依次调用，实际 {called_agents}"
        )
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"force '[错误]' 结果不应写任何游标: {writes}"

    def test_normal_result_advances_force_cursors(self):
        """对照：正常返回时 force entity/dream/journal 游标必须推进（判别力）。"""
        result, write_mock, _ = self._run_force_tidy("处理完成 @end processed_up_to=2")
        assert result.get("status") == "ok", f"force tidy 应正常结束: {result}"
        writes = self._cursor_writes(write_mock)
        assert writes, f"force 正常返回应推进游标: {writes}"
        assert writes[-1].get("last_journal_id") == "m2", (
            f"journal 游标应推进到 m2: {writes}"
        )


# ---------------------------------------------------------------------------
# 4. runner _run_nap_background 代表点（entity + dream 分支顺序）
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, mid):
        self.id = mid


def _nap_messages(n):
    return [_Msg(f"m{i}") for i in range(1, n + 1)]


def _fake_build_incremental_msg_text(messages, last_cursor_id, out_msg_ids, msg_tokens=None, **kwargs):
    """与真实签名兼容：把 db 消息 id 全量填入 out_msg_ids（游标后增量 = 全部）。"""
    out_msg_ids.extend([getattr(m, "id", "") for m in messages])
    return ""


def _fake_build_plain_history(messages, out_msg_ids=None):
    """返回 ([N] 前缀 history, {idx: id} 映射) —— 与真实 _build_plain_history 同构（int 键）。"""
    hist = []
    idx_to_id = {}
    for i, m in enumerate(messages, 1):
        idx_to_id[i] = getattr(m, "id", "")
        hist.append(f"[{i}] {getattr(m, 'content', '')}")
    return hist, idx_to_id


class TestNapFailureCursorBranches:
    """_run_nap_background 失败前缀分支（T7：failure 优先于 overflow / else 推进）。

    - entity 收 '[错误]' → entity 游标保持 last（m1），绝不推进到 m2
    - dream 收 'SUBAGENT_ERROR:' → dream 游标保持 m1；不触发 overflow 1/3 兜底
      （改写前顺序 if overflow→elif incomplete→else 全量推进，'SUBAGENT_ERROR:' 落 else 写 m2 → 红相）
    - 判别力双否定：无 "Overflow fallback" / 无 "Dream cursor advanced"
    """

    def _run_nap(self, dream_result, entity_result="处理完成 @end processed_up_to=2", n_msgs=2):
        from agent.runner import NiuRunner

        runner = NiuRunner.__new__(NiuRunner)
        runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        runner._nap_running = mock.MagicMock()
        runner._sync_get_messages = lambda: _nap_messages(n_msgs)
        runner._recalc_msg_stats = lambda msgs: [100] * len(msgs)
        runner._read_cursor_locked = lambda path, field: "m1"  # 上一游标 = 真实消息 id（m1）
        runner._ensure_session_chain = mock.MagicMock()

        def _call_side(agent_name, task, **kwargs):
            if agent_name == "entity-extractor":
                return entity_result
            if agent_name == "dream-evolver":
                return dream_result
            raise AssertionError(f"unexpected subagent: {agent_name}")

        write_mock = mock.MagicMock()
        with mock.patch("agent.subagent.call_subagent_with_auto_answer", side_effect=_call_side), \
             mock.patch("niu_api.compat._build_incremental_msg_text", side_effect=_fake_build_incremental_msg_text), \
             mock.patch("niu_api.compat._build_plain_history", side_effect=_fake_build_plain_history), \
             mock.patch("niu_api.compat._write_cursor_with_lock", write_mock), \
             mock.patch("agent.runner.logger") as logger_mock:
            runner._run_nap_background()
        return write_mock, logger_mock, runner

    @staticmethod
    def _entity_writes(write_mock):
        return [c.args[1] for c in write_mock.call_args_list if "last_entity_extract_id" in c.args[1]]

    @staticmethod
    def _dream_writes(write_mock):
        return [c.args[1] for c in write_mock.call_args_list if "last_dream_evolve_id" in c.args[1]]

    @staticmethod
    def _logged(logger_mock, level, needle):
        return any(needle in str(c.args[0]) for c in getattr(logger_mock, level).call_args_list)

    def test_error_bracket_entity_cursor_not_advanced(self):
        """entity 收 '[错误]' → entity 游标保持 m1（写入幂等回原游标），绝不推进到 m2。"""
        write_mock, logger_mock, runner = self._run_nap(
            "处理完成 @end processed_up_to=2", entity_result=ERROR_BRACKET,
        )
        writes = self._entity_writes(write_mock)
        assert len(writes) == 1, f"失败也应幂等写回原游标: {writes}"
        assert writes[0]["last_entity_extract_id"] == "m1", (
            f"'[错误]' 时 entity 游标不得推进（应为 last=m1）: {writes}"
        )
        assert self._logged(logger_mock, "warning", "[Nap] entity-extractor incomplete") is False, (
            f"'[错误]' 不应落 incomplete 分支: {logger_mock.warning.call_args_list}"
        )
        assert not self._logged(logger_mock, "info", "Entity cursor advanced"), (
            f"'[错误]' 不应推进: {logger_mock.info.call_args_list}"
        )
        runner._nap_running.clear.assert_called_once()

    def test_subagent_error_dream_cursor_not_advanced_and_overflow_priority(self):
        """dream 收 'SUBAGENT_ERROR:' → 游标保持 m1 + 无 overflow fallback + 无 else 推进。

        分支顺序验证：failure 判定优先级高于 overflow（'SUBAGENT_ERROR:' 非 JSON 不命中
        overflow，但改写前落 else 全量推进 → 写 m2 红相；改写后保持 m1 绿相）。
        """
        write_mock, logger_mock, runner = self._run_nap(SUBAGENT_ERROR_STR)
        writes = self._dream_writes(write_mock)
        assert len(writes) == 1, f"失败也应幂等写回原游标: {writes}"
        assert writes[0]["last_dream_evolve_id"] == "m1", (
            f"'SUBAGENT_ERROR:' 时 dream 游标不得推进（应为 last=m1）: {writes}"
        )
        # 双否定判别：不落 overflow 1/3 兜底、不落 else 全量推进
        assert not self._logged(logger_mock, "info", "Overflow fallback"), (
            f"不应触发 overflow 1/3 兜底: {logger_mock.info.call_args_list}"
        )
        assert not self._logged(logger_mock, "info", "Dream cursor advanced"), (
            f"不应触发 else 推进: {logger_mock.info.call_args_list}"
        )
        assert not logger_mock.error.call_args_list, f"不应有 error: {logger_mock.error.call_args_list}"
        runner._nap_running.clear.assert_called_once()


# ---------------------------------------------------------------------------
# 5. handler._update_journal_cursor（journal 游标 hermetic——T7 决策点补齐）
# ---------------------------------------------------------------------------

class TestHandlerJournalCursorFailure:
    """_update_journal_cursor 失败前缀分支（T7：handler.py L1152 决策点）。

    - '[错误]' → last_journal.json 零写入（游标不动）
    - 'SUBAGENT_ERROR:' → last_journal.json 零写入
    - 正常 processed_up_to=2 → 写 m2（判别力，证明 fixture 非空洞）

    断言基于 mock write_text call args（同 test_incomplete_cursor 第 3 节模式），
    不依赖真实 last_journal.json / DB。
    """

    def _make_handler(self):
        from agent.handler import NiuHandler

        handler = NiuHandler(mcp_client=None)
        handler._sync_get_messages = lambda: [_Msg("m1"), _Msg("m2")]
        return handler

    def test_error_bracket_does_not_write_journal_cursor(self):
        """journal-agent 返回 '[错误]' 前缀 → journal 游标不写入（Quality P1 场景）。"""
        handler = self._make_handler()
        write_text = mock.MagicMock()
        with mock.patch("agent.handler.Path.exists", return_value=False), \
             mock.patch("agent.handler.Path.write_text", write_text):
            handler._update_journal_cursor(
                ERROR_BRACKET,
                ["m1", "m2"],
                {"1": "m1", "2": "m2"},
            )
        write_text.assert_not_called()

    def test_subagent_error_prefix_does_not_write_journal_cursor(self):
        """journal-agent 返回 'SUBAGENT_ERROR:' 前缀 → journal 游标不写入。"""
        handler = self._make_handler()
        write_text = mock.MagicMock()
        with mock.patch("agent.handler.Path.exists", return_value=False), \
             mock.patch("agent.handler.Path.write_text", write_text):
            handler._update_journal_cursor(
                SUBAGENT_ERROR_STR,
                ["m1", "m2"],
                {"1": "m1", "2": "m2"},
            )
        write_text.assert_not_called()

    def test_normal_result_writes_journal_cursor(self):
        """对照：正常 processed_up_to=2 → journal 游标写 m2（判别力）。"""
        handler = self._make_handler()
        write_text = mock.MagicMock()
        with mock.patch("agent.handler.Path.exists", return_value=False), \
             mock.patch("agent.handler.Path.write_text", write_text):
            handler._update_journal_cursor(
                "处理完成 @end processed_up_to=2",
                ["m1", "m2"],
                {"1": "m1", "2": "m2"},
            )
        assert write_text.call_count == 1
        payload = json.loads(write_text.call_args.args[0])
        assert payload.get("last_journal_id") == "m2"
