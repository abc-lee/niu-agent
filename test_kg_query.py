#!/usr/bin/env python3
"""
LightRAG KG Query Full Test Suite

Tests all query paths through the LightRAG knowledge graph to identify
why queries return empty results. Tests three layers:
  1. Adapter layer (direct LightRAGAdapter method calls)
  2. Disk Executor layer (CLI arg parsing via DiskExecutor)
  3. Config comparison (YAML vs TOOL_SCHEMAS parameter declarations)

Usage:
    python3 test_kg_query.py

No system startup required — directly imports adapter and disk_executor.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project root on sys.path ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "lightrag-server" / "src"))

# ── Test result tracking ─────────────────────────────────────────────────
_results: List[Dict[str, Any]] = []


def _record(name: str, passed: bool, result_summary: str,
            elapsed: float, failure_reason: str = ""):
    _results.append({
        "name": name,
        "passed": passed,
        "result": result_summary,
        "elapsed": elapsed,
        "failure": failure_reason,
    })
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}")
    print(f"  结果: {result_summary}")
    print(f"  耗时: {elapsed:.2f}s")
    if failure_reason:
        print(f"  失败原因: {failure_reason}")
    print()


def _safe_call(fn, *args, **kwargs):
    """Call fn safely, return (result, error_str)."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1: Adapter Layer Tests
# ═══════════════════════════════════════════════════════════════════════

def test_adapter_layer(adapter):
    """Test all LightRAGAdapter query methods directly."""
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    print("=" * 70)
    print("SECTION 1: Adapter Layer (direct method calls)")
    print("=" * 70)
    print()

    # ── 1. adapter.query() without keywords ────────────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query, query="Python编程", mode="hybrid"
    )
    elapsed = time.time() - t0
    if err:
        _record("1. adapter.query(hybrid) 无keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("1. adapter.query(hybrid) 无keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    elif result == "":
        _record("1. adapter.query(hybrid) 无keywords", False, "返回空字符串(fail_response)", elapsed,
                "LightRAG返回[no-context]错误标记")
    else:
        summary = result[:200] + "..." if len(result) > 200 else result
        _record("1. adapter.query(hybrid) 无keywords", True, f"返回文本({len(result)}字): {summary}", elapsed)

    # ── 2. adapter.query() with keywords ───────────────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query, query="Python编程", mode="hybrid", keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("2. adapter.query(hybrid) 有keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("2. adapter.query(hybrid) 有keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    elif result == "":
        _record("2. adapter.query(hybrid) 有keywords", False, "返回空字符串(fail_response)", elapsed,
                "LightRAG返回[no-context]错误标记")
    else:
        summary = result[:200] + "..." if len(result) > 200 else result
        _record("2. adapter.query(hybrid) 有keywords", True, f"返回文本({len(result)}字): {summary}", elapsed)

    # ── 3. adapter.query_data() without keywords ──────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query_data, query="Python编程", mode="local"
    )
    elapsed = time.time() - t0
    if err:
        _record("3. adapter.query_data(local) 无keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("3. adapter.query_data(local) 无keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    else:
        # Check structure
        is_no_result = LightRAGAdapter._is_no_result(result)
        if is_no_result:
            _record("3. adapter.query_data(local) 无keywords", False,
                    f"返回空结果: {json.dumps(result, ensure_ascii=False)[:300]}", elapsed,
                    "_is_no_result=True — 无匹配实体/关系")
        else:
            entities = result.get("data", {}).get("entities", []) if isinstance(result, dict) else []
            _record("3. adapter.query_data(local) 无keywords", True,
                    f"返回结构化数据: {len(entities)}个实体", elapsed)

    # ── 4. adapter.query_data() with keywords ─────────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query_data, query="Python编程", mode="local", keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("4. adapter.query_data(local) 有keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("4. adapter.query_data(local) 有keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    else:
        is_no_result = LightRAGAdapter._is_no_result(result)
        if is_no_result:
            _record("4. adapter.query_data(local) 有keywords", False,
                    f"返回空结果: {json.dumps(result, ensure_ascii=False)[:300]}", elapsed,
                    "_is_no_result=True — keywords可能未正确传递到QueryParam")
        else:
            entities = result.get("data", {}).get("entities", []) if isinstance(result, dict) else []
            _record("4. adapter.query_data(local) 有keywords", True,
                    f"返回结构化数据: {len(entities)}个实体", elapsed)

    # ── 5. adapter.search_multi_lightrag() ─────────────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.search_multi_lightrag, "Python编程", mode="local", top_k=10, keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("5. adapter.search_multi_lightrag(local) 有keywords", False, f"异常: {err}", elapsed, err)
    else:
        total_entities = sum(len(v) for v in result.values())
        if total_entities == 0:
            _record("5. adapter.search_multi_lightrag(local) 有keywords", False,
                    f"所有分类桶为空: {list(result.keys())}", elapsed,
                    "query_data返回空或无匹配实体")
        else:
            non_empty = {k: len(v) for k, v in result.items() if v}
            _record("5. adapter.search_multi_lightrag(local) 有keywords", True,
                    f"找到{total_entities}个实体: {non_empty}", elapsed)

    # ── 6. adapter.list_entities() ─────────────────────────────────────
    t0 = time.time()
    result, err = _safe_call(adapter.list_entities)
    elapsed = time.time() - t0
    if err:
        _record("6. adapter.list_entities()", False, f"异常: {err}", elapsed, err)
    else:
        status = result.get("status", "unknown")
        data = result.get("data", [])
        if status == "ok" and data:
            _record("6. adapter.list_entities()", True, f"列出{len(data)}个实体", elapsed)
        elif status == "ok" and not data:
            _record("6. adapter.list_entities()", False, "返回空列表", elapsed, "知识图谱中无实体")
        else:
            _record("6. adapter.list_entities()", False, f"status={status}: {result}", elapsed)

    # ── 7. adapter.query_data() mix mode with keywords ────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query_data, query="Python编程", mode="mix", keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("7. adapter.query_data(mix) 有keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("7. adapter.query_data(mix) 有keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    else:
        is_no_result = LightRAGAdapter._is_no_result(result)
        if is_no_result:
            _record("7. adapter.query_data(mix) 有keywords", False,
                    f"返回空结果: {json.dumps(result, ensure_ascii=False)[:300]}", elapsed,
                    "mix模式下keywords可能未正确设置hl_keywords")
        else:
            entities = result.get("data", {}).get("entities", []) if isinstance(result, dict) else []
            _record("7. adapter.query_data(mix) 有keywords", True,
                    f"返回结构化数据: {len(entities)}个实体", elapsed)

    # ── 8. adapter.query_data() naive mode (vector only) ──────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.query_data, query="Python编程", mode="naive", keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("8. adapter.query_data(naive) 有keywords", False, f"异常: {err}", elapsed, err)
    elif result is None:
        _record("8. adapter.query_data(naive) 有keywords", False, "返回None", elapsed, "LightRAG不可用或查询失败")
    else:
        is_no_result = LightRAGAdapter._is_no_result(result)
        if is_no_result:
            _record("8. adapter.query_data(naive) 有keywords", False,
                    f"返回空结果", elapsed, "naive模式(纯向量)无匹配")
        else:
            entities = result.get("data", {}).get("entities", []) if isinstance(result, dict) else []
            _record("8. adapter.query_data(naive) 有keywords", True,
                    f"返回结构化数据: {len(entities)}个实体", elapsed)

    # ── 9. adapter.search_skills() ─────────────────────────────────────
    t0 = time.time()
    result, err = _safe_call(
        adapter.search_skills, "Python编程", keywords=["Python"]
    )
    elapsed = time.time() - t0
    if err:
        _record("9. adapter.search_skills() 有keywords", False, f"异常: {err}", elapsed, err)
    else:
        _record("9. adapter.search_skills() 有keywords", len(result) > 0,
                f"找到{len(result)}个Skill实体" if result else "无Skill实体",
                elapsed,
                "" if result else "无匹配Skill类型实体")

    # ── 10. adapter.explore_node() ─────────────────────────────────────
    t0 = time.time()
    result, err = _safe_call(adapter.explore_node, "Python", depth=1)
    elapsed = time.time() - t0
    if err:
        _record("10. adapter.explore_node(Python)", False, f"异常: {err}", elapsed, err)
    else:
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])
        _record("10. adapter.explore_node(Python)", len(nodes) > 0,
                f"{len(nodes)}个节点, {len(edges)}条边",
                elapsed,
                "" if nodes else "实体'Python'不存在于图中")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2: Disk Executor Layer Tests
# ═══════════════════════════════════════════════════════════════════════

def test_disk_executor_layer():
    """Test CLI argument parsing via DiskExecutor + DiskParser."""
    print("=" * 70)
    print("SECTION 2: Disk Executor Layer (CLI arg parsing)")
    print("=" * 70)
    print()

    from niu_api.internal.disk_config import DiskConfig
    from niu_api.internal.disk_parser import DiskParser
    from niu_api.internal.disk_executor import DiskExecutor

    # Load config from YAML
    config_dir = str(PROJECT_ROOT / "config" / "disk")
    try:
        config = DiskConfig(config_dir)
    except Exception as e:
        _record("DiskConfig加载", False, f"异常: {e}", 0, str(e))
        return

    parser = DiskParser()

    # We don't need a real registry for parsing tests — just need to test
    # _build_kwargs which validates args against YAML-defined ToolConfig.
    # Create a minimal executor with None registry (won't actually call tools).
    executor = DiskExecutor(config, registry=None)

    # ── 7. Parse "/lightrag/lightrag_query Python编程 --mode hybrid --keywords Python"
    cmd = '/lightrag/lightrag_query Python编程 --mode hybrid --keywords Python'
    t0 = time.time()
    parsed = parser.parse(cmd)
    elapsed = time.time() - t0

    if parsed.action != "EXECUTE":
        _record("7. 解析lightrag_query --keywords", False,
                f"解析action={parsed.action}", elapsed, "命令未解析为EXECUTE")
    else:
        # Check if YAML has keywords defined for lightrag_query
        tool_config = config.get_tool_config("lightrag", "lightrag_query")
        yaml_args = {a.name for a in tool_config.args} if tool_config else set()
        yaml_flags = {a.flag for a in tool_config.args if a.flag} if tool_config else set()

        has_keywords_in_yaml = "keywords" in yaml_args
        has_keywords_flag = "keywords" in yaml_flags

        # Try _build_kwargs to see if --keywords is accepted
        tool_path = "/lightrag/lightrag_query"
        kwargs, error = executor._build_kwargs(parsed, tool_config, tool_path)

        if error:
            _record("7. 解析lightrag_query --keywords", False,
                    f"YAML参数: {sorted(yaml_args)}, flags: {sorted(yaml_flags)}, "
                    f"解析错误: {error[:200]}", elapsed,
                    f"YAML中lightrag_query未声明keywords参数 → --keywords被拒绝为unknown flag")
        else:
            _record("7. 解析lightrag_query --keywords", True,
                    f"kwargs={kwargs}, YAML有keywords={has_keywords_in_yaml}", elapsed)

    # ── 8. Parse "/lightrag/lightrag_search_entities Python编程 --keywords Python"
    cmd = '/lightrag/lightrag_search_entities Python编程 --keywords Python'
    t0 = time.time()
    parsed = parser.parse(cmd)
    elapsed = time.time() - t0

    if parsed.action != "EXECUTE":
        _record("8. 解析lightrag_search_entities --keywords", False,
                f"解析action={parsed.action}", elapsed, "命令未解析为EXECUTE")
    else:
        tool_config = config.get_tool_config("lightrag", "lightrag_search_entities")
        yaml_args = {a.name for a in tool_config.args} if tool_config else set()
        yaml_flags = {a.flag for a in tool_config.args if a.flag} if tool_config else set()

        has_keywords_in_yaml = "keywords" in yaml_args
        has_keywords_flag = "keywords" in yaml_flags

        tool_path = "/lightrag/lightrag_search_entities"
        kwargs, error = executor._build_kwargs(parsed, tool_config, tool_path)

        if error:
            _record("8. 解析lightrag_search_entities --keywords", False,
                    f"YAML参数: {sorted(yaml_args)}, flags: {sorted(yaml_flags)}, "
                    f"解析错误: {error[:200]}", elapsed,
                    f"YAML中lightrag_search_entities未声明keywords参数 → --keywords被拒绝为unknown flag")
        else:
            _record("8. 解析lightrag_search_entities --keywords", True,
                    f"kwargs={kwargs}, YAML有keywords={has_keywords_in_yaml}", elapsed)

    # ── 9. Parse "/lightrag/lightrag_query_data Python编程 --mode local --keywords Python"
    cmd = '/lightrag/lightrag_query_data Python编程 --mode local --keywords Python'
    t0 = time.time()
    parsed = parser.parse(cmd)
    elapsed = time.time() - t0

    if parsed.action != "EXECUTE":
        _record("9. 解析lightrag_query_data --keywords", False,
                f"解析action={parsed.action}", elapsed, "命令未解析为EXECUTE")
    else:
        tool_config = config.get_tool_config("lightrag", "lightrag_query_data")
        yaml_args = {a.name for a in tool_config.args} if tool_config else set()
        yaml_flags = {a.flag for a in tool_config.args if a.flag} if tool_config else set()

        has_keywords_in_yaml = "keywords" in yaml_args
        has_keywords_flag = "keywords" in yaml_flags

        tool_path = "/lightrag/lightrag_query_data"
        kwargs, error = executor._build_kwargs(parsed, tool_config, tool_path)

        if error:
            _record("9. 解析lightrag_query_data --keywords", False,
                    f"YAML参数: {sorted(yaml_args)}, flags: {sorted(yaml_flags)}, "
                    f"解析错误: {error[:200]}", elapsed,
                    f"解析失败: {error[:100]}")
        else:
            # Check if keywords was properly parsed as array
            kw_val = kwargs.get("keywords")
            _record("9. 解析lightrag_query_data --keywords", True,
                    f"kwargs={kwargs}, keywords值={kw_val} (type={type(kw_val).__name__})", elapsed)

    # ── 10. Parse with repeatable --keywords for query_data
    cmd = '/lightrag/lightrag_query_data Python编程 --mode local --keywords Python --keywords 编程'
    t0 = time.time()
    parsed = parser.parse(cmd)
    elapsed = time.time() - t0

    tool_config = config.get_tool_config("lightrag", "lightrag_query_data")
    tool_path = "/lightrag/lightrag_query_data"
    kwargs, error = executor._build_kwargs(parsed, tool_config, tool_path)

    if error:
        _record("10. 解析lightrag_query_data 多个--keywords(repeatable)", False,
                f"解析错误: {error[:200]}", elapsed, f"repeatable解析失败: {error[:100]}")
    else:
        kw_val = kwargs.get("keywords")
        is_list = isinstance(kw_val, list)
        _record("10. 解析lightrag_query_data 多个--keywords(repeatable)", is_list and len(kw_val) == 2,
                f"keywords={kw_val} (type={type(kw_val).__name__})", elapsed,
                "" if (is_list and len(kw_val) == 2) else f"期望list['Python','编程'], 实际={kw_val}")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3: Config Comparison (YAML vs TOOL_SCHEMAS)
# ═══════════════════════════════════════════════════════════════════════

def test_config_comparison():
    """Compare YAML config and TOOL_SCHEMAS for query tool parameter declarations."""
    print("=" * 70)
    print("SECTION 3: Config Comparison (YAML vs TOOL_SCHEMAS)")
    print("=" * 70)
    print()

    import yaml

    # Load YAML config
    yaml_path = PROJECT_ROOT / "config" / "disk" / "lightrag-server.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f)

    yaml_tools = {t["name"]: t for t in yaml_data.get("tools", [])}

    # Load TOOL_SCHEMAS
    from niu_lightrag_server import TOOL_SCHEMAS

    # Query tools to compare
    query_tools = ["lightrag_query", "lightrag_query_data", "lightrag_search_entities"]

    for tool_name in query_tools:
        # ── YAML params ────────────────────────────────────────────────
        yaml_tool = yaml_tools.get(tool_name, {})
        yaml_params = {}
        for p in yaml_tool.get("parameters", []):
            yaml_params[p["name"]] = {
                "type": p.get("type"),
                "flag": p.get("flag"),
                "position": p.get("position"),
                "required": p.get("required", False),
                "default": p.get("default", "NOT_SET"),
                "cli_format": p.get("cli_format"),
            }

        # ── TOOL_SCHEMAS params ────────────────────────────────────────
        schema = TOOL_SCHEMAS.get(tool_name, {})
        schema_params = {}
        for pname, pdef in schema.get("input_schema", {}).get("properties", {}).items():
            schema_params[pname] = {
                "type": pdef.get("type"),
                "items": pdef.get("items", {}).get("type") if "items" in pdef else None,
                "default": pdef.get("default", "NOT_SET"),
                "required": pname in schema.get("input_schema", {}).get("required", []),
            }

        # ── Compare ────────────────────────────────────────────────────
        all_params = sorted(set(yaml_params.keys()) | set(schema_params.keys()))

        differences = []
        yaml_only = set(yaml_params.keys()) - set(schema_params.keys())
        schema_only = set(schema_params.keys()) - set(yaml_params.keys())

        if yaml_only:
            differences.append(f"YAML独有参数: {yaml_only}")
        if schema_only:
            differences.append(f"SCHEMA独有参数: {schema_only}")

        for pname in all_params:
            if pname in yaml_only or pname in schema_only:
                continue
            yp = yaml_params[pname]
            sp = schema_params[pname]
            diffs = []
            # Compare type
            yt = yp["type"]
            st = sp["type"]
            if yt != st:
                # array in YAML, array in schema — check items type
                if yt == "array" and st == "array":
                    if sp.get("items") and sp["items"] != "string":
                        diffs.append(f"type items: YAML无items声明 vs SCHEMA items={sp['items']}")
                else:
                    diffs.append(f"type: YAML={yt} vs SCHEMA={st}")
            # Compare default
            yd = yp["default"]
            sd = sp["default"]
            if yd != sd and not (yd == "NOT_SET" and sd == "NOT_SET"):
                diffs.append(f"default: YAML={yd} vs SCHEMA={sd}")
            # Compare required
            yr = yp["required"]
            sr = sp["required"]
            if yr != sr:
                diffs.append(f"required: YAML={yr} vs SCHEMA={sr}")
            if diffs:
                differences.append(f"参数'{pname}': {', '.join(diffs)}")

        test_name = f"11. 配置对比: {tool_name}"
        if differences:
            _record(test_name, False, "; ".join(differences), 0,
                    "YAML和TOOL_SCHEMAS参数声明不一致，可能导致CLI解析或MCP调用失败")
        else:
            _record(test_name, True, f"YAML({len(yaml_params)}参数) 与 SCHEMA({len(schema_params)}参数) 一致", 0)

    # ── Summary: keywords presence in each tool ────────────────────────
    print("-" * 50)
    print("keywords参数存在性汇总:")
    for tool_name in query_tools:
        yaml_tool = yaml_tools.get(tool_name, {})
        yaml_has_kw = any(p["name"] == "keywords" for p in yaml_tool.get("parameters", []))
        schema = TOOL_SCHEMAS.get(tool_name, {})
        schema_has_kw = "keywords" in schema.get("input_schema", {}).get("properties", {})
        yaml_flag = "有" if yaml_has_kw else "无"
        schema_flag = "有" if schema_has_kw else "无"
        match = "一致" if yaml_has_kw == schema_has_kw else "不一致"
        print(f"  {tool_name}: YAML={yaml_flag}, SCHEMA={schema_flag} → {match}")
    print()


# ═══════════════════════════════════════════════════════════════════════
# SECTION 4: LightRAG Instance Diagnostics
# ═══════════════════════════════════════════════════════════════════════

def test_lightrag_diagnostics():
    """Check LightRAG instance health and graph content."""
    print("=" * 70)
    print("SECTION 4: LightRAG Instance Diagnostics")
    print("=" * 70)
    print()

    from niu_api.internal.lightrag_manager import get_lightrag

    # ── Check if LightRAG is available ─────────────────────────────────
    t0 = time.time()
    rag = get_lightrag()
    elapsed = time.time() - t0

    if rag is None:
        _record("12. LightRAG实例可用性", False, "get_lightrag()返回None", elapsed,
                "LightRAG未初始化 — 检查lightrag-hku是否安装、embedding模型是否可用、LLM代理是否运行")
        return None

    _record("12. LightRAG实例可用性", True, "LightRAG实例已初始化", elapsed)

    # ── Check graph content ────────────────────────────────────────────
    t0 = time.time()
    try:
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            _record("13. 知识图谱内容", False, "chunk_entity_relation_graph为None", time.time() - t0,
                    "LightRAG实例缺少图对象")
            return rag

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        node_count = nx_graph.number_of_nodes()
        edge_count = nx_graph.number_of_edges()

        if node_count == 0:
            _record("13. 知识图谱内容", False, f"0个节点, 0条边", time.time() - t0,
                    "知识图谱为空 — 所有查询必然返回空结果")
        else:
            # Sample some node names
            sample_nodes = list(nx_graph.nodes())[:5]
            _record("13. 知识图谱内容", True,
                    f"{node_count}个节点, {edge_count}条边, 样本: {sample_nodes}",
                    time.time() - t0)
    except Exception as e:
        _record("13. 知识图谱内容", False, f"异常: {e}", time.time() - t0, str(e))

    # ── Check storage directory ────────────────────────────────────────
    storage_dir = Path.home() / ".niu" / "lightrag_storage"
    if storage_dir.exists():
        files = list(storage_dir.iterdir())
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        _record("14. 存储目录", True,
                f"{len(files)}个文件, 总大小{total_size / 1024 / 1024:.1f}MB", 0)
    else:
        _record("14. 存储目录", False, f"目录不存在: {storage_dir}", 0,
                "LightRAG存储未创建")

    # ── Check QueryParam keywords behavior ─────────────────────────────
    t0 = time.time()
    try:
        from lightrag import QueryParam

        # Test 1: Default QueryParam
        p1 = QueryParam(mode="local")
        has_hl_default = hasattr(p1, 'hl_keywords')
        has_ll_default = hasattr(p1, 'll_keywords')
        hl_val = getattr(p1, 'hl_keywords', 'MISSING')
        ll_val = getattr(p1, 'll_keywords', 'MISSING')

        # Test 2: Set keywords
        p2 = QueryParam(mode="local")
        p2.ll_keywords = ["Python"]
        p2.hl_keywords = ["Python"]

        info = (f"QueryParam默认: hl_keywords={hl_val}, ll_keywords={ll_val}; "
                f"设置后: hl_keywords={p2.hl_keywords}, ll_keywords={p2.ll_keywords}")
        _record("15. QueryParam keywords行为", True, info, time.time() - t0)
    except Exception as e:
        _record("15. QueryParam keywords行为", False, f"异常: {e}", time.time() - t0, str(e))

    return rag


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print()
    print("=" * 70)
    print("LightRAG KG Query 全量测试")
    print("=" * 70)
    print()

    # ── Section 4: Diagnostics first (check if LightRAG is alive) ──────
    rag = test_lightrag_diagnostics()

    # ── Section 1: Adapter layer ───────────────────────────────────────
    if rag is not None:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        test_adapter_layer(adapter)
    else:
        print("=" * 70)
        print("SECTION 1: Adapter Layer — SKIPPED (LightRAG不可用)")
        print("=" * 70)
        print()

    # ── Section 2: Disk Executor layer ─────────────────────────────────
    test_disk_executor_layer()

    # ── Section 3: Config comparison ───────────────────────────────────
    test_config_comparison()

    # ── Final Summary ──────────────────────────────────────────────────
    print("=" * 70)
    print("总结")
    print("=" * 70)
    print()

    passed = sum(1 for r in _results if r["passed"])
    failed = sum(1 for r in _results if not r["passed"])
    total = len(_results)
    print(f"总计: {total}个测试, {passed}通过, {failed}失败")
    print()

    # Summary table for adapter query methods
    adapter_tests = [r for r in _results if "adapter" in r["name"].lower()]
    if adapter_tests:
        print("方法 | 结果 | 耗时 | 失败原因")
        print("-" * 80)
        for r in adapter_tests:
            status = "PASS" if r["passed"] else "FAIL"
            reason = r["failure"] if r["failure"] else "-"
            print(f"{r['name']} | {status} | {r['elapsed']:.2f}s | {reason}")
        print()

    # Disk executor summary
    disk_tests = [r for r in _results if "解析" in r["name"]]
    if disk_tests:
        print("CLI解析 | 结果 | 失败原因")
        print("-" * 80)
        for r in disk_tests:
            status = "PASS" if r["passed"] else "FAIL"
            reason = r["failure"][:60] if r["failure"] else "-"
            print(f"{r['name']} | {status} | {reason}")
        print()

    # Config comparison summary
    config_tests = [r for r in _results if "配置对比" in r["name"]]
    if config_tests:
        print("配置对比 | 结果 | 差异")
        print("-" * 80)
        for r in config_tests:
            status = "PASS" if r["passed"] else "FAIL"
            result_text = r["result"][:60] if r["passed"] else r["failure"][:60]
            print(f"{r['name']} | {status} | {result_text}")
        print()

    # Root cause analysis
    if failed > 0:
        print("=" * 70)
        print("根因分析")
        print("=" * 70)
        print()

        # Check for common patterns
        yaml_kw_issues = [r for r in _results if "unknown flag" in r.get("failure", "").lower()]
        empty_result_issues = [r for r in _results if "空结果" in r.get("result", "") or "_is_no_result" in r.get("failure", "")]
        lrag_unavailable = [r for r in _results if "None" in r.get("result", "") or "不可用" in r.get("failure", "")]

        if lrag_unavailable:
            print("1. LightRAG实例不可用 — 这是所有查询失败的根因")
            print("   检查: lightrag-hku是否安装、embedding模型是否可用、LLM代理是否运行")
            print()

        if yaml_kw_issues:
            print("2. YAML配置缺少keywords参数声明 — CLI路径无法传递keywords")
            print("   影响: Agent通过虚拟磁盘调用时无法使用--keywords加速查询")
            print("   修复: 在lightrag-server.yaml的lightrag_query和lightrag_search_entities中添加keywords参数")
            print()

        if empty_result_issues:
            print("3. 查询返回空结果 — 即使LightRAG可用，查询也可能因以下原因返回空:")
            print("   a) keywords未传递到QueryParam (检查query_data实现)")
            print("   b) 知识图谱确实为空 (检查list_entities结果)")
            print("   c) 向量索引损坏或embedding模型不匹配")
            print("   d) LLM关键词提取失败 (不传keywords时)")
            print()


if __name__ == "__main__":
    main()
