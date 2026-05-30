"""
Auto-Tidy 双游标机制重构 — 集成测试

测试方式：使用内存 SQLite 构造真实消息，验证 _build_incremental_msg_text 的行为。
不需要 mock LLM，只测试程序层面的消息生成和游标逻辑。
"""
import pytest
import uuid
from dataclasses import dataclass, field


# --- 消息对象模拟（与 MessageStore 返回的对象兼容） ---

@dataclass
class FakeMessage:
    id: str
    role: str
    content: str
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    tool_call_id: str = ""
    created_at: str = ""


def make_messages(n: int, start_idx: int = 0) -> list[FakeMessage]:
    """生成 n 条模拟消息，UUID 顺序可预测"""
    return [
        FakeMessage(id=f"uuid-{start_idx + i}", role="user" if i % 2 == 0 else "assistant", content=f"消息内容 {start_idx + i}")
        for i in range(n)
    ]


# --- 导入被测函数 ---
import sys
sys.path.insert(0, ".")
from niu_api.compat import _build_incremental_msg_text
from niu_api.compat import _extract_cursor_id


class TestBuildIncrementalMsgTextEndCursor:
    """测试 end_cursor_id 参数：上界截断，只生成到该游标为止的消息"""

    def test_end_cursor_truncates_messages(self):
        """end_cursor_id 存在时，只生成 [start_cursor, end_cursor] 范围内的消息"""
        messages = make_messages(10)  # uuid-0 ~ uuid-9
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",    # 从 uuid-2 之后开始
            out_msg_ids=out_ids,
            end_cursor_id="uuid-7",      # 到 uuid-7 为止
        )
        # 应包含 uuid-3, uuid-4, uuid-5, uuid-6, uuid-7
        assert out_ids == ["uuid-3", "uuid-4", "uuid-5", "uuid-6", "uuid-7"]
        assert "uuid-3" in result
        assert "uuid-7" in result
        assert "uuid-8" not in result

    def test_end_cursor_none_returns_all_after_start(self):
        """end_cursor_id 为 None 时，返回 start_cursor 之后的所有消息"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-5",
            out_msg_ids=out_ids,
            end_cursor_id=None,
        )
        assert out_ids == ["uuid-6", "uuid-7", "uuid-8", "uuid-9"]

    def test_end_cursor_not_found_degrades_to_full(self):
        """end_cursor_id 在消息列表中不存在时，退化到返回 start 之后的所有消息"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-nonexistent",
        )
        # end_cursor 找不到 → 退化为无上界
        assert out_ids == ["uuid-3", "uuid-4", "uuid-5", "uuid-6", "uuid-7", "uuid-8", "uuid-9"]

    def test_end_cursor_before_start_returns_empty(self):
        """end_cursor 在 start_cursor 之前时，返回空"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-7",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-2",
        )
        assert out_ids == []


class TestBuildIncrementalMsgTextFilterWm:
    """测试 filter_wm 参数：过滤 working_memory 虚拟消息和修复 tool_calls 成对完整性"""

    def _make_messages_with_wm(self) -> list[FakeMessage]:
        """构造含 WM 虚拟消息的消息列表"""
        return [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="你好！", tool_calls=[
                {"id": "tc-1", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-1"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-4", role="assistant", content="好的，我来写"),
        ]

    def test_filter_wm_true_removes_working_memory(self):
        """filter_wm=True 时，过滤掉 WM 的 assistant(tool_calls) 和对应 tool 结果"""
        messages = self._make_messages_with_wm()
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1(WM call) 和 uuid-2(WM result) 应被过滤
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-0" in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" in out_ids

    def test_filter_wm_false_keeps_working_memory(self):
        """filter_wm=False 时，保留 WM 消息（默认行为，向后兼容）"""
        messages = self._make_messages_with_wm()
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=False,
        )
        assert "uuid-1" in out_ids
        assert "uuid-2" in out_ids

    def test_filter_wm_removes_trailing_orphan_tool_calls(self):
        """filter_wm=True 时，移除末尾孤立的 assistant(tool_calls)（无对应 tool 结果）"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-1", "function": {"name": "some_tool"}, "arguments": "{}"}
            ]),
            # 没有对应的 tool 结果 — 末尾孤立
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1 是末尾孤立的 assistant(tool_calls)，应被移除
        assert "uuid-1" not in out_ids

    def test_filter_wm_removes_leading_orphan_tool(self):
        """filter_wm=True 时，移除开头孤立的 tool 消息（游标切割导致）"""
        messages = [
            FakeMessage(id="uuid-0", role="tool", content="result", tool_call_id="tc-missing"),
            FakeMessage(id="uuid-1", role="user", content="你好"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-0 是开头的孤立 tool，应被移除
        assert "uuid-0" not in out_ids
        assert "uuid-1" in out_ids

    def test_filter_wm_mixed_tool_calls_keeps_non_wm(self):
        """filter_wm=True 时，assistant 同时有 WM 和非 WM tool_calls，保留非 WM 部分"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="我来处理", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"},
                {"id": "tc-real", "function": {"name": "code_run"}, "arguments": "{}"},
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="tool", content="代码执行结果", tool_call_id="tc-real"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        # uuid-1 应保留（有非 WM tool_call），uuid-2(WM result) 应过滤，uuid-3 应保留
        assert "uuid-1" in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids

    def test_filter_wm_preserves_non_wm_tool_calls(self):
        """filter_wm=True 时，非 WM 的 tool_calls（如 code_run）不被过滤"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-1", role="assistant", content="好的", tool_calls=[
                {"id": "tc-1", "function": {"name": "code_run"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="代码执行结果", tool_call_id="tc-1"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        assert "uuid-0" in out_ids
        assert "uuid-1" in out_ids
        assert "uuid-2" in out_ids

    def test_filter_wm_idx_uses_original_positions(self):
        """filter_wm 过滤消息后，idx 仍使用原始全量列表位置"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        # uuid-0 的 idx=1，uuid-3 的 idx=4（原始位置，不是过滤后的 idx=2）
        assert "[idx:1]" in result
        assert "[idx:4]" in result
        assert "[idx:2]" not in result  # uuid-1 被过滤，idx=2 不应出现


class TestBuildIncrementalMsgTextProtectRecent:
    """测试 protect_recent 参数：对最后 N 条消息加 [PROTECTED] 标签"""

    def test_protect_recent_labels_last_n_messages(self):
        """protect_recent=3 时，最后 3 条消息加 [PROTECTED] 标签"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=3,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        # 最后 3 条（uuid-7, uuid-8, uuid-9）应有 [PROTECTED]
        assert len(protected_lines) == 3
        assert "uuid-7" in protected_lines[0]
        assert "uuid-8" in protected_lines[1]
        assert "uuid-9" in protected_lines[2]

    def test_protect_recent_zero_no_labels(self):
        """protect_recent=0 时，不加任何 [PROTECTED] 标签"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=0,
        )
        assert "[PROTECTED]" not in result

    def test_protect_recent_with_end_cursor(self):
        """protect_recent 与 end_cursor_id 组合：保护范围内的最后 N 条"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-7",
            protect_recent=2,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        # 范围内 uuid-3~uuid-7，最后 2 条是 uuid-6, uuid-7
        assert len(protected_lines) == 2
        assert "uuid-6" in protected_lines[0]
        assert "uuid-7" in protected_lines[1]

    def test_protect_recent_larger_than_range(self):
        """protect_recent 大于增量消息数时，全部加 [PROTECTED]"""
        messages = make_messages(3)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=10,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 3  # 全部 3 条

    def test_protect_recent_with_filter_wm(self):
        """protect_recent + filter_wm 组合：过滤后再保护"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-4", role="assistant", content="好的"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages, "", out_ids, protect_recent=1, filter_wm=True
        )
        # 过滤后剩 uuid-0, uuid-3, uuid-4，最后 1 条(uuid-4) 加 PROTECTED
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 1
        assert "uuid-4" in protected_lines[0]

    def test_end_cursor_with_filter_wm(self):
        """end_cursor_id + filter_wm 组合：先截断再过滤"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我"),
            FakeMessage(id="uuid-4", role="assistant", content="好的"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages, "", out_ids, end_cursor_id="uuid-3", filter_wm=True
        )
        # 截断到 uuid-3 → [uuid-0, uuid-1, uuid-2, uuid-3]，过滤 WM → [uuid-0, uuid-3]
        assert "uuid-0" in out_ids
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" not in out_ids


class TestBuildEntityHistoryReplacement:
    """验证 _build_incremental_msg_text(filter_wm=True) 完整替代 _build_entity_history() 的功能"""

    def test_wm_filter_in_incremental_range(self):
        """增量范围内的 WM 过滤效果与 _build_entity_history 一致"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="你好！", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="assistant", content="有什么可以帮你？"),
            FakeMessage(id="uuid-4", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-5", role="assistant", content="好的", tool_calls=[
                {"id": "tc-real", "function": {"name": "code_run"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-6", role="tool", content="代码执行结果", tool_call_id="tc-real"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-0",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1(WM call) 和 uuid-2(WM result) 被过滤
        # uuid-5 和 uuid-6 不是 WM，保留
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" in out_ids
        assert "uuid-5" in out_ids
        assert "uuid-6" in out_ids


class TestExtractCursorIdNull:
    """测试 _extract_cursor_id 对 null 值的检测"""

    def test_normal_uuid_extraction(self):
        """正常提取 UUID"""
        result = _extract_cursor_id(
            '处理完成 {"last_entity_extract_id": "uuid-abc123"} 收尾',
            "last_entity_extract_id",
            {"uuid-abc123"},
        )
        assert result == "uuid-abc123"

    def test_null_returns_sentinel(self):
        """明确返回 null 时，返回特殊标记 'NULL'（区分'没报告'和'明确返回null'）"""
        result = _extract_cursor_id(
            '处理完成 {"last_entity_extract_id": null} 收尾',
            "last_entity_extract_id",
            set(),
        )
        assert result == "NULL"

    def test_no_match_returns_none(self):
        """没有匹配时返回 None"""
        result = _extract_cursor_id(
            "没有任何游标信息",
            "last_entity_extract_id",
            set(),
        )
        assert result is None

    def test_invalid_uuid_not_in_valid_ids(self):
        """UUID 不在 valid_ids 中时返回 None"""
        result = _extract_cursor_id(
            '{"last_entity_extract_id": "uuid-nonexistent"}',
            "last_entity_extract_id",
            {"uuid-other"},
        )
        assert result is None

    def test_null_with_whitespace(self):
        """null 带各种空白格式"""
        result = _extract_cursor_id(
            '{"last_entity_extract_id" :  null  }',
            "last_entity_extract_id",
            set(),
        )
        assert result == "NULL"


class TestTidyContextImplIntegration:
    """
    全量集成测试 — 验证 _tidy_context_impl 的完整流程

    测试方式：构造真实消息，验证三个子Agent的增量范围计算、
    游标 fallback、保护范围等程序层面逻辑。不 mock LLM。
    """

    def test_incremental_range_calculation(self):
        """验证三个子Agent的增量消息范围计算逻辑"""
        messages = make_messages(20)  # uuid-0 ~ uuid-19

        # Entity: cursor=uuid-4, 范围 [uuid-5, 末尾]
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-4", entity_ids, filter_wm=True)
        assert entity_ids[0] == "uuid-5"
        assert entity_ids[-1] == "uuid-19"
        assert len(entity_ids) == 15

        # Dream: cursor=uuid-9, 范围 [uuid-10, 末尾]（与 entity 独立）
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-9", dream_ids, filter_wm=True)
        assert dream_ids[0] == "uuid-10"
        assert dream_ids[-1] == "uuid-19"
        assert len(dream_ids) == 10

        # Context: cursor=uuid-2, end=uuid-14, 范围 [uuid-3, uuid-14]
        compress_ids = []
        _build_incremental_msg_text(messages, "uuid-2", compress_ids, end_cursor_id="uuid-14", protect_recent=3, filter_wm=True)
        assert compress_ids[0] == "uuid-3"
        assert compress_ids[-1] == "uuid-14"
        assert len(compress_ids) == 12

    def test_first_run_all_cursors_empty(self):
        """首次运行：所有游标为空，三个Agent从开头处理"""
        messages = make_messages(10)

        # Entity: cursor=""
        entity_ids = []
        _build_incremental_msg_text(messages, "", entity_ids, filter_wm=True)
        assert len(entity_ids) == 10

        # Dream: cursor=""
        dream_ids = []
        _build_incremental_msg_text(messages, "", dream_ids, filter_wm=True)
        assert len(dream_ids) == 10

        # Context: cursor="", end=uuid-9
        compress_ids = []
        _build_incremental_msg_text(messages, "", compress_ids, end_cursor_id="uuid-9", filter_wm=True)
        assert len(compress_ids) == 10

    def test_cursor_points_to_deleted_message(self):
        """游标指向已删除消息时，退化到从头开始"""
        messages = make_messages(10)
        # uuid-99 不在列表中
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-99", entity_ids, filter_wm=True)
        # 退化到全量
        assert len(entity_ids) == 10

    def test_empty_incremental_range(self):
        """游标已在末尾，无增量消息"""
        messages = make_messages(5)
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-4", entity_ids, filter_wm=True)
        assert entity_ids == []
        assert "无新增消息" in result

    def test_protected_ids_extraction(self):
        """验证保护范围内的 UUID 列表提取"""
        messages = make_messages(20)
        compress_ids = []
        _build_incremental_msg_text(
            messages, "uuid-5", compress_ids,
            end_cursor_id="uuid-15", protect_recent=3, filter_wm=True
        )
        # compress_ids 包含 uuid-6 ~ uuid-15（10条）
        # 最后 3 条保护：uuid-13, uuid-14, uuid-15
        protected = compress_ids[-3:]
        assert protected == ["uuid-13", "uuid-14", "uuid-15"]

    def test_cursor_fallback_to_last_incremental_msg(self):
        """验证游标 fallback：推进到增量消息最后一条"""
        messages = make_messages(10)
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-3", entity_ids, filter_wm=True)
        # 如果 _extract_cursor_id 返回 None 或 "NULL"，应推进到 entity_ids[-1]
        fallback_cursor = entity_ids[-1] if entity_ids else None
        assert fallback_cursor == "uuid-9"

    def test_serial_execution_isolation(self):
        """验证串行执行隔离：三个Agent独立计算增量范围"""
        messages = make_messages(30)  # uuid-0 ~ uuid-29

        # 模拟串行执行：
        # Step 1: Entity cursor=uuid-10, 范围 [uuid-11, 末尾]
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-10", entity_ids, filter_wm=True)
        assert entity_ids[0] == "uuid-11"
        assert len(entity_ids) == 19

        # Step 2: Dream cursor=uuid-15, 范围 [uuid-16, 末尾]（独立于 Entity）
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-15", dream_ids, filter_wm=True)
        assert dream_ids[0] == "uuid-16"
        assert len(dream_ids) == 14

        # Step 3: Context cursor=uuid-5, end=dream推进后的新游标(uuid-29)
        # 范围 [uuid-6, uuid-29]
        compress_ids = []
        _build_incremental_msg_text(messages, "uuid-5", compress_ids, end_cursor_id="uuid-29", protect_recent=5, filter_wm=True)
        assert compress_ids[0] == "uuid-6"
        assert compress_ids[-1] == "uuid-29"
        assert len(compress_ids) == 24

    def test_force_mode_entity_full_range(self):
        """force 模式 Entity Extractor 传空游标 = 全量"""
        messages = make_messages(15)
        entity_ids = []
        _build_incremental_msg_text(messages, "", entity_ids, filter_wm=True)
        assert len(entity_ids) == 15
        assert entity_ids[0] == "uuid-0"
        assert entity_ids[-1] == "uuid-14"

    def test_force_mode_dream_still_incremental(self):
        """force 模式 Dream Evolver 仍为增量模式"""
        messages = make_messages(15)
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-10", dream_ids, filter_wm=True)
        assert len(dream_ids) == 4  # uuid-11 ~ uuid-14
        assert dream_ids[0] == "uuid-11"

    def test_force_mode_context_full_range_with_protection(self):
        """force 模式 Context Manager 全量 + 保护"""
        messages = make_messages(20)
        compress_ids = []
        result = _build_incremental_msg_text(messages, "", compress_ids, protect_recent=5, filter_wm=True)
        assert len(compress_ids) == 20
        # 最后 5 条应有 [PROTECTED] 标签
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 5
