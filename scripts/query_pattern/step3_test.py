#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query Pattern Tester - Entry script.

Tests each pattern from candidates.jsonl using recursive_search().
Writes verified_patterns.jsonl and failed_patterns.jsonl, then reports
statistics grouped by variation_type.
"""
import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict

# UTF-8 wrapper for Windows
if sys.platform == "win32":
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if not isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from scripts.query_pattern.tools import recursive_search

SCRIPT_DIR = Path(__file__).parent
DEFAULT_INPUT = SCRIPT_DIR / "candidates.jsonl"
VERIFIED_OUTPUT = SCRIPT_DIR / "verified_patterns.jsonl"
FAILED_OUTPUT = SCRIPT_DIR / "failed_patterns.jsonl"
SCORE_THRESHOLD = 0.5


def _report_by_type(records: list[dict]) -> dict:
    """Group results by variation_type and compute pass rates."""
    groups: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "total": 0})
    for r in records:
        vtype = r.get("variation_type") or "unknown"
        groups[vtype]["total"] += 1
        if r["passed"]:
            groups[vtype]["passed"] += 1
        else:
            groups[vtype]["failed"] += 1

    stats = {}
    for vtype, counts in sorted(groups.items()):
        total = counts["total"]
        passed = counts["passed"]
        rate = (passed / total * 100) if total > 0 else 0.0
        stats[vtype] = {
            "total": total,
            "passed": passed,
            "failed": counts["failed"],
            "pass_rate": round(rate, 1),
        }
    return stats


def test_patterns(input_path: str | Path, threshold: float = SCORE_THRESHOLD) -> dict:
    """Test each pattern from candidates.jsonl using recursive_search().

    Args:
        input_path: Path to candidates.jsonl.
        threshold: Minimum score to consider a result valid.

    Returns:
        Summary dict with passed/failed/total counts and per-type stats.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        logger.error(f"[Tester] Input file not found: {input_path}")
        return {"passed": 0, "failed": 0, "total": 0, "by_type": {}}

    verified: list[dict] = []
    failed: list[dict] = []

    with open(input_path, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"[Tester] Line {line_idx}: skipped invalid JSON")
                continue

            content = p.get("content", "")
            target_tool = p.get("target_tool", "")  # e.g. "scheduler-server/schedule_task"
            doc_id = p.get("doc_id", "")
            variation_type = p.get("variation_type", "")

            if not content:
                logger.warning(f"[Tester] Line {line_idx}: empty content, skipping")
                continue

            # Run recursive search
            results, top_score = recursive_search(content, min_score=0.2)

            # Extract tool name from target (e.g. "schedule_task" from "scheduler-server/schedule_task")
            tool_name = target_tool.split("/")[-1] if target_tool else ""

            # Determine if correct tool was matched
            is_correct = False
            matched_id = ""
            matched_tool_name = ""

            if results:
                matched_id = getattr(results[0], "id", "") or ""
                matched_tool_name = (
                    getattr(results[0], "metadata", {}).get("name", "")
                    if isinstance(results[0].metadata, dict)
                    else ""
                )
                is_correct = (
                    tool_name in matched_id or tool_name in matched_tool_name
                )

            passed = bool(top_score >= threshold and is_correct)

            record = {
                "pattern_id": doc_id,
                "content": content,
                "target_tool": target_tool,
                "recursion_score": round(top_score, 4),
                "matched_id": matched_id,
                "matched_tool_name": matched_tool_name,
                "passed": passed,
                "variation_type": variation_type,
            }

            if passed:
                verified.append(record)
            else:
                if top_score < threshold:
                    record["reason"] = "score below threshold"
                elif not is_correct:
                    record["reason"] = "wrong tool matched"
                else:
                    record["reason"] = "unknown"
                failed.append(record)

    # Write output files
    with open(VERIFIED_OUTPUT, "w", encoding="utf-8") as f:
        for r in verified:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(FAILED_OUTPUT, "w", encoding="utf-8") as f:
        for r in failed:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    all_records = verified + failed
    by_type = _report_by_type(all_records)

    # Print summary
    total = len(all_records)
    passed_count = len(verified)
    failed_count = len(failed)
    overall_rate = (passed_count / total * 100) if total > 0 else 0.0

    print("\n" + "=" * 60)
    print("  QUERY PATTERN TESTER SUMMARY")
    print("=" * 60)
    print(f"  Input    : {input_path}")
    print(f"  Threshold: {threshold}")
    print(f"  Total    : {total}")
    print(f"  Passed   : {passed_count} ({overall_rate:.1f}%)")
    print(f"  Failed   : {failed_count} ({100 - overall_rate:.1f}%)")
    # Use ASCII bar for Windows compatibility
    bar_char = "#" if sys.platform == "win32" else "█"
    print("-" * 60)
    print("  By Variation Type:")
    for vtype, stats in by_type.items():
        bar = bar_char * int(stats["pass_rate"] / 10)
        print(
            f"    [{stats['pass_rate']:5.1f}%] {vtype:<25} "
            f"{stats['passed']:>3}/{stats['total']:<3}  {bar}"
        )
    print("-" * 60)
    print(f"  Verified : {VERIFIED_OUTPUT} ({passed_count} lines)")
    print(f"  Failed   : {FAILED_OUTPUT} ({failed_count} lines)")
    print("=" * 60 + "\n")

    return {
        "passed": passed_count,
        "failed": failed_count,
        "total": total,
        "by_type": by_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Query Pattern Tester")
    parser.add_argument(
        "--input",
        type=str,
        default=str(DEFAULT_INPUT),
        help="Path to candidates.jsonl (default: candidates.jsonl in script dir)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=SCORE_THRESHOLD,
        help=f"Minimum score threshold (default: {SCORE_THRESHOLD})",
    )
    args = parser.parse_args()

    result = test_patterns(args.input, args.threshold)

    # Exit code reflects pass/fail count
    if result["failed"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
