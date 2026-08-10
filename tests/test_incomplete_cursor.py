"""Task 4 测试：未完成结果（incomplete JSON）游标不推进。

覆盖：
1. _is_subagent_incomplete 严格判定（R3-13 六例）
2. _tidy_context_impl sleep 模式一集成（独立 fixture，不动 _tidy_common_patches，
   四个游标文件 READ 强制 cursor=''，真实 _build_incremental_msg_text 非空范围）
3. handler._update_journal_cursor（journal 游标 hermetic：基于 mock write_text，
   不依赖真实 last_journal.json）

fixture 数学验证（R6-B）：2 条消息 × _FakeCalc 100 token/条 = 200 tokens，
_read_context_window_tokens=8000 → 2.5% usage < 50% 走模式一，且 < 70% 不 skip，范围非空。
"""
import json
from unittest import mock

from niu_api.compat import _incomplete_reason, _is_subagent_incomplete


# ---------------------------------------------------------------------------
# 1. _is_subagent_incomplete 单元测试（R3-13）
# ---------------------------------------------------------------------------

class TestIsSubagentIncomplete:
    def test_incomplete_json_true(self):
        assert _is_subagent_incomplete(
            '{"incomplete": true, "agent": "a", "reason": "STOPPED", "partial_result": ""}'
        ) is True

    def test_incomplete_false_literal_not_matched(self):
        assert _is_subagent_incomplete('{"incomplete": false}') is False

    def test_incomplete_string_true_not_matched(self):
        # 严格 is True 判定：字符串 "true" 不命中
        assert _is_subagent_incomplete('{"incomplete": "true"}') is False

    def test_overflow_json_not_matched(self):
        assert _is_subagent_incomplete(
            '{"overflow": true, "agent": "a", "turns_completed": 5, "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}'
        ) is False

    def test_plain_text_not_matched(self):
        assert _is_subagent_incomplete("处理完成 @end processed_up_to=5") is False

    def test_malformed_json_not_matched(self):
        assert _is_subagent_incomplete('{"incomplete": true, broken') is False

    def test_incomplete_reason_extraction(self):
        assert _incomplete_reason(
            '{"incomplete": true, "agent": "a", "reason": "MAX_TURNS_EXCEEDED", "partial_result": ""}'
        ) == "MAX_TURNS_EXCEEDED"
        assert _incomplete_reason("plain text") == ""
        assert _incomplete_reason('{"overflow": true}') == ""


# ---------------------------------------------------------------------------
# 2. _tidy_context_impl sleep 模式一集成（独立 fixture）
# ---------------------------------------------------------------------------

INCOMPLETE_JSON = json.dumps({
    "incomplete": True,
    "agent": "context-manager",
    "reason": "TERMINATED_BY_SUPPLEMENT",
    "partial_result": "再精简几个小工具输出：idx:33",
})
NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete 的正常返回


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


def _tidy_incomplete_patches(subagent_result, call_mock):
    """Task 4 独立 fixture（R3-9：不动 test_stop_interruptible 的 _tidy_common_patches）。

    R4-10 patch 清单显式化：
    - 四个游标文件 READ 强制 cursor=''（R3-4）：Path.exists→False（缺失文件 → 游标留空）
    - _read_protect_recent_count→0 / _read_warning_threshold→0.8 / 窗口 8000 / _FakeCalc
    - 保留真实 _build_incremental_msg_text（不 patch）→ 范围非空
    - _write_cursor_with_lock → MagicMock（记录调用，测试 hermetic）
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=0),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        # 四个游标文件 READ 强制 cursor=''（R3-4）：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock"),
    ]


class TestTidyContextImplIncomplete:
    def _run_sleep_tidy(self, subagent_result):
        import asyncio
        from contextlib import ExitStack

        from niu_api.compat import _tidy_context_impl

        store = mock.MagicMock()
        store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
        call_mock = mock.MagicMock()
        call_mock.return_value = subagent_result
        with ExitStack() as stack:
            stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
            for p in _tidy_incomplete_patches(subagent_result, call_mock):
                stack.enter_context(p)
            write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
            result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
        return result, write_mock, call_mock

    @staticmethod
    def _cursor_writes(write_mock):
        return [call.args[1] for call in write_mock.call_args_list]

    def test_incomplete_result_does_not_advance_any_cursor(self):
        """entity/dream/journal/compress 全部收 incomplete JSON → 四个游标都不推进（R4-6/R4-7）。

        判别力：断言子 Agent 真实被调用（entity+dream+cm；journal 2.5%<50% 跳过），
        且 _write_cursor_with_lock 零次调用（四个游标全不动）。
        """
        result, write_mock, call_mock = self._run_sleep_tidy(INCOMPLETE_JSON)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        # 子 Agent 真实跑了 entity + dream + cm（journal usage 2.5% < 50% 跳过）
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents == ["entity-extractor", "dream-evolver", "context-manager"], (
            f"期望 entity/dream/cm 依次调用，实际 {called_agents}"
        )
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"incomplete 结果不应写任何游标: {writes}"

    def test_normal_result_advances_compress_cursor(self):
        """对照：正常返回时压缩游标必须推进（证明 fixture 非空洞、断言有判别力）。"""
        result, write_mock, _ = self._run_sleep_tidy(NORMAL_JSON)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        compress_writes = [
            d for d in self._cursor_writes(write_mock)
            if d.get("last_compress_id")
        ]
        assert compress_writes, "正常返回应推进 compress 游标"
        assert compress_writes[-1]["last_compress_id"] in ("m1", "m2")


# ---------------------------------------------------------------------------
# 3. handler._update_journal_cursor（journal 游标 hermetic）
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, mid):
        self.id = mid


class TestHandlerJournalCursorIncomplete:
    def _make_handler(self):
        from agent.handler import NiuHandler

        handler = NiuHandler(mcp_client=None)
        handler._sync_get_messages = lambda: [_Msg("m1"), _Msg("m2")]
        return handler

    def test_incomplete_json_does_not_write_journal_cursor(self):
        """journal-agent 返回 incomplete JSON → last_journal.json 不写入（R2-1/R4-7）。

        断言基于 mock write_text call args，不依赖真实 last_journal.json。
        """
        handler = self._make_handler()
        write_text = mock.MagicMock()
        with mock.patch("agent.handler.Path.exists", return_value=False), \
             mock.patch("agent.handler.Path.write_text", write_text):
            handler._update_journal_cursor(
                json.dumps({"incomplete": True, "agent": "journal-agent", "reason": "STOPPED", "partial_result": ""}),
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
