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
