# tests/test_message_injector.py
from niu_api.internal.message_injector import (
    format_refined_document,
    generate_doc_id,
    get_next_segment_number,
    split_into_segments,
)


class TestDocIdGeneration:
    def test_generate_doc_id_format(self):
        """doc_id 格式应为 refined:{date}:{seq:03d}"""
        result = generate_doc_id("2026-04-27", 1)
        assert result == "refined:2026-04-27:001"

    def test_generate_doc_id_padding(self):
        """序号应零填充到3位"""
        assert generate_doc_id("2026-04-27", 5) == "refined:2026-04-27:005"
        assert generate_doc_id("2026-04-27", 42) == "refined:2026-04-27:042"

    def test_get_next_segment_number_no_existing(self):
        """无已有段时，从1开始"""
        result = get_next_segment_number(existing_doc_ids=[])
        assert result == 1

    def test_get_next_segment_number_with_existing(self):
        """有已有段时，返回最大段号+1"""
        existing = [
            "refined:2026-04-27:001",
            "refined:2026-04-27:002",
            "refined:2026-04-27:003",
        ]
        result = get_next_segment_number(existing_doc_ids=existing)
        assert result == 4

    def test_get_next_segment_number_filters_by_date(self):
        """只统计当天日期的段号"""
        existing = [
            "refined:2026-04-26:001",
            "refined:2026-04-26:002",
            "refined:2026-04-27:001",
        ]
        result = get_next_segment_number(
            existing_doc_ids=existing, date="2026-04-27"
        )
        assert result == 2


class TestFormatRefinedDocument:
    def test_format_empty_items(self):
        """无提炼内容时返回空字符串"""
        result = format_refined_document([], "2026-04-27", 1)
        assert result == ""

    def test_format_single_item(self):
        """单条提炼内容格式化"""
        items = [
            {
                "type": "偏好",
                "timestamp": "14:23:15",
                "content": "用户偏好 Rust 语言",
            }
        ]
        result = format_refined_document(items, "2026-04-27", 3)
        assert "[记忆提炼 2026-04-27 段3]" in result
        assert "## 14:23:15 偏好" in result
        assert "用户偏好 Rust 语言" in result

    def test_format_multiple_items(self):
        """多条提炼内容格式化"""
        items = [
            {"type": "偏好", "timestamp": "14:23:15", "content": "偏好暗色主题"},
            {"type": "技能", "timestamp": "15:01:08", "content": "换用新解析库处理PDF"},
        ]
        result = format_refined_document(items, "2026-04-27", 1)
        assert "## 14:23:15 偏好" in result
        assert "## 15:01:08 技能" in result


class TestSegmentSplitting:
    def test_split_within_limit(self):
        """内容在限制内时，不拆分"""
        items = [
            {"type": "偏好", "timestamp": "14:00:00", "content": "偏好A"},
            {"type": "技能", "timestamp": "15:00:00", "content": "技能B"},
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 1
        assert len(segments[0]) == 2

    def test_split_at_limit(self):
        """内容超过限制时，拆分为多段"""
        items = [
            {"type": "偏好", "timestamp": f"{10+i}:00:00", "content": f"内容{i}"}
            for i in range(25)
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 2
        assert len(segments[0]) == 20
        assert len(segments[1]) == 5

    def test_split_exact_limit(self):
        """内容恰好等于限制时，不拆分"""
        items = [
            {"type": "偏好", "timestamp": f"{10+i}:00:00", "content": f"内容{i}"}
            for i in range(20)
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 1
