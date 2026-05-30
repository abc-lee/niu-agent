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
