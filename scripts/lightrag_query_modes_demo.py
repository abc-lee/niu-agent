"""
LightRAG Search Mode Comparison Script

Queries LightRAG with the same keyword using all 6 search modes
(local, global, hybrid, naive, mix, bypass) and prints the complete
return data structure for each mode.

Usage:
    python scripts/lightrag_query_modes_demo.py [--query "便签"]

The script initializes LightRAG using the project's existing lightrag_manager
and uses call_async() to bridge async calls.
"""

import io
import json
import sys
import argparse
import time
from pathlib import Path

# Fix Windows console encoding for Chinese characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Add project root to sys.path so niu_api can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))

from niu_api.internal.lightrag_manager import call_async, get_lightrag

# All valid search modes
ALL_MODES = ["local", "global", "hybrid", "naive", "mix", "bypass"]

DEFAULT_QUERY = "便签"


def truncate_string(s: str, max_len: int = 200) -> str:
    """Truncate a string to max_len characters, appending '...' if truncated."""
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def print_separator(title: str, char: str = "=", width: int = 80):
    """Print a formatted separator with a title."""
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_result_detail(result, mode, query):
    """Print detailed breakdown of a query_data result."""
    if result is None:
        print("  Result: None (LightRAG not available or error)")
        return

    if not isinstance(result, dict):
        print(f"  Result type: {type(result).__name__}")
        print(f"  Result value: {truncate_string(str(result), 300)}")
        return

    # --- Full JSON dump ---
    print("\n  [Full JSON Structure]")
    # Use json.dumps with ensure_ascii=False to preserve Chinese characters
    json_str = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    # Print full JSON if not too long, otherwise truncate
    if len(json_str) > 5000:
        print(f"  (JSON too long: {len(json_str)} chars, showing first 5000)")
        print(json_str[:5000])
        print("  ... (truncated)")
    else:
        print(json_str)

    # --- Top-level summary ---
    print(f"\n  [Top-Level Summary]")
    status = result.get("status", "(missing)")
    message = result.get("message", "(missing)")
    print(f"    status:    {status}")
    print(f"    message:   {truncate_string(message, 100)}")

    metadata = result.get("metadata", {})
    if metadata:
        print(f"    metadata keys: {sorted(metadata.keys())}")
        query_mode = metadata.get("query_mode", "(missing)")
        print(f"      query_mode: {query_mode}")
        keywords = metadata.get("keywords", {})
        if keywords:
            hl = keywords.get("high_level", [])
            ll = keywords.get("low_level", [])
            print(f"      high_level_keywords: {hl}")
            print(f"      low_level_keywords:  {ll}")
        proc_info = metadata.get("processing_info", {})
        if proc_info:
            print(f"      processing_info: {json.dumps(proc_info, ensure_ascii=False)}")

    # --- Data section ---
    data = result.get("data", {})
    if not data:
        # Maybe the result IS the data dict (fallback case in adapter)
        if any(k in result for k in ("entities", "relationships", "chunks")):
            data = result
        else:
            print("    data: (empty or missing)")
            return

    # --- Entities ---
    entities = data.get("entities", [])
    print(f"\n  [Entities] count: {len(entities)}")
    for i, entity in enumerate(entities):
        entity_name = entity.get("entity_name", "(missing)")
        entity_type = entity.get("entity_type", "(missing)")
        description = truncate_string(entity.get("description", ""), 150)
        source_id = truncate_string(entity.get("source_id", ""), 80)
        file_path = entity.get("file_path", "(missing)")
        created_at = entity.get("created_at", "(missing)")
        print(f"    [{i}] entity_name: {entity_name}")
        print(f"        entity_type: {entity_type}")
        print(f"        description: {description}")
        print(f"        source_id:   {source_id}")
        print(f"        file_path:   {file_path}")
        print(f"        created_at:  {created_at}")
        # Check for any extra keys not in the standard schema
        standard_keys = {"entity_name", "entity_type", "description", "source_id",
                         "file_path", "created_at", "reference_id"}
        extra_keys = set(entity.keys()) - standard_keys
        if extra_keys:
            print(f"        extra_keys:  {sorted(extra_keys)}")
            for ek in sorted(extra_keys):
                print(f"          {ek}: {truncate_string(str(entity[ek]), 100)}")

    # --- Relationships ---
    relationships = data.get("relationships", [])
    print(f"\n  [Relationships] count: {len(relationships)}")
    for i, rel in enumerate(relationships):
        src_id = rel.get("src_id", "(missing)")
        tgt_id = rel.get("tgt_id", "(missing)")
        keywords = rel.get("keywords", "(missing)")
        description = truncate_string(rel.get("description", ""), 150)
        weight = rel.get("weight", "(missing)")
        source_id = truncate_string(rel.get("source_id", ""), 80)
        file_path = rel.get("file_path", "(missing)")
        print(f"    [{i}] src_id:     {src_id}")
        print(f"        tgt_id:     {tgt_id}")
        print(f"        keywords:   {keywords}")
        print(f"        description: {description}")
        print(f"        weight:     {weight}")
        print(f"        source_id:  {source_id}")
        print(f"        file_path:  {file_path}")
        # Check for extra keys
        standard_keys = {"src_id", "tgt_id", "description", "keywords", "weight",
                         "source_id", "file_path", "created_at", "reference_id"}
        extra_keys = set(rel.keys()) - standard_keys
        if extra_keys:
            print(f"        extra_keys: {sorted(extra_keys)}")
            for ek in sorted(extra_keys):
                print(f"          {ek}: {truncate_string(str(rel[ek]), 100)}")

    # --- Chunks ---
    chunks = data.get("chunks", [])
    print(f"\n  [Chunks] count: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        content = truncate_string(chunk.get("content", ""), 200)
        chunk_id = chunk.get("chunk_id", "(missing)")
        file_path = chunk.get("file_path", "(missing)")
        reference_id = chunk.get("reference_id", "(missing)")
        print(f"    [{i}] chunk_id:    {chunk_id}")
        print(f"        file_path:   {file_path}")
        print(f"        reference_id: {reference_id}")
        print(f"        content (first 200 chars): {content}")
        # Check for extra keys
        standard_keys = {"content", "file_path", "chunk_id", "reference_id"}
        extra_keys = set(chunk.keys()) - standard_keys
        if extra_keys:
            print(f"        extra_keys:  {sorted(extra_keys)}")
            for ek in sorted(extra_keys):
                print(f"          {ek}: {truncate_string(str(chunk[ek]), 100)}")

    # --- References ---
    references = data.get("references", [])
    print(f"\n  [References] count: {len(references)}")
    for i, ref in enumerate(references):
        reference_id = ref.get("reference_id", "(missing)")
        file_path = ref.get("file_path", "(missing)")
        print(f"    [{i}] reference_id: {reference_id}")
        print(f"        file_path:    {file_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Query LightRAG with all 6 search modes and show raw data"
    )
    parser.add_argument(
        "--query", "-q",
        default=DEFAULT_QUERY,
        help=f"Query string (default: '{DEFAULT_QUERY}')",
    )
    parser.add_argument(
        "--top-k", "-k",
        type=int, default=20,
        help="top_k parameter for retrieval (default: 20)",
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        help="Output file path to also write results (default: stdout only)",
    )
    args = parser.parse_args()

    query = args.query
    top_k = args.top_k

    # If output file specified, tee output to file as well
    output_file = None
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_file = open(output_path, "w", encoding="utf-8")
        original_write = sys.stdout.write

        def tee_write(text):
            original_write(text)
            output_file.write(text)
            output_file.flush()

        sys.stdout.write = tee_write
        print(f"Output also being written to: {output_path}")

    print(f"Query: '{query}' | top_k: {top_k}")
    print(f"Project root: {PROJECT_ROOT}")

    # Step 1: Initialize LightRAG
    print("\n--- Initializing LightRAG ---")
    rag = get_lightrag()
    if rag is None:
        print("ERROR: LightRAG initialization failed. Cannot proceed.")
        print("Check that lightrag-hku is installed and user-config.json has valid LLM config.")
        sys.exit(1)

    print(f"LightRAG initialized successfully")
    print(f"  Working dir: {rag.working_dir}")

    # Step 2: Quick status check - how many entities are there?
    print("\n--- Knowledge Graph Status ---")
    try:
        status = call_async(rag.get_processing_status())
        print(f"  Processing status: {json.dumps(status, ensure_ascii=False, default=str)}")
    except Exception as e:
        print(f"  Could not get processing status: {e}")

    try:
        labels = call_async(rag.get_graph_labels())
        print(f"  Graph labels: {json.dumps(labels, ensure_ascii=False, default=str)}")
    except Exception as e:
        print(f"  Could not get graph labels: {e}")

    # Step 3: Query each mode using rag.aquery_data() directly
    # (bypasses adapter timeout of 120s so we can see real errors)
    from lightrag import QueryParam

    print(f"\n--- Querying all 6 modes with: '{query}' ---")

    for mode in ALL_MODES:
        print_separator(f"MODE: {mode} | Query: '{query}'")

        start_time = time.time()
        try:
            param = QueryParam(mode=mode)
            param.top_k = top_k
            result = call_async(rag.aquery_data(query, param=param))
        except Exception as e:
            print(f"  EXCEPTION: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            result = None

        elapsed = time.time() - start_time
        print(f"  Elapsed time: {elapsed:.2f}s")

        print_result_detail(result, mode, query)

    # Step 4: Also test the string-based aquery() method for comparison
    print_separator("EXTRA: aquery() (string result) | only_need_context=True | mode: mix")
    start_time = time.time()
    try:
        param = QueryParam(mode="mix", only_need_context=True)
        text_result = call_async(rag.aquery(query, param=param))
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        text_result = None
    elapsed = time.time() - start_time
    print(f"  Elapsed time: {elapsed:.2f}s")
    print(f"  Result type: {type(text_result).__name__}")
    if text_result and isinstance(text_result, str):
        print(f"  Result length: {len(text_result)} chars")
        print(f"  Content (first 500 chars):")
        print(text_result[:500])
        if len(text_result) > 500:
            print("  ... (truncated)")
    else:
        print(f"  Result value: {text_result}")

    print_separator("DONE", char="-")
    print(f"All 6 modes queried with: '{query}'")


if __name__ == "__main__":
    main()