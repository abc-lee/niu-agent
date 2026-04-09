#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Pattern Generator

根据 MCP 工具描述生成多样化的候选 query patterns
"""
import json
import sys
from pathlib import Path

# UTF-8 wrapper for stdout only (stderr usually handles UTF-8 natively)
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Add scripts/ to path so we can import query_pattern.tools
_script_dir = Path(__file__).parent  # e.g., .../scripts/query_pattern
sys.path.insert(0, str(_script_dir.parent))  # .../scripts
from query_pattern.tools import call_llm, logger


def generate_patterns_for_tool(
    server: str,
    tool: str,
    description: str,
    count: int = 12,
    failed_patterns: list = None
) -> list[dict]:
    """
    为单个工具生成候选 patterns

    Args:
        server: MCP 服务器名称
        tool: 工具名称
        description: 工具描述
        count: 目标数量
        failed_patterns: 失败反馈列表（可选）

    Returns:
        list of pattern dicts
    """
    # 读取 GENERATOR.md 作为提示词参考
    generator_prompt_path = Path(__file__).parent / "GENERATOR.md"
    if generator_prompt_path.exists():
        generator_instructions = generator_prompt_path.read_text(encoding="utf-8")
    else:
        generator_instructions = ""

    # 构建用户提示词（包含工具信息和生成要求）
    user_prompt = f"""# Task
Generate {count} diverse natural language query patterns for the following MCP tool.

## Tool Info
- Server: {server}
- Tool: {tool}
- Description: {description}

## Requirements
1. Output ONLY valid JSONL (one JSON per line), no markdown, no explanation
2. Each line must have: target_tool, content, variation_type, generative_note
3. Cover ALL 7 variation_type categories:
   - time_relative, time_absolute, action_verb, context_embedded, informal, question, negative
4. Mix Chinese and English expressions
5. Keep patterns SHORT (5-20 words)
6. Patterns must be semantically related to the tool's purpose

## Output Format
Each line:
{{"target_tool": "{server}/{tool}", "content": "your pattern here", "variation_type": "category", "generative_note": "why this pattern"}}

Generate {count} patterns now. Start directly with the first JSON line:
"""

    # 如果有失败反馈，追加到提示词
    if failed_patterns:
        user_prompt += "\n\n## Previous Failed Patterns (avoid these styles)\n"
        for fp in failed_patterns:
            user_prompt += f'- "{fp["content"]}" — reason: {fp.get("reason", "unknown")}\n'
        user_prompt += "\nPlease generate DIFFERENT patterns, avoid the failing styles above.\n"

    logger.info(f"[Generator] Generating {count} patterns for {server}/{tool}")

    # 调用 LLM
    response = call_llm(user_prompt, system=generator_instructions, temperature=0.9)

    if not response:
        logger.error(f"[Generator] LLM call failed for {server}/{tool}")
        return []

    # 解析 JSONL 输出
    patterns = []
    lines = response.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去掉可能的 markdown 代码块标记
        if line.startswith("```"):
            line = line.lstrip("`")
            line = line.replace("json", "", 1).strip()
        try:
            p = json.loads(line)
            if "content" in p and "target_tool" in p:
                patterns.append(p)
        except json.JSONDecodeError:
            logger.warning(f"[Generator] Failed to parse JSON: {line[:80]}")
            continue

    logger.info(f"[Generator] Generated {len(patterns)} patterns for {server}/{tool}")
    return patterns


def main():
    """主函数：生成所有 scheduler-server 工具的 patterns"""
    import argparse

    parser = argparse.ArgumentParser(description="Query Pattern Generator")
    parser.add_argument("--server", default="scheduler-server", help="MCP server name")
    parser.add_argument("--tool", help="Specific tool name (optional, generates all if not set)")
    parser.add_argument("--count", type=int, default=12, help="Patterns per tool")
    parser.add_argument("--output", default="candidates.jsonl", help="Output file")
    args = parser.parse_args()

    # Scheduler-server 工具定义
    TOOLS = {
        "schedule_task": "Create a one-time or recurring scheduled task with content, scheduled_at time, event_type, and optional cron_expr for recurrence",
        "cancel_task": "Cancel a scheduled task by task_id",
        "update_task": "Update an existing scheduled task's content, time, or cron expression",
        "list_scheduled_tasks": "Query scheduled task list, optionally filtered by status (pending/triggered/cancelled)",
    }

    output_path = Path(__file__).parent / args.output
    counter = 0
    all_patterns = []

    tools_to_generate = {args.tool: TOOLS[args.tool]} if args.tool else TOOLS

    for tool_name, tool_desc in tools_to_generate.items():
        patterns = generate_patterns_for_tool(
            server=args.server,
            tool=tool_name,
            description=tool_desc,
            count=args.count
        )

        for p in patterns:
            p["doc_id"] = f"pattern:{args.server.replace('-', '_')}:{tool_name}:{counter}"
            all_patterns.append(p)
            counter += 1

    # 写入 JSONL
    with open(output_path, "w", encoding="utf-8") as f:
        for p in all_patterns:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    logger.info(f"[Generator] Wrote {len(all_patterns)} patterns to {output_path}")
    print(f"Wrote {len(all_patterns)} patterns to {output_path}")


if __name__ == "__main__":
    main()
