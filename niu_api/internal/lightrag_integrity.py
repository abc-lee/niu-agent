"""LightRAG 数据一致性检查（简化版 v2）。

检查项：
1. 2 真相源完整可用（full_docs + llm_response_cache）
2. GraphML 后置验证（重建后应该有 node）
3. vdb_*_missing 检测（GraphML 有 node 但 vdb 没对应向量 → 启动放行风险）
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
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]

# 僵尸脑区 description 语义标记（LLM 写的 description，明确告诉系统这个实体该删）
# repair_brainregion_zombies（lightrag_repair.py:1775）import 这个常量用于：
# 1. 识别 GraphML 里 description 含"被删除"标记的脑区 node
# 2. 清理 llm_response_cache 里 entity_type=brainregion + description 含标记的 extract entry
# 注意：替换 lightrag_integrity.py 时必须保留这个常量，否则 lightrag_repair.py 会 ImportError
_ZOMBIE_DESCRIPTION_MARKERS = (
    "被删除的重复脑区实体之一",
    "被删除的脑区",
    "已删除的脑区",
    "已删除的重复脑区",
)


def _resolve_storage_dir() -> Path:
    return _STORAGE_DIR


def _load_json_dict(path: Path) -> tuple[dict, dict | None]:
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


def _check_graphml_post(storage_dir: Path) -> dict[str, Any]:
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
        node_ids, edges, _, err = _load_graphml(graphml_path)
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


def _check_vdb_missing(storage_dir: Path) -> list[dict[str, Any]]:
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


def check_all() -> dict[str, Any]:
    """简化版 check_all v2：检 2 真相源 + GraphML 后置 + vdb_*_missing。
    """
    storage_dir = _resolve_storage_dir()
    all_errors: list[dict[str, Any]] = []

    # 1. 检测 2 真相源
    truth_errors = []
    for fname in _TRUTH_SOURCE_FILES:
        err = _check_truth_source(fname, storage_dir)
        if err:
            truth_errors.append(err)
            all_errors.append(err)

    # 2. 后置验证 GraphML
    graphml_errors = []
    graphml_err = _check_graphml_post(storage_dir)
    if graphml_err:
        graphml_errors.append(graphml_err)
        all_errors.append(graphml_err)

    # 3. vdb_*_missing 检测
    vdb_errors = _check_vdb_missing(storage_dir)
    all_errors.extend(vdb_errors)

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
            "graphml_post": {"name": "graphml_post", "errors": graphml_errors},
            "vdb_missing": {"name": "vdb_missing", "errors": vdb_errors},
        },
    }
