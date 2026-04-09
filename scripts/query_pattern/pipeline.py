#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query Pattern TDD Pipeline — Main Controller.

Orchestrates the generate → write → test loop for each tool, with retry
support. Failed patterns from the tester are fed back to the generator so
the next attempt produces different variations.

Pipeline per tool:
    retry = 0
    while retry <= MAX_RETRIES:
        patterns = generate(...)          # step1
        write_patterns(patterns)          # step2: candidates.jsonl + upsert
        result = test_patterns(...)       # step3: reads candidates.jsonl
        if result["failed"] == 0:
            break
        retry += 1

Output files (all in scripts/query_pattern/):
    candidates.jsonl        — latest candidate patterns (overwritten each attempt)
    verified_patterns.jsonl — patterns that passed testing
    failed_patterns.jsonl   — patterns that failed testing
"""
import sys
import json
from pathlib import Path

# UTF-8 wrapper for Windows (guard against double-wrapping)
if sys.platform == "win32":
    import io

    if not isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if not isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ── path setup ──────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).parent  # .../scripts/query_pattern/
# Add scripts/ so that "query_pattern" resolves to _SCRIPT_DIR
sys.path.insert(0, str(_SCRIPT_DIR.parent))          # scripts/
sys.path.insert(1, str(_SCRIPT_DIR.parent.parent))    # project root

from loguru import logger

# ── step imports ────────────────────────────────────────────────────────────
# step1_generate is a plain module → direct import works
from query_pattern.step1_generate import generate_patterns_for_tool
# step3_test is a plain module → direct import works
from query_pattern.step3_test import test_patterns
# step2_write has "from tools import upsert_pattern" at module level, which
# fails (agent/tools.py does not exist).  We bypass step2_write entirely and
# import upsert_pattern directly from query_pattern.tools.  Our own
# _clean_patterns_from_db and _write_and_upsert provide the Step-2 logic.
from query_pattern.tools import upsert_pattern as _upsert_pattern

# ── configuration ──────────────────────────────────────────────────────────
MAX_RETRIES = 3
PATTERNS_PER_TOOL = 12
SCORE_THRESHOLD = 0.5
SERVER = "scheduler-server"

TOOLS: dict[str, str] = {
    "schedule_task": (
        "Create a one-time or recurring scheduled task with content, "
        "scheduled_at time, event_type, and optional cron_expr for recurrence"
    ),
    "cancel_task": "Cancel a scheduled task by task_id",
    "update_task": (
        "Update an existing scheduled task's content, time, or cron expression"
    ),
    "list_scheduled_tasks": (
        "Query scheduled task list, optionally filtered by "
        "status (pending/triggered/cancelled)"
    ),
}

CANDIDATES_PATH = _SCRIPT_DIR / "candidates.jsonl"


def _clean_patterns_from_db(namespace: str) -> int:
    """Delete pattern records for a given namespace from vector DB.

    Args:
        namespace: prefix match on doc_id, e.g. "pattern:scheduler_server"

    Returns:
        Number of records deleted.
    """
    import sqlite3

    from query_pattern.tools import get_vector_db_path

    db_path = get_vector_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        f"DELETE FROM documents WHERE id LIKE 'pattern:{namespace}_%'"
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def _write_candidates(patterns: list[dict]) -> int:
    """Write pattern list to candidates.jsonl.

    Returns the number of lines written.
    """
    with open(CANDIDATES_PATH, "w", encoding="utf-8") as f:
        for p in patterns:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return len(patterns)


# refined_query mapping mirrored from step2_write.py
_REFINED_QUERY_MAP: dict[str, str] = {
    "schedule_task": "schedule task",
    "cancel_task": "cancel scheduled task",
    "update_task": "update scheduled task",
    "list_scheduled_tasks": "list scheduled tasks",
}


def _extract_tool_name(target_tool: str) -> str:
    """Extract tool name from target_tool string.

    Example: "scheduler-server/schedule_task" -> "schedule_task"
    """
    return target_tool.split("/")[-1]


def _build_metadata(candidate: dict) -> dict:
    """Build L1 metadata for a pattern candidate (mirrors step2_write.build_metadata)."""
    tool_name = _extract_tool_name(candidate["target_tool"])
    refined_query = _REFINED_QUERY_MAP.get(tool_name, "")
    return {
        "level": "l1",
        "category": "query_pattern",
        "language": "en",
        "type": "query_pattern",
        "is_recursive": True,
        "refined_query": refined_query,
        "target_tool": candidate.get("target_tool", ""),
        "variation_type": candidate.get("variation_type", "unknown"),
        "verified": False,
        "verified_score": None,
    }


def _write_and_upsert(input_path: Path) -> dict[str, int]:
    """Read candidates.jsonl and upsert each pattern to the vector DB.

    Mirrors step2_write.run() but uses _upsert_pattern from query_pattern.tools
    directly (bypasses step2_write's broken "from tools import" import).

    Returns:
        dict with total, success, failed counts.
    """
    counts: dict[str, int] = {"total": 0, "success": 0, "failed": 0}
    if not input_path.exists():
        logger.error(f"[Pipeline/Writer] File not found: {input_path}")
        return counts

    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"[Pipeline/Writer] JSON decode error: {e}")
                counts["total"] += 1
                counts["failed"] += 1
                continue

            if "content" not in candidate or "variation_type" not in candidate:
                logger.warning(
                    f"[Pipeline/Writer] Skipping malformed candidate: "
                    f"{str(candidate)[:80]}"
                )
                counts["total"] += 1
                counts["failed"] += 1
                continue

            doc_id = candidate.get("doc_id", "")
            content = candidate.get("content", "")
            metadata = _build_metadata(candidate)
            counts["total"] += 1

            if _upsert_pattern(doc_id, content, metadata):
                counts["success"] += 1
                logger.debug(f"[Pipeline/Writer] Upserted: {doc_id}")
            else:
                counts["failed"] += 1
                logger.warning(f"[Pipeline/Writer] Failed: {doc_id}")

    return counts


def pipeline_for_tool(server: str, tool: str, description: str) -> dict:
    """Run generate → write → test for a single tool with retry support.

    Args:
        server: MCP server name (e.g. "scheduler-server")
        tool: Tool name (e.g. "schedule_task")
        description: Human-readable tool description

    Returns:
        Summary dict:
            tool, verified (int), total (int), hit_rate (float),
            retries (int), status (str: "success" | "exhausted" | "error")
    """
    namespace = server.replace("-", "_")
    retry = 0
    all_verified = 0
    total_attempted = 0

    while retry <= MAX_RETRIES:
        logger.info(
            f"[Pipeline] {server}/{tool} — attempt {retry + 1} "
            f"(max {MAX_RETRIES + 1} total)"
        )

        # ── Step 1: generate ──────────────────────────────────────────────
        failed_feedback = None
        if retry > 0 and retry - 1 < retry:
            # Load failed patterns from the previous attempt for feedback
            failed_path = _SCRIPT_DIR / "failed_patterns.jsonl"
            if failed_path.exists():
                failed_list = []
                with open(failed_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                failed_list.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                if failed_list:
                    failed_feedback = failed_list
                    logger.info(
                        f"[Pipeline] {server}/{tool} — "
                        f"{len(failed_list)} failed patterns as feedback"
                    )

        patterns = generate_patterns_for_tool(
            server=server,
            tool=tool,
            description=description,
            count=PATTERNS_PER_TOOL,
            failed_patterns=failed_feedback,
        )

        if not patterns:
            logger.error(f"[Pipeline] {server}/{tool} — no patterns generated, abort")
            return {
                "tool": f"{server}/{tool}",
                "verified": 0,
                "total": 0,
                "hit_rate": 0.0,
                "retries": retry,
                "status": "error",
            }

        # ── Step 1b: write candidates.jsonl ───────────────────────────────
        written = _write_candidates(patterns)
        logger.info(f"[Pipeline] {server}/{tool} — wrote {written} candidates")

        # ── Step 2: upsert to DB ──────────────────────────────────────────
        # Clean old patterns for this tool only on first attempt
        if retry == 0:
            deleted = _clean_patterns_from_db(namespace)
            logger.info(
                f"[Pipeline] {server}/{tool} — cleaned {deleted} old DB records"
            )

        db_result = _write_and_upsert(CANDIDATES_PATH)
        logger.info(
            f"[Pipeline] {server}/{tool} — DB upsert: "
            f"{db_result['success']}/{db_result['total']} succeeded"
        )

        # ── Step 3: test ──────────────────────────────────────────────────
        test_result = test_patterns(CANDIDATES_PATH, threshold=SCORE_THRESHOLD)

        passed = test_result.get("passed", 0)
        failed = test_result.get("failed", 0)
        total = test_result.get("total", 0)

        logger.info(
            f"[Pipeline] {server}/{tool} — test: "
            f"{passed}/{total} passed, {failed} failed"
        )

        all_verified += passed
        total_attempted += total

        if failed == 0:
            logger.info(f"[Pipeline] {server}/{tool} — all passed on attempt {retry + 1}")
            break

        retry += 1
        if retry > MAX_RETRIES:
            logger.warning(
                f"[Pipeline] {server}/{tool} — exhausted retries ({MAX_RETRIES}), "
                f"moving on"
            )
            break

    hit_rate = (all_verified / total_attempted * 100) if total_attempted > 0 else 0.0
    return {
        "tool": f"{server}/{tool}",
        "verified": all_verified,
        "total": total_attempted,
        "hit_rate": round(hit_rate, 1),
        "retries": retry,
        "status": "success" if failed == 0 else "exhausted",
    }


def _print_summary(results: list[dict]) -> None:
    """Print a formatted summary table."""
    print("\n" + "=" * 70)
    print("  QUERY PATTERN TDD PIPELINE — SUMMARY")
    print("=" * 70)
    print(
        f"  {'Tool':<35} {'Verified':>8} {'Total':>6} {'Rate':>7} {'Retries':>7} {'Status':>10}"
    )
    print("-" * 70)

    total_verified = 0
    total_attempted = 0

    for r in results:
        print(
            f"  {r['tool']:<35} "
            f"{r['verified']:>8} "
            f"{r['total']:>6} "
            f"{r['hit_rate']:>6.1f}% "
            f"{r['retries']:>7} "
            f"{r['status']:>10}"
        )
        total_verified += r["verified"]
        total_attempted += r["total"]

    print("-" * 70)
    overall_rate = (total_verified / total_attempted * 100) if total_attempted > 0 else 0.0
    print(
        f"  {'TOTAL':<35} "
        f"{total_verified:>8} "
        f"{total_attempted:>6} "
        f"{overall_rate:>6.1f}%"
    )
    print("=" * 70 + "\n")

    # Print output file paths
    print(f"  Candidates  : {CANDIDATES_PATH}")
    print(
        f"  Verified    : {_SCRIPT_DIR / 'verified_patterns.jsonl'} "
        f"({sum(1 for _ in open(_SCRIPT_DIR / 'verified_patterns.jsonl', encoding='utf-8'))} lines)"
    )
    print(
        f"  Failed      : {_SCRIPT_DIR / 'failed_patterns.jsonl'} "
        f"({sum(1 for _ in open(_SCRIPT_DIR / 'failed_patterns.jsonl', encoding='utf-8'))} lines)"
    )
    print()


def main() -> None:
    print("\n[Pipeline] Starting Query Pattern TDD Pipeline")
    print(f"[Pipeline] Server   : {SERVER}")
    print(f"[Pipeline] Tools    : {len(TOOLS)}")
    print(f"[Pipeline] Patterns : {PATTERNS_PER_TOOL} per tool")
    print(f"[Pipeline] Retries  : {MAX_RETRIES} max")
    print(f"[Pipeline] Threshold : {SCORE_THRESHOLD}")
    print()

    results: list[dict] = []

    for tool, description in TOOLS.items():
        result = pipeline_for_tool(SERVER, tool, description)
        results.append(result)

    _print_summary(results)

    # Exit code: non-zero if any tool exhausted retries
    exhausted = [r for r in results if r["status"] == "exhausted"]
    if exhausted:
        print(f"[Pipeline] {len(exhausted)} tool(s) exhausted retries — check output.")
        sys.exit(1)
    else:
        print("[Pipeline] All tools passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
