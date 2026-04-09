#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Writer entry script for Query Pattern TDD Pipeline.

Reads candidates.jsonl and upserts each pattern into the vector database
with properly constructed metadata.
"""
import sys
import json
import argparse
from pathlib import Path

# UTF-8 wrapper for Windows (guard against double-wrapping)
if sys.platform == "win32":
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if not isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from tools import upsert_pattern


# refined_query mapping from WRITER.md
REFINED_QUERY_MAP: dict[str, str] = {
    "schedule_task": "schedule task",
    "cancel_task": "cancel scheduled task",
    "update_task": "update scheduled task",
    "list_scheduled_tasks": "list scheduled tasks",
}


def extract_tool_name(target_tool: str) -> str:
    """Extract tool name from target_tool string.

    Example: "scheduler-server/schedule_task" -> "schedule_task"
    """
    return target_tool.split("/")[-1]


def build_metadata(candidate: dict) -> dict:
    """Build complete L1 metadata for a pattern candidate."""
    tool_name = extract_tool_name(candidate["target_tool"])
    refined_query = REFINED_QUERY_MAP.get(tool_name, "")

    return {
        "level": "l1",
        "category": "query_pattern",
        "language": "en",
        "type": "query_pattern",
        "is_recursive": True,
        "refined_query": refined_query,
        "target_tool": candidate["target_tool"],
        "variation_type": candidate["variation_type"],
        "verified": False,
        "verified_score": None,
    }


def run(input_path: Path) -> dict[str, int]:
    """Read candidates.jsonl and upsert each pattern.

    Returns:
        dict with counts: total, success, failed
    """
    counts = {"total": 0, "success": 0, "failed": 0}

    if not input_path.exists():
        logger.error(f"[Writer] File not found: {input_path}")
        return counts

    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"[Writer] JSON decode error: {e}, skipping line")
                counts["total"] += 1
                counts["failed"] += 1
                continue

            doc_id = candidate.get("doc_id", "")
            content = candidate.get("content", "")
            metadata = build_metadata(candidate)

            counts["total"] += 1

            if upsert_pattern(doc_id, content, metadata):
                counts["success"] += 1
                logger.info(f"[Writer] Upserted: {doc_id}")
            else:
                counts["failed"] += 1
                logger.warning(f"[Writer] Failed: {doc_id}")

    return counts


def clean_old_patterns(db_path: str) -> None:
    """Delete all pattern records from vector database."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("DELETE FROM documents WHERE id LIKE 'pattern:%'")
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    logger.info(f"[Writer] Cleaned {deleted} old pattern records")


def main() -> None:
    parser = argparse.ArgumentParser(description="Writer: upsert query patterns to vector DB")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to candidates.jsonl (default: <script_dir>/candidates.jsonl)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all existing pattern:* records before writing",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    input_path = args.input or (script_dir / "candidates.jsonl")

    if args.clean:
        from tools import get_vector_db_path
        db_path = get_vector_db_path()
        logger.info(f"[Writer] Cleaning patterns from: {db_path}")
        clean_old_patterns(db_path)

    logger.info(f"[Writer] Reading candidates from: {input_path}")
    counts = run(input_path)

    logger.info(
        f"[Writer] Done — total={counts['total']}, "
        f"success={counts['success']}, failed={counts['failed']}"
    )

    if counts["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
