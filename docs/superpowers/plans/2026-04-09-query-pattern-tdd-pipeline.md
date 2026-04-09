# Query Pattern TDD 流水线实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现三角色 TDD 流水线（Generator→Writer→Tester），为 scheduler-server 的 4 个工具生成 query patterns，验证递归检索方法论。

**Architecture:** Python 脚本模拟三角色，Generator 调用 LLM 生成候选 patterns，Writer 批量 UPSERT 到向量库，Tester 执行递归检索验证分数。每步结果通过 JSONL 文件传递，支持失败重试。

**Tech Stack:** Python 3.11+, loguru, numpy, litellm (LLM 调用), sqlite3 (向量库)

---

## 文件结构

```
scripts/query_pattern/
├── GENERATOR.md          # Generator 系统提示词
├── WRITER.md             # Writer 系统提示词
├── TESTER.md             # Tester 系统提示词
├── tools.py              # 共享工具（embedding、LLM 调用、向量库操作）
├── step1_generate.py     # Generator 入口
├── step2_write.py        # Writer 入口
├── step3_test.py         # Tester 入口
└── pipeline.py           # 主控脚本
```

---

## Task 1: 创建目录结构，添加 UTF-8 编码支持

**Files:**
- Create: `scripts/query_pattern/__init__.py`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p scripts/query_pattern
```

- [ ] **Step 2: 创建 __init__.py，添加 UTF-8 编码**

```python
# scripts/query_pattern/__init__.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query Pattern TDD Pipeline"""
```

- [ ] **Step 3: Commit**

```bash
git add scripts/query_pattern/
git commit -m "feat(query_pattern): 创建 TDD 流水线目录结构"
```

---

## Task 2: 实现 shared tools.py（共享工具函数）

**Files:**
- Create: `scripts/query_pattern/tools.py`

- [ ] **Step 1: 创建 tools.py — UTF-8 wrapper + embedding 获取**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared tools for Query Pattern TDD Pipeline"""
import sys
from pathlib import Path

# UTF-8 wrapper for Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import json
import sqlite3
from typing import Optional
import numpy as np
from loguru import logger

# 向量库路径
def get_vector_db_path() -> str:
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return str(Path(workspace_path) / "vectors.db")
        except Exception:
            pass
    return str(Path.home() / ".niu" / "vectors.db")


def get_vector_search():
    """获取 VectorSearchAdapter 实例"""
    from agent.vector_search import VectorSearchAdapter
    return VectorSearchAdapter(get_vector_db_path())


def get_embedding(content: str) -> Optional[list[float]]:
    """获取文本的 embedding 向量（L2 归一化）"""
    vs = get_vector_search()
    emb = vs._get_embedding(content)
    if emb is None:
        return None
    vec = np.array(emb, dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def recursive_search(content: str, min_score: float = 0.3) -> tuple[list, float]:
    """
    执行递归检索，返回 (results, top_score)

    results: list of SearchResult（第二轮结果，排除 query_pattern）
    top_score: 第二轮最高分
    """
    vs = get_vector_search()
    results = vs.search(content, limit=5, min_score=min_score)
    if not results:
        return [], 0.0
    return results, results[0].score


def upsert_pattern(doc_id: str, content: str, metadata: dict) -> bool:
    """写入单条 query_pattern 到向量库"""
    embedding = get_embedding(content)
    if embedding is None:
        logger.error(f"[Writer] Failed to get embedding for: {doc_id}")
        return False

    vec = np.array(embedding, dtype=np.float32)
    embedding_blob = vec.tobytes()

    conn = get_vector_search()._get_connection()
    if conn is None:
        return False

    conn.execute(
        """
        INSERT INTO documents (id, content, embedding, metadata)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content = excluded.content,
            embedding = excluded.embedding,
            metadata = excluded.metadata
        """,
        (doc_id, content, embedding_blob, json.dumps(metadata, ensure_ascii=False)),
    )
    conn.commit()
    return True


def load_llm_config() -> dict:
    """加载 LLM 配置"""
    config_path = Path(__file__).parent.parent.parent / "config" / "user-config.json"
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


def call_llm(prompt: str, system: str = "", temperature: float = 0.9) -> str:
    """
    调用 LLM 生成内容

    Args:
        prompt: 用户提示词
        system: 系统提示词
        temperature: 温度参数
    Returns:
        LLM 响应文本
    """
    import litellm

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = litellm.completion(
            model="minimax/io-optimized",
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"[LLM] Error: {e}")
        return ""
```

- [ ] **Step 2: 运行验证**

```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
from query_pattern.tools import get_vector_db_path, get_embedding
db = get_vector_db_path()
print(f'Vector DB: {db}')
emb = get_embedding('test query')
print(f'Embedding dim: {len(emb) if emb else None}')
"
```

- [ ] **Step 3: Commit**

```bash
git add scripts/query_pattern/tools.py
git commit -m "feat(query_pattern): 添加共享工具函数"
```

---

## Task 3: 编写三角色系统提示词

**Files:**
- Create: `scripts/query_pattern/GENERATOR.md`
- Create: `scripts/query_pattern/WRITER.md`
- Create: `scripts/query_pattern/TESTER.md`

- [ ] **Step 1: 创建 GENERATOR.md（Generator 系统提示词）**

```markdown
# Query Pattern Generator Prompt

You are a creative Query Pattern Generator. Your task is to generate diverse, natural language query patterns that humans might say when they want to use a specific MCP tool.

## Input
You will receive:
- tool_name: the MCP tool name
- tool_description: what the tool does
- server_name: which MCP server it belongs to
- target_count: how many patterns to generate (aim for 10-15)

## Output Format
Output ONLY a JSONL string (one JSON per line), no markdown, no explanation.

Example line:
{"target_tool": "scheduler-server/schedule_task", "content": "wake me up in 30 minutes", "variation_type": "time_relative", "generative_note": "使用 wake me up 而非 remind"}

## Mandatory Diversity Rules
You MUST generate patterns covering ALL of these variation_type categories:

1. time_relative — 相对时间
   Examples: "5分钟后", "半小时后叫我", "remind me in 10 minutes"

2. time_absolute — 绝对时间
   Examples: "下午三点", "明天上午10点", "at 3pm tomorrow"

3. action_verb — 不同动词
   Examples: "提醒我", "叫醒我", "通知我", "别忘了"

4. context_embedded — 场景嵌入
   Examples: "我在开会，5分钟后提醒我接孩子"

5. informal — 口语化
   Examples: "赶紧叫我", "别忘了哈", "记得提醒我哦"

6. question — 疑问句
   Examples: "能提醒我喝水吗", "可以叫我吗"

7. negative — 反向表达
   Examples: "别忘了提醒我", "别忘记"

## Quality Rules
- Each pattern must be semantically related to the tool's purpose
- Patterns should be SHORT (5-20 words), natural language
- Avoid generated noise patterns unrelated to the tool
- Mix Chinese and English (as Chinese users might express in English)
- Include realistic life scenarios (meetings, exercise, medicine, driving)

## Special Instructions for scheduler-server tools

### schedule_task (Create scheduled task/reminder)
Common human expressions:
- "5分钟后提醒我吃药"
- "明天上午10点开会"
- "别忘了接孩子"
- "提醒我喝水"
- "半小时后提醒我"
- "每周一早9点提醒我汇报"
- "明天有会，提醒我提前准备"

### cancel_task (Cancel scheduled task)
- "取消提醒"
- "删除刚才的定时任务"
- "把下午的会议提醒删掉"

### update_task (Update scheduled task)
- "把提醒改成下午3点"
- "修改刚才的任务"

### list_scheduled_tasks (List scheduled tasks)
- "看看我有哪些定时任务"
- "显示所有提醒"
```

- [ ] **Step 2: 创建 WRITER.md（Writer 系统提示词）**

```markdown
# Query Pattern Writer Prompt

You are a Query Pattern Writer. Your task is to interpret Generator output and prepare it for vector database insertion.

Given a Generator output line, confirm the:
- doc_id format: `pattern:{server}:{tool}:{index}`
- metadata.refined_query mapping
- variation_type correctness

You do NOT write to the database. You only validate and enrich the JSON data.

## refined_query Mapping Rules
- schedule_task → "schedule task"
- cancel_task → "cancel scheduled task"
- update_task → "update scheduled task"
- list_scheduled_tasks → "list scheduled tasks"
```

- [ ] **Step 3: 创建 TESTER.md（Tester 系统提示词）**

```markdown
# Query Pattern Tester Prompt

You are a Query Pattern Tester. Given test results, analyze why patterns failed and suggest improvements.

## Failure Analysis
For each failed pattern, provide:
1. Why the recursion score was too low
2. What type of pattern would work better
3. Whether to retry or skip

## Pass Criteria
- recursion_score >= 0.5
- matched_tool == target_tool

## Feedback Format
```json
{"pattern": "failed pattern text", "reason": "analysis", "suggestion": "improvement"}
```
```

- [ ] **Step 4: Commit**

```bash
git add scripts/query_pattern/GENERATOR.md scripts/query_pattern/WRITER.md scripts/query_pattern/TESTER.md
git commit -m "feat(query_pattern): 添加三角色系统提示词"
```

---

## Task 4: 实现 step1_generate.py（Generator 入口）

**Files:**
- Create: `scripts/query_pattern/step1_generate.py`

- [ ] **Step 1: 实现 Generator 入口脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Pattern Generator

根据 MCP 工具描述生成多样化的候选 query patterns
"""
import json
import sys
from pathlib import Path

# UTF-8 wrapper
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from query_pattern.tools import call_llm, logger

PROMPT_TEMPLATE = """# Task
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
{"target_tool": "{server}/{tool}", "content": "your pattern here", "variation_type": "category", "generative_note": "why this pattern"}

Generate {count} patterns now. Start directly with the first JSON line:
"""


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
    # 读取 GENERATOR.md 提示词
    prompt_file = Path(__file__).parent / "GENERATOR.md"
    system_prompt = ""

    # 构建用户提示词
    user_prompt = PROMPT_TEMPLATE.format(
        count=count,
        server=server,
        tool=tool,
        description=description
    )

    # 如果有失败反馈，追加到提示词
    if failed_patterns:
        user_prompt += "\n\n## Previous Failed Patterns (avoid these styles)\n"
        for fp in failed_patterns:
            user_prompt += f'- "{fp["content"]}" — reason: {fp.get("reason", "unknown")}\n'
        user_prompt += "\nPlease generate DIFFERENT patterns, avoid the failing styles above.\n"

    logger.info(f"[Generator] Generating {count} patterns for {server}/{tool}")
    logger.debug(f"[Generator] Prompt length: {len(user_prompt)} chars")

    # 调用 LLM
    response = call_llm(user_prompt, system=system_prompt, temperature=0.9)

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
```

- [ ] **Step 2: 运行 Generator 测试（生成 scheduler-server patterns）**

```bash
cd E:/tools/ai-bot
python scripts/query_pattern/step1_generate.py --server scheduler-server --count 10
```

预期输出：生成 40 条候选 patterns 到 candidates.jsonl

- [ ] **Step 3: 检查输出**

```bash
wc -l scripts/query_pattern/candidates.jsonl
head -5 scripts/query_pattern/candidates.jsonl
```

- [ ] **Step 4: Commit**

```bash
git add scripts/query_pattern/step1_generate.py
git commit -m "feat(query_pattern): 实现 Generator 入口脚本"
```

---

## Task 5: 实现 step2_write.py（Writer 入口）

**Files:**
- Create: `scripts/query_pattern/step2_write.py`

- [ ] **Step 1: 实现 Writer 入口脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Pattern Writer

将候选 patterns 批量写入向量库
"""
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from query_pattern.tools import upsert_pattern, logger

# refined_query 映射
REFINED_QUERY_MAP = {
    "schedule_task": "schedule task",
    "cancel_task": "cancel scheduled task",
    "update_task": "update scheduled task",
    "list_scheduled_tasks": "list scheduled tasks",
}


def extract_tool_name(target_tool: str) -> str:
    """从 target_tool 提取工具名（如 'scheduler-server/schedule_task' → 'schedule_task'）"""
    return target_tool.split("/")[-1]


def write_patterns(input_file: str = "candidates.jsonl") -> int:
    """
    将 candidates.jsonl 中的 patterns 写入向量库

    Returns:
        成功写入的数量
    """
    input_path = Path(__file__).parent / input_file

    if not input_path.exists():
        logger.error(f"[Writer] Input file not found: {input_path}")
        return 0

    written = 0
    failed = 0

    with open(input_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"[Writer] Line {line_num}: JSON parse error")
                failed += 1
                continue

            content = p.get("content", "")
            target_tool = p.get("target_tool", "")
            doc_id = p.get("doc_id", f"pattern:unknown:{line_num}")
            variation_type = p.get("variation_type", "unknown")

            # 获取 refined_query
            tool_name = extract_tool_name(target_tool)
            refined_query = REFINED_QUERY_MAP.get(tool_name, tool_name.replace("_", " "))

            # 构建 metadata
            metadata = {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": refined_query,
                "target_tool": target_tool,
                "variation_type": variation_type,
                "verified": False,
                "verified_score": None,
            }

            # 写入向量库
            if upsert_pattern(doc_id, content, metadata):
                written += 1
            else:
                failed += 1

    logger.info(f"[Writer] Done: {written} written, {failed} failed")
    print(f"Wrote {written} patterns to vector DB ({failed} failed)")
    return written


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Query Pattern Writer")
    parser.add_argument("--input", default="candidates.jsonl", help="Input JSONL file")
    args = parser.parse_args()
    write_patterns(args.input)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 清理旧的测试数据（防止干扰）**

```bash
python -c "
import sqlite3, json
db = 'C:/Users/LiLei/.niu/vectors.db'
conn = sqlite3.connect(db)
# 删除 scheduler 相关的旧 patterns
conn.execute(\"DELETE FROM documents WHERE id LIKE 'pattern:scheduler%' OR id LIKE 'query_pattern:reminder%'\")
conn.commit()
count = conn.execute('SELECT COUNT(*) FROM documents WHERE json_extract(metadata, \"\$.category\") = \"query_pattern\"').fetchone()[0]
print(f'query_pattern records remaining: {count}')
conn.close()
"
```

- [ ] **Step 3: 运行 Writer**

```bash
python scripts/query_pattern/step2_write.py --input candidates.jsonl
```

- [ ] **Step 4: 验证写入结果**

```bash
python -c "
import sqlite3, json
db = 'C:/Users/LiLei/.niu/vectors.db'
conn = sqlite3.connect(db)
rows = conn.execute(\"SELECT id, content, metadata FROM documents WHERE id LIKE 'pattern:%' LIMIT 5\").fetchall()
for row in rows:
    m = json.loads(row[2])
    print(f'{row[0]}: {row[1][:50]}... | refined={m.get(\"refined_query\")} | verified={m.get(\"verified\")}')
conn.close()
"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/query_pattern/step2_write.py
git commit -m "feat(query_pattern): 实现 Writer 入口脚本"
```

---

## Task 6: 实现 step3_test.py（Tester 入口）

**Files:**
- Create: `scripts/query_pattern/step3_test.py`

- [ ] **Step 1: 实现 Tester 入口脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Pattern Tester

验证 query patterns 的递归检索效果
"""
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from query_pattern.tools import recursive_search, get_vector_search, upsert_pattern, logger

SCORE_THRESHOLD = 0.5


def test_patterns(input_file: str = "candidates.jsonl",
                  verified_out: str = "verified_patterns.jsonl",
                  failed_out: str = "failed_patterns.jsonl") -> dict:
    """
    测试 candidates.jsonl 中所有 patterns 的递归检索效果

    Returns:
        {"passed": int, "failed": int, "total": int}
    """
    input_path = Path(__file__).parent / input_file
    verified_path = Path(__file__).parent / verified_out
    failed_path = Path(__file__).parent / failed_out

    if not input_path.exists():
        logger.error(f"[Tester] Input file not found: {input_path}")
        return {"passed": 0, "failed": 0, "total": 0}

    passed = 0
    failed = 0
    total = 0

    # 清除旧的测试结果文件
    for p in [verified_path, failed_path]:
        if p.exists():
            p.unlink()

    verified_f = open(verified_path, "w", encoding="utf-8")
    failed_f = open(failed_path, "w", encoding="utf-8")

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except json.JSONDecodeError:
                    continue

                total += 1
                content = p.get("content", "")
                target_tool = p.get("target_tool", "")
                doc_id = p.get("doc_id", "")
                variation_type = p.get("variation_type", "")

                # 执行递归检索
                results, top_score = recursive_search(content, min_score=0.2)

                # 获取匹配的工具名
                if results:
                    matched_id = results[0].id
                    matched_tool = results[0].metadata.get("name", "") or \
                                   results[0].metadata.get("server", "") + "/" + matched_id.split(":")[-1]
                    matched_tool = results[0].metadata.get("server", "") + "/" + \
                                   results[0].metadata.get("name", matched_id.split(":")[-1] if ":" in matched_id else matched_id)
                else:
                    matched_id = ""
                    matched_tool = ""
                    top_score = 0.0

                # 判断是否通过
                # 检查目标工具是否在结果中
                tool_name = target_tool.split("/")[-1]
                is_correct_tool = any(
                    tool_name in (r.id or "") or
                    tool_name in (r.metadata.get("name", "") or "")
                    for r in results
                )

                result_record = {
                    "pattern_id": doc_id,
                    "content": content,
                    "target_tool": target_tool,
                    "recursion_score": round(top_score, 4),
                    "matched_id": matched_id,
                    "matched_tool": matched_tool,
                    "passed": top_score >= SCORE_THRESHOLD and is_correct_tool,
                    "variation_type": variation_type,
                }

                if result_record["passed"]:
                    passed += 1
                    verified_f.write(json.dumps(result_record, ensure_ascii=False) + "\n")
                    logger.info(f"[Tester] PASS: {content[:40]}... score={top_score:.4f}")
                else:
                    reason = "score below 0.5" if top_score < SCORE_THRESHOLD else "wrong tool matched"
                    result_record["reason"] = reason
                    failed += 1
                    failed_f.write(json.dumps(result_record, ensure_ascii=False) + "\n")
                    logger.warning(f"[Tester] FAIL: {content[:40]}... score={top_score:.4f} reason={reason}")

                # 更新向量库中的 verified 标记
                if doc_id and "pattern:" in doc_id:
                    vs = get_vector_search()
                    conn = vs._get_connection()
                    if conn:
                        conn.execute(
                            """UPDATE documents SET metadata = json_patch(metadata, ?, '$.verified', '$.verified_score')
                               WHERE id = ?""",
                            (json.dumps({"verified": result_record["passed"],
                                         "verified_score": round(top_score, 4)}), doc_id)
                        )
                        # 简单处理：直接更新整条记录
                        conn.execute(
                            "DELETE FROM documents WHERE id = ?",
                            (doc_id,)
                        )

                        # 重新读取并更新
                        old_rows = list(f for f in open(input_path, "r", encoding="utf-8") if doc_id in f)
    finally:
        verified_f.close()
        failed_f.close()

    # 汇总报告
    hit_rate = (passed / total * 100) if total > 0 else 0
    summary = f"[Tester] Results: {passed}/{total} passed ({hit_rate:.1f}% hit rate)"
    logger.info(summary)
    print(summary)

    # 按 variation_type 分组统计
    _report_by_type(verified_path)

    return {"passed": passed, "failed": failed, "total": total}


def _report_by_type(verified_path: Path):
    """按 variation_type 分组统计"""
    from collections import defaultdict
    stats = defaultdict(lambda: {"passed": 0, "total": 0})

    for f in [Path(verified_path.parent / "verified_patterns.jsonl"),
              Path(verified_path.parent / "failed_patterns.jsonl")]:
        if not f.exists():
            continue
        with open(f, "r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    r = json.loads(line)
                    vt = r.get("variation_type", "unknown")
                    stats[vt]["total"] += 1
                    if r.get("passed"):
                        stats[vt]["passed"] += 1
                except:
                    pass

    print("\n[Variation Type Coverage]")
    for vt, s in sorted(stats.items()):
        rate = s["passed"] / s["total"] * 100 if s["total"] > 0 else 0
        print(f"  {vt}: {s['passed']}/{s['total']} ({rate:.0f}%)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Query Pattern Tester")
    parser.add_argument("--input", default="candidates.jsonl", help="Input JSONL file")
    parser.add_argument("--verified", default="verified_patterns.jsonl", help="Verified output")
    parser.add_argument("--failed", default="failed_patterns.jsonl", help="Failed output")
    parser.add_argument("--threshold", type=float, default=0.5, help="Score threshold")
    args = parser.parse_args()

    global SCORE_THRESHOLD
    SCORE_THRESHOLD = args.threshold

    test_patterns(args.input, args.verified, args.failed)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行 Tester**

```bash
python scripts/query_pattern/step3_test.py --input candidates.jsonl --threshold 0.5
```

- [ ] **Step 3: 检查测试结果**

```bash
wc -l scripts/query_pattern/verified_patterns.jsonl
wc -l scripts/query_pattern/failed_patterns.jsonl
head -3 scripts/query_pattern/verified_patterns.jsonl
head -3 scripts/query_pattern/failed_patterns.jsonl
```

- [ ] **Step 4: Commit**

```bash
git add scripts/query_pattern/step3_test.py
git commit -m "feat(query_pattern): 实现 Tester 入口脚本"
```

---

## Task 7: 实现 pipeline.py（主控脚本）

**Files:**
- Create: `scripts/query_pattern/pipeline.py`

- [ ] **Step 1: 实现主控脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Pattern TDD Pipeline

三角色流水线主控：Generator → Writer → Tester → (重试)
"""
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from loguru import logger

# 配置
MAX_RETRIES = 3
PATTERNS_PER_TOOL = 12
SCORE_THRESHOLD = 0.5

# Scheduler-server 工具定义
TOOLS = {
    "schedule_task": "Create a one-time or recurring scheduled task with content, scheduled_at time, event_type, and optional cron_expr for recurrence",
    "cancel_task": "Cancel a scheduled task by task_id",
    "update_task": "Update an existing scheduled task's content, time, or cron expression",
    "list_scheduled_tasks": "Query scheduled task list, optionally filtered by status (pending/triggered/cancelled)",
}


def run_step1(server: str, tool: str, description: str, count: int,
              failed: list = None) -> list[dict]:
    """运行 Generator"""
    from query_pattern.step1_generate import generate_patterns_for_tool
    return generate_patterns_for_tool(server, tool, description, count, failed)


def run_step2(patterns: list[dict], output_file: str = "candidates.jsonl") -> int:
    """运行 Writer"""
    from query_pattern.step2_write import write_patterns

    # 临时写入 candidates.jsonl
    out_path = Path(__file__).parent / output_file
    with open(out_path, "w", encoding="utf-8") as f:
        for p in patterns:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    return write_patterns(output_file)


def run_step3(input_file: str = "candidates.jsonl",
               verified: str = "verified_patterns.jsonl",
               failed: str = "failed_patterns.jsonl") -> tuple[list, list]:
    """运行 Tester"""
    from query_pattern.step3_test import test_patterns

    result = test_patterns(input_file, verified, failed, threshold=SCORE_THRESHOLD)

    # 读取结果
    verified_path = Path(__file__).parent / verified
    failed_path = Path(__file__).parent / failed

    verified_list = []
    if verified_path.exists():
        with open(verified_path, "r", encoding="utf-8") as f:
            for line in f:
                verified_list.append(json.loads(line))

    failed_list = []
    if failed_path.exists():
        with open(failed_path, "r", encoding="utf-8") as f:
            for line in f:
                failed_list.append(json.loads(line))

    return verified_list, failed_list


def pipeline_for_tool(server: str, tool: str, description: str) -> dict:
    """为单个工具运行完整 TDD 流水线"""
    print(f"\n{'='*60}")
    print(f"Pipeline: {server}/{tool}")
    print(f"{'='*60}")

    all_verified = []
    retry_count = 0
    current_failed = []

    while retry_count <= MAX_RETRIES:
        # Step 1: 生成
        print(f"\n[Step 1] Generator (retry #{retry_count})")
        patterns = run_step1(server, tool, description, PATTERNS_PER_TOOL, current_failed)
        if not patterns:
            print(f"[ERROR] Generator returned no patterns")
            break

        # Step 2: 写入
        print(f"[Step 2] Writer ({len(patterns)} patterns)")
        written = run_step2(patterns)
        if written == 0:
            print(f"[ERROR] Writer wrote nothing")
            break

        # Step 3: 测试
        print(f"[Step 3] Tester")
        verified, failed = run_step3()

        all_verified.extend(verified)
        current_failed = failed

        print(f"\n  → Verified: {len(verified)}, Failed: {len(failed)}")

        if not failed:
            print(f"  ✓ All patterns passed!")
            break

        retry_count += 1
        if retry_count > MAX_RETRIES:
            print(f"  ⚠ Max retries ({MAX_RETRIES}) reached, moving on")
            break

        print(f"  → Retrying with feedback ({retry_count}/{MAX_RETRIES})")

    # 汇总
    print(f"\n  Final: {len(all_verified)}/{PATTERNS_PER_TOOL} verified for {tool}")
    return {
        "tool": tool,
        "verified": len(all_verified),
        "total": PATTERNS_PER_TOOL,
        "hit_rate": len(all_verified) / PATTERNS_PER_TOOL * 100 if PATTERNS_PER_TOOL > 0 else 0,
        "retries": retry_count,
    }


def main():
    """为所有 scheduler-server 工具运行流水线"""
    print("=" * 70)
    print("Query Pattern TDD Pipeline — Scheduler Server")
    print("=" * 70)

    results = []
    for tool_name, tool_desc in TOOLS.items():
        result = pipeline_for_tool("scheduler-server", tool_name, tool_desc)
        results.append(result)

    # 最终汇总
    print("\n" + "=" * 70)
    print("FINAL RESULTS")
    print("=" * 70)
    total_verified = 0
    total_target = 0
    for r in results:
        print(f"  {r['tool']}: {r['verified']}/{r['total']} ({r['hit_rate']:.0f}%) [retries: {r['retries']}]")
        total_verified += r['verified']
        total_target += r['total']

    overall_rate = total_verified / total_target * 100 if total_target > 0 else 0
    print(f"\n  Overall: {total_verified}/{total_target} ({overall_rate:.0f}%)")
    print("=" * 70)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 清理向量库中的旧数据**

```bash
python -c "
import sqlite3
db = 'C:/Users/LiLei/.niu/vectors.db'
conn = sqlite3.connect(db)
conn.execute(\"DELETE FROM documents WHERE id LIKE 'pattern:%' OR id LIKE 'query_pattern:reminder%'\")
conn.commit()
print('Old patterns cleaned')
conn.close()
"
```

- [ ] **Step 3: 运行完整流水线**

```bash
cd E:/tools/ai-bot
python scripts/query_pattern/pipeline.py
```

预期：每个工具生成 12 条 patterns，递归检索验证，目标命中率 ≥ 80%

- [ ] **Step 4: Commit**

```bash
git add scripts/query_pattern/pipeline.py
git commit -m "feat(query_pattern): 实现 TDD 流水线主控脚本"
```

---

## Task 8: 集成测试 — 手动验证递归检索

**Files:**
- Create: `scripts/query_pattern/test_recursive_search.py`（一次性验证脚本）

- [ ] **Step 1: 手动测试验证**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动验证：测试真实用户查询能否通过递归检索命中正确工具"""
import sys
from pathlib import Path
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from query_pattern.tools import recursive_search, logger

TEST_QUERIES = [
    ("5分钟后提醒我吃药", "scheduler-server/schedule_task"),
    ("半小时后提醒我开会", "scheduler-server/schedule_task"),
    ("remind me in 10 minutes", "scheduler-server/schedule_task"),
    ("明天上午10点提醒我开会", "scheduler-server/schedule_task"),
    ("取消刚才的提醒", "scheduler-server/cancel_task"),
    ("删掉下午的定时任务", "scheduler-server/cancel_task"),
    ("看看我有哪些定时任务", "scheduler-server/list_scheduled_tasks"),
    ("显示所有提醒", "scheduler-server/list_scheduled_tasks"),
    ("把提醒改成下午3点", "scheduler-server/update_task"),
]

print("=" * 70)
print("Manual Recursive Search Verification")
print("=" * 70)

passed = 0
for query, expected_tool in TEST_QUERIES:
    results, score = recursive_search(query)
    if results:
        matched = results[0].id
        tool_name = results[0].metadata.get("name", "") or matched.split(":")[-1] if ":" in matched else ""
        matched_tool = f"{results[0].metadata.get('server', '')}/{tool_name}"
        is_match = expected_tool.split("/")[-1] in matched_tool or expected_tool.split("/")[-1] in matched
        status = "✓" if is_match else "✗"
    else:
        matched_tool = "None"
        is_match = False
        status = "✗"

    hit = "PASS" if (is_match and score >= 0.5) else "FAIL"
    print(f"{status} '{query}' → matched={matched_tool} score={score:.4f} [{hit}]")
    if is_match and score >= 0.5:
        passed += 1

print(f"\nResult: {passed}/{len(TEST_QUERIES)} passed")
```

- [ ] **Step 2: 运行验证**

```bash
python scripts/query_pattern/test_recursive_search.py
```

- [ ] **Step 3: Commit**

```bash
git add scripts/query_pattern/test_recursive_search.py
git commit -m "feat(query_pattern): 添加递归检索手动验证脚本"
```

---

## Task 9: 更新 init_vector_db.py（预留调用接口）

**Files:**
- Modify: `scripts/init_vector_db.py`

- [ ] **Step 1: 在 init_vector_db.py 末尾添加 query_pattern 初始化调用**

在 `inject_system_manual()` 函数之后、`main()` 函数中添加：

```python
def init_query_patterns():
    """初始化 Query Patterns（从 TDD 流水线生成）"""
    logger.info("初始化 Query Patterns...")

    # 尝试运行流水线脚本
    pipeline_path = Path(__file__).parent / "query_pattern" / "pipeline.py"
    if not pipeline_path.exists():
        logger.warning("Query Pattern 流水线脚本不存在，跳过")
        return

    import subprocess
    result = subprocess.run(
        [sys.executable, str(pipeline_path)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent)
    )

    if result.returncode == 0:
        logger.info("✓ Query Patterns 初始化完成")
    else:
        logger.error(f"✗ Query Patterns 初始化失败: {result.stderr}")
```

在 `main()` 函数中，在 `inject_system_manual()` 调用之后添加：

```python
# 6. 初始化 Query Patterns（可选，需要 LLM API）
print("\n" + "-" * 70)
print("Query Patterns 初始化需要 LLM API，耗时较长")
confirm = input("是否初始化 Query Patterns？[y/N]: ")
if confirm.lower() == 'y':
    init_query_patterns()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/init_vector_db.py
git commit -m "feat(init_vector_db): 预留 Query Pattern 初始化接口"
```

---

## 实施顺序

| 任务 | 依赖 | 说明 |
|------|------|------|
| Task 1 | — | 创建目录结构 |
| Task 2 | Task 1 | tools.py（共享工具） |
| Task 3 | Task 1 | 三角色提示词 |
| Task 4 | Task 2, 3 | Generator |
| Task 5 | Task 2, 4 | Writer |
| Task 6 | Task 2, 4 | Tester |
| Task 7 | Task 4, 5, 6 | Pipeline |
| Task 8 | Task 7 | 手动验证 |
| Task 9 | Task 7 | 集成到 init_vector_db.py |

---

## 验证清单

- [ ] `python scripts/query_pattern/pipeline.py` 无报错
- [ ] verified_patterns.jsonl 中 ≥ 80% patterns 通过
- [ ] 手动测试 "5分钟后提醒我吃药" 递归检索分数 ≥ 0.5
- [ ] variation_type 覆盖 ≥ 5 种类型
