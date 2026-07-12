"""LightRAG 数据一致性外挂检测（因果链引用完整性版）

设计原则：
1. **空文件不是错**——新用户/刚清空时所有文件都空，因果链自洽，合法启动
2. **不一致才是错**——引用的 key 在被引用方不存在 = 因果链断裂 = 损坏
3. **不做假数据**——修不好让 check 仍检测到损坏，拒绝启动

10 项因果链检查（每项只验证引用完整性，不检查文件是否空）：
| #  | 检查                      | 引用方 -> 被引用方                                | severity |
|----|---------------------------|---------------------------------------------------|----------|
| 1  | entity_chunks 引用悬空    | kv_store_entity_chunks key -> GraphML node        | major    |
| 2  | relation_chunks 引用悬空  | kv_store_relation_chunks key -> GraphML edge       | major    |
| 3  | text_chunks 文档悬空      | kv_store_text_chunks full_doc_id -> full_docs     | critical |
| 4  | text_chunks 缓存悬空      | kv_store_text_chunks llm_cache_list -> llm_cache  | minor    |
| 5  | doc_status chunks 悬空    | kv_store_doc_status chunks_list -> text_chunks    | major    |
| 6  | vdb_entities 向量缺失     | GraphML node -> vdb_entities.data __id__          | major    |
| 7  | vdb_relationships 向量缺失| GraphML edge -> vdb_relationships.data __id__      | major    |
| 8  | vdb_chunks 向量缺失       | text_chunks -> vdb_chunks.data __id__             | major    |
| 9  | GraphML edge 端点悬空     | GraphML edge source/target -> GraphML node        | major    |
| 10 | vdb_relationships 端点悬空| vdb_relationships src_id/tgt_id -> GraphML node   | major    |

文件级 critical（JSON 解析失败 / matrix 维度不匹配）= 文件本身损坏。

空文件合法：文件不存在或 JSON 解析为空 dict（`{}`）= 通过（无引用即无悬空）。
JSON 解析为 list 或其他类型 = 文件级 critical。
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from loguru import logger

from lightrag.utils import (
    compute_mdhash_id,
    make_relation_vdb_ids,
    parse_relation_chunk_key,
)

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"


# =============================================================================
# 工具函数：文件读取 + GraphML 解析
# =============================================================================


def _resolve_storage_dir() -> Path:
    """返回 _STORAGE_DIR 的 Path 形式（兼容 monkeypatch 注入 str 的场景）。"""
    return Path(_STORAGE_DIR)


def _load_json_dict(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """加载 JSON 文件为 dict。

    Returns:
        (data, error)  二元组：
        - 文件不存在 → ({}, None)
        - JSON 解析为空 dict 或非空 dict → (dict, None)
        - JSON 解析为 list 或其他类型 → (None, {"check": "json_not_dict", ...})
        - JSON 解析失败 → (None, {"check": "json_parse", ...})
    """
    if not path.exists():
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return None, {
            "check": "json_parse",
            "file": path.name,
            "msg": str(e),
            "line": e.lineno,
            "col": e.colno,
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return None, {
            "check": "json_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }
    if not isinstance(raw, dict):
        return None, {
            "check": "json_not_dict",
            "file": path.name,
            "type": type(raw).__name__,
            "severity": "critical",
        }
    return raw, None


def _load_vdb(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, dict[str, Any] | None]:
    """加载 nano-vectordb 文件，返回 (raw_dict, data_list, error)。

    Returns:
        - 文件不存在 → ({}, [], None)（空数据，通过）
        - matrix 维度不匹配 → (raw, data_list, {"check": "matrix_size_mismatch", ...})（critical 文件级损坏）
        - JSON 解析失败 / 非 dict 类型 → (None, None, error)
        - data 不是 list → (None, None, {"check": "data_not_list", ...})
    """
    if not path.exists():
        return {}, [], None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return None, None, {
            "check": "json_parse",
            "file": path.name,
            "msg": str(e),
            "line": e.lineno,
            "col": e.colno,
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return None, None, {
            "check": "json_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }
    if not isinstance(raw, dict):
        return None, None, {
            "check": "json_not_dict",
            "file": path.name,
            "type": type(raw).__name__,
            "severity": "critical",
        }
    data_list = raw.get("data", [])
    if not isinstance(data_list, list):
        return None, None, {
            "check": "data_not_list",
            "file": path.name,
            "type": type(data_list).__name__,
            "severity": "critical",
        }
    # 检查 matrix 维度（如果存在 embedding_dim 和 matrix 字段）
    embedding_dim = raw.get("embedding_dim")
    matrix_b64 = raw.get("matrix", "")
    if embedding_dim is not None and matrix_b64:
        import base64

        try:
            matrix_bytes = base64.b64decode(matrix_b64)
            expected_bytes = 4 * embedding_dim * len(data_list)
            if len(matrix_bytes) != expected_bytes:
                return raw, data_list, {
                    "check": "matrix_size_mismatch",
                    "file": path.name,
                    "bytes": len(matrix_bytes),
                    "expected": expected_bytes,
                    "severity": "critical",
                }
        except Exception as e:  # noqa: BLE001
            return raw, data_list, {
                "check": "matrix_b64_decode",
                "file": path.name,
                "msg": f"{type(e).__name__}: {e}",
                "severity": "critical",
            }
    return raw, data_list, None


def _load_graphml(path: Path) -> tuple[set[str], list[tuple[str, str]], dict[str, dict[str, str]], dict[str, Any] | None]:
    """解析 GraphML 文件，返回 (node_ids, edges, node_meta, error)。

    Returns:
        - 文件不存在 → (set(), [], {}, None)（空数据，通过）
        - XML 解析失败 → (set(), [], {}, {"check": "xml_parse", ...})（critical）
        - 成功 → (node_id_set, [(src, tgt), ...], {node_id: {entity_type, description, source_id}}, None)

    注意：node id 和 edge source/target 都已 lower 化（LightRAG 设计），
    这里不再额外 lower，直接使用原始值。
    """
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
    except Exception as e:  # noqa: BLE001
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    # 找到 graph 元素（支持 namespace）
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


def _parse_brain_meta(description: str | None) -> dict[str, str]:
    """解析脑区 description 里的 brain_meta_* 字段。

    description 格式：<SEP> 分隔的多字段，每段形如 `brain_meta_<key>:<value>`

    Returns:
        {field_name_without_prefix: value}，比如 {"size": "0", "shrink_count": "1", ...}
        空字段（value 为空）也保留，便于检测 size:0 这种"故意 0"的语义。
    """
    if not description:
        return {}
    result: dict[str, str] = {}
    parts = description.split("<SEP>")
    for part in parts:
        if not part:
            continue
        if ":" in part:
            key, _, value = part.partition(":")
            if key.startswith("brain_meta_"):
                result[key[len("brain_meta_"):]] = value
    return result


# =============================================================================
# 10 项因果链检查 + 文件级 critical
# =============================================================================


def check_entity_chunks_dangling() -> dict[str, Any]:
    """检查 #1: kv_store_entity_chunks 的 key(entity_name) 是否都在 GraphML node 里。

    引用方：kv_store_entity_chunks 的 key
    被引用方：GraphML node id 集合
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    ec_data, ec_err = _load_json_dict(storage_dir / "kv_store_entity_chunks.json")
    if ec_err:
        errors.append(ec_err)
        return {"name": "entity_chunks_dangling", "errors": errors}
    if not ec_data:
        return {"name": "entity_chunks_dangling", "errors": []}

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "entity_chunks_dangling", "errors": errors}

    for entity_name in ec_data:
        if entity_name not in node_ids:
            errors.append({
                "check": "entity_chunks_dangling",
                "severity": "major",
                "ref_key": entity_name,
                "ref_file": "kv_store_entity_chunks.json",
                "target_file": _GRAPHML_FILE,
                "msg": f"entity_chunks key '{entity_name}' 在 GraphML node 中不存在",
            })
    return {"name": "entity_chunks_dangling", "errors": errors}


def check_relation_chunks_dangling() -> dict[str, Any]:
    """检查 #2: kv_store_relation_chunks 的每个 key 拆分为 (src, tgt)，检查 GraphML 是否存在 edge (src, tgt) 或 (tgt, src)。

    引用方：kv_store_relation_chunks 的 key（格式 `<SEP>` 分隔的 src/tgt）
    被引用方：GraphML edge 集合（无向，正序+逆序都算匹配）
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    rc_data, rc_err = _load_json_dict(storage_dir / "kv_store_relation_chunks.json")
    if rc_err:
        errors.append(rc_err)
        return {"name": "relation_chunks_dangling", "errors": errors}
    if not rc_data:
        return {"name": "relation_chunks_dangling", "errors": []}

    _, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "relation_chunks_dangling", "errors": errors}

    # 构造无向 edge 集合：frozenset({src, tgt}) → True
    edge_set: set[frozenset[str]] = {frozenset((src, tgt)) for src, tgt in edges if src and tgt}

    for key in rc_data:
        try:
            src, tgt = parse_relation_chunk_key(key)
        except ValueError:
            errors.append({
                "check": "relation_chunk_key_invalid",
                "severity": "major",
                "ref_key": key,
                "ref_file": "kv_store_relation_chunks.json",
                "msg": f"relation_chunks key '{key}' 解析失败（非 <SEP> 格式）",
            })
            continue
        if frozenset((src, tgt)) not in edge_set:
            errors.append({
                "check": "relation_chunks_dangling",
                "severity": "major",
                "ref_key": key,
                "ref_src": src,
                "ref_tgt": tgt,
                "ref_file": "kv_store_relation_chunks.json",
                "target_file": _GRAPHML_FILE,
                "msg": f"relation_chunks key '{key}' 在 GraphML edges 中不存在（src={src}, tgt={tgt}）",
            })
    return {"name": "relation_chunks_dangling", "errors": errors}


def check_text_chunks_doc_dangling() -> dict[str, Any]:
    """检查 #3: kv_store_text_chunks 的 full_doc_id 是否都在 full_docs 里。

    引用方：kv_store_text_chunks 的 full_doc_id 字段
    被引用方：kv_store_full_docs 的 key
    severity: critical（真相源断裂）

    只检查普通文档 chunk（full_doc_id 以 'doc-' 或 'refined:' 开头）。
    自定义 KG chunk（brain_*/custom_kg_*/skill://*/文件路径等）通过 ainsert_custom_kg
    写入，不写 full_docs，跳过检查以避免误报。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "text_chunks_doc_dangling", "errors": errors}
    if not tc_data:
        return {"name": "text_chunks_doc_dangling", "errors": []}

    fd_data, fd_err = _load_json_dict(storage_dir / "kv_store_full_docs.json")
    if fd_err:
        errors.append(fd_err)
        return {"name": "text_chunks_doc_dangling", "errors": errors}
    assert fd_data is not None  # fd_err is None → fd_data is not None

    for chunk_id, chunk_value in tc_data.items():
        if not isinstance(chunk_value, dict):
            continue
        full_doc_id = chunk_value.get("full_doc_id", "")
        if not full_doc_id:
            continue
        # 自定义 KG chunk 跳过（只有普通文档 chunk 才在 full_docs 里）
        if not full_doc_id.startswith("doc-") and not full_doc_id.startswith("refined:"):
            continue
        if full_doc_id not in fd_data:
            errors.append({
                "check": "text_chunks_doc_dangling",
                "severity": "critical",
                "ref_key": chunk_id,
                "full_doc_id": full_doc_id,
                "ref_file": "kv_store_text_chunks.json",
                "target_file": "kv_store_full_docs.json",
                "msg": f"text_chunks['{chunk_id}'].full_doc_id='{full_doc_id}' 在 full_docs 中不存在",
            })
    return {"name": "text_chunks_doc_dangling", "errors": errors}


def check_text_chunks_cache_dangling() -> dict[str, Any]:
    """检查 #4: kv_store_text_chunks 的 llm_cache_list 引用的 cache_key 是否都在 llm_response_cache 里。

    引用方：kv_store_text_chunks 的 llm_cache_list 字段（不存在视为空列表，通过）
    被引用方：kv_store_llm_response_cache 的 key
    severity: minor（缓存丢失可重建）
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "text_chunks_cache_dangling", "errors": errors}
    if not tc_data:
        return {"name": "text_chunks_cache_dangling", "errors": []}

    cache_data, cache_err = _load_json_dict(storage_dir / "kv_store_llm_response_cache.json")
    if cache_err:
        errors.append(cache_err)
        return {"name": "text_chunks_cache_dangling", "errors": errors}
    assert cache_data is not None  # cache_err is None → cache_data is not None

    for chunk_id, chunk_value in tc_data.items():
        if not isinstance(chunk_value, dict):
            continue
        cache_list = chunk_value.get("llm_cache_list", [])
        if not cache_list:
            # 字段不存在或空列表 → 通过
            continue
        if not isinstance(cache_list, list):
            continue
        for cache_key in cache_list:
            if not isinstance(cache_key, str):
                continue
            if cache_key not in cache_data:
                errors.append({
                    "check": "text_chunks_cache_dangling",
                    "severity": "minor",
                    "ref_key": chunk_id,
                    "cache_key": cache_key,
                    "ref_file": "kv_store_text_chunks.json",
                    "target_file": "kv_store_llm_response_cache.json",
                    "msg": f"text_chunks['{chunk_id}'].llm_cache_list 引用 '{cache_key}' 在 llm_response_cache 中不存在",
                })
    return {"name": "text_chunks_cache_dangling", "errors": errors}


def check_doc_status_chunks_dangling() -> dict[str, Any]:
    """检查 #5: kv_store_doc_status 的 chunks_list 引用的 chunk_id 是否都在 text_chunks 里。

    引用方：kv_store_doc_status 的 chunks_list 字段
    被引用方：kv_store_text_chunks 的 key
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    ds_data, ds_err = _load_json_dict(storage_dir / "kv_store_doc_status.json")
    if ds_err:
        errors.append(ds_err)
        return {"name": "doc_status_chunks_dangling", "errors": errors}
    if not ds_data:
        return {"name": "doc_status_chunks_dangling", "errors": []}

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "doc_status_chunks_dangling", "errors": errors}
    if tc_data is None:
        return {"name": "doc_status_chunks_dangling", "errors": errors}
    tc_keys: set[str] = set(tc_data.keys())

    for doc_id, doc_value in ds_data.items():
        if not isinstance(doc_value, dict):
            continue
        chunks_list = doc_value.get("chunks_list", [])
        if not chunks_list:
            continue
        if not isinstance(chunks_list, list):
            continue
        for chunk_id in chunks_list:
            if not isinstance(chunk_id, str):
                continue
            if chunk_id not in tc_keys:
                errors.append({
                    "check": "doc_status_chunks_dangling",
                    "severity": "major",
                    "ref_key": doc_id,
                    "chunk_id": chunk_id,
                    "ref_file": "kv_store_doc_status.json",
                    "target_file": "kv_store_text_chunks.json",
                    "msg": f"doc_status['{doc_id}'].chunks_list 引用 '{chunk_id}' 在 text_chunks 中不存在",
                })
    return {"name": "doc_status_chunks_dangling", "errors": errors}


def check_vdb_entities_missing() -> dict[str, Any]:
    """检查 #6: GraphML 每个 node 的 `ent-{md5(name)}` 是否都在 vdb_entities.data 里。

    引用方：GraphML node（id = entity_name）
    被引用方：vdb_entities.data 的 __id__ 字段
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "vdb_entities_missing", "errors": errors}
    if not node_ids:
        return {"name": "vdb_entities_missing", "errors": []}

    _, vdb_data, vdb_err = _load_vdb(storage_dir / "vdb_entities.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_entities_missing", "errors": errors}

    vdb_ids: set[str] = set()
    for item in vdb_data or []:
        if isinstance(item, dict):
            iid = item.get("__id__")
            if iid:
                vdb_ids.add(iid)

    for name in node_ids:
        expected_id = compute_mdhash_id(name, prefix="ent-")
        if expected_id not in vdb_ids:
            errors.append({
                "check": "vdb_entities_missing",
                "severity": "major",
                "ref_node": name,
                "expected_id": expected_id,
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_entities.json",
                "msg": f"GraphML node '{name}' 的 vdb id '{expected_id}' 在 vdb_entities 中不存在",
            })
    return {"name": "vdb_entities_missing", "errors": errors}


def check_vdb_relationships_missing() -> dict[str, Any]:
    """检查 #7: GraphML 每个 edge 用 make_relation_vdb_ids 生成候选 ID 列表（正序+逆序），检查列表中是否至少一个 ID 在 vdb_relationships.data 里。

    引用方：GraphML edge (src, tgt)
    被引用方：vdb_relationships.data 的 __id__ 字段
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    _, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "vdb_relationships_missing", "errors": errors}
    if not edges:
        return {"name": "vdb_relationships_missing", "errors": []}

    _, vdb_data, vdb_err = _load_vdb(storage_dir / "vdb_relationships.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_relationships_missing", "errors": errors}

    vdb_ids: set[str] = set()
    for item in vdb_data or []:
        if isinstance(item, dict):
            iid = item.get("__id__")
            if iid:
                vdb_ids.add(iid)

    for src, tgt in edges:
        if not src or not tgt:
            continue
        candidate_ids = make_relation_vdb_ids(src, tgt)
        if not any(cid in vdb_ids for cid in candidate_ids):
            errors.append({
                "check": "vdb_relationships_missing",
                "severity": "major",
                "ref_edge": f"{src}->{tgt}",
                "candidate_ids": candidate_ids,
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_relationships.json",
                "msg": f"GraphML edge '{src}->{tgt}' 的候选 vdb id {candidate_ids} 在 vdb_relationships 中均不存在",
            })
    return {"name": "vdb_relationships_missing", "errors": errors}


def check_vdb_chunks_missing() -> dict[str, Any]:
    """检查 #8: text_chunks 每个 chunk 的 `chunk-{md5(content)}` 是否都在 vdb_chunks.data 里。

    引用方：kv_store_text_chunks 的每个 chunk（key=chunk_id, value.content=内容）
    被引用方：vdb_chunks.data 的 __id__ 字段
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "vdb_chunks_missing", "errors": errors}
    if not tc_data:
        return {"name": "vdb_chunks_missing", "errors": []}

    _, vdb_data, vdb_err = _load_vdb(storage_dir / "vdb_chunks.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_chunks_missing", "errors": errors}

    vdb_ids: set[str] = set()
    for item in vdb_data or []:
        if isinstance(item, dict):
            iid = item.get("__id__")
            if iid:
                vdb_ids.add(iid)

    for chunk_id, chunk_value in tc_data.items():
        if not isinstance(chunk_value, dict):
            continue
        content = chunk_value.get("content", "")
        if not content:
            continue
        expected_id = compute_mdhash_id(content, prefix="chunk-")
        if expected_id not in vdb_ids:
            errors.append({
                "check": "vdb_chunks_missing",
                "severity": "major",
                "ref_key": chunk_id,
                "expected_id": expected_id,
                "ref_file": "kv_store_text_chunks.json",
                "target_file": "vdb_chunks.json",
                "msg": f"text_chunks['{chunk_id}'] 的 vdb id '{expected_id}' 在 vdb_chunks 中不存在",
            })
    return {"name": "vdb_chunks_missing", "errors": errors}


def check_graphml_edge_dangling() -> dict[str, Any]:
    """检查 #9: GraphML 每个 edge 的 source 和 target 是否都在 GraphML node 集合中存在。

    引用方：GraphML edge source/target
    被引用方：GraphML node id 集合
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "graphml_edge_dangling", "errors": errors}
    if not edges:
        return {"name": "graphml_edge_dangling", "errors": []}

    for src, tgt in edges:
        if src and src not in node_ids:
            errors.append({
                "check": "graphml_edge_dangling_source",
                "severity": "major",
                "source": src,
                "target": tgt,
                "ref_file": _GRAPHML_FILE,
                "msg": f"GraphML edge source '{src}' 在 node 集合中不存在（target={tgt}）",
            })
        if tgt and tgt not in node_ids:
            errors.append({
                "check": "graphml_edge_dangling_target",
                "severity": "major",
                "source": src,
                "target": tgt,
                "ref_file": _GRAPHML_FILE,
                "msg": f"GraphML edge target '{tgt}' 在 node 集合中不存在（source={src}）",
            })
    return {"name": "graphml_edge_dangling", "errors": errors}


def check_vdb_relationships_endpoint_dangling() -> dict[str, Any]:
    """检查 #10: vdb_relationships 每条记录的 src_id / tgt_id 是否都在 GraphML node 中存在。

    引用方：vdb_relationships.data 的 src_id / tgt_id 字段
    被引用方：GraphML node id 集合
    severity: major
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "vdb_relationships_endpoint_dangling", "errors": errors}
    if not node_ids:
        # GraphML 为空，但 vdb_relationships 可能有数据 → 检查 vdb 是否也空
        _, vdb_data, vdb_err = _load_vdb(storage_dir / "vdb_relationships.json")
        if vdb_err:
            errors.append(vdb_err)
            return {"name": "vdb_relationships_endpoint_dangling", "errors": errors}
        if not vdb_data:
            return {"name": "vdb_relationships_endpoint_dangling", "errors": []}
        # vdb 有数据但 GraphML 空 → 每条记录的端点都悬空
        for i, item in enumerate(vdb_data or []):
            if not isinstance(item, dict):
                continue
            src_id = item.get("src_id", "")
            tgt_id = item.get("tgt_id", "")
            if src_id:
                errors.append({
                    "check": "vdb_relationships_endpoint_dangling",
                    "severity": "major",
                    "index": i,
                    "src_id": src_id,
                    "tgt_id": tgt_id,
                    "ref_file": "vdb_relationships.json",
                    "target_file": _GRAPHML_FILE,
                    "msg": f"vdb_relationships[{i}].src_id='{src_id}' 在 GraphML node 中不存在（GraphML 为空）",
                })
        return {"name": "vdb_relationships_endpoint_dangling", "errors": errors}

    _, vdb_data, vdb_err = _load_vdb(storage_dir / "vdb_relationships.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_relationships_endpoint_dangling", "errors": errors}
    if not vdb_data:
        return {"name": "vdb_relationships_endpoint_dangling", "errors": []}

    for i, item in enumerate(vdb_data or []):
        if not isinstance(item, dict):
            continue
        src_id = item.get("src_id", "")
        tgt_id = item.get("tgt_id", "")
        if src_id and src_id not in node_ids:
            errors.append({
                "check": "vdb_relationships_endpoint_dangling",
                "severity": "major",
                "index": i,
                "src_id": src_id,
                "tgt_id": tgt_id,
                "ref_file": "vdb_relationships.json",
                "target_file": _GRAPHML_FILE,
                "msg": f"vdb_relationships[{i}].src_id='{src_id}' 在 GraphML node 中不存在",
            })
        if tgt_id and tgt_id not in node_ids:
            errors.append({
                "check": "vdb_relationships_endpoint_dangling",
                "severity": "major",
                "index": i,
                "src_id": src_id,
                "tgt_id": tgt_id,
                "ref_file": "vdb_relationships.json",
                "target_file": _GRAPHML_FILE,
                "msg": f"vdb_relationships[{i}].tgt_id='{tgt_id}' 在 GraphML node 中不存在",
            })
    return {"name": "vdb_relationships_endpoint_dangling", "errors": errors}


# =============================================================================
# 语义维度检查（句法自洽但语义死亡的数据）
# =============================================================================

# "被删除"语义标记（LLM 写的 description，明确告诉系统这个实体该删）
_ZOMBIE_DESCRIPTION_MARKERS = (
    "被删除的重复脑区实体之一",
    "被删除的脑区",
    "已删除的脑区",
    "已删除的重复脑区",
)


def check_brainregion_semantic_zombie() -> dict[str, Any]:
    """语义 check #1: 检测脑区 description 含'被删除'标记但 GraphML node 仍存在。

    引用方：脑区 description 的语义标记
    被引用方：GraphML node 存在性
    severity: major（句法自洽但语义死亡，会让 dissolve 卡在中间态）

    历史 Agent 用 custom_kg 写"删除日志"但没真删，description 含明确"被删除"标记。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "brainregion_semantic_zombie", "errors": errors}
    if not node_meta:
        return {"name": "brainregion_semantic_zombie", "errors": []}

    for nid, meta in node_meta.items():
        # 只检测 brainregion 类型
        if meta.get("entity_type") != "brainregion":
            continue
        desc = meta.get("description", "")
        if not desc:
            continue
        for marker in _ZOMBIE_DESCRIPTION_MARKERS:
            if marker in desc:
                errors.append({
                    "check": "brainregion_semantic_zombie",
                    "severity": "major",
                    "ref_key": nid,
                    "ref_file": _GRAPHML_FILE,
                    "target_file": _GRAPHML_FILE,
                    "marker": marker,
                    "msg": f"脑区 '{nid}' description 含语义标记'{marker}'但 node 仍存在（僵尸脑区）",
                })
                break  # 一个脑区只报一次（匹配第一个 marker 就停）
    return {"name": "brainregion_semantic_zombie", "errors": errors}


def check_entity_chunks_source_id_mismatch() -> dict[str, Any]:
    """语义 check #3: 检测 entity_chunks 的 chunk_ids 跟 GraphML node d3 source_id 不一致。

    引用方：kv_store_entity_chunks 的 chunk_ids
    被引用方：GraphML node 的 d3 source_id
    severity: major

    正常情况：脑区 d3 source_id 应该是脑区专属 chunk_id（brain_xxx），
    entity_chunks 的 chunk_ids 也应该指向同一个 chunk。
    僵尸脑区情况：d3 = 脑区专属 chunk，但 entity_chunks 指向"删除日志"chunk——明显异常。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    ec_data, ec_err = _load_json_dict(storage_dir / "kv_store_entity_chunks.json")
    if ec_err:
        errors.append(ec_err)
        return {"name": "entity_chunks_source_id_mismatch", "errors": errors}
    if not ec_data:
        return {"name": "entity_chunks_source_id_mismatch", "errors": []}

    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "entity_chunks_source_id_mismatch", "errors": errors}

    for entity_name, ec_entry in ec_data.items():
        if not isinstance(ec_entry, dict):
            continue
        ec_chunk_ids = ec_entry.get("chunk_ids", [])
        meta = node_meta.get(entity_name)
        if meta is None:
            # 实体不在 GraphML，由 check_entity_chunks_dangling 报，这里不重复
            continue
        graphml_source_id = meta.get("source_id", "")
        if not graphml_source_id:
            continue  # GraphML 没记 source_id，跳过（没法比对）
        # GraphML d3 可能含 <SEP> 分隔多个 source_id
        graphml_ids = [s for s in graphml_source_id.split("<SEP>") if s]
        # 检查 ec_chunk_ids 是否都在 graphml_ids 里
        ec_ids_set = set(ec_chunk_ids)
        graphml_ids_set = set(graphml_ids)
        # 不一致 = ec_chunk_ids 有 graphml_ids 没有的 chunk
        orphan_ec_ids = ec_ids_set - graphml_ids_set
        if orphan_ec_ids:
            errors.append({
                "check": "entity_chunks_source_id_mismatch",
                "severity": "major",
                "ref_key": entity_name,
                "ref_file": "kv_store_entity_chunks.json",
                "target_file": _GRAPHML_FILE,
                "graphml_source_id": graphml_source_id,
                "entity_chunks_ids": list(ec_chunk_ids),
                "orphan_ids": list(orphan_ec_ids),
                "msg": f"实体 '{entity_name}' entity_chunks 指向 {list(orphan_ec_ids)} 但 GraphML d3 source_id 是 {graphml_source_id}",
            })
    return {"name": "entity_chunks_source_id_mismatch", "errors": errors}


# =============================================================================
# 文件级 critical 预扫描
# =============================================================================


_KV_STORE_FILES = [
    "kv_store_doc_status.json",
    "kv_store_entity_chunks.json",
    "kv_store_full_docs.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_relation_chunks.json",
    "kv_store_text_chunks.json",
    "kv_store_llm_response_cache.json",
]

_VDB_FILES = [
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
]


def _check_file_level_critical() -> dict[str, Any]:
    """文件级 critical 预扫描：扫所有 JSON 文件 + GraphML，确保文件本身没损坏。

    文件损坏 = JSON 解析失败 / JSON 不是 dict（kv_store）/ matrix 维度不匹配（vdb）/ XML 解析失败（GraphML）。
    空文件 = 通过（无引用即无悬空）。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    # kv_store 文件
    for fname in _KV_STORE_FILES:
        _, err = _load_json_dict(storage_dir / fname)
        if err:
            errors.append(err)

    # vdb 文件（用 _load_vdb 检查 matrix 维度）
    for fname in _VDB_FILES:
        _, _, err = _load_vdb(storage_dir / fname)
        if err:
            errors.append(err)

    # GraphML 文件
    _, _, _, err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if err:
        errors.append(err)

    return {"name": "file_level_critical", "errors": errors}


# =============================================================================
# check_all 聚合
# =============================================================================


_CHECK_FUNCTIONS = [
    _check_file_level_critical,
    check_entity_chunks_dangling,
    check_relation_chunks_dangling,
    check_text_chunks_doc_dangling,
    check_text_chunks_cache_dangling,
    check_doc_status_chunks_dangling,
    check_vdb_entities_missing,
    check_vdb_relationships_missing,
    check_vdb_chunks_missing,
    check_graphml_edge_dangling,
    check_vdb_relationships_endpoint_dangling,
]


def check_all() -> dict[str, Any]:
    """聚合 10 项因果链检查 + 文件级 critical 检查。

    Returns:
        {
            "ok": bool,                   # critical==0 and major==0
            "storage_dir": str,
            "errors": list[dict],          # 所有 errors（带 severity）
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "checks": dict[str, dict],     # 每项检查的详细 report
        }
    """
    all_errors: list[dict[str, Any]] = []
    checks: dict[str, dict[str, Any]] = {}
    for fn in _CHECK_FUNCTIONS:
        report = fn()
        checks[report["name"]] = report
        all_errors.extend(report["errors"])

    critical_count = sum(1 for e in all_errors if e.get("severity") == "critical")
    major_count = sum(1 for e in all_errors if e.get("severity") == "major")
    minor_count = sum(1 for e in all_errors if e.get("severity") == "minor")

    ok = (critical_count == 0 and major_count == 0)

    return {
        "ok": ok,
        "storage_dir": str(_STORAGE_DIR),
        "errors": all_errors,
        "critical_errors": critical_count,
        "major_errors": major_count,
        "minor_errors": minor_count,
        "checks": checks,
    }


def check_all_vdbs() -> dict[str, Any]:
    """检测所有 vdb 文件（文件级 + 引用完整性子集）。

    兼容旧 API：只跑 vdb 相关的检查（#6/#7/#8/#10）。
    """
    vdb_checks = [
        check_vdb_entities_missing,
        check_vdb_relationships_missing,
        check_vdb_chunks_missing,
        check_vdb_relationships_endpoint_dangling,
    ]
    all_errors: list[dict[str, Any]] = []
    files: dict[str, dict[str, Any]] = {}
    for fn in vdb_checks:
        report = fn()
        files[report["name"]] = report
        all_errors.extend(report["errors"])
    critical_count = sum(1 for e in all_errors if e.get("severity") == "critical")
    major_count = sum(1 for e in all_errors if e.get("severity") == "major")
    return {
        "ok": (critical_count == 0 and major_count == 0),
        "files": files,
        "errors": all_errors,
        "critical_errors": critical_count,
        "major_errors": major_count,
        "minor_errors": sum(1 for e in all_errors if e.get("severity") == "minor"),
    }


# 保留向后兼容的废弃函数签名（已废弃，新代码应使用 check_all）
def check_vdb(path: str) -> dict[str, Any]:  # noqa: ARG001
    """已废弃：用 check_all() 或 check_all_vdbs() 代替。"""
    logger.warning("check_vdb is deprecated, use check_all() instead")
    return {"file": path, "ok": True, "errors": [], "stats": {}, "deprecated": True}


def check_kv_store(path: str) -> dict[str, Any]:  # noqa: ARG001
    """已废弃：用 check_all() 代替。"""
    logger.warning("check_kv_store is deprecated, use check_all() instead")
    return {"file": path, "ok": True, "errors": [], "stats": {}, "deprecated": True}


def check_graphml(path: str) -> dict[str, Any]:  # noqa: ARG001
    """已废弃：用 check_all() 代替。"""
    logger.warning("check_graphml is deprecated, use check_all() instead")
    return {"file": path, "ok": True, "errors": [], "stats": {}, "deprecated": True}


def check_entity_sync() -> dict[str, Any]:
    """已废弃：用 check_vdb_entities_missing() + check_entity_chunks_dangling() 代替。"""
    logger.warning("check_entity_sync is deprecated, use check_all() instead")
    return {"ok": True, "errors": [], "stats": {}, "deprecated": True}
