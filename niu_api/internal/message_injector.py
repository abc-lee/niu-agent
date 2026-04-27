"""
Message Injector — Refined document incremental segment injection.

entity-extractor 提炼的精炼文档按时间段增量注入 LightRAG，
每段独立 doc_id，不删除重注入。
"""

from typing import List, Optional


def generate_doc_id(date: str, seq: int) -> str:
    """生成精炼文档的 doc_id。

    格式: refined:{date}:{seq:03d}
    例如: refined:2026-04-27:001
    """
    return f"refined:{date}:{seq:03d}"


def get_next_segment_number(
    existing_doc_ids: List[str],
    date: Optional[str] = None,
) -> int:
    """根据已有 doc_id 列表，返回下一个段号。

    Args:
        existing_doc_ids: 已有的精炼文档 doc_id 列表。
        date: 日期过滤，只统计该日期的段号。None 则统计所有。

    Returns:
        下一个段号（从1开始）。
    """
    max_seq = 0
    prefix = f"refined:{date}:" if date else "refined:"

    for doc_id in existing_doc_ids:
        if not doc_id.startswith(prefix):
            continue
        parts = doc_id.split(":")
        if len(parts) >= 3:
            try:
                seq = int(parts[-1])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                continue

    return max_seq + 1


def format_refined_document(
    items: List[dict],
    date: str,
    segment: int,
) -> str:
    """将提炼内容格式化为精炼文档。

    Args:
        items: 提炼内容列表，每项包含 type, timestamp, content。
        date: 日期字符串。
        segment: 段序号。

    Returns:
        格式化的精炼文档字符串。
    """
    if not items:
        return ""

    lines = [f"[记忆提炼 {date} 段{segment}]", ""]

    for item in items:
        item_type = item.get("type", "记忆")
        timestamp = item.get("timestamp", "")
        content = item.get("content", "")
        lines.append(f"## {timestamp} {item_type}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


def split_into_segments(
    items: List[dict],
    max_items_per_segment: int = 20,
) -> List[List[dict]]:
    """将提炼内容按条数拆分为多段。

    Args:
        items: 提炼内容列表。
        max_items_per_segment: 每段最大条数。

    Returns:
        拆分后的段列表，每段是一个 items 子列表。
    """
    if not items:
        return []

    segments = []
    for i in range(0, len(items), max_items_per_segment):
        segments.append(items[i : i + max_items_per_segment])
    return segments
