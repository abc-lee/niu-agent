"""LightRAG 数据一致性检查 v2（3 真相源 corrupt + vdb 数据一致性）。

检查项：
1. 3 真相源 corrupt 检测（GraphML XML 解析失败 / full_docs/cache JSON 解析失败）→ critical
2. vdb 与 GraphML 数据一致性检测（node/edge 在 vdb 有对应向量）→ major = 真损坏
3. vdb 文件内部一致性检测（matrix 行数 vs data 条数，孤儿向量）→ major
   （nano-vectordb _cosine_query 孤儿行号越界崩溃；检测到后由
   lightrag_manager.run_resilience_phase1 自动修复——从 data.vector 重建 matrix）

派生 kv_store 文件缺失不是损坏：脑区/Skills 注入路径只写 GraphML + 3 vdb +
可选 text_chunks，其他派生 kv_store 由 LightRAG 内部 JsonKVStorage.initialize
按需 lazy 加载为空 dict（load_json() or {}），运行时按需 upsert。
本方案不主动调用任何重建，不写空文件。
"""

import base64
import json
import logging
import os
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"

_TRUTH_SOURCE_FILES = [
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]

# vdb 文件（nano-vectordb 持久化：matrix=base64(float32)，data[].vector=base64(zlib(float16))）
# v3 新增：内部一致性检测（matrix 行数 vs data 条数）覆盖这三个文件
# （R3-P1 修正：vdb_chunks.json 同样被 LightRAG local 查询路径
#  _perform_kg_search → _get_vector_context → chunks_vdb.query 检索——漏修则查询仍越界；
#  2026-08-14 实证三文件全部不一致：entities 3225/3227、relationships 6265/6266、chunks 1095/1097）
_VDB_FILES = ["vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"]

# 6 派生 kv_store 文件（仅用于文档入库 pipeline，脑区/Skills 路径不写）
# 注意：lightrag_repair._DERIVED_FILES 仍含 9 个文件（含 3 vdb）用于 repair_all 删除派生，
# 本清单只列 kv_store 派生用于检测（vdb 由 _check_vdb_missing 数据一致性检查负责）。
# 派生 kv_store 缺失不是损坏——LightRAG JsonKVStorage.initialize 把缺失文件当空 dict。
_DERIVED_FILES_KVSTORE = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
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
        data = json.loads(path.read_text(encoding="utf-8"))
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
        data = json.loads(path.read_text(encoding="utf-8"))
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


def _load_vdb_full(path: Path) -> tuple[list[dict], int | None, dict[str, Any] | None]:
    """加载 vdb 文件，返回 (data_list, matrix_rows, error)。

    matrix_rows 从 matrix base64 + 顶层 embedding_dim 计算（len(bytes) // (4*dim)）。
    matrix 键缺失（旧格式）→ matrix_rows=None，调用方跳过一致性检查（不误报）；
    matrix 键存在但为空串（0 行空矩阵）→ matrix_rows=0（R3-P3 修正：空 matrix + 非空 data
    是真实不一致形态，须报 mismatch 而非跳过）。
    matrix 字节数不能被 4*dim 整除 → 格式损坏，报 major error。
    """
    if not path.exists():
        return [], None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return [], None, {
                "check": "vdb_type_mismatch",
                "file": path.name,
                "msg": f"expected dict, got {type(data).__name__}",
                "severity": "major",
            }
        entries = data.get("data", []) or []
        dim = data.get("embedding_dim")
        if "matrix" in data and dim:
            matrix_b64 = data["matrix"]
            if matrix_b64:
                raw = base64.b64decode(matrix_b64)
                row_bytes = 4 * int(dim)
                if len(raw) % row_bytes != 0:
                    return entries, None, {
                        "check": "vdb_matrix_format",
                        "file": path.name,
                        "msg": f"matrix 字节数 {len(raw)} 不能被 4*embedding_dim({row_bytes}) 整除——格式损坏",
                        "severity": "major",
                    }
                return entries, len(raw) // row_bytes, None
            return entries, 0, None  # matrix 键存在但空串 → 0 行
        return entries, None, None  # 无 matrix 键（旧格式）→ 跳过
    except json.JSONDecodeError as e:
        return [], None, {"check": "vdb_parse", "file": path.name, "msg": str(e), "severity": "major"}
    except Exception as e:
        return [], None, {"check": "vdb_read", "file": path.name, "msg": f"{type(e).__name__}: {e}", "severity": "major"}


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
        data = json.loads(fpath.read_text(encoding="utf-8"))
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


def _check_vdb_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测 vdb_*_missing：GraphML 有 node/edge 但 vdb 没对应向量。

    数据一致性检查（v2 真损坏判定）：GraphML node/edge ⊆ vdb 向量集合。
    返回 errors 列表（可能为空）。
    """
    errors: list[dict[str, Any]] = []

    # 一次解析 GraphML 拿 node_ids + edges（避免重复解析数十 MB 文件）
    node_ids, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err or not node_ids:
        # GraphML 解析失败或无 node：critical 由 _check_truth_source_graphml 报，
        # 这里不重复报；无 node 时也无 edge，vdb 一致性检查无意义
        return errors

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
    # 复用 L282 已解析的 edges（避免重复解析 GraphML）
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


def _check_vdb_internal(storage_dir: Path) -> list[dict[str, Any]]:
    """vdb 文件内部一致性：matrix 行数 vs data 条数（v3 新增）。

    不一致 → major（真损坏）：nano-vectordb _cosine_query 的
    filter_index[sort_index] 会因孤儿向量行号 ≥ len(data) 越界崩溃
    （2026-08-14 实证：index 3225 is out of bounds for axis 0 with size 3225）。
    matrix 缺失（旧格式/全新）→ 跳过不误报。
    """
    errors: list[dict[str, Any]] = []
    for fname in _VDB_FILES:
        path = storage_dir / fname
        entries, matrix_rows, err = _load_vdb_full(path)
        if err:
            errors.append(err)
            continue
        if matrix_rows is None:
            continue  # 无 matrix → 无法比对
        if matrix_rows != len(entries):
            errors.append({
                "check": "vdb_matrix_mismatch",
                "severity": "major",
                "target_file": fname,
                "matrix_rows": matrix_rows,
                "data_count": len(entries),
                "msg": f"{fname} matrix 行数({matrix_rows}) != data 条数({len(entries)})——查询会越界崩溃",
            })
    return errors


def _decode_vdb_vector(entry: dict) -> np.ndarray | None:
    """解压 data[].vector（base64(zlib(float16))）→ float32 L2 归一化向量。

    matrix 存的是 L2 归一化行（nano-vectordb upsert 对 cosine metric 先 normalize
    再 vstack）。data[].vector 实测（2026-08-14）解压后已是归一化向量
    （范数 0.9999~1.0001，float16 量化偏差内）——此处无条件归一化对已归一化
    输入幂等，修正量化范数偏差并对齐 matrix 的 float32 单位行语义。
    任何解码/范数异常 → None（调用方判定 data 损坏）。
    """
    try:
        raw = base64.b64decode(entry["vector"])
        vec = np.frombuffer(zlib.decompress(raw), dtype=np.float16).astype(np.float32)
        norm = np.linalg.norm(vec)
        if not np.isfinite(norm) or norm <= 0:
            return None
        return vec / norm
    except Exception:
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON：写同目录 .tmp 文件 + os.replace（避免写一半崩溃损坏文件）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _repair_vdb_matrix_inplace(vdb_path: Path) -> dict[str, Any]:
    """从 data.vector 重建 matrix（外科 in-place 修复）——data 是权威，matrix 完全重建。

    任一条 vector 解码失败 → 不写回，status=error（data 本身损坏，需走全量重建
    repair_all 路径——本函数不碰 data、不删其他文件）。
    返回 {status, target_file, data_count, matrix_rows, message}。
    """
    result: dict[str, Any] = {"target_file": vdb_path.name}
    try:
        data = json.loads(vdb_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {**result, "status": "error", "message": f"expected dict, got {type(data).__name__}"}
        entries = data.get("data", []) or []
        dim = data.get("embedding_dim") or 768  # 顶层缺失兜底 768（LightRAG bge-base-zh-v1.5 固定维度）
        if not entries:
            # 空 data：matrix 若非空则清空（(0, dim)），保持一致
            if data.get("matrix"):
                empty = np.array([], dtype=np.float32).reshape(0, int(dim))
                data["matrix"] = base64.b64encode(empty.tobytes()).decode()
                _atomic_write_json(vdb_path, data)
            return {**result, "status": "ok", "data_count": 0, "matrix_rows": 0, "message": "空 data，无需重建"}
        rows = [_decode_vdb_vector(e) for e in entries]
        bad = sum(1 for r in rows if r is None)
        if bad:
            return {**result, "status": "error", "data_count": len(entries),
                    "message": f"{bad}/{len(entries)} 条 vector 解码失败——data 损坏，需走全量重建"}
        matrix = np.vstack(rows).astype(np.float32)  # type: ignore[arg-type]
        if matrix.shape[1] != int(dim):
            return {**result, "status": "error",
                    "message": f"向量维度 {matrix.shape[1]} != embedding_dim {dim}"}
        data["matrix"] = base64.b64encode(matrix.tobytes()).decode()
        _atomic_write_json(vdb_path, data)
        return {**result, "status": "ok", "data_count": len(entries), "matrix_rows": int(matrix.shape[0]),
                "message": "matrix 已从 data.vector 重建"}
    except Exception as e:
        return {**result, "status": "error", "message": f"{type(e).__name__}: {e}"}


def auto_repair_vdb_matrices() -> dict[str, Any]:
    """自动修复所有 vdb 文件的 matrix/data 不一致。返回 {"repaired": [...], "errors": [...]}。

    只修复真不一致文件（matrix_rows != len(data)）——健康文件不重建
    （避免从 float16 data.vector 重导出 float32 matrix 引入量化降级，R1-P1 修正）。
    """
    storage_dir = _resolve_storage_dir()
    repaired: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fname in _VDB_FILES:
        path = storage_dir / fname
        if not path.exists():
            continue
        entries, matrix_rows, err = _load_vdb_full(path)
        if err:
            errors.append(err)
            continue
        if matrix_rows is None or matrix_rows == len(entries):
            continue  # 无 matrix（旧格式）或已一致 → 跳过
        r = _repair_vdb_matrix_inplace(path)
        (repaired if r.get("status") == "ok" else errors).append(r)
    return {"repaired": repaired, "errors": errors}


def _check_derived_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测派生 kv_store 文件缺失。

    v2 修复（2026-07-28）：派生 kv_store 文件缺失不是损坏。
    LightRAG fork 版的脑区/Skills 注入路径只写 GraphML + 3 vdb + 可选 text_chunks，
    不写 doc_status / entity_chunks / relation_chunks / full_entities / full_relations。
    LightRAG `JsonKVStorage.initialize`（json_kv_impl.py:62）`load_json() or {}` 把
    缺失文件当空 dict，运行时按需 upsert——**本方案不主动调用任何重建，不写空文件**。

    真损坏由 `_check_vdb_missing`（vdb 与 GraphML 数据一致性）和
    `_check_truth_source`（3 真相源 corrupt）负责。

    派生缺失记录 INFO 日志（不进 errors 列表，不阻断启动）——保留知情权，
    让用户在日志里能看到"派生文件 X 缺失（正常状态，未入库文档）"。

    Returns:
        空列表（保留函数签名兼容 `check_all` 调用方）。
    """
    for fname in _DERIVED_FILES_KVSTORE:
        fpath = storage_dir / fname
        if not fpath.exists():
            logger.info(
                "派生文件 %s 缺失（正常状态，未入库文档，LightRAG 按需 lazy 加载为空 dict）",
                fname,
            )
    return []


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
    """v4 简化版 check_all：检 3 真相源完好性 + 6 派生 kv_store missing + vdb 一致性。

    1. 检 3 真相源（GraphML + full_docs + cache）完好性
       - 文件不存在 / size=0 → ok（全新用户合法）
       - JSON/XML 解析失败 / 非 dict → critical
    2. 检 6 派生 kv_store 文件 missing（_check_derived_missing——当前恒返回 []，
       派生缺失由 LightRAG 内部 lazy 加载兜底，不是损坏）
    3. 检 vdb 与 GraphML 数据一致性（_check_vdb_missing——node/edge 缺对应
       向量 → major，防数据丢失）
    4. 检 3 vdb 文件内部一致性（_check_vdb_internal——vdb_entities/vdb_relationships/
       vdb_chunks 的 matrix 行数 vs data 条数）
       - 不一致 → major vdb_matrix_mismatch（启动自动修复：从 data.vector 重建 matrix）

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
                "vdb_missing": {"name": ..., "errors": list},
                "vdb_internal": {"name": ..., "errors": list},
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

    # 2. 检测派生 kv_store 文件缺失（v2：不再报 major，派生缺失不是损坏）
    derived_errors = _check_derived_missing(storage_dir)
    all_errors.extend(derived_errors)

    # 3. 检测 vdb 与 GraphML 数据一致性（真损坏：node/edge 缺对应向量）
    #    v2 启用：原为死代码（标 pyright: ignore），现 check_all 主动调用
    vdb_errors = _check_vdb_missing(storage_dir)
    all_errors.extend(vdb_errors)

    # 4. 检测 vdb 文件内部一致性（matrix 行数 vs data 条数）——v3 新增
    #    不一致 → major（查询必崩：_cosine_query filter_index[sort_index] 越界）
    vdb_internal_errors = _check_vdb_internal(storage_dir)
    all_errors.extend(vdb_internal_errors)

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
            "vdb_missing": {"name": "vdb_missing", "errors": vdb_errors},
            "vdb_internal": {"name": "vdb_internal", "errors": vdb_internal_errors},
        },
    }
