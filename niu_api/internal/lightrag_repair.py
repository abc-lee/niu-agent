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

    v8：只用 niu_api.internal.embedding 预加载的模型，不调 get_lightrag_for_repair（铁律 3）。
    失败返回 None。

    空列表返回 []（不调模型）。
    """
    if not texts:
        return []

    try:
        from niu_api.internal.embedding import get_model

        model = get_model()
        if model is not None:
            vecs = model.encode(texts)
            # 转 list[list[float]]（vecs 可能是 numpy ndarray 或 Tensor）
            return [list(map(float, v)) for v in vecs]
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LightRAGRepair] embedding 模型失败: {e}")
        return None

    logger.error("[LightRAGRepair] embedding 模型未就绪（get_model() 返回 None）")
    return None


def _get_tokenizer():
    """独立加载 tokenizer（不调 get_lightrag_for_repair，铁律 3）。

    v8-Task 2：委托给独立模块 niu_api.internal.lightrag_repair_tokenizer。
    用 lightrag.utils.TiktokenTokenizer（model_name="gpt-4o-mini"）。
    失败返回 None。
    """
    from niu_api.internal.lightrag_repair_tokenizer import get_tokenizer

    return get_tokenizer()


def _get_chunk_config() -> tuple[int, int]:
    """读 chunk_token_size + chunk_overlap_token_size（不调 get_lightrag，铁律 3）。

    v8-Task 2：委托给独立模块 niu_api.internal.lightrag_repair_tokenizer。
    从 niu_api.internal.lightrag_manager._get_lightrag_config 读（只读 preferences.json）。
    fallback (1200, 50)（与 lightrag_manager.py:853 真实默认值一致）。
    """
    from niu_api.internal.lightrag_repair_tokenizer import get_chunk_config

    return get_chunk_config()


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
    except json.JSONDecodeError:
        # 只捕获 JSON 解析失败；OSError/PermissionError 等自然向上抛
        # （调用方已有 try/except 兜底，避免静默吞掉真正的 I/O 故障）
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
           - edge_keywords: edge 的 d9 字段（关系关键词，逗号分隔，跟 LightRAG operate.py L2173 ",".join 一致）
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


def _load_graphml_nodes() -> tuple[dict[str, tuple[str, str, str]], dict[str, Any] | None]:
    """解析 GraphML nodes，返回 {node_id: (entity_type, description, source_id)} + error。

    v8：返回 3 元组 (entity_type, desc, src)，识别脑区节点 d1=="brainregion"。

    entity_type = d1（缺省空字符串）, description = d2, source_id = d3
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
    nodes: dict[str, tuple[str, str, str]] = {}

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
            etype = ""
            desc = ""
            src = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d1":
                    etype = data.text or ""
                elif key == "d2":
                    desc = data.text or ""
                elif key == "d3":
                    src = data.text or ""
            nodes[nid] = (etype, desc, src)
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


def _check_truth_sources_intact() -> dict[str, Any]:
    """检测 3 真相源完好性：GraphML + full_docs + cache。

    任一损坏 = intact=False，repair_all 应报 unrecoverable。

    全新用户合法场景（intact=True）：
    - 文件不存在（还没导入文档）
    - 文件 size=0（空文件）
    - GraphML 无 node（空图）
    - full_docs/cache 是空 dict

    损坏场景（intact=False）：
    - GraphML 文件存在但 XML 解析失败 / 无 graph 元素
    - full_docs/cache 文件存在但 JSON 解析失败 / 非 dict

    检测标准跟现有 lightrag_integrity._check_truth_source（L166-203）一致：
    - 文件不存在 → ok（全新用户合法）
    - size=0 → ok（全新用户合法）
    - JSON 解析失败 / 非 dict → critical
    """
    import xml.etree.ElementTree as ET

    storage_dir = _storage_dir()

    # 1. GraphML
    #    文件不存在 / size=0 → intact=True（全新用户合法）
    #    XML 解析失败 / 无 graph 元素 → intact=False
    #    无 node → intact=True（空图合法，repair 重建空集）
    graphml_path = storage_dir / _GRAPHML_FILE
    graphml_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not graphml_path.exists() or graphml_path.stat().st_size == 0:
        # 全新用户合法，空 GraphML 不算损坏
        graphml_check["reason"] = "GraphML 不存在或为空（全新用户合法）"
    else:
        try:
            tree = ET.parse(graphml_path)
            root = tree.getroot()
            # 用现有 _load_graphml_nodes 的 fallback 模式：
            # 先尝试带 namespace 查找，再 fallback 到无 namespace 遍历子元素
            ns_str = "{http://graphml.graphdrawing.org/xmlns}"
            graph_elem = root.find(f"{ns_str}graph")
            if graph_elem is None:
                for child in root:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "graph":
                        graph_elem = child
                        break
            if graph_elem is None:
                graphml_check["intact"] = False
                graphml_check["reason"] = "GraphML 无 graph 元素"
            else:
                # 有 graph 元素就算完好（无 node 是空图，全新用户合法）
                graphml_check["intact"] = True
        except Exception as e:
            graphml_check["intact"] = False
            graphml_check["reason"] = f"XML 解析失败: {e}"

    # 2. full_docs
    #    文件不存在 / size=0 / 空 dict → intact=True（全新用户合法）
    #    JSON 解析失败 / 非 dict → intact=False
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    full_docs_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not full_docs_path.exists() or full_docs_path.stat().st_size == 0:
        full_docs_check["reason"] = "full_docs 不存在或为空（全新用户合法）"
    else:
        loaded = _load_json_dict(full_docs_path)
        if loaded is None:
            full_docs_check["intact"] = False
            full_docs_check["reason"] = "full_docs JSON 解析失败或非 dict"
        else:
            # 空 dict 或有内容都算完好
            full_docs_check["intact"] = True

    # 3. cache
    #    文件不存在 / size=0 / 空 dict → intact=True（全新用户合法）
    #    JSON 解析失败 / 非 dict → intact=False
    cache_path = storage_dir / "kv_store_llm_response_cache.json"
    cache_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        cache_check["reason"] = "cache 不存在或为空（全新用户合法）"
    else:
        loaded = _load_json_dict(cache_path)
        if loaded is None:
            cache_check["intact"] = False
            cache_check["reason"] = "cache JSON 解析失败或非 dict"
        else:
            cache_check["intact"] = True

    return {
        "intact": graphml_check["intact"] and full_docs_check["intact"] and cache_check["intact"],
        "graphml": graphml_check,
        "full_docs": full_docs_check,
        "cache": cache_check,
    }


# =============================================================================
# 11 个 repair 函数（按依赖链顺序）
# =============================================================================


def repair_text_chunks() -> dict[str, Any]:
    """1. 从 GraphML 提活跃 chunk_id 集合 C，按需提取重建 text_chunks。

    真相源：GraphML（提活跃 chunk_id）+ full_docs（text_chunks 没有时反查原文）+ cache（反向构建 llm_cache_list）
    派生：kv_store_text_chunks.json

    算法：
    1. 解析 GraphML 提取活跃 chunk_id 集合 C（从所有 node d3 + edge d10）
    2. 对 C 中每个 chunk_id：
       - 优先从现有 text_chunks 按 cid 查原文（天然最后版本，json_kv_impl.py:181 dict.update 覆盖）
       - 现有 text_chunks 没有时，从 full_docs 重新 chunking 反查（多条匹配取 create_time 最大）
    3. llm_cache_list 从 cache 按 chunk_id 反向构建
    4. 只重建 C 中的 chunk，旧版本 chunk 丢弃（不在 C 中的旧 chunk 不重建）

    GraphML 损坏 = unrecoverable
    full_docs 损坏且 text_chunks 损坏 = unrecoverable
    """
    storage_dir = _storage_dir()
    tc_path = storage_dir / "kv_store_text_chunks.json"
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    # 1. 解析 GraphML 提取活跃 chunk_id 集合 C
    nodes, nodes_err = _load_graphml_nodes()
    if nodes_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {nodes_err.get('msg', '')}",
            "unrecoverable": True,
        }
    _, edges_list, edges_err = _load_graphml_nodes_edges()
    if edges_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {edges_err.get('msg', '')}",
            "unrecoverable": True,
        }

    active_chunk_ids: set[str] = set()
    for _, (_, _, src_ids) in nodes.items():
        if src_ids:
            active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
    for edge_tuple in edges_list:
        edge_src_ids = edge_tuple[2]  # (src, tgt, src_ids, desc, kw) 的 index 2
        if edge_src_ids:
            active_chunk_ids.update(c for c in edge_src_ids.split(GRAPH_FIELD_SEP) if c)

    # 全新用户（GraphML 为空 / 无活跃 chunk）→ 返回 ok 空结果，不报 unrecoverable
    if not active_chunk_ids:
        logger.info("[LightRAGRepair] GraphML 无活跃 chunk_id（全新用户或空图谱），写空 text_chunks")
        _backup_corrupt(tc_path)
        _atomic_write_json(tc_path, {})
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + full_docs",
            "message": "GraphML 无活跃 chunk_id，重建空 text_chunks",
        }

    # 2. 读现有 text_chunks（按 cid 查原文，天然最后版本）
    existing_tc: dict[str, Any] = {}
    tc_corrupt = False
    if tc_path.exists():
        loaded = _load_json_dict(tc_path)
        if isinstance(loaded, dict):
            existing_tc = loaded
        elif loaded is None and tc_path.exists():
            # 文件存在但解析失败 → 损坏
            # 不立即报错，降级到 full_docs 反查（如果 full_docs 也损坏才报 unrecoverable）
            tc_corrupt = True

    # 3. 读 full_docs（text_chunks 没有时才用）
    full_docs: dict[str, Any] = {}
    full_docs_corrupt = False
    if full_docs_path.exists():
        loaded = _load_json_dict(full_docs_path)
        if isinstance(loaded, dict):
            full_docs = loaded
        elif loaded is None and full_docs_path.exists():
            full_docs_corrupt = True

    # 4. 读 cache（反向构建 llm_cache_list）
    cache: dict[str, Any] = {}
    if cache_path.exists():
        loaded = _load_json_dict(cache_path)
        if isinstance(loaded, dict):
            cache = loaded

    # 5. 判断是否需要扫 full_docs（如果 existing_tc 已覆盖所有 C，就不扫）
    need_full_docs_scan = any(cid not in existing_tc for cid in active_chunk_ids)
    full_docs_chunk_map: dict[str, tuple[int, str, str, str]] = {}
    # 类型: chunk_id -> (create_time, doc_id, chunk_content, file_path)
    if need_full_docs_scan:
        if not full_docs:
            # text_chunks 损坏 + full_docs 损坏/空 → unrecoverable
            missing_count = sum(1 for cid in active_chunk_ids if cid not in existing_tc)
            if missing_count > 0:
                src_detail = "text_chunks 损坏且 full_docs 损坏" if (tc_corrupt and full_docs_corrupt) else "部分活跃 chunk 在 text_chunks 和 full_docs 中均缺失"
                return {
                    "status": "error",
                    "expected": len(active_chunk_ids),
                    "actual": len(existing_tc),
                    "lost": missing_count,
                    "source": "GraphML + full_docs",
                    "message": f"{src_detail}，{missing_count} 个活跃 chunk 无法重建",
                    "unrecoverable": True,
                }
        # v8-Task 1：删除调 get_lightrag_for_repair 拿 tokenizer + chunking_by_token_size 的整段（铁律 3）。
        # Task 4 会用独立 tokenizer 重写整段重建逻辑。当前直接返回 unrecoverable stub。
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(existing_tc),
            "lost": len(active_chunk_ids) - len(existing_tc),
            "source": "GraphML + full_docs",
            "message": "v8-Task 1 删除了依赖 get_lightrag_for_repair 的重建路径，Task 4 将用独立 tokenizer 重写",
            "unrecoverable": True,
        }

    # 以下代码在 need_full_docs_scan=True 时不可达（上方 stub return 已退出）。
    # need_full_docs_scan=False 路径（existing_tc 覆盖所有活跃 chunk）仍可执行。
    # v8-Task 4 将用独立 tokenizer 重写整个函数，届时死代码会被新算法替换。

    # 6. 预构建 cache 的 chunk_id → [cache_key] 映射（用于 llm_cache_list）
    #    同一 chunk_id 多条 cache entry（多轮 gleaning）时全部保留
    chunk_id_to_cache_keys: dict[str, list[str]] = {}
    for cache_key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("cache_type") != "extract":
            continue
        cid = entry.get("chunk_id")
        if cid:
            chunk_id_to_cache_keys.setdefault(cid, []).append(cache_key)

    # 7. 遍历 C 构建 new_tc
    new_tc: dict[str, Any] = {}
    missing_chunks: list[str] = []

    for cid in active_chunk_ids:
        # 优先从 existing_tc 查
        if cid in existing_tc and isinstance(existing_tc[cid], dict):
            chunk_data = dict(existing_tc[cid])
            chunk_data["llm_cache_list"] = chunk_id_to_cache_keys.get(cid, [])
            new_tc[cid] = chunk_data
        # 降级从 full_docs_chunk_map 查
        elif cid in full_docs_chunk_map:
            _, doc_id, content, file_path = full_docs_chunk_map[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": doc_id,
                "file_path": file_path,
                "llm_cache_list": chunk_id_to_cache_keys.get(cid, []),
            }
        else:
            # 脑区 chunk（full_doc_id="brain"）可能不在 full_docs 里
            # 如果 existing_tc 也没有，记为 missing（region_sync 会重新注入）
            missing_chunks.append(cid)

    # 8. 备份损坏的 text_chunks + 原子写
    _backup_corrupt(tc_path)
    try:
        _atomic_write_json(tc_path, new_tc)
    except Exception as e:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(new_tc),
            "lost": len(active_chunk_ids) - len(new_tc),
            "source": "GraphML + full_docs",
            "message": f"写 text_chunks 失败: {e}",
            "unrecoverable": True,
        }

    actual = len(new_tc)
    logger.info(
        f"[LightRAGRepair] 重建 text_chunks: {actual}/{len(active_chunk_ids)} 条 "
        f"(source=GraphML 活跃 chunk_id + full_docs 按需提取)"
    )
    return {
        "status": "ok",
        "expected": len(active_chunk_ids),
        "actual": actual,
        "lost": len(missing_chunks),
        "source": "GraphML + full_docs",
        "missing_chunks": missing_chunks[:10],
        "message": f"重建 {actual}/{len(active_chunk_ids)} 个 chunk",
    }


def repair_doc_status() -> dict[str, Any]:
    """2. 从 text_chunks 派生 chunks_list + 从 full_docs 派生 status。

    真相源：kv_store_text_chunks.json + kv_store_full_docs.json
    派生：kv_store_doc_status.json

    chunks_list: 按 full_doc_id 分组 text_chunks 的 key
    status: processed 如果 GraphML 有数据，否则 pending（DocStatus.value 小写）
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

    # 3. 判断 GraphML 是否有数据（决定 status 是 processed 还是 pending，小写匹配 DocStatus.value）
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
    # 循环外加载 doc_status 一次（循环内只读不改，避免每次迭代重读同一文件）
    old_ds = _load_json_dict(doc_status_path) or {}
    if not isinstance(old_ds, dict):
        old_ds = {}
    for doc_id in full_docs.keys():
        chunks_list = sorted(chunks_by_doc.get(doc_id, []))  # 排序保证稳定
        # 保留原 doc_status 的 file_path 等元数据（如果存在）
        old_value = old_ds.get(doc_id, {})
        new_doc_status[doc_id] = {
            # DocStatus.value 是小写（"processed"/"pending"/"failed"），
            # LightRAG get_docs_by_statuses/get_status_counts 用小写字符串匹配，
            # 必须写小写值否则枚举查询找不到文档
            "status": "processed" if graphml_has_data else "pending",
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
    for node_id, (_, desc, src) in nodes.items():
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
    # LightRAG operate.py L1601/L2527: rel_content = f"{combined_keywords}\t{src}\n{tgt}\n{final_description}"
    # combined_keywords 是逗号分隔的多个关键词合并后的字符串（LightRAG operate.py L2173: ",".join(sorted(all_keywords))）
    # GraphML d9 字段直接存储 combined_keywords，已经是逗号分隔的字符串
    # 这里做防御性 normalize：若 d9 错误用 <SEP> 分隔则转成逗号分隔，保持跟 LightRAG 写入格式一致
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
        # normalize keywords：把 <SEP> 分隔（如有）拆成 list 再用 ", " join
        # （跟 LightRAG operate.py L1483 ", ".join(set(keywords)) 一致——多关键词用逗号+空格分隔）
        if edge_keywords and GRAPH_FIELD_SEP in edge_keywords:
            kw_list = [k.strip() for k in edge_keywords.split(GRAPH_FIELD_SEP) if k.strip()]
            normalized_keywords = ", ".join(kw_list)
        else:
            normalized_keywords = edge_keywords or ""
        content = f"{normalized_keywords}\t{sorted_src}\n{sorted_tgt}\n{edge_desc}"
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
    for node_id, (_, _, src) in nodes.items():
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
    for src, tgt, edge_src_id, _, _ in edges:
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
    for node_id, (_, _, src) in nodes.items():
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
    for src, tgt, edge_src_id, _, _ in edges:
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

# 重建依赖链顺序（v8：只含 9 个派生文件的 repair 函数）
# 不含 repair_graphml / repair_brainregion_zombies / repair_graphml_orphan_edges /
# repair_llm_response_cache——v8-Task 1 已删除这些违反铁律 3 的函数（写 3 真相源）。
# 用直接函数引用（不是字符串），拼写错误会在模块加载时 NameError，避免静默跳过。
_REBUILD_ORDER: list[tuple[str, Any]] = [
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
]


def repair_all() -> dict[str, Any]:
    """3 真相源不可动 + 按需提取重建 9 派生文件。

    流程：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
    3. 备份 9 个派生文件（不备份真相源，因为不动）
    4. 删除 9 个派生文件
    5. 按依赖链重建 9 派生文件（从 GraphML + full_docs + cache 按需提取）
    6. 失败时回滚 9 派生文件备份

    3 真相源（GraphML + full_docs + cache）完全不可动：
    - 不写不改不删（读取是必要的，用于按需提取重建派生文件）
    - 损坏 = unrecoverable
    - 完好 = 一根毫毛不动

    返回扁平结构（向后兼容 Rust format_repair_summary）：
        {
            "text_chunks": {status, ...},
            "doc_status": {status, ...},
            ...
            "_unrecoverable": bool,
            "_unrecoverable_reason": str,
            "_truth_source_check": {...},
            "_backed_up": [...],
            "_deleted": [...],
            "_rolled_back": bool,
        }

    注意：repair_all 是同步函数，不能声明 async（调用方 lightrag_manager.py
    是同步调用 repair_all()，async 会导致返回 coroutine 对象）。
    """
    storage_dir = _storage_dir()
    result: dict[str, Any] = {}

    # 0. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager（兼容测试 monkeypatch）
    #    现有代码有这段同步逻辑，重写 repair_all 时必须保留。
    #    否则测试 monkeypatch lightrag_repair._STORAGE_DIR 后，lightrag_integrity._STORAGE_DIR
    #    仍是真实 ~/.niu/lightrag_storage，导致 check_all 读真实路径污染数据。
    try:
        from niu_api.internal import lightrag_integrity
        if lightrag_integrity._STORAGE_DIR != _STORAGE_DIR:
            lightrag_integrity._STORAGE_DIR = _STORAGE_DIR
    except Exception:  # noqa: BLE001
        pass
    try:
        import niu_api.internal.lightrag_manager as lightrag_manager
        lightrag_manager._rag_instance = None
        lightrag_manager._init_failed_at = 0
        lightrag_manager._init_error = None
        lightrag_manager.STORAGE_DIR = storage_dir
    except Exception:  # noqa: BLE001
        pass

    # 用 try/finally 确保所有路径（成功/失败/异常）都清理 .corrupt.*.bak 垃圾文件 + backup_dir
    # backup_dir 提升到 try 外声明，让 finally 能在所有路径访问到
    # （包括备份阶段失败时 backup_dir 未赋值的情况）
    backup_dir: Path | None = None
    try:
        # 1. 检测 3 真相源完好性
        truth_check = _check_truth_sources_intact()
        result["_truth_source_check"] = truth_check
        if not truth_check["intact"]:
            result["_unrecoverable"] = True
            reasons = []
            if not truth_check["graphml"]["intact"]:
                reasons.append(f"GraphML: {truth_check['graphml']['reason']}")
            if not truth_check["full_docs"]["intact"]:
                reasons.append(f"full_docs: {truth_check['full_docs']['reason']}")
            if not truth_check["cache"]["intact"]:
                reasons.append(f"cache: {truth_check['cache']['reason']}")
            result["_unrecoverable_reason"] = "3 真相源损坏，无法恢复: " + "; ".join(reasons)
            result["_rolled_back"] = False  # 没删任何东西，不需要回滚
            return result

        # 2. 备份 9 个派生文件（不备份 3 真相源，因为完全不动）
        #    备份目录放在 storage_dir 外部，避免备份残留污染 storage 目录 + 避免 glob 误扫
        ts = int(time.time())
        backup_dir = storage_dir.parent / f"lightrag_storage.prerepair_{ts}"
        backed_up: list[str] = []
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for fname in _DERIVED_FILES:
                src = storage_dir / fname
                if src.exists():
                    shutil.copy2(src, backup_dir / fname)
                    backed_up.append(fname)
            result["_backed_up"] = backed_up
            logger.info(f"[LightRAGRepair] 备份 {len(backed_up)} 个派生文件到 {backup_dir}")
        except Exception as e:
            result["_unrecoverable"] = True
            result["_unrecoverable_reason"] = f"备份失败: {e}"
            result["_rolled_back"] = False
            return result

        # 3. 删除 9 个派生文件
        deleted: list[str] = []
        for fname in _DERIVED_FILES:
            path = storage_dir / fname
            if path.exists():
                try:
                    path.unlink()
                    deleted.append(fname)
                except Exception as e:
                    # 删除失败，回滚已删除的
                    _rollback_backup(backup_dir, storage_dir, backed_up)
                    result["_unrecoverable"] = True
                    result["_unrecoverable_reason"] = f"删除 {fname} 失败: {e}"
                    result["_deleted"] = deleted
                    result["_rolled_back"] = True
                    return result
        result["_deleted"] = deleted

        # 4. 按依赖链重建 9 派生文件
        #    用 getattr 间接查找函数（不直接引用 _REBUILD_ORDER 里的函数对象），
        #    让测试 monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_fn) 能生效
        #    （如果直接用 _REBUILD_ORDER 里的 fn 对象，monkeypatch 替换模块属性不影响已绑定的 fn）
        import niu_api.internal.lightrag_repair as _self_mod
        for name, fn in _REBUILD_ORDER:
            # 重新从模块属性读取，让 monkeypatch 能注入失败版本
            fn = getattr(_self_mod, fn.__name__)
            try:
                step_result = fn()
                result[name] = step_result
                if isinstance(step_result, dict) and (
                    step_result.get("unrecoverable") or step_result.get("status") == "unrecoverable"
                ):
                    _rollback_backup(backup_dir, storage_dir, backed_up)
                    result["_unrecoverable"] = True
                    result["_unrecoverable_reason"] = f"{name} 重建失败: {step_result.get('message', '')}"
                    result["_rolled_back"] = True
                    logger.warning(
                        f"[LightRAGRepair] {name} 报 unrecoverable: {step_result.get('message', '')}，停止后续重建并回滚"
                    )
                    return result
            except Exception as e:
                logger.error(f"[LightRAGRepair] {name} 抛异常: {e}", exc_info=True)
                _rollback_backup(backup_dir, storage_dir, backed_up)
                result[name] = {
                    "status": "error",
                    "message": f"repair 函数抛异常: {type(e).__name__}: {e}",
                }
                result["_unrecoverable"] = True
                result["_unrecoverable_reason"] = f"{name} 重建异常: {e}"
                result["_rolled_back"] = True
                return result

        # 5. 重建成功，清理备份
        result["_unrecoverable"] = False
        result["_rolled_back"] = False
        try:
            shutil.rmtree(backup_dir)
            logger.info("[LightRAGRepair] 重建成功，清理备份目录")
        except Exception:  # noqa: BLE001
            pass  # 备份没删掉不影响主流程

        return result
    finally:
        # 不论成功/失败/异常，都清理 .corrupt.*.bak 垃圾文件
        # _backup_corrupt 在各 repair 子函数重建前备份损坏文件，
        # 不论 repair 成功还是失败，都清理这些临时文件（备份是用户自己的事）。
        # glob 模式 *.corrupt.*.bak 匹配 _backup_corrupt 创建的 {name}.corrupt.{ts}.bak 文件
        try:
            cleaned = 0
            for bak in storage_dir.glob("*.corrupt.*.bak"):
                try:
                    bak.unlink()
                    cleaned += 1
                except Exception:  # noqa: BLE001
                    pass
            if cleaned > 0:
                logger.info(f"[LightRAGRepair] 清理 {cleaned} 个 .corrupt.*.bak 备份文件")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LightRAGRepair] 清理 .corrupt.*.bak 备份文件失败: {e}")
        # 清理 backup_dir（成功路径已 rmtree，失败回滚路径 return 时也需清理）
        # backup_dir 可能为 None（备份阶段前失败）或路径不存在（成功路径已删），都需防御
        if backup_dir is not None and backup_dir.exists():
            try:
                shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


def _rollback_backup(backup_dir: Path, storage_dir: Path, backed_up: list[str]) -> None:
    """回滚备份：恢复 backed_up 中的文件 + 删除新建的派生文件。

    注意：3 真相源（GraphML + full_docs + cache）不在 _DERIVED_FILES 里，
    回滚不会动它们（它们也从未被修改）。
    """
    # 1. 恢复 backed_up 中的文件
    restored = 0
    for fname in backed_up:
        src = backup_dir / fname
        if src.exists():
            try:
                shutil.copy2(src, storage_dir / fname)
                restored += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LightRAGRepair] 回滚恢复 {fname} 失败: {e}")
    # 2. 删除 repair 前不存在但 repair 后新建的派生文件
    cleaned = 0
    for fname in _DERIVED_FILES:
        if fname not in backed_up:
            fpath = storage_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                    cleaned += 1
                    logger.warning(f"[LightRAGRepair] 回滚：删除错误重建的 {fname}")
                except Exception:  # noqa: BLE001
                    pass
    logger.warning(
        f"[LightRAGRepair] 重建失败，已回滚 {restored} 个文件，清理 {cleaned} 个错误重建文件"
    )


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

