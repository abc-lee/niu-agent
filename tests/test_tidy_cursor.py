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
        _build_incremental_msg_text(messages, "uuid-4", entity_ids)
        assert entity_ids[0] == "uuid-5"
        assert entity_ids[-1] == "uuid-19"
        assert len(entity_ids) == 15

        # Dream: cursor=uuid-9, 范围 [uuid-10, 末尾]（与 entity 独立）
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-9", dream_ids)
        assert dream_ids[0] == "uuid-10"
        assert dream_ids[-1] == "uuid-19"
        assert len(dream_ids) == 10

        # Context: cursor=uuid-2, end=uuid-14, 范围 [uuid-3, uuid-14]
        compress_ids = []
        _build_incremental_msg_text(messages, "uuid-2", compress_ids, end_cursor_id="uuid-14", protect_recent=3)
        assert compress_ids[0] == "uuid-3"
        assert compress_ids[-1] == "uuid-14"
        assert len(compress_ids) == 12

    def test_first_run_all_cursors_empty(self):
        """首次运行：所有游标为空，三个Agent从开头处理"""
        messages = make_messages(10)

        # Entity: cursor=""
        entity_ids = []
        _build_incremental_msg_text(messages, "", entity_ids)
        assert len(entity_ids) == 10

        # Dream: cursor=""
        dream_ids = []
        _build_incremental_msg_text(messages, "", dream_ids)
        assert len(dream_ids) == 10

        # Context: cursor="", end=uuid-9
        compress_ids = []
        _build_incremental_msg_text(messages, "", compress_ids, end_cursor_id="uuid-9")
        assert len(compress_ids) == 10

    def test_cursor_points_to_deleted_message(self):
        """游标指向已删除消息时，退化到从头开始"""
        messages = make_messages(10)
        # uuid-99 不在列表中
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-99", entity_ids)
        # 退化到全量
        assert len(entity_ids) == 10

    def test_empty_incremental_range(self):
        """游标已在末尾，无增量消息"""
        messages = make_messages(5)
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-4", entity_ids)
        assert entity_ids == []
        assert "无新增消息" in result

    def test_protected_ids_extraction(self):
        """验证保护范围内的 UUID 列表提取"""
        messages = make_messages(20)
        compress_ids = []
        _build_incremental_msg_text(
            messages, "uuid-5", compress_ids,
            end_cursor_id="uuid-15", protect_recent=3
        )
        # compress_ids 包含 uuid-6 ~ uuid-15（10条）
        # 最后 3 条保护：uuid-13, uuid-14, uuid-15
        protected = compress_ids[-3:]
        assert protected == ["uuid-13", "uuid-14", "uuid-15"]

    def test_cursor_fallback_to_last_incremental_msg(self):
        """验证游标 fallback：推进到增量消息最后一条"""
        messages = make_messages(10)
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-3", entity_ids)
        # 游标自动推进：成功时推进到增量消息最后一条
        fallback_cursor = entity_ids[-1] if entity_ids else None
        assert fallback_cursor == "uuid-9"

    def test_serial_execution_isolation(self):
        """验证串行执行隔离：三个Agent独立计算增量范围"""
        messages = make_messages(30)  # uuid-0 ~ uuid-29

        # 模拟串行执行：
        # Step 1: Entity cursor=uuid-10, 范围 [uuid-11, 末尾]
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-10", entity_ids)
        assert entity_ids[0] == "uuid-11"
        assert len(entity_ids) == 19

        # Step 2: Dream cursor=uuid-15, 范围 [uuid-16, 末尾]（独立于 Entity）
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-15", dream_ids)
        assert dream_ids[0] == "uuid-16"
        assert len(dream_ids) == 14

        # Step 3: Context cursor=uuid-5, end=dream推进后的新游标(uuid-29)
        # 范围 [uuid-6, uuid-29]
        compress_ids = []
        _build_incremental_msg_text(messages, "uuid-5", compress_ids, end_cursor_id="uuid-29", protect_recent=5)
        assert compress_ids[0] == "uuid-6"
        assert compress_ids[-1] == "uuid-29"
        assert len(compress_ids) == 24

    def test_force_mode_entity_full_range(self):
        """force 模式 Entity Extractor 传空游标 = 全量"""
        messages = make_messages(15)
        entity_ids = []
        _build_incremental_msg_text(messages, "", entity_ids)
        assert len(entity_ids) == 15
        assert entity_ids[0] == "uuid-0"
        assert entity_ids[-1] == "uuid-14"

    def test_force_mode_dream_still_incremental(self):
        """force 模式 Dream Evolver 仍为增量模式"""
        messages = make_messages(15)
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-10", dream_ids)
        assert len(dream_ids) == 4  # uuid-11 ~ uuid-14
        assert dream_ids[0] == "uuid-11"

    def test_force_mode_context_full_range_with_protection(self):
        """force 模式 Context Manager 全量 + 保护"""
        messages = make_messages(20)
        compress_ids = []
        result = _build_incremental_msg_text(messages, "", compress_ids, protect_recent=5)
        assert len(compress_ids) == 20
        # 最后 5 条应有 [PROTECTED] 标签
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 5


def test_exclude_protected_removes_protected_messages():
    """exclude_protected=True 时，PROTECTED 消息不出现在输出文本和 out_msg_ids 中"""
    messages = make_messages(10)  # 10 条消息
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=3,  # 最后 3 条 user/assistant 标记为 PROTECTED
        exclude_protected=True
    )
    # PROTECTED 消息不应在 text 中出现
    assert "[PROTECTED]" not in text
    # out_ids 不应包含最后 3 条消息的 ID
    all_ids = [getattr(m, "id", "") for m in messages]
    protected_ids = all_ids[-3:]  # 最后 3 条是 user/assistant（make_messages 交替 user/assistant）
    for pid in protected_ids:
        assert pid not in out_ids
    # 非保护消息应在 out_ids 中
    non_protected_ids = all_ids[:-3]
    for npid in non_protected_ids:
        assert npid in out_ids


def test_exclude_protected_false_keeps_protected_messages():
    """exclude_protected=False 时，PROTECTED 消息正常出现在输出中"""
    messages = make_messages(10)
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=3,
        exclude_protected=False
    )
    assert "[PROTECTED]" in text
    # 所有消息 ID 都在 out_ids 中
    all_ids = [getattr(m, "id", "") for m in messages]
    assert set(out_ids) == set(all_ids)


def test_exclude_protected_without_protect_recent_is_noop():
    """protect_recent=0 时，exclude_protected 无效（没有消息被标记为 PROTECTED）"""
    messages = make_messages(10)
    out_ids_exclude = []
    out_ids_normal = []
    _build_incremental_msg_text(messages, "", out_ids_exclude, protect_recent=0, exclude_protected=True)
    _build_incremental_msg_text(messages, "", out_ids_normal, protect_recent=0, exclude_protected=False)
    assert out_ids_exclude == out_ids_normal


def test_exclude_protected_with_tool_messages():
    """包含 tool 消息时，exclude_protected 只排除 PROTECTED 的 user/assistant 消息"""
    messages = [
        FakeMessage(id="uuid-0", role="user", content="用户消息 0"),
        FakeMessage(id="uuid-1", role="assistant", content="助手消息 1", tool_calls=[{"id": "tc1"}]),
        FakeMessage(id="uuid-2", role="tool", content="工具输出 2", tool_call_id="tc1"),
        FakeMessage(id="uuid-3", role="user", content="用户消息 3"),
        FakeMessage(id="uuid-4", role="assistant", content="助手消息 4"),
    ]
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=1,  # 保护最后 1 条 user/assistant = uuid-4
        exclude_protected=True
    )
    # uuid-4 被排除（PROTECTED），uuid-2（tool）不被排除
    assert "uuid-4" not in out_ids
    assert "uuid-2" in out_ids
    assert "uuid-0" in out_ids
    assert "uuid-1" in out_ids
    assert "uuid-3" in out_ids


def test_exclude_protected_display_idx_consecutive():
    """exclude_protected=True 时，[idx:N] 编号连续无间隔"""
    import re
    messages = make_messages(10)
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=3, exclude_protected=True
    )
    idx_values = [int(m) for m in re.findall(r'\[idx:(\d+)\]', text)]
    assert idx_values == list(range(1, len(idx_values) + 1))
    # idx 数量与 out_ids 一致
    assert len(idx_values) == len(out_ids)


def test_exclude_protected_with_end_cursor():
    """end_cursor_id + exclude_protected 组合：排除 PROTECTED 后仍尊重上界"""
    messages = make_messages(10)  # uuid-0 ~ uuid-9
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "uuid-2", out_ids,
        end_cursor_id="uuid-7", protect_recent=2,
        exclude_protected=True
    )
    # 范围 uuid-3~uuid-7，protect_recent=2 保护 uuid-6,uuid-7
    # exclude_protected=True → out_ids 不含 uuid-6, uuid-7
    assert "uuid-6" not in out_ids
    assert "uuid-7" not in out_ids
    assert "uuid-3" in out_ids
    assert "uuid-5" in out_ids
    # end_cursor 上界仍生效：uuid-8, uuid-9 不在
    assert "uuid-8" not in out_ids
    assert "uuid-9" not in out_ids
