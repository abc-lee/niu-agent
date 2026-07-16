"""LightRAG 数据一致性检查（v4：3 真相源不可动 + 9 派生文件 missing）。

检查项：
1. 3 真相源完整可用（GraphML + full_docs + cache）→ critical = unrecoverable
2. 9 派生文件 missing 检测 → major（需 repair 重建）

全新用户合法：3 真相源都不存在时，9 派生文件 missing 也不报错。
"""

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"

_TRUTH_SOURCE_FILES = [
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]

# 9 派生文件（跟 lightrag_repair._DERIVED_FILES 一致）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]

# 僵尸脑区 description 语义标记（LLM 写的 description，明确告诉系统这个实体该删）
# v8-Task 1：原使用者 repair_brainregion_zombies 已删除（违反铁律 3）。
# 当前此常量无引用，保留供 Task 8 重写 repair_all 时脑区节点识别复用。
_ZOMBIE_DESCRIPTION_MARKERS = (  # pyright: ignore[reportUnusedVariable]
    "被删除的重复脑区实体之一",
    "被删除的脑区",
    "已删除的脑区",
    "已删除的重复脑区",
)


def _resolve_storage_dir() -> Path:
    return _STORAGE_DIR


def _load_json_dict(path: Path) -> tuple[dict, dict | None]:  # pyright: ignore[reportUnusedFunction]
    """加载 JSON dict 文件，返回 (data, error)。"""
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}, {
                "check": "json_type_mismatch",
                "file": path.name,
                "msg": f"expected dict, got {type(data).__name__}",
                "severity": "critical",
            }
        return data, None
    except json.JSONDecodeError as e:
        return {}, {
            "check": "json_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:
        return {}, {
            "check": "json_read",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }


def _load_graphml(path: Path) -> tuple[set[str], list[tuple[str, str]], dict[str, dict[str, str]], dict[str, Any] | None]:
    """解析 GraphML 文件，返回 (node_ids, edges, node_meta, error)。"""
    if not path.exists():
        return set(), [], {}, None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    graph = root.find("graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return set(), [], {}, {
            "check": "no_graph_element",
            "file": path.name,
            "severity": "critical",
        }

    node_ids: set[str] = set()
    edges: list[tuple[str, str]] = []
    node_meta: dict[str, dict[str, str]] = {}
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if nid:
                node_ids.add(nid)
                meta = {"entity_type": "", "description": "", "source_id": ""}
                for data in child:
                    d_key = data.get("key", "")
                    d_text = data.text or ""
                    if d_key == "d1":
                        meta["entity_type"] = d_text
                    elif d_key == "d2":
                        meta["description"] = d_text
                    elif d_key == "d3":
                        meta["source_id"] = d_text
                node_meta[nid] = meta
        elif tag == "edge":
            src = child.get("source", "")
            tgt = child.get("target", "")
            edges.append((src, tgt))
    return node_ids, edges, node_meta, None


def _load_vdb(path: Path) -> tuple[list[dict], dict[str, Any] | None]:
    """加载 vdb 文件，返回 (data_list, error)。"""
    if not path.exists():
        return [], None  # 文件不存在视为空 vdb
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return [], {
                "check": "vdb_type_mismatch",
                "file": path.name,
                "msg": f"expected dict, got {type(data).__name__}",
                "severity": "major",
            }
        return data.get("data", []) or [], None
    except json.JSONDecodeError as e:
        return [], {
            "check": "vdb_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "major",
        }
    except Exception as e:
        return [], {
            "check": "vdb_read",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "major",
        }


def _check_truth_source(fname: str, storage_dir: Path) -> dict[str, Any]:
    """检测单个真相源文件（全新用户合法，空文件/空 dict/不存在都 ok）。

    只有"文件存在但 JSON 解析失败/内容残缺（非 dict）"才算 critical。
    """
    fpath = storage_dir / fname
    if not fpath.exists():
        # 文件不存在 = 全新用户，ok（返回空 dict 表示无错误）
        return {}
    try:
        size = fpath.stat().st_size
        if size == 0:
            # 空文件 = 全新用户，ok
            return {}
        data = json.loads(fpath.read_text())
        if not isinstance(data, dict):
            return {
                "check": "truth_source_corrupt",
                "severity": "critical",
                "file": fname,
                "msg": f"真相源 {fname} 内容非 dict（{type(data).__name__}）",
            }
        # 空 dict 或有内容都 ok（全新用户合法）
        return {}
    except json.JSONDecodeError as e:
        return {
            "check": "truth_source_corrupt",
            "severity": "critical",
            "file": fname,
            "msg": f"真相源 {fname} JSON 解析失败: {e}",
        }
    except Exception as e:
        return {
            "check": "truth_source_read_fail",
            "severity": "critical",
            "file": fname,
            "msg": f"真相源 {fname} 读取失败: {e}",
        }


def _check_graphml_post(storage_dir: Path) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
    """后置验证：GraphML 是否存在且非空。"""
    graphml_path = storage_dir / _GRAPHML_FILE
    if not graphml_path.exists():
        return {
            "check": "graphml_missing",
            "severity": "major",
            "file": _GRAPHML_FILE,
            "msg": "GraphML 不存在（重建未完成或失败）",
        }
    try:
        size = graphml_path.stat().st_size
        if size == 0:
            return {
                "check": "graphml_empty",
                "severity": "major",
                "file": _GRAPHML_FILE,
                "msg": "GraphML 为空文件",
            }
        node_ids, _, _, err = _load_graphml(graphml_path)
        if err:
            return err
        if not node_ids:
            return {
                "check": "graphml_no_nodes",
                "severity": "major",
                "file": _GRAPHML_FILE,
                "msg": "GraphML 无 node（重建失败信号）",
            }
    except Exception as e:
        return {
            "check": "graphml_read_fail",
            "severity": "major",
            "file": _GRAPHML_FILE,
            "msg": f"GraphML 读取失败: {e}",
        }
    return {}


def _check_vdb_missing(storage_dir: Path) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
    """检测 vdb_*_missing：GraphML 有 node 但 vdb 没对应向量。

    返回 errors 列表（可能为空）。
    """
    errors: list[dict[str, Any]] = []

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err or not node_ids:
        return errors  # GraphML 有问题由 _check_graphml_post 报，这里不重复

    # vdb_entities 检测：GraphML node 应在 vdb_entities 有对应向量
    vdb_e_path = storage_dir / "vdb_entities.json"
    vdb_e_list, vdb_e_err = _load_vdb(vdb_e_path)
    if vdb_e_err:
        errors.append(vdb_e_err)
    else:
        # vdb_entities 的 entity_name 集合
        vdb_e_names = {
            entry.get("entity_name", "").lower() if isinstance(entry, dict) else ""
            for entry in vdb_e_list
        }
        vdb_e_names.discard("")
        # GraphML node id 是小写化的（LightRAG 设计），直接比对
        missing_in_vdb = {n for n in node_ids if n.lower() not in vdb_e_names}
        if missing_in_vdb:
            errors.append({
                "check": "vdb_entities_missing",
                "severity": "major",
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_entities.json",
                "missing_count": len(missing_in_vdb),
                "msg": f"GraphML 有 {len(missing_in_vdb)} 个 node 在 vdb_entities 中无对应向量",
            })

    # vdb_relationships 检测：GraphML edge 应在 vdb_relationships 有对应向量
    _, edges, _, _ = _load_graphml(storage_dir / _GRAPHML_FILE)
    vdb_r_path = storage_dir / "vdb_relationships.json"
    vdb_r_list, vdb_r_err = _load_vdb(vdb_r_path)
    if vdb_r_err:
        errors.append(vdb_r_err)
    elif edges:
        # vdb_relationships 的 (src, tgt) 集合
        # 用 sorted pair 比对，跟 repair_vdb_relationships 写入逻辑一致
        # （repair_vdb_relationships 用 sorted((src, tgt)) 存 src_id/tgt_id，见 lightrag_repair.py:1441）
        vdb_r_pairs = set()
        for entry in vdb_r_list:
            if not isinstance(entry, dict):
                continue
            src = entry.get("src_id", "")
            tgt = entry.get("tgt_id", "")
            if src and tgt:
                vdb_r_pairs.add(tuple(sorted((src.lower(), tgt.lower()))))
        # GraphML edge 集合（同样用 sorted pair，跟 vdb_r_pairs 比对一致）
        graphml_pairs = {tuple(sorted((s.lower(), t.lower()))) for s, t in edges}
        missing_pairs = graphml_pairs - vdb_r_pairs
        if missing_pairs:
            errors.append({
                "check": "vdb_relationships_missing",
                "severity": "major",
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_relationships.json",
                "missing_count": len(missing_pairs),
                "msg": f"GraphML 有 {len(missing_pairs)} 条 edge 在 vdb_relationships 中无对应向量",
            })

    return errors


def _check_derived_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测 9 派生文件 missing。

    全新用户场景（3 真相源都不存在）时，派生文件 missing 不报错
    （LightRAG 首次启动会自动初始化所有文件）。

    Returns:
        errors 列表（可能为空）。每个 error 含 file/severity=major/msg。
    """
    errors: list[dict[str, Any]] = []

    # 全新用户判定：3 真相源都不存在 → 派生文件 missing 不报错
    truth_sources_exist = any(
        (storage_dir / fname).exists() for fname in _TRUTH_SOURCE_FILES
    )
    if not truth_sources_exist:
        return errors  # 全新用户，不报错

    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if not fpath.exists():
            errors.append({
                "check": "derived_file_missing",
                "severity": "major",
                "file": fname,
                "msg": f"派生文件 {fname} 缺失（需要 repair 重建）",
            })
        elif fpath.stat().st_size == 0:
            errors.append({
                "check": "derived_file_empty",
                "severity": "major",
                "file": fname,
                "msg": f"派生文件 {fname} 为空（需要 repair 重建）",
            })
    return errors


def _check_truth_source_graphml(storage_dir: Path) -> dict[str, Any]:
    """检测 GraphML 真相源完好性（XML 解析）。

    全新用户合法：文件不存在 / size=0 → ok
    损坏：XML 解析失败 / 无 graph 元素 → critical
    """
    graphml_path = storage_dir / _GRAPHML_FILE
    if not graphml_path.exists():
        return {}  # 全新用户合法
    try:
        size = graphml_path.stat().st_size
        if size == 0:
            return {}  # 全新用户合法
        # 用现有 _load_graphml 解析（已处理 namespace + graph 元素 fallback）
        _, _, _, err = _load_graphml(graphml_path)
        if err:
            # _load_graphml 返回的 err 已含 check/severity/file/msg
            return err
        return {}  # 解析成功
    except Exception as e:
        return {
            "check": "truth_source_read_fail",
            "severity": "critical",
            "file": _GRAPHML_FILE,
            "msg": f"GraphML 读取失败: {e}",
        }


def check_all() -> dict[str, Any]:
    """v4 简化版 check_all：检 3 真相源完好性 + 9 派生文件 missing。

    1. 检 3 真相源（GraphML + full_docs + cache）完好性
       - 文件不存在 / size=0 → ok（全新用户合法）
       - JSON/XML 解析失败 / 非 dict → critical
    2. 检 9 派生文件 missing
       - 全新用户（3 真相源都不存在）时不报错
       - 否则 missing/empty 文件 → major

    Returns:
        {
            "ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "errors": list[dict],
            "checks": {
                "truth_source": {"name": ..., "errors": list},
                "derived_missing": {"name": ..., "errors": list},
            },
        }
    """
    storage_dir = _resolve_storage_dir()
    all_errors: list[dict[str, Any]] = []

    # 1. 检测 3 真相源（GraphML + full_docs + cache）
    #    GraphML 走 XML 专门检测（不是 JSON），其他 2 个走 _check_truth_source
    truth_errors: list[dict[str, Any]] = []
    for fname in _TRUTH_SOURCE_FILES:
        if fname == _GRAPHML_FILE:
            err = _check_truth_source_graphml(storage_dir)
        else:
            err = _check_truth_source(fname, storage_dir)
        if err:
            truth_errors.append(err)
            all_errors.append(err)

    # 2. 检测 9 派生文件 missing
    derived_errors = _check_derived_missing(storage_dir)
    all_errors.extend(derived_errors)

    critical = sum(1 for e in all_errors if e.get("severity") == "critical")
    major = sum(1 for e in all_errors if e.get("severity") == "major")
    minor = sum(1 for e in all_errors if e.get("severity") == "minor")

    return {
        "ok": len(all_errors) == 0,
        "critical_errors": critical,
        "major_errors": major,
        "minor_errors": minor,
        "errors": all_errors,
        "checks": {
            "truth_source": {"name": "truth_source", "errors": truth_errors},
            "derived_missing": {"name": "derived_missing", "errors": derived_errors},
        },
    }
