#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LightRAG Query Strategy Validation Script

Inserts test data into LightRAG, then queries with different parameter
combinations to determine which strategy is optimal for the injection
use case.

Usage:
    python scripts/lightrag_query_test.py [--skip-insert] [--query "便签"]

The script initializes LightRAG using the project's lightrag_manager
and uses call_async() to bridge async calls.
"""

import io
import json
import sys
import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Fix Windows console encoding for Chinese characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Add project root to sys.path so niu_api can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))

from niu_api.internal.lightrag_manager import call_async, get_lightrag

DEFAULT_QUERY = "便签"


# ============================================================
# Test Data Definitions
# ============================================================

TEST_ENTITIES = [
    {
        "entity_name": "note-management",
        "entity_type": "skill",
        "description": "便签管理技能，用于创建、读取、更新、删除便签，支持分类和搜索",
        "source_id": "test_data",
        "file_path": "skill://note-management",
    },
    {
        "entity_name": "memory_remember",
        "entity_type": "mcp_tool",
        "description": "记忆存储工具，将用户偏好、身份信息和上下文记忆持久化到本地存储",
        "source_id": "test_data",
        "file_path": "mcp_tool://memory-server/user_memory_remember",
    },
    {
        "entity_name": "Python",
        "entity_type": "knowledge",
        "description": "Python编程语言，广泛用于数据科学、Web开发和自动化脚本",
        "source_id": "test_data",
        "file_path": "knowledge://Python",
    },
    {
        "entity_name": "file-parser",
        "entity_type": "tool",
        "description": "文件解析工具，支持PDF、Word、PPT、Excel、Markdown和HTML格式的文档解析",
        "source_id": "test_data",
        "file_path": "mcp_tool://file-parser/parse",
    },
    {
        "entity_name": "kg-server",
        "entity_type": "tool",
        "description": "知识图谱服务器，管理文档实体和关系，支持图谱查询和遍历",
        "source_id": "test_data",
        "file_path": "mcp_tool://kg-server/query",
    },
    {
        "entity_name": "vector-search",
        "entity_type": "knowledge",
        "description": "向量语义搜索，使用embedding模型进行相似度检索，支持L0/L1/L2三级存储",
        "source_id": "test_data",
        "file_path": "knowledge://vector-search",
    },
]

TEST_RELATIONSHIPS = [
    {
        "src_id": "note-management",
        "tgt_id": "memory_remember",
        "keywords": "depends_on",
        "description": "便签管理依赖记忆服务来持久化便签数据",
        "source_id": "test_data",
        "file_path": "skill://note-management",
    },
    {
        "src_id": "note-management",
        "tgt_id": "Python",
        "keywords": "uses",
        "description": "便签管理技能使用Python实现",
        "source_id": "test_data",
        "file_path": "skill://note-management",
    },
    {
        "src_id": "note-management",
        "tgt_id": "file-parser",
        "keywords": "depends_on",
        "description": "便签管理使用文件解析器来导入外部文档为便签",
        "source_id": "test_data",
        "file_path": "skill://note-management",
    },
    {
        "src_id": "kg-server",
        "tgt_id": "vector-search",
        "keywords": "depends_on",
        "description": "知识图谱服务器依赖向量搜索进行语义检索",
        "source_id": "test_data",
        "file_path": "mcp_tool://kg-server",
    },
]

TEST_CHUNKS = [
    {
        "content": "note-management: Use when user asks to create, read, update, or delete sticky notes. "
                   "Supports categorization, search, and import from external documents. "
                   "Depends on memory_remember for persistence and file-parser for document import.",
        "source_id": "test_data",
        "file_path": "skill://note-management",
    },
    {
        "content": "memory_remember: Store user preferences, identity information, and contextual memories. "
                   "Called automatically by the agent loop to persist important information across sessions.",
        "source_id": "test_data",
        "file_path": "mcp_tool://memory-server/user_memory_remember",
    },
    {
        "content": "Python is the primary implementation language for the ai-bot project. "
                   "Used for the agent core, MCP servers, and API layer. "
                   "Key libraries: sentence-transformers, insightface, fastapi.",
        "source_id": "test_data",
        "file_path": "knowledge://Python",
    },
]

# Unstructured documents for ainsert() — lets LightRAG extract entities/relations
TEST_DOCUMENTS = [
    "便签管理是ai-bot项目的核心技能之一。用户可以通过自然语言指令创建、查看、编辑和删除便签。"
    "便签支持分类标签和全文搜索。便签数据通过memory-server持久化存储。"
    "当用户要求导入文档为便签时，系统会调用file-parser解析文档内容。",

    "知识图谱(knowledge graph)用于存储和管理项目中的结构化知识。"
    "实体类型包括skill(技能)、tool/mcp_tool(工具)、knowledge/concept(知识概念)。"
    "kg-server提供图谱查询和遍历功能，内部依赖vector-store进行语义检索。"
    "图谱的注入路径支持结构化注入(ainsert_custom_kg)和非结构化注入(ainsert)两种方式。",
]


# ============================================================
# Query Strategy Definitions
# ============================================================

def get_query_strategies(query: str) -> List[Dict[str, Any]]:
    """Define the 6 query strategies to test."""
    return [
        {
            "label": "hybrid + LLM关键词",
            "short": "hybrid+LLM",
            "needs_llm": True,
            "make_param": lambda q=query: _make_param(mode="hybrid", top_k=20),
        },
        {
            "label": "local + LLM关键词",
            "short": "local+LLM",
            "needs_llm": True,
            "make_param": lambda q=query: _make_param(mode="local", top_k=20),
        },
        {
            "label": "local + 预提供关键词 + only_need_context=True",
            "short": "local+pre+noLLM",
            "needs_llm": False,
            "make_param": lambda q=query: _make_param(
                mode="local",
                ll_keywords=[q],
                hl_keywords=[q],
                only_need_context=True,
                top_k=20,
            ),
        },
        {
            "label": "hybrid + 预提供关键词 + only_need_context=True",
            "short": "hybrid+pre+noLLM",
            "needs_llm": False,
            "make_param": lambda q=query: _make_param(
                mode="hybrid",
                ll_keywords=[q],
                hl_keywords=[q],
                only_need_context=True,
                top_k=20,
            ),
        },
        {
            "label": "naive + only_need_context=True",
            "short": "naive+noLLM",
            "needs_llm": False,
            "make_param": lambda q=query: _make_param(
                mode="naive",
                only_need_context=True,
                top_k=20,
            ),
        },
        {
            "label": "local + 预提供关键词 + only_need_context=False",
            "short": "local+pre+LLMsum",
            "needs_llm": True,
            "make_param": lambda q=query: _make_param(
                mode="local",
                ll_keywords=[q],
                hl_keywords=[q],
                only_need_context=False,
                top_k=20,
            ),
        },
    ]


def _make_param(
    mode: str,
    top_k: int = 20,
    ll_keywords: Optional[List[str]] = None,
    hl_keywords: Optional[List[str]] = None,
    only_need_context: bool = False,
) -> Any:
    """Create a QueryParam with the given settings."""
    from lightrag import QueryParam

    param = QueryParam(mode=mode, only_need_context=only_need_context, top_k=top_k)
    if ll_keywords:
        param.ll_keywords = ll_keywords
    if hl_keywords:
        param.hl_keywords = hl_keywords
    return param


# ============================================================
# LLM Call Counter
# ============================================================

class LLMCallCounter:
    """Count LLM calls by patching the LLM model function.

    Uses a wrapper around the real llm_model_func to count invocations.
    Thread-safe via a simple integer counter (GIL-protected in CPython).
    """

    def __init__(self) -> None:
        self.count = 0
        self._original_func = None

    def install(self, rag: Any) -> None:
        """Wrap rag.llm_model_func with a counter."""
        if hasattr(rag, "llm_model_func") and rag.llm_model_func is not None:
            self._original_func = rag.llm_model_func
            original = self._original_func
            counter = self

            async def _counting_llm_func(*args, **kwargs):
                counter.count += 1
                return await original(*args, **kwargs)

            rag.llm_model_func = _counting_llm_func

    def reset(self) -> None:
        """Reset the counter to 0."""
        self.count = 0

    def uninstall(self, rag: Any) -> None:
        """Restore the original llm_model_func."""
        if self._original_func is not None:
            rag.llm_model_func = self._original_func
            self._original_func = None


# ============================================================
# Data Insertion
# ============================================================

def insert_test_data(rag: Any) -> None:
    """Insert test entities, relationships, chunks, and documents."""
    from niu_api.internal.lightrag_adapter import LightRAGIngester

    ingester = LightRAGIngester()

    # --- Structured path: ainsert_custom_kg ---
    print("\n[1/2] Inserting structured data (entities + relationships + chunks)...")
    result = ingester.inject_custom_kg(
        entities=TEST_ENTITIES,
        relationships=TEST_RELATIONSHIPS,
        chunks=TEST_CHUNKS,
        source_id="test_data",
    )
    print(f"  Result: {json.dumps(result, ensure_ascii=False)}")

    # --- Unstructured path: ainsert() ---
    print("[2/2] Inserting unstructured documents (LLM-driven extraction)...")
    for i, doc in enumerate(TEST_DOCUMENTS):
        try:
            print(f"  Document {i+1}/{len(TEST_DOCUMENTS)}: inserting...")
            result = ingester.inject_document(
                content=doc,
                doc_id=f"test_doc_{i}",
                file_path=f"test://doc_{i}",
            )
            print(f"  Result: {json.dumps(result, ensure_ascii=False)}")
        except Exception as e:
            print(f"  ERROR inserting document {i+1}: {e}")

    print("  Test data insertion complete.")


def check_existing_data(rag: Any) -> int:
    """Check how many entities exist in the graph. Returns entity count."""
    try:
        kg = call_async(rag.get_knowledge_graph("*", max_depth=1, max_nodes=500))
        if kg and kg.nodes:
            return len(kg.nodes)
    except Exception as e:
        print(f"  Warning: could not check existing data: {e}")
    return 0


# ============================================================
# Query Execution & Result Analysis
# ============================================================

def extract_result_stats(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract statistics from a query_data result."""
    stats: Dict[str, Any] = {
        "entities_count": 0,
        "entity_types": {},
        "relationships_count": 0,
        "chunks_count": 0,
        "has_summary": False,
        "status": "unknown",
        "is_empty": True,
    }

    if result is None:
        stats["status"] = "none"
        return stats

    if not isinstance(result, dict):
        stats["status"] = f"unexpected_type:{type(result).__name__}"
        return stats

    stats["status"] = result.get("status", "(missing)")

    data = result.get("data", {})
    if not data:
        if any(k in result for k in ("entities", "relationships", "chunks")):
            data = result
        else:
            return stats

    entities = data.get("entities", [])
    stats["entities_count"] = len(entities)

    # Count entity_type distribution
    type_counts: Dict[str, int] = {}
    for entity in entities:
        et = entity.get("entity_type", "UNKNOWN")
        type_counts[et] = type_counts.get(et, 0) + 1
    stats["entity_types"] = type_counts

    stats["relationships_count"] = len(data.get("relationships", []))
    stats["chunks_count"] = len(data.get("chunks", []))

    # Check for natural language summary in metadata
    metadata = result.get("metadata", {})
    if metadata.get("llm_response"):
        stats["has_summary"] = True

    # Check if result has meaningful content
    if entities or data.get("relationships") or data.get("chunks"):
        stats["is_empty"] = False

    return stats


def run_query_strategy(
    rag: Any,
    query: str,
    strategy: Dict[str, Any],
    llm_counter: LLMCallCounter,
) -> Dict[str, Any]:
    """Run a single query strategy and return timing + result stats."""
    param = strategy["make_param"]()
    llm_counter.reset()

    start = time.time()
    try:
        result = call_async(rag.aquery_data(query, param=param))
    except Exception as e:
        elapsed = time.time() - start
        return {
            "elapsed": elapsed,
            "llm_calls": llm_counter.count,
            "error": f"{type(e).__name__}: {e}",
            "stats": extract_result_stats(None),
        }

    elapsed = time.time() - start
    stats = extract_result_stats(result)

    return {
        "elapsed": elapsed,
        "llm_calls": llm_counter.count,
        "error": None,
        "stats": stats,
        "raw_result": result,
    }


# ============================================================
# Output Formatting
# ============================================================

def print_separator(title: str, char: str = "=", width: int = 80) -> None:
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_insertion_summary() -> None:
    print(f"  Entities to insert: {len(TEST_ENTITIES)}")
    for e in TEST_ENTITIES:
        print(f"    - {e['entity_name']} ({e['entity_type']})")
    print(f"  Relationships to insert: {len(TEST_RELATIONSHIPS)}")
    for r in TEST_RELATIONSHIPS:
        print(f"    - {r['src_id']} --[{r['keywords']}]--> {r['tgt_id']}")
    print(f"  Chunks to insert: {len(TEST_CHUNKS)}")
    print(f"  Documents to insert: {len(TEST_DOCUMENTS)}")


def print_comparison_table(
    results: List[Dict[str, Any]],
    strategies: List[Dict[str, Any]],
) -> None:
    """Print the comparison table."""
    print_separator("COMPARISON TABLE")

    # Header
    header = (
        f"{'方案':<30} {'耗时':>6} {'LLM调用':>7} "
        f"{'entities':>8} {'relationships':>13} {'chunks':>6} {'有总结':>6} {'状态':>8}"
    )
    print(header)
    print("-" * len(header))

    for i, (strategy, result) in enumerate(zip(strategies, results)):
        stats = result["stats"]
        error = result.get("error")

        # Format entity_type distribution
        type_dist = stats.get("entity_types", {})
        if type_dist:
            type_str = "; ".join(f"{k}:{v}" for k, v in sorted(type_dist.items()))
        else:
            type_str = "-"

        # Entity count with type breakdown
        ent_count = stats["entities_count"]
        if ent_count > 0:
            ent_str = f"{ent_count}({type_dist})"
        else:
            ent_str = "0"

        has_summary = "是" if stats.get("has_summary") else "否"
        status = "OK" if not error and not stats["is_empty"] else ("EMPTY" if stats["is_empty"] else "ERR")

        label = strategy["short"]
        elapsed_str = f"{result['elapsed']:.2f}s"

        print(
            f"{label:<30} {elapsed_str:>6} {result['llm_calls']:>7} "
            f"{ent_str:>8} {stats['relationships_count']:>13} {stats['chunks_count']:>6} "
            f"{has_summary:>6} {status:>8}"
        )

        if error:
            print(f"  ERROR: {error}")

    # Print entity type distribution separately for readability
    print_separator("ENTITY TYPE DISTRIBUTION")
    for strategy, result in zip(strategies, results):
        stats = result["stats"]
        type_dist = stats.get("entity_types", {})
        label = strategy["short"]
        if type_dist:
            parts = [f"  {t}: {c}" for t, c in sorted(type_dist.items())]
            print(f"{label}:")
            for p in parts:
                print(p)
        else:
            print(f"{label}: (no entities)")


def print_detailed_results(
    results: List[Dict[str, Any]],
    strategies: List[Dict[str, Any]],
) -> None:
    """Print detailed result for each strategy."""
    for strategy, result in zip(strategies, results):
        print_separator(f"DETAIL: {strategy['label']}")

        print(f"  Elapsed: {result['elapsed']:.2f}s")
        print(f"  LLM calls: {result['llm_calls']}")
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            continue

        raw = result.get("raw_result")
        if raw is None:
            print("  Result: None")
            continue

        # Print structured breakdown
        data = raw.get("data", {})
        if not data and any(k in raw for k in ("entities", "relationships", "chunks")):
            data = raw

        entities = data.get("entities", [])
        relationships = data.get("relationships", [])
        chunks = data.get("chunks", [])

        print(f"\n  Entities ({len(entities)}):")
        for e in entities:
            name = e.get("entity_name", "?")
            etype = e.get("entity_type", "?")
            desc = e.get("description", "")[:100]
            print(f"    - {name} [{etype}]: {desc}")

        print(f"\n  Relationships ({len(relationships)}):")
        for r in relationships:
            src = r.get("src_id", "?")
            tgt = r.get("tgt_id", "?")
            kw = r.get("keywords", "?")
            desc = r.get("description", "")[:80]
            print(f"    - {src} --[{kw}]--> {tgt}: {desc}")

        print(f"\n  Chunks ({len(chunks)}):")
        for c in chunks:
            content = c.get("content", "")[:150]
            print(f"    - {content}...")

        # Metadata
        metadata = raw.get("metadata", {})
        if metadata:
            print(f"\n  Metadata:")
            keywords = metadata.get("keywords", {})
            if keywords:
                print(f"    high_level: {keywords.get('high_level', [])}")
                print(f"    low_level:  {keywords.get('low_level', [])}")
            proc_info = metadata.get("processing_info", {})
            if proc_info:
                print(f"    processing_info: {json.dumps(proc_info, ensure_ascii=False)}")


def print_recommendation(
    results: List[Dict[str, Any]],
    strategies: List[Dict[str, Any]],
) -> None:
    """Print recommendation based on test results."""
    print_separator("RECOMMENDATION")

    # Find the best no-LLM strategy (most entities + relationships, fastest)
    no_llm_results = []
    for strategy, result in zip(strategies, results):
        if not strategy["needs_llm"]:
            stats = result["stats"]
            score = (
                stats["entities_count"] * 2
                + stats["relationships_count"]
                + stats["chunks_count"]
            )
            no_llm_results.append((strategy, result, score))

    # Sort by score (descending), then by elapsed (ascending)
    no_llm_results.sort(key=lambda x: (-x[2], x[1]["elapsed"]))

    if no_llm_results:
        best_strategy, best_result, best_score = no_llm_results[0]
        best_stats = best_result["stats"]

        print(f"  Best no-LLM strategy: {best_strategy['label']}")
        print(f"    Entities: {best_stats['entities_count']}, "
              f"Relationships: {best_stats['relationships_count']}, "
              f"Chunks: {best_stats['chunks_count']}")
        print(f"    Elapsed: {best_result['elapsed']:.2f}s, LLM calls: {best_result['llm_calls']}")
        print()

    # Compare with LLM-based strategies
    llm_results = []
    for strategy, result in zip(strategies, results):
        if strategy["needs_llm"]:
            stats = result["stats"]
            score = (
                stats["entities_count"] * 2
                + stats["relationships_count"]
                + stats["chunks_count"]
            )
            llm_results.append((strategy, result, score))

    if llm_results and no_llm_results:
        llm_ok = [r["elapsed"] for _, r, _ in llm_results if not r.get("error") and r["elapsed"] > 0]
        no_llm_ok = [r["elapsed"] for _, r, _ in no_llm_results if not r.get("error") and r["elapsed"] > 0]

        if llm_ok and no_llm_ok:
            avg_llm_time = sum(llm_ok) / len(llm_ok)
            avg_no_llm_time = sum(no_llm_ok) / len(no_llm_ok)
            speedup = avg_llm_time / avg_no_llm_time if avg_no_llm_time > 0 else float("inf")

            print(f"  Speed comparison (excluding errored runs):")
            print(f"    Avg LLM-based time:  {avg_llm_time:.2f}s ({len(llm_ok)} runs)")
            print(f"    Avg no-LLM time:     {avg_no_llm_time:.2f}s ({len(no_llm_ok)} runs)")
            print(f"    Speedup:             {speedup:.1f}x")
        elif no_llm_ok:
            avg_no_llm_time = sum(no_llm_ok) / len(no_llm_ok)
            print(f"  Speed comparison:")
            print(f"    All LLM-based runs errored (API likely unavailable)")
            print(f"    Avg no-LLM time:     {avg_no_llm_time:.2f}s ({len(no_llm_ok)} runs)")
        print()

    print("  RECOMMENDED FOR INJECTION (runner.py _inject_dynamic_resources):")
    print("  -----------------------------------------------------------------")
    print("  mode='local', ll_keywords=[query], hl_keywords=[query],")
    print("  only_need_context=True, top_k=20")
    print()
    print("  Rationale:")
    print("  - Zero LLM calls (keywords pre-provided, no summary needed)")
    print("  - Full graph traversal capability (entities + relationships)")
    print("  - entity_type preserved for category-based injection")
    print("  - Sub-second latency suitable for per-turn injection")
    print()
    print("  Code example:")
    print("  -----------------------------------------------------------------")
    print("""
  from lightrag import QueryParam
  param = QueryParam(
      mode="local",
      ll_keywords=[user_query],
      hl_keywords=[user_query],
      only_need_context=True,
      top_k=20,
  )
  result = await rag.aquery_data(user_query, param=param)
  entities = result.get("data", {}).get("entities", [])
  """)


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LightRAG query strategy validation script"
    )
    parser.add_argument(
        "--skip-insert", "-s",
        action="store_true",
        help="Skip test data insertion (use existing graph data)",
    )
    parser.add_argument(
        "--query", "-q",
        default=DEFAULT_QUERY,
        help=f"Query string (default: '{DEFAULT_QUERY}')",
    )
    parser.add_argument(
        "--detail", "-d",
        action="store_true",
        help="Print detailed results for each strategy",
    )
    args = parser.parse_args()

    query = args.query

    print("=" * 80)
    print("  LightRAG Query Strategy Validation")
    print("=" * 80)
    print(f"  Query: '{query}'")
    print(f"  Project root: {PROJECT_ROOT}")

    # Step 1: Initialize LightRAG
    print("\n--- Step 1: Initialize LightRAG ---")
    rag = get_lightrag()
    if rag is None:
        print("ERROR: LightRAG initialization failed.")
        print("Ensure lightrag-hku is installed and user-config.json has valid LLM config.")
        sys.exit(1)

    print(f"  LightRAG initialized: {rag.working_dir}")

    # Step 2: Check existing data
    existing_count = check_existing_data(rag)
    print(f"  Existing entities in graph: {existing_count}")

    # Step 3: Insert test data
    if not args.skip_insert:
        print("\n--- Step 2: Insert Test Data ---")
        print_insertion_summary()
        insert_test_data(rag)

        # Verify insertion
        new_count = check_existing_data(rag)
        print(f"\n  Entities after insertion: {new_count} (delta: {new_count - existing_count})")
    else:
        print("\n--- Step 2: Skipped (using existing data) ---")

    # Step 4: Install LLM call counter
    print("\n--- Step 3: Query Strategies ---")
    llm_counter = LLMCallCounter()
    llm_counter.install(rag)
    print("  LLM call counter installed.")

    # Warm up: pre-load the embedding model with a lightweight query.
    # The first embedding call takes ~7s; subsequent ones are <0.1s.
    # We use local+pre+only_need_context which requires no LLM.
    print("  Warming up embedding model...")
    try:
        warm_param = _make_param(
            mode="local",
            ll_keywords=["__warmup__"],
            hl_keywords=["__warmup__"],
            only_need_context=True,
            top_k=1,
        )
        call_async(rag.aquery_data("__warmup__", param=warm_param))
        print("  Warm-up complete.")
    except Exception as e:
        print(f"  Warm-up skipped ({type(e).__name__}: {e}).")
        print("  First query may be slow due to embedding model loading.")

    strategies = get_query_strategies(query)
    results: List[Dict[str, Any]] = []

    for i, strategy in enumerate(strategies):
        label = strategy["label"]
        needs_llm = strategy["needs_llm"]
        print(f"\n  [{i+1}/{len(strategies)}] {label} (needs_llm={needs_llm})...")

        try:
            result = run_query_strategy(rag, query, strategy, llm_counter)
            results.append(result)
            stats = result["stats"]
            elapsed = result["elapsed"]
            llm_calls = result["llm_calls"]
            err = result.get("error")

            if err:
                print(f"    ERROR: {err}")
            else:
                print(
                    f"    OK: {elapsed:.2f}s, {llm_calls} LLM calls, "
                    f"{stats['entities_count']} entities, "
                    f"{stats['relationships_count']} rels, "
                    f"{stats['chunks_count']} chunks"
                )
        except Exception as e:
            print(f"    UNEXPECTED ERROR: {type(e).__name__}: {e}")
            results.append({
                "elapsed": 0,
                "llm_calls": 0,
                "error": f"{type(e).__name__}: {e}",
                "stats": extract_result_stats(None),
            })

    # Uninstall counter
    llm_counter.uninstall(rag)

    # Step 5: Print comparison table
    print_comparison_table(results, strategies)

    # Step 6: Print detailed results if requested
    if args.detail:
        print_detailed_results(results, strategies)

    # Step 7: Print recommendation
    print_recommendation(results, strategies)

    print_separator("DONE", char="-")


if __name__ == "__main__":
    main()
