"""T7 测试：子 Agent 程序化失败（'[错误]' / 'SUBAGENT_ERROR:'）游标不推进。

方案 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.4 + §6 T7：
判定扩展 overflow or incomplete or failure（11 决策点）——[错误]（注册冲突）/ SUBAGENT_ERROR:（LLM 错误）
非 JSON 不命中 overflow/incomplete 判定，现状落 else 全量推进 → 游标假推进。本 Task 修复。

覆盖：
1. _is_subagent_failure 单元测试（前缀判定 + 非 str/正常 JSON 不命中）
2. compat sleep 代表点：entity 失败 F1 不剪切 → 梦境循环 D5 短路 → 门控 skipped，游标全不动
3. compat force 代表点：journal（skip_compress=True；模式三梦境腿已摘除）→ 游标全不动
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
        assert _is_subagent_failure("[错误]".encode()) is False

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
    - 窗口 8000 / _FakeCalc
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
        # T5：sleep 管道测试保持睡眠态（CP1-CP3 检查点需 is_sleeping=True 才不打断）
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        # 四个游标文件 READ 强制 cursor=''：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock"),
    ]


class TestTidySleepFailureCursor:
    def _run_sleep_tidy(self, subagent_result):
        """v2：种子记录写入隔离 F1 使 entity 步真实执行。

        门控已随工程四重排摘除（决策 2）——无 cursor_value 参与对应 patch 缝。

        成功结果（NORMAL_JSON）补 processed_line 触发 relay 剪切；失败/incomplete
        结果保持原样（F1 不剪切契约）。F2 patch 到测试专用 tmp。返回附 (f1, f2) 路径。
        """
        import os as _os
        import tempfile

        import agent.md_mirror as mdm
        from niu_api.compat import _tidy_context_impl

        store = mock.MagicMock()
        store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
        call_mock = mock.MagicMock()

        def _keyed(agent_name=None, **kwargs):
            if agent_name == "entity-extractor" and subagent_result == NORMAL_JSON:
                return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=999999"
            return subagent_result

        call_mock.side_effect = _keyed
        f2_path = _os.path.join(tempfile.mkdtemp(prefix="t7_relay_"), "f2.md")
        with ExitStack() as stack:
            stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
            for p in _tidy_failure_patches(subagent_result, call_mock):
                stack.enter_context(p)
            stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
            write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
            block = mdm.format_message_record(
                msg_id="t7-seed-not-in-db", created_at="t", role="user", content="种子",
            )
            assert mdm.append_record(block, mdm.F1_PATH)
            result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
        return result, write_mock, call_mock, mdm.F1_PATH, f2_path

    @staticmethod
    def _cursor_writes(write_mock):
        return [call.args[1] for call in write_mock.call_args_list]

    def test_error_bracket_does_not_advance_any_cursor(self):
        """sleep 全链收 '[错误]' → 游标零写 + F1 不剪切（契约：数据保留下次重跑）。

        T6：cm 腿退役——entity 收 '[错误]'（F1 不剪切）→ F2 空 → 梦境循环 D5 短路
        → 终态 ok。
        """
        result, write_mock, call_mock, f1, _f2 = self._run_sleep_tidy(ERROR_BRACKET)
        assert result.get("status") == "ok", f"失败应续跑至终态 ok，实际: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents == ["entity-extractor"], (
            f"T6 后无 cm；entity 失败 F1 未剪切 → F2 空 → 梦境循环 D5 短路，实际 {called_agents}"
        )
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"'[错误]' 结果不应写任何游标: {writes}"
        with open(f1, encoding="utf-8") as f:
            assert '"msg_id": "t7-seed-not-in-db"' in f.read(), "失败时 F1 不得被剪切"

    def test_subagent_error_does_not_advance_any_cursor(self):
        """sleep 全链收 'SUBAGENT_ERROR:' → 游标全不动（T6：cm 腿退役，仅 entity 路径）。

        entity 收 SUBAGENT_ERROR → F1 不剪切。终态 ok；
        判别力：若不判定 failure 会 relay 推进、写游标。
        """
        result, write_mock, call_mock, _f1, _f2 = self._run_sleep_tidy(SUBAGENT_ERROR_STR)
        assert result.get("status") == "ok", f"失败吸收应续跑至终态 ok: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents[0] == "entity-extractor", f"T6 后 entity 应最先被调: {called_agents}"
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"'SUBAGENT_ERROR:' 结果不应写任何游标: {writes}"

    def test_normal_result_cuts_f1_no_compress_cursor(self):
        """对照：正常返回时 relay 剪切执行且 compress 游标零写（T6 退役反向钉）。"""
        result, write_mock, _call, f1, f2 = self._run_sleep_tidy(NORMAL_JSON)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        compress_writes = [
            d for d in self._cursor_writes(write_mock)
            if d.get("last_compress_id")
        ]
        assert compress_writes == [], "compress 游标已退役，零写"
        with open(f1, encoding="utf-8") as f:
            assert f.read() == "", "成功提炼后 F1 应被剪切清空"
        with open(f2, encoding="utf-8") as f:
            assert '"msg_id": "t7-seed-not-in-db"' in f.read(), "剪下前缀应追加到 F2"



class _Msg:
    def __init__(self, mid):
        self.id = mid


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
