"""LightRAG 外挂修复（按依赖链从真相源重建版）

设计原则：
1. **空文件不是错** — repair 期间无数据 → 返回 ok（expected=0, actual=0）
2. **不做假数据** — 修不好 status=error 不写文件，让 check 仍检测到损坏
3. **真相源不可重建** — full_docs/text_chunks 损坏 → unrecoverable=True
4. **按依赖链重建** — 先修上游再修下游

依赖链：
  full_docs (真相源，不可重建)
    ↓ chunking
  text_chunks (真相源，不可重建)
    ↓ 从 text_chunks.full_doc_id 反向构建 chunk→doc 映射
  doc_status (chunks_list 从 text_chunks 的 key 派生)
    ↓ 重跑 LLM extract（用 llm_response_cache 重放）
  GraphML (图谱结构)
    ↓ embedding
  vdb_entities + vdb_relationships (实体/关系向量)
    ↓ embedding text_chunks
  vdb_chunks (chunk 向量)
    ↓ 从 GraphML source_id 提取
  entity_chunks + relation_chunks (chunk 引用)
    ↓ 从 GraphML source_id → chunk→doc 映射
  full_entities + full_relations (文档级索引)
  llm_response_cache (不可重建，清空)

每个 repair 函数返回：
  {
    "status": "ok"|"error",
    "expected": int,   # 应重建数量
    "actual": int,     # 实际重建数量
    "lost": int,       # 丢失数量 = expected - actual
    "source": str,     # 数据源说明
    "message": str,
    "unrecoverable": bool,  # 可选，True 表示无法修复
  }
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
import zlib
from pathlib import Path
from typing import Any

from loguru import logger

from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import (
    compute_mdhash_id,
    make_relation_chunk_key,
    make_relation_vdb_ids,
)

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"


# =============================================================================
# 工具函数
# =============================================================================


def _storage_dir() -> Path:
    """获取 _STORAGE_DIR（兼容 monkeypatch 注入 str 的形式）。"""
    return Path(_STORAGE_DIR)


def _atomic_write_json(path: Path, data: Any, indent: int | None = None) -> None:
    """原子写 JSON：写 tmp + fsync + replace。

    Args:
        path: 目标文件路径
        data: 要序列化的对象
        indent: json.dump 的 indent 参数（None = 紧凑）
    """
    tmp_file = path.with_name(path.name + ".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, path)


def _backup_corrupt(path: Path) -> None:
    """备份损坏文件到 .corrupt.{ms_timestamp}.bak。

    用毫秒时间戳防 1 秒内连续 repair 覆盖。
    备份失败不 abort（只是日志警告，让 repair 继续写新文件）。
    """
    if not path.exists():
        return
    timestamp = int(time.time() * 1000)
    bak_path = path.with_name(f"{path.name}.corrupt.{timestamp}.bak")
    try:
        shutil.copy2(path, bak_path)
        logger.info(f"[LightRAGRepair] 损坏文件备份到: {bak_path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LightRAGRepair] 备份损坏文件失败: {e}（继续覆盖）")


def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """批量 embedding。

    优先用 niu_api.internal.embedding 预加载的模型。
    失败 fallback 到 LightRAG 实例的 embedding_func。
    都失败返回 None。

    空列表返回 []（不调模型）。
    """
    if not texts:
        return []

    # 1. 优先用预加载的 embedding 模型
    try:
        from niu_api.internal.embedding import get_model

        model = get_model()
        if model is not None:
            vecs = model.encode(texts)
            # 转 list[list[float]]（vecs 可能是 numpy ndarray 或 Tensor）
            return [list(map(float, v)) for v in vecs]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LightRAGRepair] 预加载 embedding 模型失败: {e}，fallback LightRAG")

    # 2. fallback 到 LightRAG 实例
    try:
        import asyncio

        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
        if rag is None:
            logger.error("[LightRAGRepair] embedding 失败：预加载模型未就绪 + LightRAG 未初始化")
            return None
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(rag.embedding_func(texts))
            return [list(map(float, v)) for v in result]
        finally:
            loop.close()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LightRAGRepair] LightRAG embedding 也失败: {e}")
        return None


def _embed_text(text: str) -> list[float] | None:
    """单条 embedding（内部调 _embed_batch）。

    失败返回 None（不抛异常，让调用方决定如何处理）。
    """
    batch = _embed_batch([text])
    if batch is None or len(batch) == 0:
        return None
    return batch[0]


def _get_embedding_dim() -> int:
    """获取 embedding 维度。

    优先调 _embed_text 测一条获取维度。
    失败 fallback 768（bge-base-zh-v1.5 默认）。
    """
    try:
        vec = _embed_text("dim_probe")
        if vec is not None and len(vec) > 0:
            return len(vec)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LightRAGRepair] embedding 维度探测失败: {e}，用 fallback 768")
    return 768


def _encode_vector(vec_f16) -> str:
    """vector 字段三层编码：base64(zlib(float16 bytes))"""
    import numpy as np

    arr = vec_f16.astype(np.float16) if hasattr(vec_f16, "astype") else np.array(vec_f16, dtype=np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix(matrix_f32) -> str:
    """matrix 字段一层编码：base64(float32 bytes)"""
    import numpy as np

    arr = matrix_f32.astype(np.float32) if hasattr(matrix_f32, "astype") else np.array(matrix_f32, dtype=np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    """加载 JSON 文件为 dict。

    Returns:
        - 文件不存在 → {}（空 dict，合法）
        - JSON 解析失败 / 非 dict → None（损坏）
        - 成功 → dict
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, Exception):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _load_graphml_nodes_edges() -> tuple[set[str], list[tuple[str, str, str, str, str]], dict[str, Any] | None]:
    """解析 GraphML，返回 (node_ids, edges, error)。

    node_ids: set of node id
    edges: list of (src, tgt, edge_source_id, edge_description, edge_keywords)
           - edge_source_id: edge 的 d10 字段（<SEP> 分隔的 chunk_id 列表）
           - edge_description: edge 的 d8 字段（描述文本）
           - edge_keywords: edge 的 d9 字段（关系关键词，<SEP> 分隔）
    error: None 或 {"check": ..., "severity": "critical", ...}

    GraphML edge key 定义（参考真实 GraphML 头部）：
        d7=weight, d8=description, d9=keywords, d10=source_id,
        d11=file_path, d12=created_at, d13=truncate
    """
    import xml.etree.ElementTree as ET

    path = _storage_dir() / _GRAPHML_FILE
    if not path.exists():
        return set(), [], None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return set(), [], {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return set(), [], {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    ns = "{http://graphml.graphdrawing.org/xmlns}"
    node_ids: set[str] = set()
    edges: list[tuple[str, str, str, str, str]] = []  # (src, tgt, edge_source_id, edge_description, edge_keywords)

    # 找 graph 元素
    graph = root.find(f"{ns}graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return set(), [], {
            "check": "no_graph_element",
            "file": _GRAPHML_FILE,
            "severity": "critical",
        }

    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if nid:
                node_ids.add(nid)
        elif tag == "edge":
            src = child.get("source", "")
            tgt = child.get("target", "")
            edge_src_id = ""
            edge_desc = ""
            edge_keywords = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d8":
                    edge_desc = data.text or ""
                elif key == "d10":
                    edge_src_id = data.text or ""
                elif key == "d9":
                    edge_keywords = data.text or ""
            edges.append((src, tgt, edge_src_id, edge_desc, edge_keywords))
    return node_ids, edges, None


def _load_graphml_nodes() -> tuple[dict[str, tuple[str, str]], dict[str, Any] | None]:
    """解析 GraphML nodes，返回 {node_id: (description, source_id)} + error。

    description = d2, source_id = d3
    """
    import xml.etree.ElementTree as ET

    path = _storage_dir() / _GRAPHML_FILE
    if not path.exists():
        return {}, None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return {}, {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return {}, {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    ns = "{http://graphml.graphdrawing.org/xmlns}"
    nodes: dict[str, tuple[str, str]] = {}

    graph = root.find(f"{ns}graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return {}, {
            "check": "no_graph_element",
            "file": _GRAPHML_FILE,
            "severity": "critical",
        }

    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if not nid:
                continue
            desc = ""
            src = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d2":
                    desc = data.text or ""
                elif key == "d3":
                    src = data.text or ""
            nodes[nid] = (desc, src)
    return nodes, None


def _build_vdb_file(
    vdb_path: Path, data_list: list[dict[str, Any]], vectors: list[list[float]],
    embedding_dim: int,
) -> None:
    """构造 vdb 文件内容并原子写入。

    每条 data 的 vector 字段已 encode 后存入；matrix 单独 encode 后存入。
    """
    import numpy as np

    matrix_f32 = np.array(vectors, dtype=np.float32) if vectors else np.zeros((0, embedding_dim), dtype=np.float32)
    # 编码 vector 字段到每条 data
    encoded_data = []
    for item, vec in zip(data_list, vectors):
        new_item = {k: v for k, v in item.items() if k != "vector"}
        new_item["vector"] = _encode_vector(np.array(vec, dtype=np.float16))
        encoded_data.append(new_item)
    storage = {
        "embedding_dim": embedding_dim,
        "data": encoded_data,
        "matrix": _encode_matrix(matrix_f32),
    }
    _atomic_write_json(vdb_path, storage)


# =============================================================================
# 11 个 repair 函数（按依赖链顺序）
# =============================================================================


def repair_text_chunks() -> dict[str, Any]:
    """1. 从 full_docs 重新 chunking 重建 text_chunks。

    真相源：kv_store_full_docs.json
    派生：kv_store_text_chunks.json

    用 LightRAG 的 chunking_by_token_size（需要 tokenizer）。
    chunk_id = compute_mdhash_id(content, prefix="chunk-")

    如果 chunk_size 配置变更导致 chunk_id 不一致（跟旧 doc_status.chunks_list 比对）→ unrecoverable=True。
    如果 full_docs 损坏 → unrecoverable=True。
    """
    storage_dir = _storage_dir()
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"
    doc_status_path = storage_dir / "kv_store_doc_status.json"

    # 1. 读 full_docs（真相源）
    full_docs = _load_json_dict(full_docs_path)
    if full_docs is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_full_docs",
            "message": "full_docs 损坏（JSON 解析失败或非 dict）",
            "unrecoverable": True,
        }
    if not full_docs:
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_full_docs",
            "message": "full_docs 为空，无需重建 text_chunks",
        }

    # 2. 获取 chunking 配置（跟 lightrag_manager 一致）
    try:
        from niu_api.internal.lightrag_manager import _get_lightrag_config

        config = _get_lightrag_config()
        chunk_token_size = config.get("chunk_token_size", 1200)
        chunk_overlap_token_size = config.get("chunk_overlap_token_size", 50)
    except Exception:  # noqa: BLE001
        chunk_token_size = 1200
        chunk_overlap_token_size = 50

    # 3. 获取 tokenizer（从 LightRAG 实例）
    try:
        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
        if rag is None or not hasattr(rag, "tokenizer"):
            return {
                "status": "error",
                "expected": len(full_docs),
                "actual": 0,
                "lost": len(full_docs),
                "source": "kv_store_full_docs",
                "message": "LightRAG 实例未初始化，无法获取 tokenizer 重新 chunking",
                "unrecoverable": True,
            }
        tokenizer = rag.tokenizer
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "expected": len(full_docs),
            "actual": 0,
            "lost": len(full_docs),
            "source": "kv_store_full_docs",
            "message": f"获取 tokenizer 失败: {e}",
            "unrecoverable": True,
        }

    # 4. 重新 chunking
    from lightrag.operate import chunking_by_token_size

    new_text_chunks: dict[str, dict[str, Any]] = {}
    expected_chunk_count = 0
    for doc_id, doc_value in full_docs.items():
        if not isinstance(doc_value, dict):
            continue
        content = doc_value.get("content", "")
        if not content:
            continue
        try:
            chunks = chunking_by_token_size(
                tokenizer=tokenizer,
                content=content,
                chunk_token_size=chunk_token_size,
                chunk_overlap_token_size=chunk_overlap_token_size,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LightRAGRepair] 文档 {doc_id} chunking 失败: {e}，跳过")
            continue
        for chunk in chunks:
            chunk_content = chunk["content"]
            chunk_id = compute_mdhash_id(chunk_content, prefix="chunk-")
            new_text_chunks[chunk_id] = {
                "content": chunk_content,
                "full_doc_id": doc_id,
                "chunk_order_index": chunk.get("chunk_order_index", 0),
                "tokens": chunk.get("tokens", 0),
                "llm_cache_list": [],
            }
            expected_chunk_count += 1

    # 5. chunk_id 一致性检查（如果 doc_status 存在且记录了 chunks_list）
    doc_status = _load_json_dict(doc_status_path)
    if doc_status:
        old_chunk_ids: set[str] = set()
        for ds_value in doc_status.values():
            if not isinstance(ds_value, dict):
                continue
            for cid in ds_value.get("chunks_list", []) or []:
                if isinstance(cid, str):
                    old_chunk_ids.add(cid)
        # 如果旧 chunks_list 跟新 chunk_id 集合差异过大 → unrecoverable
        # （chunk_size 配置变更会导致 chunk_id 全变，下游 entity_chunks/relation_chunks 引用全失效）
        if old_chunk_ids:
            new_chunk_ids = set(new_text_chunks.keys())
            intersection = old_chunk_ids & new_chunk_ids
            # 如果重合率 < 50%，认为是 chunk_size 变更 → unrecoverable
            overlap_ratio = len(intersection) / len(old_chunk_ids) if old_chunk_ids else 1.0
            if overlap_ratio < 0.5:
                return {
                    "status": "error",
                    "expected": expected_chunk_count,
                    "actual": 0,
                    "lost": expected_chunk_count,
                    "source": "kv_store_full_docs",
                    "message": (
                        f"chunk_size 配置变更导致 chunk_id 不一致"
                        f"（重合率 {overlap_ratio:.1%} < 50%），下游引用全失效"
                    ),
                    "unrecoverable": True,
                }

    # 6. 备份损坏的 text_chunks 并写新文件
    _backup_corrupt(text_chunks_path)
    _atomic_write_json(text_chunks_path, new_text_chunks)

    actual = len(new_text_chunks)
    logger.info(f"[LightRAGRepair] 重建 text_chunks: {actual} 条 (source=full_docs)")
    return {
        "status": "ok",
        "expected": expected_chunk_count,
        "actual": actual,
        "lost": expected_chunk_count - actual,
        "source": "kv_store_full_docs",
        "message": f"从 full_docs 重新 chunking 重建 {actual} 条 text_chunks",
    }


def repair_doc_status() -> dict[str, Any]:
    """2. 从 text_chunks 派生 chunks_list + 从 full_docs 派生 status。

    真相源：kv_store_text_chunks.json + kv_store_full_docs.json
    派生：kv_store_doc_status.json

    chunks_list: 按 full_doc_id 分组 text_chunks 的 key
    status: PROCESSED 如果 GraphML 有数据，否则 PENDING
    """
    storage_dir = _storage_dir()
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    doc_status_path = storage_dir / "kv_store_doc_status.json"
    graphml_path = storage_dir / _GRAPHML_FILE

    # 1. 读 text_chunks（真相源）
    text_chunks = _load_json_dict(text_chunks_path)
    if text_chunks is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 损坏",
            "unrecoverable": True,
        }
    if not text_chunks:
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 为空，无需重建 doc_status",
        }

    # 2. 读 full_docs（真相源）
    full_docs = _load_json_dict(full_docs_path)
    if full_docs is None:
        return {
            "status": "error",
            "expected": len(full_docs) if isinstance(full_docs, dict) else 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "full_docs 损坏",
            "unrecoverable": True,
        }

    # 3. 判断 GraphML 是否有数据（决定 status 是 PROCESSED 还是 PENDING）
    graphml_has_data = graphml_path.exists() and graphml_path.stat().st_size > 200

    # 4. 按 full_doc_id 分组 chunks_list
    chunks_by_doc: dict[str, list[str]] = {}
    for chunk_id, chunk_value in text_chunks.items():
        if not isinstance(chunk_value, dict):
            continue
        full_doc_id = chunk_value.get("full_doc_id", "")
        if not full_doc_id:
            continue
        chunks_by_doc.setdefault(full_doc_id, []).append(chunk_id)

    # 5. 构造 doc_status
    new_doc_status: dict[str, dict[str, Any]] = {}
    expected_count = len(full_docs) if full_docs else 0
    for doc_id in full_docs.keys():
        chunks_list = sorted(chunks_by_doc.get(doc_id, []))  # 排序保证稳定
        # 保留原 doc_status 的 file_path 等元数据（如果存在）
        old_ds = _load_json_dict(doc_status_path) or {}
        old_value = old_ds.get(doc_id, {}) if isinstance(old_ds, dict) else {}
        new_doc_status[doc_id] = {
            "status": "PROCESSED" if graphml_has_data else "PENDING",
            "chunks_count": len(chunks_list),
            "content_summary": old_value.get("content_summary", "") if isinstance(old_value, dict) else "",
            "content_length": old_value.get("content_length", 0) if isinstance(old_value, dict) else 0,
            "created_at": old_value.get("created_at", "") if isinstance(old_value, dict) else "",
            "updated_at": old_value.get("updated_at", "") if isinstance(old_value, dict) else "",
            "file_path": old_value.get("file_path", "") if isinstance(old_value, dict) else "",
            "chunks_list": chunks_list,
        }

    # 6. 备份 + 写
    _backup_corrupt(doc_status_path)
    _atomic_write_json(doc_status_path, new_doc_status)

    actual = len(new_doc_status)
    logger.info(f"[LightRAGRepair] 重建 doc_status: {actual} 条 (source=text_chunks+full_docs)")
    return {
        "status": "ok",
        "expected": expected_count,
        "actual": actual,
        "lost": expected_count - actual,
        "source": "kv_store_text_chunks + kv_store_full_docs",
        "message": f"从 text_chunks 派生 chunks_list + 从 full_docs 派生 status，重建 {actual} 条",
    }


def repair_graphml() -> dict[str, Any]:
    """3. 从零重建 GraphML（最复杂的 repair）。

    策略：
    1. 先 drop 旧 GraphML（直接删文件）
    2. 改所有 doc_status 为 PENDING（触发重处理）
    3. monkeypatch force_llm_summary_on_merge = 999999（尽量不调 LLM summary）
    4. 调 LightRAG.apipeline_process_enqueue_documents 重处理
       - extract 阶段：llm_response_cache 命中不调 LLM
       - summary 阶段：可能调 LLM 如果 summary cache miss
    5. 如果 llm_response_cache 损坏（extract cache miss）→ unrecoverable

    简化方案：
    - 如果 LightRAG 实例不可用 → unrecoverable
    - 如果 apipeline 抛异常 → status=error，记录原因
    """
    storage_dir = _storage_dir()
    graphml_path = storage_dir / _GRAPHML_FILE
    doc_status_path = storage_dir / "kv_store_doc_status.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    # 1. 检查 llm_response_cache 是否可用（extract cache 来源）
    cache_data = _load_json_dict(cache_path)
    if cache_data is None:
        return {
            "status": "error",
            "expected": 1,
            "actual": 0,
            "lost": 1,
            "source": "llm_response_cache",
            "message": "llm_response_cache 损坏，无法重放 extract，GraphML 不可重建",
            "unrecoverable": True,
        }
    if not cache_data:
        # cache 空也允许重处理（但会调 LLM，用户承担费用）
        logger.warning("[LightRAGRepair] llm_response_cache 为空，重处理会调 LLM")

    # 2. 获取 LightRAG 实例
    try:
        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "expected": 1,
            "actual": 0,
            "lost": 1,
            "source": "LightRAG apipeline",
            "message": f"获取 LightRAG 实例失败: {e}",
            "unrecoverable": True,
        }
    if rag is None:
        return {
            "status": "error",
            "expected": 1,
            "actual": 0,
            "lost": 1,
            "source": "LightRAG apipeline",
            "message": "LightRAG 实例未初始化，无法重处理",
            "unrecoverable": True,
        }

    # 3. drop 旧 GraphML
    try:
        if graphml_path.exists():
            _backup_corrupt(graphml_path)
            graphml_path.unlink()
            logger.info("[LightRAGRepair] 已删除旧 GraphML")
        # 重置内存中的 graph
        if hasattr(rag, "chunk_entity_relation_graph"):
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(rag.chunk_entity_relation_graph.drop())
                finally:
                    loop.close()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LightRAGRepair] 内存 graph drop 失败（忽略）: {e}")
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "expected": 1,
            "actual": 0,
            "lost": 1,
            "source": "graphml drop",
            "message": f"drop 旧 GraphML 失败: {e}",
        }

    # 4. 改所有 doc_status 为 PENDING（触发重处理）
    doc_status = _load_json_dict(doc_status_path)
    if doc_status is None:
        return {
            "status": "error",
            "expected": 1,
            "actual": 0,
            "lost": 1,
            "source": "doc_status",
            "message": "doc_status 损坏，无法改为 PENDING",
            "unrecoverable": True,
        }
    if doc_status:
        for doc_id, ds_value in doc_status.items():
            if isinstance(ds_value, dict):
                ds_value["status"] = "PENDING"
        _atomic_write_json(doc_status_path, doc_status)

    expected_docs = len(doc_status) if doc_status else 0

    # 5. monkeypatch force_llm_summary_on_merge
    old_force = None
    try:
        # global_config 在 LightRAG 实例上以 asdict 形式传递，但 force_llm_summary_on_merge 是字段
        if hasattr(rag, "force_llm_summary_on_merge"):
            old_force = rag.force_llm_summary_on_merge
            rag.force_llm_summary_on_merge = 999999
            logger.info("[LightRAGRepair] monkeypatch force_llm_summary_on_merge = 999999")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LightRAGRepair] monkeypatch force_llm_summary_on_merge 失败: {e}")

    # 6. 调 apipeline_process_enqueue_documents
    try:
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(rag.apipeline_process_enqueue_documents())
        finally:
            loop.close()
    except Exception as e:  # noqa: BLE001
        # 恢复 monkeypatch
        if old_force is not None and hasattr(rag, "force_llm_summary_on_merge"):
            rag.force_llm_summary_on_merge = old_force
        return {
            "status": "error",
            "expected": expected_docs,
            "actual": 0,
            "lost": expected_docs,
            "source": "LightRAG apipeline",
            "message": f"apipeline_process_enqueue_documents 失败: {e}",
            "unrecoverable": True,
        }
    finally:
        # 恢复 monkeypatch
        if old_force is not None and hasattr(rag, "force_llm_summary_on_merge"):
            rag.force_llm_summary_on_merge = old_force

    # 7. 验证 GraphML 是否生成
    if not graphml_path.exists():
        return {
            "status": "error",
            "expected": expected_docs,
            "actual": 0,
            "lost": expected_docs,
            "source": "LightRAG apipeline",
            "message": "apipeline 执行完成但 GraphML 未生成（可能 llm_response_cache 不完整）",
            "unrecoverable": True,
        }

    # 统计 node + edge 数
    node_ids, edges, _ = _load_graphml_nodes_edges()
    actual = len(node_ids) + len(edges)
    logger.info(f"[LightRAGRepair] 重建 GraphML: {len(node_ids)} nodes + {len(edges)} edges")
    return {
        "status": "ok",
        "expected": expected_docs,
        "actual": actual,
        "lost": 0,
        "source": "LightRAG apipeline (extract cache replay + summary 禁用 LLM)",
        "message": f"从零重建 GraphML: {len(node_ids)} nodes + {len(edges)} edges",
    }


def repair_graphml_orphan_edges() -> dict[str, Any]:
    """3b. 清理 GraphML 里引用不存在 node 的孤儿 edge。

    场景：GraphML 里删了某个 node 但 edge 仍引用该 node → check #9（graphml_edge_dangling）报 major。
    本函数遍历所有 edge，删除 source/target 不在 node 集合中的孤儿 edge，原子写回 GraphML。

    真相源：GraphML 自身（node 集合是权威，edge 引用必须对齐）
    派生：GraphML 自身（只删 edge，不删 node，不重建）

    实现细节：
    1. 用 ElementTree 解析 GraphML（跟 check_graphml_edge_dangling 一致）
       注意：不能用 networkx.read_graphml，因为 networkx 看到 edge 引用未声明 node
       会自动创建该 node，无法检测到孤儿 edge。
    2. 收集 node id 集合
    3. 遍历 edge，找 source/target 不在 node 集合的 edge → 直接从 XML 树删除
    4. 原子写回（写 tmp + fsync + os.replace）

    原子写策略：
    - ElementTree.write 不支持 tmp+rename，需要包装
    - 写 tmp 文件 → fsync → os.replace 替换原文件
    """
    import xml.etree.ElementTree as ET

    storage_dir = _storage_dir()
    graphml_path = storage_dir / _GRAPHML_FILE

    # 1. 检查 GraphML 文件
    if not graphml_path.exists():
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 文件不存在",
            "unrecoverable": True,
        }

    # 2. 用 ElementTree 解析（不能用 networkx，原因见 docstring）
    try:
        tree = ET.parse(graphml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML XML 解析失败: {e}",
            "unrecoverable": True,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 解析失败: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    ns = "{http://graphml.graphdrawing.org/xmlns}"
    graph = root.find(f"{ns}graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 <graph> 元素",
            "unrecoverable": True,
        }

    # 3. 收集 node id 集合
    node_ids: set[str] = set()
    total_edges = 0
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if nid:
                node_ids.add(nid)
        elif tag == "edge":
            total_edges += 1

    # 4. 找孤儿 edge（source 或 target 不在 node_ids 中）
    orphan_edge_elements: list[ET.Element] = []
    for child in list(graph):  # list() 拷贝避免迭代中修改
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag != "edge":
            continue
        src = child.get("source", "")
        tgt = child.get("target", "")
        if (src and src not in node_ids) or (tgt and tgt not in node_ids):
            orphan_edge_elements.append(child)

    expected = total_edges
    if not orphan_edge_elements:
        logger.info(f"[LightRAGRepair] GraphML 无孤儿 edge（{expected} edges 全部健康）")
        return {
            "status": "ok",
            "expected": expected,
            "actual": expected,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 无孤儿 edge（{expected} edges 全部健康）",
        }

    # 5. 从 XML 树删除孤儿 edge
    for edge_elem in orphan_edge_elements:
        graph.remove(edge_elem)

    # 6. 备份旧文件 + 原子写回
    _backup_corrupt(graphml_path)
    tmp_path = graphml_path.with_name(graphml_path.name + ".tmp")
    try:
        # ET.write 默认 UTF-8 + xml_declaration
        tree.write(str(tmp_path), encoding="utf-8", xml_declaration=True)
        # fsync + os.replace 保证原子性
        with open(tmp_path, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, graphml_path)
    except Exception as e:  # noqa: BLE001
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": len(orphan_edge_elements),
            "source": "GraphML",
            "message": f"写回 GraphML 失败: {type(e).__name__}: {e}",
        }

    remaining_edges = expected - len(orphan_edge_elements)
    logger.info(
        f"[LightRAGRepair] 清理 GraphML 孤儿 edge: 删除 {len(orphan_edge_elements)} 条，"
        f"剩余 {remaining_edges} edges（nodes={len(node_ids)}）"
    )
    return {
        "status": "ok",
        "expected": expected,
        "actual": remaining_edges,
        "lost": len(orphan_edge_elements),
        "source": "GraphML",
        "message": f"清理 {len(orphan_edge_elements)} 条孤儿 edge，剩余 {remaining_edges} edges",
    }


def repair_vdb_chunks() -> dict[str, Any]:
    """4. 遍历 text_chunks 重新 embedding 重建 vdb_chunks。

    真相源：kv_store_text_chunks.json
    派生：vdb_chunks.json

    每条 chunk 的 __id__ = compute_mdhash_id(content, prefix="chunk-")
    embedding 失败 >10% → status=error 不写文件
    """
    storage_dir = _storage_dir()
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"
    vdb_path = storage_dir / "vdb_chunks.json"

    # 1. 读 text_chunks（真相源）
    text_chunks = _load_json_dict(text_chunks_path)
    if text_chunks is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 损坏",
            "unrecoverable": True,
        }
    if not text_chunks:
        # 空 text_chunks → 写空 vdb（让 check 通过）
        _backup_corrupt(vdb_path)
        embedding_dim = _get_embedding_dim()
        _build_vdb_file(vdb_path, [], [], embedding_dim)
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 为空，写空 vdb_chunks",
        }

    # 2. 收集要 embedding 的 texts
    items: list[tuple[str, str, dict[str, Any]]] = []  # (chunk_id, content, original_chunk_value)
    for chunk_id, chunk_value in text_chunks.items():
        if not isinstance(chunk_value, dict):
            continue
        content = chunk_value.get("content", "")
        if not content:
            continue
        items.append((chunk_id, content, chunk_value))

    if not items:
        _backup_corrupt(vdb_path)
        embedding_dim = _get_embedding_dim()
        _build_vdb_file(vdb_path, [], [], embedding_dim)
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 无有效 content，写空 vdb_chunks",
        }

    expected = len(items)
    texts = [t for _, t, _ in items]

    # 3. 批量 embedding
    vectors = _embed_batch(texts)
    if vectors is None:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "kv_store_text_chunks",
            "message": "embedding 完全失败，无法重建 vdb_chunks",
        }
    if len(vectors) != len(texts):
        # 部分失败，补 None 占位
        while len(vectors) < len(texts):
            vectors.append(None)  # type: ignore[arg-type]

    # 4. 构造 data_list
    embedding_dim = len(vectors[0]) if vectors and vectors[0] is not None else _get_embedding_dim()
    data_list: list[dict[str, Any]] = []
    final_vectors: list[list[float]] = []
    failed_count = 0
    for (chunk_id, content, chunk_value), vec in zip(items, vectors):
        if vec is None:
            failed_count += 1
            continue
        # __id__ 用 compute_mdhash_id 重新算（跟 LightRAG 写入一致）
        expected_id = compute_mdhash_id(content, prefix="chunk-")
        data_list.append({
            "__id__": expected_id,
            "content": content,
            "full_doc_id": chunk_value.get("full_doc_id", ""),
            "chunk_order_index": chunk_value.get("chunk_order_index", 0),
            "tokens": chunk_value.get("tokens", 0),
            "file_path": chunk_value.get("file_path", ""),
        })
        final_vectors.append(vec)

    # 5. embedding 失败率检查
    if expected > 0 and failed_count / expected > 0.1:
        return {
            "status": "error",
            "expected": expected,
            "actual": len(data_list),
            "lost": failed_count,
            "source": "kv_store_text_chunks",
            "message": f"embedding 失败率 {failed_count}/{expected} > 10%，不写文件",
        }

    if not data_list:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "kv_store_text_chunks",
            "message": "embedding 全部失败，无数据可重建",
        }

    # 6. 备份 + 写
    _backup_corrupt(vdb_path)
    _build_vdb_file(vdb_path, data_list, final_vectors, embedding_dim)

    actual = len(data_list)
    logger.info(f"[LightRAGRepair] 重建 vdb_chunks: {actual} 条 (source=text_chunks)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "kv_store_text_chunks",
        "message": f"从 text_chunks 重新 embedding 重建 {actual} 条 vdb_chunks",
    }


def repair_vdb_entities() -> dict[str, Any]:
    """5. 遍历 GraphML node 重新 embedding 重建 vdb_entities。

    真相源：graph_chunk_entity_relation.graphml（node id + d2 description + d3 source_id）
    派生：vdb_entities.json

    每条 entity 的 __id__ = compute_mdhash_id(name, prefix="ent-")
    embedding 失败 >10% → status=error 不写文件
    """
    storage_dir = _storage_dir()
    vdb_path = storage_dir / "vdb_entities.json"

    # 1. 读 GraphML nodes（真相源）
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }
    if not nodes:
        _backup_corrupt(vdb_path)
        embedding_dim = _get_embedding_dim()
        _build_vdb_file(vdb_path, [], [], embedding_dim)
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 node，写空 vdb_entities",
        }

    # 2. 收集要 embedding 的 texts
    # LightRAG operate.py L1160: entity_content = f"{entity_name}\n{final_description}"
    # embedding 输入用同样的 content（保证向量跟 LightRAG 原生写入一致）
    items: list[tuple[str, str, str]] = []  # (node_id, content, source_id)
    for node_id, (desc, src) in nodes.items():
        # desc 为空时用 node_id 作为 fallback（保证有内容可 embed）
        # 格式: f"{node_id}\n{desc}"，跟 LightRAG 一致
        content = f"{node_id}\n{desc}" if desc else f"{node_id}\n{node_id}"
        items.append((node_id, content, src))

    expected = len(items)
    texts = [t for _, t, _ in items]

    # 3. 批量 embedding
    vectors = _embed_batch(texts)
    if vectors is None:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "GraphML",
            "message": "embedding 完全失败，无法重建 vdb_entities",
        }
    if len(vectors) != len(texts):
        while len(vectors) < len(texts):
            vectors.append(None)  # type: ignore[arg-type]

    # 4. 构造 data_list
    # content 字段直接用 items[1]（已经按 LightRAG 格式构造好的 f"{node_id}\n{desc}"）
    embedding_dim = len(vectors[0]) if vectors and vectors[0] is not None else _get_embedding_dim()
    data_list: list[dict[str, Any]] = []
    final_vectors: list[list[float]] = []
    failed_count = 0
    for (node_id, content, src), vec in zip(items, vectors):
        if vec is None:
            failed_count += 1
            continue
        # __id__ = compute_mdhash_id(node_id, prefix="ent-")
        # node_id 已 lower（LightRAG 设计），但 compute_mdhash_id 对原始字符串算 hash
        expected_id = compute_mdhash_id(node_id, prefix="ent-")
        data_list.append({
            "__id__": expected_id,
            "entity_name": node_id,
            "content": content,
            "source_id": src or "",
        })
        final_vectors.append(vec)

    # 5. embedding 失败率检查
    if expected > 0 and failed_count / expected > 0.1:
        return {
            "status": "error",
            "expected": expected,
            "actual": len(data_list),
            "lost": failed_count,
            "source": "GraphML",
            "message": f"embedding 失败率 {failed_count}/{expected} > 10%，不写文件",
        }

    if not data_list:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "GraphML",
            "message": "embedding 全部失败，无数据可重建",
        }

    # 6. 备份 + 写
    _backup_corrupt(vdb_path)
    _build_vdb_file(vdb_path, data_list, final_vectors, embedding_dim)

    actual = len(data_list)
    logger.info(f"[LightRAGRepair] 重建 vdb_entities: {actual} 条 (source=GraphML)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML",
        "message": f"从 GraphML nodes 重新 embedding 重建 {actual} 条 vdb_entities",
    }


def repair_vdb_relationships() -> dict[str, Any]:
    """6. 遍历 GraphML edge 重新 embedding 重建 vdb_relationships。

    真相源：graph_chunk_entity_relation.graphml（edge src/tgt + d2 description + d3 source_id）
    派生：vdb_relationships.json

    每条 relationship 的 __id__ 用 make_relation_vdb_ids 生成正序 ID
    src_id/tgt_id 用 sorted 后的值（跟 LightRAG 写入一致）
    embedding 失败 >10% → status=error 不写文件
    """
    storage_dir = _storage_dir()
    vdb_path = storage_dir / "vdb_relationships.json"

    # 1. 读 GraphML edges（真相源）
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }
    if not edges:
        _backup_corrupt(vdb_path)
        embedding_dim = _get_embedding_dim()
        _build_vdb_file(vdb_path, [], [], embedding_dim)
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 edge，写空 vdb_relationships",
        }

    # 2. 收集要 embedding 的 texts
    # LightRAG operate.py L1601: rel_content = f"{combined_keywords}\t{src}\n{tgt}\n{final_description}"
    # combined_keywords 是 <SEP> 分隔的多个关键词合并后的字符串
    # 这里用 GraphML d9 keywords（已经是 <SEP> 分隔的字符串），desc 为空用空字符串保持格式
    items: list[tuple[str, str, str, str, str]] = []
    # (sorted_src, sorted_tgt, content, source_id, edge_id_for_vdb)
    for src, tgt, edge_src_id, edge_desc, edge_keywords in edges:
        if not src or not tgt:
            continue
        # sorted 后存，跟 LightRAG 写入一致
        sorted_src, sorted_tgt = sorted((src, tgt))
        # __id__ 用 make_relation_vdb_ids 的第一个（正序）
        candidate_ids = make_relation_vdb_ids(sorted_src, sorted_tgt)
        vdb_id = candidate_ids[0]
        # content 格式: f"{keywords}\t{src}\n{tgt}\n{desc}"
        # keywords/desc 为空用空字符串（保持 LightRAG 格式一致，不破坏向量比对）
        content = f"{edge_keywords}\t{sorted_src}\n{sorted_tgt}\n{edge_desc}"
        items.append((sorted_src, sorted_tgt, content, edge_src_id, vdb_id))

    expected = len(items)
    if expected == 0:
        _backup_corrupt(vdb_path)
        embedding_dim = _get_embedding_dim()
        _build_vdb_file(vdb_path, [], [], embedding_dim)
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无有效 edge，写空 vdb_relationships",
        }

    texts = [t for _, _, t, _, _ in items]

    # 3. 批量 embedding
    vectors = _embed_batch(texts)
    if vectors is None:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "GraphML",
            "message": "embedding 完全失败，无法重建 vdb_relationships",
        }
    if len(vectors) != len(texts):
        while len(vectors) < len(texts):
            vectors.append(None)  # type: ignore[arg-type]

    # 4. 构造 data_list
    embedding_dim = len(vectors[0]) if vectors and vectors[0] is not None else _get_embedding_dim()
    data_list: list[dict[str, Any]] = []
    final_vectors: list[list[float]] = []
    failed_count = 0
    for (sorted_src, sorted_tgt, content, edge_src_id, vdb_id), vec in zip(items, vectors):
        if vec is None:
            failed_count += 1
            continue
        data_list.append({
            "__id__": vdb_id,
            "src_id": sorted_src,
            "tgt_id": sorted_tgt,
            "content": content,
            "source_id": edge_src_id or "",
        })
        final_vectors.append(vec)

    # 5. embedding 失败率检查
    if expected > 0 and failed_count / expected > 0.1:
        return {
            "status": "error",
            "expected": expected,
            "actual": len(data_list),
            "lost": failed_count,
            "source": "GraphML",
            "message": f"embedding 失败率 {failed_count}/{expected} > 10%，不写文件",
        }

    if not data_list:
        return {
            "status": "error",
            "expected": expected,
            "actual": 0,
            "lost": expected,
            "source": "GraphML",
            "message": "embedding 全部失败，无数据可重建",
        }

    # 6. 备份 + 写
    _backup_corrupt(vdb_path)
    _build_vdb_file(vdb_path, data_list, final_vectors, embedding_dim)

    actual = len(data_list)
    logger.info(f"[LightRAGRepair] 重建 vdb_relationships: {actual} 条 (source=GraphML)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML",
        "message": f"从 GraphML edges 重新 embedding 重建 {actual} 条 vdb_relationships",
    }


def repair_entity_chunks() -> dict[str, Any]:
    """7. 从 GraphML node source_id 提取重建 entity_chunks。

    真相源：GraphML node 的 d3 source_id 字段（<SEP> 分隔的 chunk_id 列表）
    派生：kv_store_entity_chunks.json

    key = entity_name (node id)
    value = {"chunk_ids": [chunk_id, ...], "count": int}
    (跟 LightRAG operate.py L1194 一致)
    """
    storage_dir = _storage_dir()
    ec_path = storage_dir / "kv_store_entity_chunks.json"

    # 1. 读 GraphML nodes
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }
    if not nodes:
        _backup_corrupt(ec_path)
        _atomic_write_json(ec_path, {})
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 node，写空 entity_chunks",
        }

    # 2. 从 source_id 提取 chunk_ids（LightRAG operate.py L1194 用 chunk_ids + count 字段）
    new_entity_chunks: dict[str, dict[str, Any]] = {}
    expected = len(nodes)
    for node_id, (desc, src) in nodes.items():
        if not src:
            # source_id 为空 → 空 chunk_ids（合法）
            new_entity_chunks[node_id] = {"chunk_ids": [], "count": 0}
            continue
        # source_id 是 <SEP> 分隔的 chunk_id 列表
        chunk_ids = [c for c in src.split(GRAPH_FIELD_SEP) if c]
        new_entity_chunks[node_id] = {"chunk_ids": chunk_ids, "count": len(chunk_ids)}

    # 3. 备份 + 写
    _backup_corrupt(ec_path)
    _atomic_write_json(ec_path, new_entity_chunks)

    actual = len(new_entity_chunks)
    logger.info(f"[LightRAGRepair] 重建 entity_chunks: {actual} 条 (source=GraphML source_id)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML node source_id",
        "message": f"从 GraphML node source_id 提取重建 {actual} 条 entity_chunks",
    }


def repair_relation_chunks() -> dict[str, Any]:
    """8. 从 GraphML edge source_id 提取重建 relation_chunks。

    真相源：GraphML edge 的 d10 source_id 字段（<SEP> 分隔的 chunk_id 列表）
    派生：kv_store_relation_chunks.json

    key = make_relation_chunk_key(src, tgt) = GRAPH_FIELD_SEP.join(sorted((src, tgt)))
    value = {"chunk_ids": [chunk_id, ...], "count": int}
    (跟 LightRAG operate.py L1404 一致)
    """
    storage_dir = _storage_dir()
    rc_path = storage_dir / "kv_store_relation_chunks.json"

    # 1. 读 GraphML edges
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }
    if not edges:
        _backup_corrupt(rc_path)
        _atomic_write_json(rc_path, {})
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 edge，写空 relation_chunks",
        }

    # 2. 从 source_id 提取 chunk_ids（LightRAG operate.py L1404 用 chunk_ids + count 字段）
    new_relation_chunks: dict[str, dict[str, Any]] = {}
    expected = 0
    for src, tgt, edge_src_id, edge_desc, edge_keywords in edges:
        if not src or not tgt:
            continue
        # sorted 后用 make_relation_chunk_key 生成 key
        key = make_relation_chunk_key(src, tgt)
        chunk_ids = []
        if edge_src_id:
            chunk_ids = [c for c in edge_src_id.split(GRAPH_FIELD_SEP) if c]
        # 同一个 key 可能被多个 edge 重复（不应该，但容错），合并 chunk_ids
        if key in new_relation_chunks:
            existing = set(new_relation_chunks[key]["chunk_ids"])
            existing.update(chunk_ids)
            merged = sorted(existing)
            new_relation_chunks[key]["chunk_ids"] = merged
            new_relation_chunks[key]["count"] = len(merged)
        else:
            new_relation_chunks[key] = {"chunk_ids": chunk_ids, "count": len(chunk_ids)}
            expected += 1

    # 3. 备份 + 写
    _backup_corrupt(rc_path)
    _atomic_write_json(rc_path, new_relation_chunks)

    actual = len(new_relation_chunks)
    logger.info(f"[LightRAGRepair] 重建 relation_chunks: {actual} 条 (source=GraphML edge source_id)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML edge source_id",
        "message": f"从 GraphML edge source_id 提取重建 {actual} 条 relation_chunks",
    }


def repair_full_entities() -> dict[str, Any]:
    """9. 从 GraphML source_id → chunk→doc 映射重建 full_entities。

    真相源：GraphML node source_id（chunk_id 列表）+ doc_status.chunks_list（chunk→doc 映射）
    派生：kv_store_full_entities.json

    key = doc_id
    value = list of entity_name（在该 doc 的 chunks 中出现的实体）
    """
    storage_dir = _storage_dir()
    fe_path = storage_dir / "kv_store_full_entities.json"
    doc_status_path = storage_dir / "kv_store_doc_status.json"

    # 1. 读 GraphML nodes
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 2. 读 doc_status（chunk→doc 映射）
    doc_status = _load_json_dict(doc_status_path)
    if doc_status is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "doc_status",
            "message": "doc_status 损坏，无法构建 chunk→doc 映射",
            "unrecoverable": True,
        }

    # 3. 构建 chunk→doc 映射
    chunk_to_doc: dict[str, str] = {}
    for doc_id, ds_value in doc_status.items():
        if not isinstance(ds_value, dict):
            continue
        for cid in ds_value.get("chunks_list", []) or []:
            if isinstance(cid, str):
                chunk_to_doc[cid] = doc_id

    # 4. 从 GraphML source_id 提取 entity→docs 映射
    entity_to_docs: dict[str, set[str]] = {}
    for node_id, (desc, src) in nodes.items():
        if not src:
            continue
        chunk_ids = [c for c in src.split(GRAPH_FIELD_SEP) if c]
        for cid in chunk_ids:
            doc_id = chunk_to_doc.get(cid)
            if doc_id:
                entity_to_docs.setdefault(node_id, set()).add(doc_id)

    # 5. 反转：doc→entities
    doc_to_entities: dict[str, list[str]] = {}
    for entity_name, doc_set in entity_to_docs.items():
        for doc_id in doc_set:
            doc_to_entities.setdefault(doc_id, []).append(entity_name)

    # 6. 备份 + 写
    expected = len(doc_status) if doc_status else 0
    _backup_corrupt(fe_path)
    _atomic_write_json(fe_path, doc_to_entities)

    actual = len(doc_to_entities)
    logger.info(f"[LightRAGRepair] 重建 full_entities: {actual} 条 (source=GraphML source_id + doc_status)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML source_id + doc_status chunks_list",
        "message": f"从 GraphML source_id → chunk→doc 映射重建 {actual} 条 full_entities",
    }


def repair_full_relations() -> dict[str, Any]:
    """10. 从 GraphML edge source_id → chunk→doc 映射重建 full_relations。

    真相源：GraphML edge source_id（chunk_id 列表）+ doc_status.chunks_list（chunk→doc 映射）
    派生：kv_store_full_relations.json

    key = doc_id
    value = list of relation_key (make_relation_chunk_key 格式)
    """
    storage_dir = _storage_dir()
    fr_path = storage_dir / "kv_store_full_relations.json"
    doc_status_path = storage_dir / "kv_store_doc_status.json"

    # 1. 读 GraphML edges
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 2. 读 doc_status
    doc_status = _load_json_dict(doc_status_path)
    if doc_status is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "doc_status",
            "message": "doc_status 损坏，无法构建 chunk→doc 映射",
            "unrecoverable": True,
        }

    # 3. 构建 chunk→doc 映射
    chunk_to_doc: dict[str, str] = {}
    for doc_id, ds_value in doc_status.items():
        if not isinstance(ds_value, dict):
            continue
        for cid in ds_value.get("chunks_list", []) or []:
            if isinstance(cid, str):
                chunk_to_doc[cid] = doc_id

    # 4. 从 GraphML edge source_id 提取 relation→docs 映射
    relation_to_docs: dict[str, set[str]] = {}
    for src, tgt, edge_src_id, edge_desc, edge_keywords in edges:
        if not src or not tgt:
            continue
        if not edge_src_id:
            continue
        key = make_relation_chunk_key(src, tgt)
        chunk_ids = [c for c in edge_src_id.split(GRAPH_FIELD_SEP) if c]
        for cid in chunk_ids:
            doc_id = chunk_to_doc.get(cid)
            if doc_id:
                relation_to_docs.setdefault(key, set()).add(doc_id)

    # 5. 反转：doc→relations
    doc_to_relations: dict[str, list[str]] = {}
    for relation_key, doc_set in relation_to_docs.items():
        for doc_id in doc_set:
            doc_to_relations.setdefault(doc_id, []).append(relation_key)

    # 6. 备份 + 写
    expected = len(doc_status) if doc_status else 0
    _backup_corrupt(fr_path)
    _atomic_write_json(fr_path, doc_to_relations)

    actual = len(doc_to_relations)
    logger.info(f"[LightRAGRepair] 重建 full_relations: {actual} 条 (source=GraphML edge source_id + doc_status)")
    return {
        "status": "ok",
        "expected": expected,
        "actual": actual,
        "lost": expected - actual,
        "source": "GraphML edge source_id + doc_status chunks_list",
        "message": f"从 GraphML edge source_id → chunk→doc 映射重建 {actual} 条 full_relations",
    }


def repair_llm_response_cache() -> dict[str, Any]:
    """11. llm_response_cache 不可重建，清空（minor 级别，允许降级启动）。

    真相源：无（不可重建）
    派生：kv_store_llm_response_cache.json

    清空文件 + 清空 text_chunks.llm_cache_list 引用。
    """
    storage_dir = _storage_dir()
    cache_path = storage_dir / "kv_store_llm_response_cache.json"
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"

    # 1. 备份 + 清空 cache
    _backup_corrupt(cache_path)
    _atomic_write_json(cache_path, {})

    # 2. 清空 text_chunks.llm_cache_list（避免引用悬空）
    text_chunks = _load_json_dict(text_chunks_path)
    if text_chunks and isinstance(text_chunks, dict):
        cleared = 0
        for chunk_id, chunk_value in text_chunks.items():
            if isinstance(chunk_value, dict) and chunk_value.get("llm_cache_list"):
                chunk_value["llm_cache_list"] = []
                cleared += 1
        if cleared > 0:
            _atomic_write_json(text_chunks_path, text_chunks)
            logger.info(f"[LightRAGRepair] 清空 {cleared} 条 text_chunks.llm_cache_list")

    logger.info("[LightRAGRepair] llm_response_cache 不可重建，已清空")
    return {
        "status": "ok",
        "expected": 0,
        "actual": 0,
        "lost": 0,
        "source": "N/A (不可重建，清空)",
        "message": "llm_response_cache 不可重建，已清空（minor 级别，允许降级启动）",
    }


# =============================================================================
# repair_all：按依赖链顺序调用
# =============================================================================


_REPAIR_ORDER = [
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("graphml", repair_graphml),
    ("graphml_orphan_edges", repair_graphml_orphan_edges),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
    ("llm_response_cache", repair_llm_response_cache),
]


# check name → repair 函数名（用于按需 repair 映射）
# 注意：部分 check 会发出带后缀的具体 error.check 值
# （如 graphml_edge_dangling 发出 graphml_edge_dangling_source / _target），
# 这些后缀变体也需要映射到同一个 repair 函数。
_CHECK_TO_REPAIR: dict[str, str] = {
    "text_chunks_doc_dangling": "text_chunks",
    "text_chunks_cache_dangling": "llm_response_cache",
    "doc_status_chunks_dangling": "doc_status",
    "graphml_edge_dangling": "graphml_orphan_edges",
    "graphml_edge_dangling_source": "graphml_orphan_edges",
    "graphml_edge_dangling_target": "graphml_orphan_edges",
    "vdb_chunks_missing": "vdb_chunks",
    "vdb_entities_missing": "vdb_entities",
    "vdb_relationships_missing": "vdb_relationships",
    "vdb_relationships_endpoint_dangling": "vdb_relationships",
    "entity_chunks_dangling": "entity_chunks",
    "relation_chunks_dangling": "relation_chunks",
}


# file_level_critical 的 error.file → repair 函数名
# 用于文件级 critical（JSON 解析失败 / 维度不匹配）的按文件分发
_FILE_TO_REPAIR: dict[str, str] = {
    "kv_store_doc_status.json": "doc_status",
    "kv_store_entity_chunks.json": "entity_chunks",
    "kv_store_full_docs.json": "full_docs_unrecoverable",  # 真相源不可重建
    "kv_store_full_entities.json": "full_entities",
    "kv_store_full_relations.json": "full_relations",
    "kv_store_relation_chunks.json": "relation_chunks",
    "kv_store_text_chunks.json": "text_chunks",
    "kv_store_llm_response_cache.json": "llm_response_cache",
    "vdb_entities.json": "vdb_entities",
    "vdb_relationships.json": "vdb_relationships",
    "vdb_chunks.json": "vdb_chunks",
    "graph_chunk_entity_relation.graphml": "graphml",
}

# 真相源不可重建的文件（file_level_critical 时直接标记 unrecoverable）
_UNRECOVERABLE_FILES = {"kv_store_full_docs.json"}


def repair_all() -> dict[str, Any]:
    """一键修复所有 LightRAG 数据文件（按 check 结果选择性调用）。

    v2 改造：先 check_all 拿 errors，按 check name 分组映射到 repair 函数，
    只对 check 报错的文件调 repair，没报错的跳过。
    避免对没坏的文件调 repair（导致不必要的 unrecoverable）。

    顺序（按依赖链）：
        text_chunks → doc_status → graphml → vdb_chunks → vdb_entities →
        vdb_relationships → entity_chunks → relation_chunks →
        full_entities → full_relations → llm_response_cache

    Returns:
        {
            "name": {status, expected, actual, lost, source, message, ...},
            ...,
            "_unrecoverable": True,  # 任意 repair 报 unrecoverable 时
            "_skipped": [...],       # 跳过的 repair 名（check 没报错）
            "_check_summary": {...},  # check_all 关键字段
        }
    """
    from niu_api.internal.lightrag_integrity import check_all
    from niu_api.internal import lightrag_integrity

    # 1. 同步 integrity 模块的 _STORAGE_DIR（兼容测试 monkeypatch repair._STORAGE_DIR 的场景）
    #    生产环境两边默认值一致（都是 ~/.niu/lightrag_storage），无需显式同步。
    #    测试场景只 monkeypatch repair._STORAGE_DIR，这里同步过去保证 check_all 用同一个目录。
    try:
        if lightrag_integrity._STORAGE_DIR != _STORAGE_DIR:
            lightrag_integrity._STORAGE_DIR = _STORAGE_DIR
    except Exception:  # noqa: BLE001
        pass

    # 2. 先 check_all 拿到 errors + checks
    check_result = check_all()
    checks = check_result.get("checks", {})
    all_errors = check_result.get("errors", [])

    # 3. 按 check name 分组收集报错的 check（含 file_level_critical 的 file 子项）
    #    注意：file_level_critical 的 errors 里 check 字段是具体类型（json_parse 等），
    #    不是 "file_level_critical"。所以用 checks["file_level_critical"]["errors"]
    #    来识别 file_level_critical 来源。
    needed_repairs: set[str] = set()
    file_level_errors: list[dict[str, Any]] = checks.get("file_level_critical", {}).get("errors", [])
    file_level_error_files: set[str] = {err.get("file", "") for err in file_level_errors}

    # 非 file_level_critical 的 check（按 check name 映射）
    for err in all_errors:
        check_name = err.get("check") or err.get("name", "")
        if not check_name:
            continue
        # file_level_critical 的 error 跳过（用 file 字段分发，下面单独处理）
        if err in file_level_errors:
            continue
        repair_name = _CHECK_TO_REPAIR.get(check_name)
        if repair_name:
            needed_repairs.add(repair_name)

    # 处理 file_level_critical 的 file 字段
    for err in file_level_errors:
        file_name = err.get("file", "")
        if file_name in _UNRECOVERABLE_FILES:
            # 真相源损坏 → 直接标记 unrecoverable，不需要 repair
            continue
        repair_name = _FILE_TO_REPAIR.get(file_name)
        if repair_name:
            needed_repairs.add(repair_name)

    # 4. 按依赖链顺序执行 needed_repairs 里的 repair
    results: dict[str, Any] = {}
    unrecoverable_detected = False
    skipped: list[str] = []

    for name, fn in _REPAIR_ORDER:
        if name not in needed_repairs:
            skipped.append(name)
            logger.info(f"[LightRAGRepair] 跳过 {name}（check 未报错）")
            continue
        try:
            result = fn()
            results[name] = result
            # 如果 unrecoverable，后续 repair 仍继续（让用户看到全部状态）
            # 但标记 unrecoverable_detected 供调用方决策
            if result.get("unrecoverable"):
                unrecoverable_detected = True
                logger.warning(
                    f"[LightRAGRepair] {name} 报 unrecoverable: {result.get('message', '')}"
                )
        except Exception as e:  # noqa: BLE001
            # 单个 repair 抛异常不影响其他 repair
            logger.error(f"[LightRAGRepair] {name} 抛异常: {e}", exc_info=True)
            results[name] = {
                "status": "error",
                "expected": 0,
                "actual": 0,
                "lost": 0,
                "source": "internal error",
                "message": f"repair 函数抛异常: {type(e).__name__}: {e}",
            }

    # 5. 真相源（kv_store_full_docs.json）文件级 critical → 直接标记 unrecoverable
    #    check_all 检测到 full_docs 损坏但无对应 repair，加一条 unrecoverable 占位
    for err in file_level_errors:
        file_name = err.get("file", "")
        if file_name in _UNRECOVERABLE_FILES:
            unrecoverable_detected = True
            results["full_docs"] = {
                "status": "error",
                "expected": 1,
                "actual": 0,
                "lost": 1,
                "source": "kv_store_full_docs",
                "message": f"真相源 {file_name} 损坏：{err.get('msg', '未知错误')}，不可重建",
                "unrecoverable": True,
            }
            logger.error(
                f"[LightRAGRepair] 真相源 {file_name} 损坏（critical），不可重建"
            )

    if unrecoverable_detected:
        results["_unrecoverable"] = True
    if skipped:
        results["_skipped"] = skipped
    results["_check_summary"] = {
        "critical_errors": check_result.get("critical_errors", 0),
        "major_errors": check_result.get("major_errors", 0),
        "minor_errors": check_result.get("minor_errors", 0),
        "ok": check_result.get("ok", False),
    }
    return results


# =============================================================================
# 向后兼容的废弃函数签名（已废弃，新代码应使用 repair_all 或具体 repair_xxx）
# =============================================================================


def repair_vdb(vdb_filename: str) -> dict[str, Any]:  # noqa: ARG001
    """已废弃：用 repair_vdb_chunks / repair_vdb_entities / repair_vdb_relationships 代替。"""
    logger.warning("repair_vdb is deprecated, use repair_vdb_chunks/entities/relationships instead")
    return {
        "status": "error",
        "expected": 0,
        "actual": 0,
        "lost": 0,
        "source": "deprecated",
        "message": "repair_vdb 已废弃，请用 repair_all() 或具体 repair_xxx 函数",
    }


def repair_kv_store(kv_filename: str) -> dict[str, Any]:  # noqa: ARG001
    """已废弃：用具体 repair_xxx 函数代替。"""
    logger.warning("repair_kv_store is deprecated, use specific repair_xxx instead")
    return {
        "status": "error",
        "expected": 0,
        "actual": 0,
        "lost": 0,
        "source": "deprecated",
        "message": "repair_kv_store 已废弃",
    }


def repair_entity_sync() -> dict[str, Any]:
    """已废弃：用 repair_vdb_entities + repair_entity_chunks 代替。"""
    logger.warning("repair_entity_sync is deprecated, use repair_vdb_entities + repair_entity_chunks instead")
    return {
        "status": "error",
        "expected": 0,
        "actual": 0,
        "lost": 0,
        "source": "deprecated",
        "message": "repair_entity_sync 已废弃",
    }


def repair_relationship_sync() -> dict[str, Any]:
    """已废弃：用 repair_vdb_relationships + repair_relation_chunks 代替。"""
    logger.warning("repair_relationship_sync is deprecated, use repair_vdb_relationships + repair_relation_chunks instead")
    return {
        "status": "error",
        "expected": 0,
        "actual": 0,
        "lost": 0,
        "source": "deprecated",
        "message": "repair_relationship_sync 已废弃",
    }
