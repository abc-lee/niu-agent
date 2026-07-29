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

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import (
    EmbeddingFunc,
    compute_mdhash_id,
    make_relation_chunk_key,
    make_relation_vdb_ids,
)
from loguru import logger

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"


# =============================================================================
# 工具函数
# =============================================================================


def _storage_dir() -> Path:
    """获取 _STORAGE_DIR（兼容 monkeypatch 注入 str 的形式）。"""
    return Path(_STORAGE_DIR)


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


@dataclass
class RepairEmbeddingFunc(EmbeddingFunc):
    """v9 Repair 专用 EmbeddingFunc，包装 v8 _embed_batch 模型加载逻辑。

    设计：
    - 继承 LightRAG EmbeddingFunc（自动获得维度校验 + 嵌套 unwrap）
    - func 属性指向内部 async 函数 _embed_async
    - _embed_async 内部调 niu_api.internal.embedding.get_model() 拿 bge-base-zh-v1.5 单例
    - 模型单例由 niu_api.internal.embedding 自身管理（_model 全局变量 + _model_lock）
    - 批量分片：超过 32 条文本分批 encode，避免 OOM
    """

    # 显式声明字段（基类已声明 embedding_dim / func / max_token_size / send_dimensions / model_name）
    # 这里不新增字段，只是确保 dataclass 继承正确
    # func 用 Optional[Callable] 而非 Any，避免 pyright 严格模式报类型不兼容
    embedding_dim: int = 768
    func: Callable[..., Any] | None = None  # 在 __post_init__ 中设为 _embed_async
    max_token_size: int | None = None
    send_dimensions: bool = False
    model_name: str | None = "bge-base-zh-v1.5"

    def __post_init__(self):
        """注入 _embed_async 作为 func，然后跑基类 __post_init__ 做维度校验。"""
        # 必须在调基类 __post_init__ 前设好 func
        # 基类 __post_init__ 会检测嵌套 EmbeddingFunc 并 unwrap，这里 func 是普通 async 函数不会被 unwrap
        if self.func is None:
            self.func = self._embed_async
        # 调基类 __post_init__（做嵌套 unwrap + 维度校验准备）
        super().__post_init__()

    async def _embed_async(self, texts: list[str], **kwargs) -> np.ndarray:
        """批量 embedding（async 包装 v8 _embed_batch 同步逻辑）。

        Args:
            texts: 待 embedding 的文本列表

        Returns:
            np.ndarray, shape=(len(texts), 768), dtype=float32

        Raises:
            RuntimeError: 模型未就绪（get_model 返回 None）或 encode 失败
        """
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)

        # 调 v8 _embed_batch（同步，内部用 niu_api.internal.embedding.get_model 单例）
        # 跑在线程池避免阻塞 asyncio loop（模型 encode 是 CPU/GPU 密集型）
        vectors = await asyncio.to_thread(self._sync_embed, texts)

        if vectors is None:
            raise RuntimeError(
                "RepairEmbeddingFunc: niu_api.internal.embedding.get_model() 返回 None 或 encode 失败"
            )

        # 转 np.ndarray + 强制 float32（LightRAG NanoVectorDBStorage 期望 float32 matrix）
        arr = np.array(vectors, dtype=np.float32)
        return arr

    def _sync_embed(self, texts: list[str]) -> list[list[float]] | None:
        """同步批量 embedding（包装 v8 _embed_batch，加分片逻辑）。

        v8 _embed_batch 一次 encode 全部 texts，超过 32 条可能 OOM。
        这里分批 encode（每批 32 条），合并结果。
        """
        BATCH_SIZE = 32  # bge-base-zh-v1.5 推荐批量

        if not texts:
            return []

        try:
            from niu_api.internal.embedding import get_model

            model = get_model()
            if model is None:
                return None

            all_vectors: list[list[float]] = []
            for i in range(0, len(texts), BATCH_SIZE):
                batch = texts[i : i + BATCH_SIZE]
                vecs = model.encode(batch)
                # 转 list[list[float]]（vecs 可能是 numpy ndarray 或 Tensor）
                all_vectors.extend(list(map(float, v)) for v in vecs)

            return all_vectors
        except Exception as e:  # noqa: BLE001
            logger.error(f"[RepairEmbeddingFunc] embedding 模型失败: {e}")
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


def _load_graphml_nodes_edges() -> tuple[set[str], list[tuple[str, str, str, str, str, str]], dict[str, Any] | None]:
    """解析 GraphML，返回 (node_ids, edges, error)。

    node_ids: set of node id
    edges: list of (src, tgt, edge_source_id, edge_description, edge_keywords, edge_file_path)
           - edge_source_id: edge 的 d10 字段（<SEP> 分隔的 chunk_id 列表）
           - edge_description: edge 的 d8 字段（描述文本）
           - edge_keywords: edge 的 d9 字段（关系关键词，逗号分隔，v9 用 dict.fromkeys 去重保序）
           - edge_file_path: edge 的 d11 字段（文件路径，v9 Task 7 新增，用于 vdb_relationships meta_fields）
    error: None 或 {"check": ..., "severity": "critical", ...}

    GraphML edge key 定义（参考真实 GraphML 头部）：
        d7=weight, d8=description, d9=keywords, d10=source_id,
        d11=file_path, d12=created_at, d13=truncate

    v9 Task 7 改动：5 元组 → 6 元组，新增 edge_file_path（d11）。
    理由：vdb_relationships 的 meta_fields 包含 file_path（lightrag.py L722），
    必须从 GraphML edge 的 d11 字段读取。
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
    edges: list[tuple[str, str, str, str, str, str]] = []  # (src, tgt, edge_source_id, edge_description, edge_keywords, edge_file_path)

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
            edge_file_path = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d8":
                    edge_desc = data.text or ""
                elif key == "d10":
                    edge_src_id = data.text or ""
                elif key == "d9":
                    edge_keywords = data.text or ""
                elif key == "d11":
                    edge_file_path = data.text or ""
            edges.append((src, tgt, edge_src_id, edge_desc, edge_keywords, edge_file_path))
    return node_ids, edges, None


def _load_graphml_nodes() -> tuple[dict[str, tuple[str, str, str, str]], dict[str, Any] | None]:
    """解析 GraphML nodes，返回 {node_id: (entity_type, description, source_id, file_path)} + error。

    v8：返回 4 元组 (entity_type, desc, source_id, file_path)，识别脑区节点 d1=="brainregion"。

    entity_type = d1, description = d2, source_id = d3, file_path = d4
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
    nodes: dict[str, tuple[str, str, str, str]] = {}

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
            file_path = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d1":
                    etype = data.text or ""
                elif key == "d2":
                    desc = data.text or ""
                elif key == "d3":
                    src = data.text or ""
                elif key == "d4":
                    file_path = data.text or ""
            nodes[nid] = (etype, desc, src, file_path)
    return nodes, None


def _check_truth_sources_intact() -> dict[str, Any]:
    """检测 3 真相源完好性：GraphML + full_docs + cache。

    判定规则（基于文件状态四态：absent / empty / has_content / corrupt）：
    - 3 个文件全部 absent/empty → intact=True（全新用户合法）
    - 3 个文件全部 has_content 且解析无异常 → intact=True
    - 部分文件 absent/empty 但其他 has_content → intact=False（partial 损坏）
    - 任何文件 corrupt（JSON/XML 解析失败） → intact=False

    关键：JSON 损坏（文件存在但解析失败）必须区分于"文件不存在/空 dict"——
    前者是损坏，后者是全新用户合法。用四态判定避免把"文件存在但 JSON 损坏"和
    "文件存在但空 dict {}"都归为"不存在"。
    """
    import xml.etree.ElementTree as ET

    storage_dir = _storage_dir()

    graphml_path = storage_dir / _GRAPHML_FILE
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    def _json_state(p: Path) -> str:
        if not p.exists() or p.stat().st_size == 0:
            return "absent"
        loaded = _load_json_dict(p)
        if loaded is None or not isinstance(loaded, dict):
            return "corrupt"
        return "has_content" if len(loaded) > 0 else "empty"

    def _graphml_state(p: Path) -> str:
        if not p.exists() or p.stat().st_size == 0:
            return "absent"
        try:
            tree = ET.parse(p)
            root = tree.getroot()
            ns_str = "{http://graphml.graphdrawing.org/xmlns}"
            graph_elem = root.find(f"{ns_str}graph")
            if graph_elem is None:
                for child in root:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "graph":
                        graph_elem = child
                        break
            if graph_elem is None:
                return "corrupt"
            node_elem = graph_elem.find(f"{ns_str}node")
            if node_elem is None:
                for child in graph_elem:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "node":
                        node_elem = child
                        break
            if node_elem is None:
                return "empty"
            return "has_content"
        except Exception:
            return "corrupt"

    graphml_state = _graphml_state(graphml_path)
    full_docs_state = _json_state(full_docs_path)
    cache_state = _json_state(cache_path)
    states = {"graphml": graphml_state, "full_docs": full_docs_state, "cache": cache_state}

    # 任一 corrupt → intact=False
    if any(s == "corrupt" for s in states.values()):
        return {
            "intact": False,
            "graphml": {
                "intact": False,
                "reason": "XML 解析失败" if graphml_state == "corrupt" else f"状态: {graphml_state}",
            },
            "full_docs": {
                "intact": False,
                "reason": "JSON 解析失败或非 dict" if full_docs_state == "corrupt" else f"状态: {full_docs_state}",
            },
            "cache": {
                "intact": False,
                "reason": "JSON 解析失败或非 dict" if cache_state == "corrupt" else f"状态: {cache_state}",
            },
        }

    # 3 文件全部 absent/empty → 全新用户合法
    if all(s in ("absent", "empty") for s in states.values()):
        return {
            "intact": True,
            "graphml": {"intact": True, "reason": "GraphML 不存在或为空（全新用户合法）"},
            "full_docs": {"intact": True, "reason": "full_docs 不存在或为空（全新用户合法）"},
            "cache": {"intact": True, "reason": "cache 不存在或为空（全新用户合法）"},
        }

    # v2 修复（2026-07-28）：partial 状态不再判为 intact=False
    # 脑区/Skills 注入路径（ainsert_custom_kg）只写 GraphML + 3 vdb + 可选 text_chunks，
    # 不写 full_docs / llm_response_cache。GraphML 有内容但 full_docs/cache absent
    # 是合法中间状态（用户未入库文档），不应阻断修复流程。
    # 真损坏判定已由 _check_truth_source（corrupt）和 _check_vdb_missing（数据不一致）负责。
    # 此分支覆盖所有非 corrupt、非全新用户场景（partial + 全部 has_content），统一返回 intact=True。
    #
    # I2 修复：reason 文案用中性表述"absent/empty，LightRAG 按需加载"，
    # 不假定"脑区/Skills 路径合法"——普通文档实体 + full_docs 被误删也走这个分支，
    # 但 _check_vdb_missing 会检测出 vdb 与 GraphML 不一致并报 major（真损坏由数据一致性检查兜底）。
    return {
        "intact": True,
        "graphml": {
            "intact": True,
            "reason": "有 node" if graphml_state == "has_content" else "GraphML 不存在或为空（合法）",
        },
        "full_docs": {
            "intact": True,
            "reason": "有 entries" if full_docs_state == "has_content" else "absent/empty（LightRAG 按需加载，未入库文档时正常）",
        },
        "cache": {
            "intact": True,
            "reason": "有 entries" if cache_state == "has_content" else "absent/empty（LightRAG 按需加载，未入库文档时正常）",
        },
    }


# =============================================================================
# 11 个 repair 函数（按依赖链顺序）
# =============================================================================


async def repair_text_chunks() -> dict[str, Any]:
    """v9：从 GraphML 提活跃 chunk_id + cache original_prompt 优先 + full_docs fallback。

    真相源：GraphML（活跃 chunk_id）+ kv_store_llm_response_cache.json（chunk 原文）+ kv_store_full_docs.json（fallback）
    派生：kv_store_text_chunks.json（通过 JsonKVStorage.upsert 写）

    走 storage 接口的好处：
    - JsonKVStorage.upsert 自动注入 _id / create_time / update_time
    - text_chunks namespace 自动补 llm_cache_list=[]（L167-169）
    - index_done_callback 统一写盘 + sanitization

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonKVStorage(namespace=text_chunks, embedding_func=None)
    3. await storage.initialize()（读已有 kv_store_text_chunks.json 到内存）
    4. 解析 GraphML 提活跃 chunk_id + 识别脑区节点（v8 逻辑保留）
    5. cache original_prompt 优先（正则提取 ``` 之间内容，多条取 create_time 最大）
    6. cache 没有则 full_docs chunking 反查（v8 逻辑保留）
    7. 调 await storage.upsert(new_tc) + await storage.index_done_callback()
    8. 全新用户（GraphML 无活跃 chunk）→ upsert({}) 会被 storage 跳过，需手动写空文件
       （LightRAG 正常启动全新用户时 text_chunks.json 是 {}，不是不存在）

    异常处理：
    - GraphML 损坏 → unrecoverable
    - cache 损坏（JSON 解析失败）→ unrecoverable
    - full_docs 损坏 → unrecoverable
    - tokenizer 加载失败 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    import re

    storage_dir = _storage_dir()
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonKVStorage
    #    global_config 必须含 working_dir（JsonKVStorage.__post_init__ L30 读）
    #    embedding_func 传 None（text_chunks 不用 embedding，跟 LightRAG lightrag.py:670 一致）
    #    LightRAG 原生用 # type: ignore 绕过 EmbeddingFunc 类型校验（base.py:364 不允许 None，
    #    但 text_chunks/full_docs 等 KV 存储从不调用 embedding_func，运行时 None 安全）
    global_config = {"working_dir": str(storage_dir)}
    storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_TEXT_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
    except Exception as e:
        logger.error(f"[LightRAGRepair] text_chunks storage.initialize 失败: {e}", exc_info=True)
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonKVStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # v9 Task 3 修复：清空 storage._data 内存 dict
    # 原因：JsonKVStorage 用全局 _shared_dicts["text_chunks"] 共享内存（shared_storage.py:1481），
    # 跨进程/跨调用会保留上一次的 chunk 数据。repair 必须从空开始重建
    # （v8 是直接覆盖写文件，相当于"清空 + 重写"）。
    # storage.initialize() 只在首次调用时从文件加载（try_initialize_namespace 返回 True），
    # 后续调用 try_initialize_namespace 返回 False 不会重新加载——所以即便文件不存在，
    # _data 也可能含上次的残留数据。这里强制 clear 保证语义正确。
    # 注意：clear 后调 upsert 时所有 key 都是新 key → create_time + update_time 都被注入。
    if storage._data is not None:
        storage._data.clear()

    # 3. 解析 GraphML 提取活跃 chunk_id 集合 + 识别脑区节点
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

    # 收集活跃 chunk_id + 识别脑区 chunk 元数据
    active_chunk_ids: set[str] = set()
    brainregion_chunks: dict[str, tuple[str, str]] = {}
    # brainregion_chunks: chunk_id -> (content, full_doc_id)

    for node_id, (etype, desc, src_ids, _file_path) in nodes.items():
        if etype == "brainregion":
            if src_ids:
                brain_content = f"{node_id}: {desc}"
                brain_full_doc_id = f"brain_{node_id}"
                for cid in src_ids.split(GRAPH_FIELD_SEP):
                    if cid:
                        brainregion_chunks[cid] = (brain_content, brain_full_doc_id)
                        active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
        else:
            if src_ids:
                active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
    for edge_tuple in edges_list:
        edge_src_ids = edge_tuple[2]
        if edge_src_ids:
            active_chunk_ids.update(c for c in edge_src_ids.split(GRAPH_FIELD_SEP) if c)

    # 4. 全新用户（GraphML 无活跃 chunk）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonKVStorage.initialize 只设 _data={} 内存空 dict，
    #    不主动写空文件到磁盘（文件不存在）。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 text_chunks.json 不存在，不要强行写空 {} 文件
    #    （v8 写空 {} 跟 LightRAG 全新用户首次启动不一致，字节级 diff 会失败）。
    #    _check_truth_sources_intact 已支持 absent/empty=合法（L460 all absent/empty），
    #    所以下次启动 check_all 不会因派生文件不存在而报 critical。
    if not active_chunk_ids:
        logger.info("[LightRAGRepair] GraphML 无活跃 chunk_id（全新用户），不写派生文件（跟 LightRAG 原生一致）")
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + cache + full_docs",
            "message": "GraphML 无活跃 chunk_id，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 读 cache（主补充源）
    cache: dict[str, Any] = {}
    cache_corrupt = False
    if cache_path.exists():
        loaded = _load_json_dict(cache_path)
        if isinstance(loaded, dict):
            cache = loaded
        elif loaded is None and cache_path.exists():
            cache_corrupt = True

    if cache_corrupt:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": 0,
            "lost": len(active_chunk_ids),
            "source": "GraphML + cache + full_docs",
            "message": "cache 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 6. 读 full_docs（fallback）
    full_docs: dict[str, Any] = {}
    full_docs_corrupt = False
    if full_docs_path.exists():
        loaded = _load_json_dict(full_docs_path)
        if isinstance(loaded, dict):
            full_docs = loaded
        elif loaded is None and full_docs_path.exists():
            full_docs_corrupt = True

    # 7. 构建 cache 的 chunk_id -> [(create_time, original_prompt, cache_key)] 映射
    cache_by_chunk_id: dict[str, list[tuple[int, str, str]]] = {}
    cache_pattern = re.compile(r"```\s*(.+?)\s*```", re.DOTALL)
    for cache_key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("cache_type") != "extract":
            continue
        cid = entry.get("chunk_id")
        if not cid:
            continue
        ct = entry.get("create_time", 0)
        op = entry.get("original_prompt", "")
        cache_by_chunk_id.setdefault(cid, []).append((ct, op, cache_key))

    for cid in cache_by_chunk_id:
        cache_by_chunk_id[cid].sort(key=lambda x: x[0], reverse=True)

    # 8. full_docs chunking 反查（补 full_doc_id / tokens / chunk_order_index / file_path）
    full_docs_chunk_map: dict[str, tuple[int, str, str, str, int, int]] = {}
    # 类型: chunk_id -> (create_time, doc_id, chunk_content, file_path, tokens, chunk_order_index)

    if full_docs_corrupt:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": 0,
            "lost": len(active_chunk_ids),
            "source": "GraphML + cache + full_docs",
            "message": "full_docs 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 用于脑区/cache-derived chunks 现算 tokens 时复用 tokenizer
    tokenizer: Any = None
    if full_docs:
        tokenizer = _get_tokenizer()
        if tokenizer is None:
            return {
                "status": "error",
                "expected": len(active_chunk_ids),
                "actual": 0,
                "lost": len(active_chunk_ids),
                "source": "GraphML + cache + full_docs",
                "message": "TiktokenTokenizer 加载失败，无法 chunking",
                "unrecoverable": True,
            }
        chunk_token_size, chunk_overlap = _get_chunk_config()

        from lightrag.operate import chunking_by_token_size

        sorted_docs = sorted(
            full_docs.items(),
            key=lambda kv: kv[1].get("create_time", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )

        for doc_id, doc_data in sorted_docs:
            if not isinstance(doc_data, dict):
                continue
            content = doc_data.get("content", "")
            if not content:
                continue
            file_path = doc_data.get("file_path", "") or "unknown_source"
            create_time = doc_data.get("create_time", 0)

            chunks = chunking_by_token_size(
                tokenizer, content,
                chunk_token_size=chunk_token_size,
                chunk_overlap_token_size=chunk_overlap,
            )
            for chunk in chunks:
                chunk_content = chunk["content"]
                cid = compute_mdhash_id(chunk_content, prefix="chunk-")
                if cid not in full_docs_chunk_map:
                    full_docs_chunk_map[cid] = (
                        create_time,
                        doc_id,
                        chunk_content,
                        file_path,
                        chunk.get("tokens", 0),
                        chunk.get("chunk_order_index", 0),
                    )

    # 9. 遍历活跃 chunk_id 构建 new_tc
    new_tc: dict[str, dict[str, Any]] = {}
    missing_chunks: list[str] = []

    for cid in active_chunk_ids:
        # 9.1 cache original_prompt 提取（取 create_time 最大的 entry）
        if cid in cache_by_chunk_id:
            latest_entry = cache_by_chunk_id[cid][0]
            _, op, _ = latest_entry
            m = cache_pattern.search(op)
            if m:
                chunk_content = m.group(1)
                # 反查 full_docs_chunk_map 补 full_doc_id / tokens / chunk_order_index / file_path
                doc_id = ""
                tokens = 0
                chunk_order_index = 0
                file_path = "unknown_source"
                if cid in full_docs_chunk_map:
                    _, doc_id, _, file_path, tokens, chunk_order_index = full_docs_chunk_map[cid]
                else:
                    # cache 有但 full_docs 没：tokens 用 tokenizer 现算
                    if tokenizer is not None:
                        try:
                            tokens = len(tokenizer.encode(chunk_content))
                        except Exception:  # noqa: BLE001
                            tokens = 0
                new_tc[cid] = {
                    "content": chunk_content,
                    "full_doc_id": doc_id,
                    "tokens": tokens,
                    "chunk_order_index": chunk_order_index,
                    "file_path": file_path,
                    "llm_cache_list": [e[2] for e in cache_by_chunk_id[cid]],
                }
                continue
        # 9.2 full_docs fallback
        if cid in full_docs_chunk_map:
            _, doc_id, content, file_path, tokens, chunk_order_index = full_docs_chunk_map[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": doc_id,
                "tokens": tokens,
                "chunk_order_index": chunk_order_index,
                "file_path": file_path,
                "llm_cache_list": [e[2] for e in cache_by_chunk_id.get(cid, [])],
            }
            continue
        # 9.3 脑区直接构造（fallback）
        if cid in brainregion_chunks:
            content, full_doc_id = brainregion_chunks[cid]
            # 脑区 content 用 tokenizer 算 tokens
            tokens = 0
            if tokenizer is not None:
                try:
                    tokens = len(tokenizer.encode(content))
                except Exception:  # noqa: BLE001
                    tokens = 0
            new_tc[cid] = {
                "content": content,
                "full_doc_id": full_doc_id,
                "tokens": tokens,
                "chunk_order_index": 0,
                "file_path": "unknown_source",
                "llm_cache_list": [e[2] for e in cache_by_chunk_id.get(cid, [])],
            }
            continue
        # 9.4 三处都没有 → missing
        missing_chunks.append(cid)

    # 10. 调 storage.upsert + index_done_callback
    try:
        await storage.upsert(new_tc)
        await storage.index_done_callback()
    except Exception as e:
        logger.error(f"[LightRAGRepair] text_chunks storage.upsert/index_done_callback 失败: {e}", exc_info=True)
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(new_tc),
            "lost": len(active_chunk_ids) - len(new_tc),
            "source": "JsonKVStorage",
            "message": f"storage.upsert 或 index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(new_tc)
    logger.info(
        f"[LightRAGRepair] 重建 text_chunks: {actual}/{len(active_chunk_ids)} 条 "
        f"(cache original_prompt 优先 + full_docs fallback + 脑区直接构造，"
        f"missing={len(missing_chunks)})"
    )
    return {
        "status": "ok",
        "expected": len(active_chunk_ids),
        "actual": actual,
        "lost": len(missing_chunks),
        "source": "GraphML + cache + full_docs",
        "missing_chunks": missing_chunks[:10],
        "message": f"重建 {actual}/{len(active_chunk_ids)} 个 chunk，missing {len(missing_chunks)} 个",
    }


async def repair_doc_status() -> dict[str, Any]:
    """v9：从 text_chunks 反查 chunks_list + 从 full_docs 派生 doc_status。

    真相源：kv_store_full_docs.json（doc 列表）+ kv_store_text_chunks.json（chunks_list 反查）
    派生：kv_store_doc_status.json（通过 JsonDocStatusStorage.upsert 写）

    走 storage 接口的好处：
    - JsonDocStatusStorage.upsert 自动补 chunks_list=[]（L215-216）
    - upsert 末尾自动调 index_done_callback（L222，无需手动）
    - write_json 做 sanitization + 自动 reload（L184-195）

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonDocStatusStorage(namespace=doc_status, embedding_func=None)
    3. await storage.initialize()
    4. 读 full_docs（doc 列表 + content_summary + content_length + file_path）
    5. 读 text_chunks（反查 chunks_list：chunk.full_doc_id == doc_id 的所有 chunk_id）
    6. 判断 GraphML 是否有数据（决定 status 是 processed 还是 pending）
    7. 构造 upsert data：每 doc 含 status/chunks_count/chunks_list/content_summary/
       content_length/created_at/updated_at/file_path/track_id/metadata/
       error_msg/multimodal_processed
    8. 调 await storage.upsert(data)（内部自动 index_done_callback 写盘）
    9. 全新用户（full_docs 为空）→ 不写派生文件

    异常处理：
    - full_docs 损坏 → unrecoverable
    - text_chunks 损坏 → unrecoverable
    - storage.initialize / upsert 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"
    graphml_path = storage_dir / _GRAPHML_FILE

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_doc_status_impl import JsonDocStatusStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonDocStatusStorage
    #    global_config 必须含 working_dir（JsonDocStatusStorage.__post_init__ L35 读）
    #    embedding_func 传 None（doc_status 不用 embedding）
    global_config: dict[str, Any] = {"working_dir": str(storage_dir)}
    storage = JsonDocStatusStorage(
        namespace=NameSpace.DOC_STATUS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
        # 跟 Task 3 一致：清空共享 dict 防止旧数据残留影响本次重建
        # （JsonDocStatusStorage.initialize 会从磁盘 load_json 合并到 _data，
        #  repair 场景下我们要求从真相源完全重新派生，所以清空旧数据）
        if storage._data is not None:
            storage._data.clear()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] doc_status storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonDocStatusStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 full_docs（真相源）
    full_docs = _load_json_dict(full_docs_path)
    if full_docs is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_full_docs",
            "message": "full_docs 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 4. 全新用户（full_docs 为空）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonDocStatusStorage.initialize 只设内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 doc_status.json 不存在，不要强行写空 {} 文件
    #    （跟原生不一致，字节级 diff 会失败）。
    #    _check_truth_sources_intact 已支持 absent/empty=合法，
    #    所以下次启动 check_all 不会因派生文件不存在而报 critical。
    if not full_docs:
        logger.info(
            "[LightRAGRepair] full_docs 为空（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_full_docs + kv_store_text_chunks",
            "message": "full_docs 为空，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 读 text_chunks（真相源）
    text_chunks = _load_json_dict(text_chunks_path)
    if text_chunks is None:
        return {
            "status": "error",
            "expected": len(full_docs),
            "actual": 0,
            "lost": len(full_docs),
            "source": "kv_store_text_chunks",
            "message": "text_chunks 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 6. 判断 GraphML 是否有数据（决定 status 是 processed 还是 pending）
    #    GraphML 文件大小 > 200 字节视为有数据（v8 逻辑保留）
    graphml_has_data = graphml_path.exists() and graphml_path.stat().st_size > 200

    # 7. 按 full_doc_id 分组 chunks_list（反查 text_chunks）
    chunks_by_doc: dict[str, list[str]] = {}
    for chunk_id, chunk_value in text_chunks.items():
        if not isinstance(chunk_value, dict):
            continue
        full_doc_id = chunk_value.get("full_doc_id", "")
        if not full_doc_id:
            continue
        chunks_by_doc.setdefault(full_doc_id, []).append(chunk_id)

    # 8. 构造 upsert data（严格对照字段表）
    #    created_at 用 full_docs.create_time 转 ISO 8601 UTC（无则空字符串）
    #    updated_at 用 repair 时刻 ISO 8601 UTC（跟 LightRAG lightrag.py:2167-2169 一致）
    from datetime import datetime

    upsert_data: dict[str, dict[str, Any]] = {}
    for doc_id, doc_data in full_docs.items():
        if not isinstance(doc_data, dict):
            continue
        chunks_list = sorted(chunks_by_doc.get(doc_id, []))  # 排序保证稳定
        content = doc_data.get("content", "")
        file_path = doc_data.get("file_path", "") or ""
        track_id = doc_data.get("track_id")  # None 或 str
        create_time_raw = doc_data.get("create_time", 0)

        # created_at: full_docs.create_time 是 Unix timestamp（int），转 ISO 8601 UTC
        # v9 走 storage 接口必须按 DocProcessingStatus 数据类要求写（base.py:781）
        # created_at 是 str 类型，空字符串是合法 fallback
        if isinstance(create_time_raw, (int, float)) and create_time_raw > 0:
            created_at = datetime.fromtimestamp(
                create_time_raw, tz=UTC
            ).isoformat()
        else:
            created_at = ""

        # updated_at: repair 时刻 ISO 8601 UTC（跟 lightrag.py:2167-2169 一致）
        updated_at = datetime.now(UTC).isoformat()

        # content_summary: content 前 100 字符（跟 base.py:774 注释一致）
        content_summary = content[:100] if content else ""
        # content_length: content 总长度
        content_length = len(content) if content else 0

        # metadata: 跟 lightrag.py:2172-2175 一致（processing_start/end_time）
        # repair 场景没有真实处理时间，用 create_time 兜底
        proc_time = (
            int(create_time_raw)
            if isinstance(create_time_raw, (int, float))
            else 0
        )
        metadata = {
            "processing_start_time": proc_time,
            "processing_end_time": proc_time,
        }

        upsert_data[doc_id] = {
            "status": "processed" if graphml_has_data else "pending",
            "chunks_count": len(chunks_list),
            "chunks_list": chunks_list,
            "content_summary": content_summary,
            "content_length": content_length,
            "created_at": created_at,
            "updated_at": updated_at,
            "file_path": file_path,
            "track_id": track_id,
            "metadata": metadata,
            # v9 第 3 轮审查修复 I3：补 error_msg / multimodal_processed 字段
            # 对齐 DocProcessingStatus 数据类（base.py:791-796）完整字段集
            "error_msg": None,
            "multimodal_processed": None,
        }

    # 9. 调 storage.upsert（内部自动 index_done_callback 写盘）
    try:
        await storage.upsert(upsert_data)
        # JsonDocStatusStorage.upsert 末尾自动调 index_done_callback（L222）
        # 不需要手动调
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] doc_status storage.upsert 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(full_docs),
            "actual": 0,
            "lost": len(full_docs),
            "source": "JsonDocStatusStorage",
            "message": f"storage.upsert 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 doc_status: {actual} 条 "
        f"(source=full_docs + text_chunks chunks_list 反查，"
        f"graphml_has_data={graphml_has_data})"
    )
    return {
        "status": "ok",
        "expected": len(full_docs),
        "actual": actual,
        "lost": len(full_docs) - actual,
        "source": "kv_store_full_docs + kv_store_text_chunks",
        "message": f"从 full_docs 派生 status + text_chunks 反查 chunks_list，重建 {actual} 条",
    }



async def repair_vdb_chunks() -> dict[str, Any]:
    """v9：从 text_chunks 读 content + 走 NanoVectorDBStorage.upsert 重建 vdb_chunks。

    真相源：kv_store_text_chunks.json（chunk content + full_doc_id + file_path）
    派生：vdb_chunks.json（通过 NanoVectorDBStorage.upsert 写）

    走 storage 接口的好处：
    - NanoVectorDBStorage.upsert 内部自动调 embedding_func 做 embed（L123-124）
    - 自动注入 __id__ / __created_at__ / vector / __vector__（L110-134）
    - index_done_callback 触发 NanoVectorDB.save 写 matrix（L2 归一化后的单位向量）
    - meta_fields 过滤掉 tokens/chunk_order_index/llm_cache_list（不落盘）

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 NanoVectorDBStorage(namespace=chunks, embedding_func=RepairEmbeddingFunc)
    3. await storage.initialize()
    4. 读 text_chunks（content + full_doc_id + file_path）
    5. 构造 upsert data：{chunk_id: {"content": ..., "full_doc_id": ..., "file_path": ...}}
    6. 调 await storage.upsert(data) + await storage.index_done_callback()
    7. 全新用户（text_chunks 为空）→ 不写派生文件

    关键：
    - 只传 meta_fields 内字段（content/full_doc_id/file_path）
    - 不要传 tokens/chunk_order_index/llm_cache_list（被过滤不落盘）
    - 不要手写 __id__/__created_at__/vector/__vector__（storage 自动注入）
    - 不要手写 matrix/embedding_dim（NanoVectorDB 内部管理）
    - upsert 后必须显式调 index_done_callback 才写盘

    异常处理：
    - text_chunks 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()
    text_chunks_path = storage_dir / "kv_store_text_chunks.json"
    vdb_path = storage_dir / "vdb_chunks.json"

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 NanoVectorDBStorage
    #    global_config 必须含：
    #    - working_dir（NanoVectorDBStorage.__post_init__ L43 读）
    #    - vector_db_storage_cls_kwargs.cosine_better_than_threshold（L36-41 强制要求）
    #    - embedding_batch_num（L59 读，控制 embedding 分批大小）
    #    embedding_func 传 RepairEmbeddingFunc 实例（Task 2 包装好的）
    global_config: dict[str, Any] = {
        "working_dir": str(storage_dir),
        "vector_db_storage_cls_kwargs": {
            "cosine_better_than_threshold": 0.2,  # 跟 lightrag_manager 配置一致
        },
        "embedding_batch_num": 32,  # 跟 RepairEmbeddingFunc 内部分片大小一致
    }
    storage = NanoVectorDBStorage(
        namespace=NameSpace.VECTOR_STORE_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=RepairEmbeddingFunc(embedding_dim=768),
        meta_fields={"full_doc_id", "content", "file_path"},
    )

    try:
        await storage.initialize()
        # 跟 Task 3/4 一致：清空旧数据防止残留影响本次重建
        # （NanoVectorDBStorage.__post_init__ L61-64 已在 _client 里加载已有 vdb_chunks.json，
        #  repair 场景下我们要求从真相源完全重新派生——如果不 clear，旧 chunk 不会被删除，
        #  NanoVectorDB.upsert 只会按 __id__ 更新已有条目，已删除的 chunk 会残留）
        # NanoVectorDB 把数据存在 self._client._NanoVectorDB__storage dict 里
        # （keys: embedding_dim / data(list) / matrix(np.ndarray)）
        client = storage._client
        if client is not None:
            client_storage = getattr(client, "_NanoVectorDB__storage", None)
            if isinstance(client_storage, dict):
                client_storage["data"] = []
                client_storage["matrix"] = np.array(
                    [], dtype=np.float32
                ).reshape(0, storage.embedding_func.embedding_dim)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_chunks storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "NanoVectorDBStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 text_chunks（真相源）
    text_chunks = _load_json_dict(text_chunks_path)
    if text_chunks is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 4. 全新用户（text_chunks 为空）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    #    LightRAG 全新用户首次启动 NanoVectorDBStorage.initialize 内存空 dict，
    #    不主动写空文件到磁盘（文件不存在）。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 vdb_chunks.json 不存在，不要强行写空 vdb 文件
    #    （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致——
    #     write_json 可能做字段重排序或 unicode 转义，跟 NanoVectorDB.save 不一致，
    #     字节级 diff 会失败）。
    #    _check_truth_sources_intact 已支持 absent/empty=合法，
    #    所以下次启动 check_all 不会因派生文件不存在而报 critical。
    if not text_chunks:
        logger.info(
            "[LightRAGRepair] text_chunks 为空（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "kv_store_text_chunks",
            "message": "text_chunks 为空，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 构造 upsert data（只传 meta_fields 内字段）
    upsert_data: dict[str, dict[str, Any]] = {}
    skipped_count = 0
    for chunk_id, chunk_value in text_chunks.items():
        if not isinstance(chunk_value, dict):
            skipped_count += 1
            continue
        content = chunk_value.get("content", "")
        if not content:
            # content 为空跳过（无法 embedding）
            skipped_count += 1
            continue
        full_doc_id = chunk_value.get("full_doc_id", "") or ""
        file_path = chunk_value.get("file_path", "") or "unknown_source"

        upsert_data[chunk_id] = {
            "content": content,
            "full_doc_id": full_doc_id,
            "file_path": file_path,
        }

    if not upsert_data:
        # text_chunks 全是空 content → 不写派生文件（v9 第 2 轮审查修复 问题 5+6 / I3+I2）
        # 跟全新用户分支一致——不写空 vdb 文件，让 vdb_chunks.json 不存在
        # （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致）
        logger.warning(
            f"[LightRAGRepair] text_chunks 有 {len(text_chunks)} 条但全部 content 为空，"
            "不写派生文件（跟 LightRAG 原生全新用户首次启动一致）"
        )
        return {
            "status": "ok",
            "expected": len(text_chunks),
            "actual": 0,
            "lost": len(text_chunks),
            "source": "kv_store_text_chunks",
            "message": (
                f"text_chunks {len(text_chunks)} 条全部 content 为空，"
                "不写派生文件（跟 LightRAG 原生一致）"
            ),
        }

    # 6. 调 storage.upsert（内部自动做 embedding + 注入 __id__/__vector__/vector）
    try:
        await storage.upsert(upsert_data)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_chunks storage.upsert 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": (
                f"storage.upsert 异常（embedding 可能失败）: "
                f"{type(e).__name__}: {e}"
            ),
            "unrecoverable": True,
        }

    # 7. 调 index_done_callback 写盘（NanoVectorDB.save 写 embedding_dim/data/matrix）
    try:
        success = await storage.index_done_callback()
        if not success:
            return {
                "status": "error",
                "expected": len(upsert_data),
                "actual": 0,
                "lost": len(upsert_data),
                "source": "NanoVectorDBStorage",
                "message": "index_done_callback 返回 False（可能被其他进程更新覆盖）",
                "unrecoverable": True,
            }
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_chunks index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": f"index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 vdb_chunks: {actual}/{len(text_chunks)} 条 "
        f"(source=text_chunks，skipped={skipped_count}，"
        f"embedding 由 RepairEmbeddingFunc 自动计算)"
    )
    # vdb_path 仅用于日志可读性（storage 内部已写盘）
    _ = vdb_path
    return {
        "status": "ok",
        "expected": len(text_chunks),
        "actual": actual,
        "lost": len(text_chunks) - actual,
        "source": "kv_store_text_chunks",
        "message": f"从 text_chunks 走 NanoVectorDBStorage.upsert 重建 {actual} 条 vdb_chunks",
    }


async def repair_vdb_entities() -> dict[str, Any]:
    """v9：从 GraphML 节点读实体 + 走 NanoVectorDBStorage.upsert 重建 vdb_entities。

    真相源：graph_chunk_entity_relation.graphml（node id + d2 description + d3 source_id + d4 file_path）
    派生：vdb_entities.json（通过 NanoVectorDBStorage.upsert 写）

    走 storage 接口的好处：
    - NanoVectorDBStorage.upsert 内部自动调 embedding_func 做 embed（L123-124）
    - 自动注入 __id__ / __created_at__ / vector / __vector__（L110-134）
    - index_done_callback 触发 NanoVectorDB.save 写 matrix（L2 归一化后的单位向量）
    - meta_fields 过滤掉 description/entity_type（不落盘）

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 NanoVectorDBStorage(namespace=entities, embedding_func=RepairEmbeddingFunc)
    3. await storage.initialize()
    4. 读 GraphML nodes（用 v8 _load_graphml_nodes，返回 4 元组）
    5. 构造 upsert data（v9 第 2 轮审查修复 问题 1：dict key 用 hash ID）：
       {compute_mdhash_id(entity_name, prefix="ent-"): {
           "content": f"{entity_name}\n{description}",  # 跟 operate.py L1160 一致
           "entity_name": entity_name,  # 防御性 .lower()
           "source_id": src or "",
           "file_path": file_path or "unknown_source",
       }}
       注意：dict key = hash ID（不是 entity_name），因为
       NanoVectorDBStorage.upsert L110 把 dict key 直接作为 __id__，
       必须跟 LightRAG operate.py L1159 compute_mdhash_id(entity_name, prefix="ent-") 一致。
    6. 调 await storage.upsert(data) + await storage.index_done_callback()
    7. 全新用户（GraphML 无节点）→ 不写派生文件（v9 第 2 轮审查修复 问题 5+6 / I3+I2）

    关键：
    - content 格式必须 f"{entity_name}\n{description}"（跟 operate.py L1160 一致，影响向量比对）
    - entity_name 必须 .lower()（GraphML 已 lower，防御性再 lower）
    - 不要传 description / entity_type（meta_fields 不含，被过滤不落盘）
    - 不要手写 __id__/__created_at__/vector/__vector__（storage 自动注入）
    - upsert 后必须显式调 index_done_callback 才写盘

    异常处理：
    - GraphML 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 NanoVectorDBStorage
    #    global_config 必须含：
    #    - working_dir（NanoVectorDBStorage.__post_init__ L43 读）
    #    - vector_db_storage_cls_kwargs.cosine_better_than_threshold（L36-41 强制要求）
    #    - embedding_batch_num（L59 读，控制 embedding 分批大小）
    #    embedding_func 传 RepairEmbeddingFunc 实例（Task 2 包装好的）
    #    meta_fields 跟 LightRAG lightrag.py:716 一致
    global_config: dict[str, Any] = {
        "working_dir": str(storage_dir),
        "vector_db_storage_cls_kwargs": {
            "cosine_better_than_threshold": 0.2,
        },
        "embedding_batch_num": 32,
    }
    storage = NanoVectorDBStorage(
        namespace=NameSpace.VECTOR_STORE_ENTITIES,
        workspace="",
        global_config=global_config,
        embedding_func=RepairEmbeddingFunc(embedding_dim=768),
        meta_fields={"entity_name", "source_id", "content", "file_path"},
    )

    try:
        await storage.initialize()
        # 跟 Task 5 一致：清空 NanoVectorDB client storage 防止旧数据残留影响本次重建
        # （NanoVectorDBStorage.__post_init__ 已在 _client 里加载已有 vdb_entities.json，
        #  repair 场景下我们要求从真相源完全重新派生——如果不 clear，旧 entity 不会被删除，
        #  NanoVectorDB.upsert 只会按 __id__ 更新已有条目，已删除的 entity 会残留）
        # NanoVectorDB 把数据存在 self._client._NanoVectorDB__storage dict 里
        # （keys: embedding_dim / data(list) / matrix(np.ndarray)）
        client = storage._client
        if client is not None:
            client_storage = getattr(client, "_NanoVectorDB__storage", None)
            if isinstance(client_storage, dict):
                client_storage["data"] = []
                client_storage["matrix"] = np.array(
                    [], dtype=np.float32
                ).reshape(0, storage.embedding_func.embedding_dim)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_entities storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "NanoVectorDBStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML nodes（真相源，v8 _load_graphml_nodes 保留）
    #    返回 {node_id: (entity_type, description, source_id, file_path)}
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 全新用户（GraphML 无节点）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    #    LightRAG 全新用户首次启动 NanoVectorDBStorage.initialize 内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 vdb_entities.json 不存在，不要强行写空 vdb 文件
    #    （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致）。
    #    _check_truth_sources_intact 已支持 absent/empty=合法（L460）。
    if not nodes:
        logger.info(
            "[LightRAGRepair] GraphML 无 node（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 node，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 构造 upsert data（严格对照字段表）
    #    content 格式：f"{entity_name}\n{description}"（跟 operate.py L1160 一致）
    #    entity_name：GraphML node id（已 lower，防御性再 lower）
    #    source_id：GraphML d3（无则空字符串）
    #    file_path：GraphML d4（无则 "unknown_source"）
    #    不传 description / entity_type（被 meta_fields 过滤不落盘）
    upsert_data: dict[str, dict[str, Any]] = {}
    skipped_count = 0
    for node_id, (_etype, desc, src, file_path) in nodes.items():
        if not node_id:
            skipped_count += 1
            continue

        # 防御性 lower（GraphML 已 lower，但脑区节点/旧数据可能没 lower）
        entity_name = node_id.lower()

        # content 格式：跟 operate.py L1160 一致
        # desc 为空时用 entity_name 作为 fallback（保证有内容可 embed）
        # 跟 LightRAG 原生一致（entity_name 已 lower）
        if desc:
            content = f"{entity_name}\n{desc}"
        else:
            content = f"{entity_name}\n{entity_name}"

        # v9 第 2 轮审查修复（问题 1 / C1）：
        # dict key 必须用 compute_mdhash_id(entity_name, prefix="ent-")
        # （跟 LightRAG operate.py L1159 一致），不能用 entity_name。
        # NanoVectorDBStorage.upsert L110 把 dict key 直接作为 __id__，
        # 如果用 entity_name 会导致 __id__ = entity_name（非 hash ID），
        # 跟 LightRAG 原生不一致，删除/查询实体功能会失效。
        entity_vdb_id = compute_mdhash_id(entity_name, prefix="ent-")
        upsert_data[entity_vdb_id] = {
            "content": content,
            "entity_name": entity_name,
            "source_id": src or "",
            "file_path": file_path or "unknown_source",
        }

    if not upsert_data:
        # GraphML 有节点但全部 node_id 为空 → 不写派生文件（v9 第 2 轮审查修复 问题 5+6 / I3+I2）
        # 跟全新用户分支一致——不写空 vdb 文件，让 vdb_entities.json 不存在
        # （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致）
        logger.warning(
            f"[LightRAGRepair] GraphML 有 {len(nodes)} 节点但全部 node_id 为空，"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": len(nodes),
            "actual": 0,
            "lost": len(nodes),
            "source": "GraphML",
            "message": (
                f"GraphML {len(nodes)} 节点全部 node_id 为空，"
                "不写派生文件（跟 LightRAG 原生一致）"
            ),
        }

    # 6. 调 storage.upsert（内部自动做 embedding + 注入 __id__/__vector__/vector）
    try:
        await storage.upsert(upsert_data)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_entities storage.upsert 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": (
                f"storage.upsert 异常（embedding 可能失败）: "
                f"{type(e).__name__}: {e}"
            ),
            "unrecoverable": True,
        }

    # 7. 调 index_done_callback 写盘（NanoVectorDB.save 写 embedding_dim/data/matrix）
    try:
        success = await storage.index_done_callback()
        if not success:
            return {
                "status": "error",
                "expected": len(upsert_data),
                "actual": 0,
                "lost": len(upsert_data),
                "source": "NanoVectorDBStorage",
                "message": "index_done_callback 返回 False（可能被其他进程更新覆盖）",
                "unrecoverable": True,
            }
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_entities index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": f"index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 vdb_entities: {actual}/{len(nodes)} 条 "
        f"(source=GraphML nodes，skipped={skipped_count}，"
        f"embedding 由 RepairEmbeddingFunc 自动计算)"
    )
    return {
        "status": "ok",
        "expected": len(nodes),
        "actual": actual,
        "lost": len(nodes) - actual,
        "source": "GraphML",
        "message": f"从 GraphML nodes 走 NanoVectorDBStorage.upsert 重建 {actual} 条 vdb_entities",
    }


async def repair_vdb_relationships() -> dict[str, Any]:
    """v9：从 GraphML edge 读关系 + 走 NanoVectorDBStorage.upsert 重建 vdb_relationships。

    真相源：graph_chunk_entity_relation.graphml（edge src/tgt + d8 description + d9 keywords + d10 source_id + d11 file_path）
    派生：vdb_relationships.json（通过 NanoVectorDBStorage.upsert 写）

    走 storage 接口的好处：
    - NanoVectorDBStorage.upsert 内部自动调 embedding_func 做 embed
    - 自动注入 __id__ / __created_at__ / vector / __vector__
    - index_done_callback 触发 NanoVectorDB.save 写 matrix（L2 归一化后的单位向量）
    - meta_fields 过滤掉 keywords/description/weight（不落盘）

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 NanoVectorDBStorage(namespace=relationships, embedding_func=RepairEmbeddingFunc)
    3. await storage.initialize()
    4. 读 GraphML edges（用 v9 6 元组 _load_graphml_nodes_edges）
    5. 构造 upsert data：
       - src/tgt 必须 sorted（跟 LightRAG operate.py L1586-1587/L2515-2516 一致）
       - keywords 用 `", ".join(dict.fromkeys(...))` 去重保序（v9 第 2 轮审查修复 问题 7 / I5，跨运行稳定）
       - content 格式 f"{keywords}\\t{sorted_src}\\n{sorted_tgt}\\n{description}"（跟 operate.py L1601/L2527 一致）
       - dict key 用 make_relation_vdb_ids(sorted_src, sorted_tgt)[0]（即 compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")）
       - value 只传 meta_fields 内字段（content/src_id/tgt_id/source_id/file_path）
    6. 调 await storage.upsert(data) + await storage.index_done_callback()
    7. 全新用户（GraphML 无 edge）→ 不写派生文件

    关键：
    - src/tgt 必须 sorted（LightRAG operate.py L1586-1587/L2515-2516）
    - keywords 用 `", ".join(dict.fromkeys(...))` 去重保序（跨运行稳定；跟 LightRAG set 不完全一致但更稳定）
    - content 格式 f"{keywords}\\t{src_id}\\n{tgt_id}\\n{description}"（tab 分隔 keywords 和 src，换行分隔后续）
    - dict key 用 make_relation_vdb_ids(sorted_src, sorted_tgt)[0]（src/tgt 已 sorted）
    - 不要传 keywords/description/weight（meta_fields 不含，被过滤不落盘）
    - 不要手写 __id__/__created_at__/vector/__vector__（storage 自动注入）
    - upsert 后必须显式调 index_done_callback 才写盘

    v9 修复 v8 两个 bug：
    - keywords 去重继续用 dict.fromkeys 保序，但显式拆分 GraphML d9 + 过滤空字符串
    - v8 5 元组没读 d11 file_path → v9 扩展 6 元组读 d11

    异常处理：
    - GraphML 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 NanoVectorDBStorage
    #    global_config 必须含：
    #    - working_dir（NanoVectorDBStorage.__post_init__ 读）
    #    - vector_db_storage_cls_kwargs.cosine_better_than_threshold（强制要求）
    #    - embedding_batch_num（控制 embedding 分批大小）
    #    meta_fields 跟 LightRAG lightrag.py:722 一致
    global_config: dict[str, Any] = {
        "working_dir": str(storage_dir),
        "vector_db_storage_cls_kwargs": {
            "cosine_better_than_threshold": 0.2,
        },
        "embedding_batch_num": 32,
    }
    storage = NanoVectorDBStorage(
        namespace=NameSpace.VECTOR_STORE_RELATIONSHIPS,
        workspace="",
        global_config=global_config,
        embedding_func=RepairEmbeddingFunc(embedding_dim=768),
        meta_fields={"src_id", "tgt_id", "source_id", "content", "file_path"},
    )

    try:
        await storage.initialize()
        # 跟 Task 5/6 一致：清空 NanoVectorDB client storage 防止旧数据残留影响本次重建
        # （NanoVectorDBStorage.__post_init__ 已在 _client 里加载已有 vdb_relationships.json，
        #  repair 场景下我们要求从真相源完全重新派生——如果不 clear，旧 relationship 不会被删除，
        #  NanoVectorDB.upsert 只会按 __id__ 更新已有条目，已删除的 relationship 会残留）
        # NanoVectorDB 把数据存在 self._client._NanoVectorDB__storage dict 里
        # （keys: embedding_dim / data(list) / matrix(np.ndarray)）
        client = storage._client
        if client is not None:
            client_storage = getattr(client, "_NanoVectorDB__storage", None)
            if isinstance(client_storage, dict):
                client_storage["data"] = []
                client_storage["matrix"] = np.array(
                    [], dtype=np.float32
                ).reshape(0, storage.embedding_func.embedding_dim)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_relationships storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "NanoVectorDBStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML edges（真相源，v9 6 元组 _load_graphml_nodes_edges）
    #    返回 (node_ids, edges, error)
    #    edges: list of (src, tgt, edge_source_id, edge_description, edge_keywords, edge_file_path)
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 全新用户（GraphML 无 edge）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    #    LightRAG 全新用户首次启动 NanoVectorDBStorage.initialize 内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 vdb_relationships.json 不存在，不要强行写空 vdb 文件
    #    （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致）。
    #    _check_truth_sources_intact 已支持 absent/empty=合法。
    if not edges:
        logger.info(
            "[LightRAGRepair] GraphML 无 edge（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 edge，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 构造 upsert data（严格对照字段表）
    #    关键：
    #    - src/tgt 必须 sorted（跟 LightRAG operate.py L1586-1587/L2515-2516 一致）
    #    - keywords 用 ", ".join(dict.fromkeys(...)) 去重保序（v9 第 2 轮审查修复 问题 7 / I5，
    #      跨运行稳定；跟 LightRAG set 不完全一致但更稳定）
    #    - content 格式 f"{keywords}\\t{sorted_src}\\n{sorted_tgt}\\n{description}"
    #      （跟 operate.py L1601/L2527 一致，tab 分隔 keywords 和 src，换行分隔后续）
    #    - dict key 用 make_relation_vdb_ids(sorted_src, sorted_tgt)[0]
    #      （= compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")，src/tgt 已 sorted）
    #    - value 只传 meta_fields 内字段（不传 keywords/description/weight，被过滤不落盘）
    upsert_data: dict[str, dict[str, Any]] = {}
    skipped_count = 0
    for src, tgt, edge_src_id, edge_desc, edge_keywords, edge_file_path in edges:
        if not src or not tgt:
            skipped_count += 1
            continue

        # 5.1 sorted src/tgt（跟 LightRAG operate.py L1586-1587/L2515-2516 一致）
        sorted_src, sorted_tgt = sorted((src, tgt))

        # 5.2 keywords 去重（v9 第 2 轮审查修复 问题 7 / I5）
        #     GraphML d9 字段可能用 <SEP> 分隔（多关键词合并）或逗号分隔。
        #     先按 <SEP> 拆分（如有），再用 dict.fromkeys 去重保序，最后 ", " join。
        #
        #     注意：LightRAG operate.py L1482-1486 原生用 `set(keywords)` 去重（无序），
        #     但 set 去重后顺序不稳定——跨运行结果可能不同（同一 GraphML d9 输入两次
        #     产生不同 combined_keywords 字符串），导致字节级 diff 不稳定。
        #
        #     v9 改用 `dict.fromkeys(kw_list)` 去重保序（Python 3.7+ dict 保持插入顺序），
        #     跨运行稳定。如果 GraphML d9 已是 LightRAG 写入时 `", ".join(set(...))` 后的字符串，
        #     dict.fromkeys 拆分 + 去重 + join 后跟原字符串一致（已去重，不会改变）。
        #
        #     取舍：跟 LightRAG operate.py L1483 `set(keywords)` 不完全一致
        #     （set 无序 vs dict.fromkeys 保序），但跨运行稳定，且如果 GraphML d9
        #     已是去重后的字符串，结果跟原字符串一致——v9 选 dict.fromkeys 优先保证稳定性。
        if edge_keywords:
            if GRAPH_FIELD_SEP in edge_keywords:
                kw_list = [k.strip() for k in edge_keywords.split(GRAPH_FIELD_SEP) if k.strip()]
            else:
                # 逗号分隔（LightRAG 写入时用 ", " join）
                kw_list = [k.strip() for k in edge_keywords.split(",") if k.strip()]
            # 用 dict.fromkeys 去重保序（跨运行稳定，跟 set 不同）
            combined_keywords = ", ".join(dict.fromkeys(kw_list)) if kw_list else ""
        else:
            combined_keywords = ""

        # 5.3 content 格式（跟 LightRAG operate.py L1601/L2527 一致）
        #     f"{combined_keywords}\t{src}\n{tgt}\n{final_description}"
        #     tab 分隔 keywords 和 src，换行分隔 src/tgt/description
        #     keywords/desc 为空用空字符串（保持 LightRAG 格式一致，不破坏向量比对）
        content = f"{combined_keywords}\t{sorted_src}\n{sorted_tgt}\n{edge_desc}"

        # 5.4 dict key 用 make_relation_vdb_ids[0]（= compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")）
        #     make_relation_vdb_ids 内部已 sorted，传入 sorted_src/sorted_tgt 跟内部行为一致
        rel_vdb_id = make_relation_vdb_ids(sorted_src, sorted_tgt)[0]

        # 5.5 value 只传 meta_fields 内字段
        upsert_data[rel_vdb_id] = {
            "content": content,
            "src_id": sorted_src,
            "tgt_id": sorted_tgt,
            "source_id": edge_src_id or "",
            "file_path": edge_file_path or "unknown_source",
        }

    if not upsert_data:
        # edges 全是空 src/tgt → 不写派生文件（v9 第 2 轮审查修复 问题 5+6 / I3+I2）
        # 跟全新用户分支一致——不写空 vdb 文件，让 vdb_relationships.json 不存在
        # （write_json 写空 vdb 跟 NanoVectorDB.save 字节级可能不一致）
        logger.warning(
            f"[LightRAGRepair] GraphML 有 {len(edges)} edge 但全部 src/tgt 为空，"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": len(edges),
            "actual": 0,
            "lost": len(edges),
            "source": "GraphML",
            "message": (
                f"GraphML {len(edges)} edge 全部 src/tgt 为空，"
                "不写派生文件（跟 LightRAG 原生一致）"
            ),
        }

    # 6. 调 storage.upsert（内部自动做 embedding + 注入 __id__/__vector__/vector）
    try:
        await storage.upsert(upsert_data)
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_relationships storage.upsert 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": (
                f"storage.upsert 异常（embedding 可能失败）: "
                f"{type(e).__name__}: {e}"
            ),
            "unrecoverable": True,
        }

    # 7. 调 index_done_callback 写盘（NanoVectorDB.save 写 embedding_dim/data/matrix）
    try:
        success = await storage.index_done_callback()
        if not success:
            return {
                "status": "error",
                "expected": len(upsert_data),
                "actual": 0,
                "lost": len(upsert_data),
                "source": "NanoVectorDBStorage",
                "message": "index_done_callback 返回 False（可能被其他进程更新覆盖）",
                "unrecoverable": True,
            }
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] vdb_relationships index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "NanoVectorDBStorage",
            "message": f"index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 vdb_relationships: {actual}/{len(edges)} 条 "
        f"(source=GraphML edges，skipped={skipped_count}，"
        f"embedding 由 RepairEmbeddingFunc 自动计算)"
    )
    return {
        "status": "ok",
        "expected": len(edges),
        "actual": actual,
        "lost": len(edges) - actual,
        "source": "GraphML",
        "message": f"从 GraphML edges 走 NanoVectorDBStorage.upsert 重建 {actual} 条 vdb_relationships",
    }


async def repair_entity_chunks() -> dict[str, Any]:
    """v9：从 GraphML node source_id 提取重建 entity_chunks，走 JsonKVStorage.upsert。

    真相源：graph_chunk_entity_relation.graphml（node id + d3 source_id 字段，<SEP> 分隔的 chunk_id 列表）
    派生：kv_store_entity_chunks.json（通过 JsonKVStorage.upsert 写）

    走 storage 接口的好处：
    - JsonKVStorage.upsert 自动注入 _id / create_time / update_time
    - index_done_callback 统一写盘 + sanitization

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonKVStorage(namespace=entity_chunks, embedding_func=None)
    3. await storage.initialize() + storage._data.clear()（清空共享内存旧数据）
    4. 读 GraphML nodes（用 v8 _load_graphml_nodes，返回 4 元组）
    5. 构造 upsert data：
       {entity_name: {"chunk_ids": list[str], "count": int}}
       - chunk_ids 用 src.split(GRAPH_FIELD_SEP) 拆分（保留顺序去重）
       - count = len(chunk_ids)
    6. 调 await storage.upsert(data) + await storage.index_done_callback()
    7. 全新用户（GraphML 无 node）→ 不写派生文件（v9 第 2 轮审查修复 问题 5 / I3）

    关键：
    - value 格式 {"chunk_ids": list[str], "count": int}（list 不是 GRAPH_FIELD_SEP 字符串）
    - chunk_ids 用 split 拆分（保留插入顺序，不 sorted）
    - _id / create_time / update_time 由 JsonKVStorage 自动注入，不要手写
    - entity_chunks namespace 不会被补字段（只有 text_chunks 补 llm_cache_list）

    异常处理：
    - GraphML 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonKVStorage
    #    global_config 必须含 working_dir（JsonKVStorage.__post_init__ L30 读）
    #    embedding_func 传 None（entity_chunks 不用 embedding，跟 LightRAG lightrag.py:670 一致）
    #    LightRAG 原生用 # type: ignore 绕过 EmbeddingFunc 类型校验（base.py:364 不允许 None，
    #    但 entity_chunks/relation_chunks/full_entities 等 KV 存储从不调用 embedding_func，运行时 None 安全）
    global_config: dict[str, Any] = {"working_dir": str(storage_dir)}
    storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_ENTITY_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
        # 清空共享内存旧数据：JsonKVStorage.initialize 会从磁盘 load 旧 entity_chunks.json
        # 到共享内存（_data），repair 场景下要求从真相源完全重新派生——如果不 clear，
        # 已删除的 entity 残留不会被覆盖（upsert 按 key 更新，旧 key 残留）。
        # 跟 Task 7 vdb_relationships 清空 client_storage 同理。
        if storage._data is not None:
            storage._data.clear()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] entity_chunks storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonKVStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML nodes（真相源，v8 _load_graphml_nodes 保留）
    #    返回 {node_id: (entity_type, description, source_id, file_path)}
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 全新用户（GraphML 无 node）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonKVStorage.initialize 只设内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 entity_chunks.json 不存在，不要强行写空 {} 文件
    #    （跟原生不一致，字节级 diff 会失败）。
    if not nodes:
        logger.info(
            "[LightRAGRepair] GraphML 无 node（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 node，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 构造 upsert data（严格对照字段表）
    #    key = entity_name（GraphML node id，已 lower）
    #    value = {"chunk_ids": list[str], "count": int}
    #    chunk_ids 用 src.split(GRAPH_FIELD_SEP) 拆分（保留顺序去重，不 sorted）
    #    source_id 为空 → 空 chunk_ids（合法，跟 LightRAG operate.py L1555 一致）
    upsert_data: dict[str, dict[str, Any]] = {}
    for node_id, (_etype, _desc, src, _file_path) in nodes.items():
        if not src:
            # source_id 为空 → 空 chunk_ids（合法）
            upsert_data[node_id] = {"chunk_ids": [], "count": 0}
            continue
        # source_id 是 <SEP> 分隔的 chunk_id 列表
        # 用 split 拆分（保留顺序，跟 merge_source_ids 单参数等价）
        chunk_ids = [c for c in src.split(GRAPH_FIELD_SEP) if c]
        upsert_data[node_id] = {"chunk_ids": chunk_ids, "count": len(chunk_ids)}

    # 6. 调 storage.upsert + index_done_callback
    try:
        await storage.upsert(upsert_data)
        await storage.index_done_callback()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] entity_chunks storage.upsert/index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(nodes),
            "actual": 0,
            "lost": len(nodes),
            "source": "JsonKVStorage",
            "message": f"storage.upsert 或 index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 entity_chunks: {actual}/{len(nodes)} 条 "
        f"(source=GraphML node source_id)"
    )
    return {
        "status": "ok",
        "expected": len(nodes),
        "actual": actual,
        "lost": len(nodes) - actual,
        "source": "GraphML node source_id",
        "message": f"从 GraphML node source_id 走 JsonKVStorage.upsert 重建 {actual} 条 entity_chunks",
    }


async def repair_relation_chunks() -> dict[str, Any]:
    """v9：从 GraphML edge source_id 提取重建 relation_chunks，走 JsonKVStorage.upsert。

    真相源：graph_chunk_entity_relation.graphml（edge src/tgt + d10 source_id 字段，<SEP> 分隔的 chunk_id 列表）
    派生：kv_store_relation_chunks.json（通过 JsonKVStorage.upsert 写）

    走 storage 接口的好处：
    - JsonKVStorage.upsert 自动注入 _id / create_time / update_time
    - index_done_callback 统一写盘 + sanitization

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonKVStorage(namespace=relation_chunks, embedding_func=None)
    3. await storage.initialize() + storage._data.clear()（清空共享内存旧数据）
    4. 读 GraphML edges（用 v9 6 元组 _load_graphml_nodes_edges）
    5. 构造 upsert data：
       {relation_key: {"chunk_ids": list[str], "count": int}}
       - relation_key = make_relation_chunk_key(src, tgt) = "<SEP>".join(sorted((src, tgt)))
       - chunk_ids 用 edge_src_id.split(GRAPH_FIELD_SEP) 拆分（保留顺序去重，不 sorted）
       - count = len(chunk_ids)
       - 同一个 key 可能被多个 edge 重复（不应该，但容错），用 merge_source_ids 合并 chunk_ids
    6. 调 await storage.upsert(data) + await storage.index_done_callback()
    7. 全新用户（GraphML 无 edge）→ 不写派生文件（v9 第 2 轮审查修复 问题 5 / I3）

    关键：
    - value 格式 {"chunk_ids": list[str], "count": int}（list 不是 GRAPH_FIELD_SEP 字符串）
    - chunk_ids 用 merge_source_ids 合并（保留插入顺序去重，不 sorted）
    - relation_key 用 make_relation_chunk_key（单个字符串，不是 tuple）
    - _id / create_time / update_time 由 JsonKVStorage 自动注入，不要手写

    异常处理：
    - GraphML 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    from lightrag.utils import merge_source_ids

    storage_dir = _storage_dir()

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonKVStorage
    #    embedding_func 传 None（relation_chunks 不用 embedding，跟 entity_chunks 同理）
    global_config: dict[str, Any] = {"working_dir": str(storage_dir)}
    storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_RELATION_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
        # 清空共享内存旧数据：跟 Task 8 repair_entity_chunks 同理，
        # 防止已删除的 relation_chunk 残留（upsert 按 key 更新，旧 key 残留）。
        if storage._data is not None:
            storage._data.clear()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] relation_chunks storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonKVStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML edges（真相源，v9 6 元组 _load_graphml_nodes_edges）
    #    返回 (node_ids, edges, error)
    #    edges: list of (src, tgt, edge_source_id, edge_description, edge_keywords, edge_file_path)
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 全新用户（GraphML 无 edge）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonKVStorage.initialize 只设内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 relation_chunks.json 不存在，不要强行写空 {} 文件
    #    （跟原生不一致，字节级 diff 会失败）。
    if not edges:
        logger.info(
            "[LightRAGRepair] GraphML 无 edge（全新用户），不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": "GraphML 无 edge，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 5. 构造 upsert data（严格对照字段表）
    #    key = make_relation_chunk_key(src, tgt) = "<SEP>".join(sorted((src, tgt)))
    #    value = {"chunk_ids": list[str], "count": int}
    #    chunk_ids 用 edge_src_id.split(GRAPH_FIELD_SEP) 拆分（保留顺序去重，不 sorted）
    #    同一个 key 可能被多个 edge 重复（不应该，但容错），用 merge_source_ids 合并 chunk_ids
    upsert_data: dict[str, dict[str, Any]] = {}
    for src, tgt, edge_src_id, _edge_desc, _edge_keywords, _edge_file_path in edges:
        if not src or not tgt:
            continue
        # sorted 后用 make_relation_chunk_key 生成 key
        key = make_relation_chunk_key(src, tgt)
        chunk_ids: list[str] = []
        if edge_src_id:
            chunk_ids = [c for c in edge_src_id.split(GRAPH_FIELD_SEP) if c]
        # 同一个 key 可能被多个 edge 重复（不应该，但容错），合并 chunk_ids
        # merge_source_ids 保留插入顺序去重（跟 LightRAG utils.py L2828 一致）
        if key in upsert_data:
            existing = upsert_data[key]["chunk_ids"]
            merged = merge_source_ids(existing, chunk_ids)
            upsert_data[key]["chunk_ids"] = merged
            upsert_data[key]["count"] = len(merged)
        else:
            upsert_data[key] = {"chunk_ids": chunk_ids, "count": len(chunk_ids)}

    # 6. 调 storage.upsert + index_done_callback
    try:
        await storage.upsert(upsert_data)
        await storage.index_done_callback()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] relation_chunks storage.upsert/index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(edges),
            "actual": 0,
            "lost": len(edges),
            "source": "JsonKVStorage",
            "message": f"storage.upsert 或 index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 relation_chunks: {actual}/{len(edges)} 条 "
        f"(source=GraphML edge source_id)"
    )
    return {
        "status": "ok",
        "expected": len(edges),
        "actual": actual,
        "lost": len(edges) - actual,
        "source": "GraphML edge source_id",
        "message": f"从 GraphML edge source_id 走 JsonKVStorage.upsert 重建 {actual} 条 relation_chunks",
    }


async def repair_full_entities() -> dict[str, Any]:
    """v9：从 GraphML node source_id → chunk→doc 反查重建 full_entities，走 JsonKVStorage.upsert。

    真相源：graph_chunk_entity_relation.graphml（node id + d3 source_id 字段）
            + kv_store_doc_status.json（chunks_list 提供 chunk→doc 映射）
    派生：kv_store_full_entities.json（通过 JsonKVStorage.upsert 写）

    走 storage 接口的好处：
    - JsonKVStorage.upsert 自动注入 _id / create_time / update_time
    - index_done_callback 统一写盘 + sanitization

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonKVStorage(namespace=full_entities, embedding_func=None)
    3. await storage.initialize()
    4. 读 GraphML nodes（用 v8 _load_graphml_nodes，返回 4 元组）
    5. 读 doc_status（chunk→doc 映射，从 chunks_list 反查）
    6. 从 GraphML source_id 提取 entity→docs 映射
       - 每个 node 的 source_id 拆分为 chunk_id 列表
       - 反查 chunk_to_doc 得到 doc_id 集合
    7. 反转：doc→entities（用 set 去重，跟 LightRAG operate.py L2904 一致）
    8. 构造 upsert data：{doc_id: {"entity_names": list[str], "count": int}}
       - entity_names 来自 set（不 sorted，跟 LightRAG 一致）
       - count = len(entity_names)
    9. 调 await storage.upsert(data) + await storage.index_done_callback()
    10. 全新用户（GraphML 无 node 或 doc_status 为空）→ 不写派生文件

    关键：
    - value 格式 {"entity_names": list[str], "count": int}（不是裸 list！）
    - entity_names 不 sorted（由 set 转来无序，跟 LightRAG operate.py L2904 一致）
    - _id / create_time / update_time 由 JsonKVStorage 自动注入，不要手写

    异常处理：
    - GraphML 损坏 → unrecoverable
    - doc_status 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()
    doc_status_path = storage_dir / "kv_store_doc_status.json"

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonKVStorage
    #    embedding_func 传 None（full_entities 不用 embedding，跟 entity_chunks 同理）
    #    但 full_entities KV 存储从不调用 embedding_func，运行时 None 安全
    global_config = {"working_dir": str(storage_dir)}
    storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_FULL_ENTITIES,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
        # 清空共享内存旧数据：JsonKVStorage.initialize 会从磁盘 load 旧 full_entities.json
        # 到共享内存（_data），repair 场景下要求从真相源完全重新派生——如果不 clear，
        # 已删除的 doc 残留不会被覆盖（upsert 按 key 更新，旧 key 残留）。
        # 跟 Task 8 entity_chunks 清空 _data 同理。
        if storage._data is not None:
            storage._data.clear()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] full_entities storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonKVStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML nodes（真相源）
    nodes, graphml_err = _load_graphml_nodes()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 读 doc_status（真相源，chunk→doc 映射）
    doc_status = _load_json_dict(doc_status_path)
    if doc_status is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "doc_status",
            "message": "doc_status 损坏（JSON 解析失败），无法构建 chunk→doc 映射",
            "unrecoverable": True,
        }

    # 5. 全新用户（GraphML 无 node 或 doc_status 为空）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonKVStorage.initialize 只设内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 full_entities.json 不存在，不要强行写空 {} 文件。
    if not nodes or not doc_status:
        logger.info(
            "[LightRAGRepair] GraphML 无 node 或 doc_status 为空（全新用户），"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + doc_status",
            "message": "GraphML 无 node 或 doc_status 为空，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 6. 构建 chunk→doc 映射（从 doc_status.chunks_list 反查）
    chunk_to_doc: dict[str, str] = {}
    for doc_id, ds_value in doc_status.items():
        if not isinstance(ds_value, dict):
            continue
        for cid in ds_value.get("chunks_list", []) or []:
            if isinstance(cid, str):
                chunk_to_doc[cid] = doc_id

    # 7. 从 GraphML source_id 提取 entity→docs 映射
    #    每个 node 的 source_id（d3）拆分为 chunk_id 列表
    #    反查 chunk_to_doc 得到 doc_id 集合
    entity_to_docs: dict[str, set[str]] = {}
    for node_id, (_etype, _desc, src, _file_path) in nodes.items():
        if not src:
            continue
        chunk_ids = [c for c in src.split(GRAPH_FIELD_SEP) if c]
        for cid in chunk_ids:
            doc_id = chunk_to_doc.get(cid)
            if doc_id:
                entity_to_docs.setdefault(node_id, set()).add(doc_id)

    # 8. 反转：doc→entities（用 set 去重，跟 LightRAG operate.py L2904 一致）
    doc_to_entities: dict[str, set[str]] = {}
    for entity_name, doc_set in entity_to_docs.items():
        for doc_id in doc_set:
            doc_to_entities.setdefault(doc_id, set()).add(entity_name)

    # 9. 构造 upsert data（严格对照字段表）
    #    value 格式 {"entity_names": list[str], "count": int}
    #    entity_names 来自 set（不 sorted，跟 LightRAG operate.py L2904 一致）
    #    count = len(entity_names)
    upsert_data: dict[str, dict[str, Any]] = {}
    for doc_id, entity_set in doc_to_entities.items():
        # entity_names 不 sorted（来自 set，跟 LightRAG 一致）
        entity_names = list(entity_set)
        upsert_data[doc_id] = {
            "entity_names": entity_names,
            "count": len(entity_names),
        }

    # 10. 调 storage.upsert + index_done_callback
    if not upsert_data:
        # 没有匹配的 chunk→doc 映射（GraphML source_id 跟 doc_status chunks_list 不交叉）
        # 不写派生文件（v9 第 2 轮审查修复 问题 5 / I3，跟全新用户分支一致）
        logger.warning(
            f"[LightRAGRepair] GraphML 有 {len(nodes)} node 但无 chunk→doc 映射匹配，"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": len(doc_status),
            "actual": 0,
            "lost": len(doc_status),
            "source": "GraphML + doc_status",
            "message": f"GraphML {len(nodes)} node 但无 chunk→doc 映射匹配，不写派生文件（跟 LightRAG 原生一致）",
        }

    try:
        await storage.upsert(upsert_data)
        await storage.index_done_callback()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] full_entities storage.upsert/index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "JsonKVStorage",
            "message": f"storage.upsert 或 index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 full_entities: {actual} 条 "
        "(source=GraphML source_id + doc_status chunks_list)"
    )
    return {
        "status": "ok",
        "expected": len(upsert_data),
        "actual": actual,
        "lost": 0,
        "source": "GraphML + doc_status",
        "message": f"从 GraphML source_id → chunk→doc 映射走 JsonKVStorage.upsert 重建 {actual} 条 full_entities",
    }


async def repair_full_relations() -> dict[str, Any]:
    """v9：从 GraphML edge source_id → chunk→doc 反查重建 full_relations，走 JsonKVStorage.upsert。

    真相源：graph_chunk_entity_relation.graphml（edge src/tgt + d10 source_id 字段）
            + kv_store_doc_status.json（chunks_list 提供 chunk→doc 映射）
    派生：kv_store_full_relations.json（通过 JsonKVStorage.upsert 写）

    走 storage 接口的好处：
    - JsonKVStorage.upsert 自动注入 _id / create_time / update_time
    - index_done_callback 统一写盘 + sanitization

    算法：
    1. initialize_share_data(workers=1) + set_default_workspace("")
    2. 实例化 JsonKVStorage(namespace=full_relations, embedding_func=None)
    3. await storage.initialize()
    4. 读 GraphML edges（用 v9 6 元组 _load_graphml_nodes_edges）
    5. 读 doc_status（chunk→doc 映射，从 chunks_list 反查）
    6. 从 GraphML edge source_id 提取 relation→docs 映射
       - 每个 edge 的 source_id（d10）拆分为 chunk_id 列表
       - 用 (src, tgt) 二元组作为 key（保留 src/tgt 信息）
       - 反查 chunk_to_doc 得到 doc_id 集合
    7. 反转：doc→relation_pairs
       - 每个 pair 必须 sorted([src, tgt])（跟 LightRAG operate.py L2889 一致）
       - 用 set 去重（同一 doc 内同一 sorted pair 只出现一次）
    8. 构造 upsert data：{doc_id: {"relation_pairs": list[list[str]], "count": int}}
       - relation_pairs 是 list of list（每个 pair 是 sorted 的 2 元素 list [src, tgt]）
       - count = len(relation_pairs)
    9. 调 await storage.upsert(data) + await storage.index_done_callback()
    10. 全新用户（GraphML 无 edge 或 doc_status 为空）→ 不写派生文件

    关键：
    - value 格式 {"relation_pairs": list[list[str]], "count": int}
    - 每个 pair 必须 sorted([src, tgt])（跟 LightRAG operate.py L2889 一致）
    - 每个 pair 是 list（不是 tuple，JSON 序列化会变 list，跟 LightRAG operate.py L2914-2915 一致）
    - _id / create_time / update_time 由 JsonKVStorage 自动注入，不要手写

    异常处理：
    - GraphML 损坏 → unrecoverable
    - doc_status 损坏 → unrecoverable
    - storage.initialize / upsert / index_done_callback 异常 → error（不写文件）
    """
    storage_dir = _storage_dir()
    doc_status_path = storage_dir / "kv_store_doc_status.json"

    # 1. 初始化 shared_storage（单进程模式，D4）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.shared_storage import (
        initialize_share_data,
        set_default_workspace,
    )
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    # 2. 实例化 JsonKVStorage
    #    embedding_func 传 None（full_relations 不用 embedding，跟 entity_chunks 同理）
    global_config = {"working_dir": str(storage_dir)}
    storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_FULL_RELATIONS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )

    try:
        await storage.initialize()
        # 清空共享内存旧数据：JsonKVStorage.initialize 会从磁盘 load 旧 full_relations.json
        # 到共享内存（_data），repair 场景下要求从真相源完全重新派生——如果不 clear，
        # 已删除的 doc 残留不会被覆盖（upsert 按 key 更新，旧 key 残留）。
        # 跟 Task 8 relation_chunks 清空 _data 同理。
        if storage._data is not None:
            storage._data.clear()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] full_relations storage.initialize 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "JsonKVStorage",
            "message": f"storage.initialize 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    # 3. 读 GraphML edges（真相源，v9 6 元组 _load_graphml_nodes_edges）
    #    返回 (node_ids, edges, error)
    #    edges: list of (src, tgt, edge_source_id, edge_description, edge_keywords, edge_file_path)
    _, edges, graphml_err = _load_graphml_nodes_edges()
    if graphml_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {graphml_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 4. 读 doc_status（真相源，chunk→doc 映射）
    doc_status = _load_json_dict(doc_status_path)
    if doc_status is None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "doc_status",
            "message": "doc_status 损坏（JSON 解析失败），无法构建 chunk→doc 映射",
            "unrecoverable": True,
        }

    # 5. 全新用户（GraphML 无 edge 或 doc_status 为空）→ 不写派生文件
    #    v9 第 2 轮审查修复（问题 5 / I3）：
    #    LightRAG 全新用户首次启动 JsonKVStorage.initialize 只设内存空 dict，
    #    不主动写空文件到磁盘。v9 跟 LightRAG 原生行为一致——
    #    全新用户场景下 full_relations.json 不存在，不要强行写空 {} 文件。
    if not edges or not doc_status:
        logger.info(
            "[LightRAGRepair] GraphML 无 edge 或 doc_status 为空（全新用户），"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + doc_status",
            "message": "GraphML 无 edge 或 doc_status 为空，全新用户不写派生文件（跟 LightRAG 原生首次启动一致）",
        }

    # 6. 构建 chunk→doc 映射（从 doc_status.chunks_list 反查）
    chunk_to_doc: dict[str, str] = {}
    for doc_id, ds_value in doc_status.items():
        if not isinstance(ds_value, dict):
            continue
        for cid in ds_value.get("chunks_list", []) or []:
            if isinstance(cid, str):
                chunk_to_doc[cid] = doc_id

    # 7. 从 GraphML edge source_id 提取 relation→docs 映射
    #    key 用 (src, tgt) 二元组（保留 src/tgt 信息，LightRAG 读取侧用 pair[0]/pair[1]）
    #    每个 edge 的 source_id（d10）拆分为 chunk_id 列表
    #    反查 chunk_to_doc 得到 doc_id 集合
    relation_pair_to_docs: dict[tuple[str, str], set[str]] = {}
    for src, tgt, edge_src_id, _edge_desc, _edge_keywords, _edge_file_path in edges:
        if not src or not tgt:
            continue
        if not edge_src_id:
            continue
        chunk_ids = [c for c in edge_src_id.split(GRAPH_FIELD_SEP) if c]
        for cid in chunk_ids:
            doc_id = chunk_to_doc.get(cid)
            if doc_id:
                relation_pair_to_docs.setdefault((src, tgt), set()).add(doc_id)

    # 8. 反转：doc→relation_pairs
    #    每个 pair 必须 sorted([src, tgt])（跟 LightRAG operate.py L2889 一致）
    #    用 set 去重（同一 doc 内同一 sorted pair 只出现一次）
    doc_to_relation_pairs: dict[str, set[tuple[str, str]]] = {}
    for (src, tgt), doc_set in relation_pair_to_docs.items():
        # sorted 后用 tuple 作为 set 元素（可哈希）
        # 显式 cast 让 pyright 推断为 tuple[str, str] 而非 tuple[str, ...]
        sorted_pair: tuple[str, str] = (src, tgt) if src <= tgt else (tgt, src)
        for doc_id in doc_set:
            doc_to_relation_pairs.setdefault(doc_id, set()).add(sorted_pair)

    # 9. 构造 upsert data（严格对照字段表）
    #    value 格式 {"relation_pairs": list[list[str]], "count": int}
    #    relation_pairs 是 list of list（每个 pair 是 sorted 的 2 元素 list [src, tgt]）
    #    跟 LightRAG operate.py L2914-2915 一致：[list(pair) for pair in final_relation_pairs]
    upsert_data: dict[str, dict[str, Any]] = {}
    for doc_id, pair_set in doc_to_relation_pairs.items():
        # 每个 pair 是 sorted 的 tuple，转 list（跟 LightRAG operate.py L2914 一致）
        relation_pairs = [list(pair) for pair in pair_set]
        upsert_data[doc_id] = {
            "relation_pairs": relation_pairs,
            "count": len(relation_pairs),
        }

    # 10. 调 storage.upsert + index_done_callback
    if not upsert_data:
        # 不写派生文件（v9 第 2 轮审查修复 问题 5 / I3，跟全新用户分支一致）
        logger.warning(
            f"[LightRAGRepair] GraphML 有 {len(edges)} edge 但无 chunk→doc 映射匹配，"
            "不写派生文件（跟 LightRAG 原生一致）"
        )
        return {
            "status": "ok",
            "expected": len(doc_status),
            "actual": 0,
            "lost": len(doc_status),
            "source": "GraphML + doc_status",
            "message": f"GraphML {len(edges)} edge 但无 chunk→doc 映射匹配，不写派生文件（跟 LightRAG 原生一致）",
        }

    try:
        await storage.upsert(upsert_data)
        await storage.index_done_callback()
    except Exception as e:
        logger.error(
            f"[LightRAGRepair] full_relations storage.upsert/index_done_callback 失败: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "expected": len(upsert_data),
            "actual": 0,
            "lost": len(upsert_data),
            "source": "JsonKVStorage",
            "message": f"storage.upsert 或 index_done_callback 异常: {type(e).__name__}: {e}",
            "unrecoverable": True,
        }

    actual = len(upsert_data)
    logger.info(
        f"[LightRAGRepair] 重建 full_relations: {actual} 条 "
        "(source=GraphML edge source_id + doc_status chunks_list)"
    )
    return {
        "status": "ok",
        "expected": len(upsert_data),
        "actual": actual,
        "lost": 0,
        "source": "GraphML + doc_status",
        "message": f"从 GraphML edge source_id → chunk→doc 映射走 JsonKVStorage.upsert 重建 {actual} 条 full_relations",
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

# v9 重建依赖链顺序（9 个派生文件的 async repair 函数）
# 跟 v8 _REBUILD_ORDER 内容相同，但所有 repair_xxx 已改为 async def（Task 3-9），
# 调用方必须用 await（在 repair_all 内部用 asyncio.run 桥接）。
# 依赖链：
#   text_chunks（独立，从 GraphML + cache + full_docs 重建）
#   → doc_status（依赖 text_chunks：chunks_list 反查）
#   → vdb_chunks（依赖 text_chunks：content + full_doc_id + file_path）
#   → vdb_entities（独立，从 GraphML nodes 重建）
#   → vdb_relationships（独立，从 GraphML edges 重建）
#   → entity_chunks（独立，从 GraphML node source_id 重建）
#   → relation_chunks（独立，从 GraphML edge source_id 重建）
#   → full_entities（依赖 doc_status：chunk→doc 映射）
#   → full_relations（依赖 doc_status：chunk→doc 映射）
# 不含 repair_graphml / repair_brainregion_zombies / repair_graphml_orphan_edges /
# repair_llm_response_cache——v8-Task 1 已删除这些违反铁律 3 的函数（写 3 真相源）。
# 用直接函数引用（不是字符串），拼写错误会在模块加载时 NameError，避免静默跳过。
_REBUILD_ORDER_ASYNC: list[tuple[str, Any]] = [
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
    """v9：3 真相源不可动 + 删 9 派生 + 按依赖链调 9 个 async repair_xxx。

    流程：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager（v8 保留）
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable（v8 保留）
    3. 删除 9 个派生文件（铁律 1：不备份，直接删；v8 保留）
    4. 按依赖链调 9 个 async repair_xxx（v9 改动：用 asyncio.run 桥接）
       - 任一 repair_xxx 报 unrecoverable → 立即 break
       - 异常时记录 unrecoverable + break
    5. 失败时无法回滚（派生文件已删光，真相源从未被修改）

    v9 跟 v8 的区别：
    - v8：9 个 repair_xxx 是同步函数，直接 for 循环调用 `fn()`
    - v9：9 个 repair_xxx 是 async 函数（Task 3-9 重写走 storage.upsert），
          repair_all 保持同步签名（向后兼容 run_repair_on_user_request 同步调用），
          内部用 asyncio.run(_repair_all_async()) 桥接

    3 真相源完全不可动（铁律 2）：
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
            "_deleted": [...],
        }

    注意：repair_all 是同步函数，不能声明 async（调用方 lightrag_manager.py
    是同步调用 repair_all()，async 会导致返回 coroutine 对象）。
    """
    # 同步签名 + 内部 async 桥接
    # 用 asyncio.run 创建临时 event loop 跑 _repair_all_async
    # 已存在 event loop 时（如 pytest-asyncio 测试）抛 RuntimeError 让调用方改用 _repair_all_async
    try:
        return asyncio.run(_repair_all_async())
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            # 已存在 running loop（如测试 @pytest.mark.asyncio 内部调用）
            # 但 repair_all 是同步函数不能 await，无法在此场景安全桥接
            # 抛错让调用方知道要改用 _repair_all_async
            raise RuntimeError(
                "repair_all() 不能在 running event loop 内调用；"
                "请用 await _repair_all_async() 替代"
            ) from e
        raise


async def _repair_all_async() -> dict[str, Any]:
    """v9 repair_all 的 async 实现（内部函数，由 repair_all 桥接调用）。

    所有 9 个 repair_xxx 都是 async 函数（Task 3-9 重写），
    在同一 event loop 内顺序 await，共享 shared_storage 全局状态。

    算法：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
    3. 删除 9 个派生文件（铁律 1：不备份，直接删）
    4. 按依赖链 await 9 个 async repair_xxx
       - 用 getattr 间接查找（让测试 monkeypatch 能注入失败版本）
       - 任一报 unrecoverable → 立即 break
       - 异常时记录 unrecoverable + break

    测试可以直接 await _repair_all_async() 跑（不用 asyncio.run 桥接）。
    """
    storage_dir = _storage_dir()
    result: dict[str, Any] = {}

    # 0. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager（v8 保留）
    #    兼容测试 monkeypatch lightrag_repair._STORAGE_DIR
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

    # 1. 检测 3 真相源完好性（v8 保留）
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
        result["_deleted"] = []  # 真相源损坏时不删派生文件，让用户看到现场
        return result

    # 2. 删除 9 个派生文件（铁律 1：不备份，直接删；v8 保留）
    #    v8：删除"备份"步骤——铁律 1 要求"其他文件全删除"。
    #    失败时不回滚——派生文件已删光，真相源从未被修改，用户重新跑 repair_all 即可。
    deleted: list[str] = []
    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(fname)
                logger.info(f"[LightRAGRepair] 删除派生文件: {fname}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LightRAGRepair] 删除 {fname} 失败: {e}")
    result["_deleted"] = deleted

    # 3. 按依赖链 await 9 个 async repair_xxx（v9 改动：async 调用）
    #    用 getattr 间接查找函数（让测试 monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_fn) 能生效）
    #    _REBUILD_ORDER_ASYNC 里的 fn 都是 async def 模块级函数，有 __name__
    import niu_api.internal.lightrag_repair as _self_mod
    for name, fn in _REBUILD_ORDER_ASYNC:
        # 重新从模块属性读取，让 monkeypatch 能注入失败版本
        # （如果直接用 _REBUILD_ORDER_ASYNC 里的 fn 对象，monkeypatch 替换模块属性不影响已绑定的 fn）
        fn = getattr(_self_mod, fn.__name__)
        # v9 第 3 轮审查修复 I2：防御性校验 fn 是 async 函数
        # 如果 monkeypatch 注入了同步 mock，await fn() 会抛 TypeError 而非 unrecoverable
        # 注意：测试 mock 必须是 async def 顶层命名函数（不可用 lambda），否则 __name__ 会 AttributeError
        if not asyncio.iscoroutinefunction(fn):
            raise RuntimeError(
                f"{name} 不是 async 函数（v9 要求所有 repair_xxx 是 async）"
            )
        try:
            step_result = await fn()  # v9 改动：await（v8 是 fn()）
            result[name] = step_result
            if isinstance(step_result, dict) and (
                step_result.get("unrecoverable") or step_result.get("status") == "unrecoverable"
            ):
                result["_unrecoverable"] = True
                result["_unrecoverable_reason"] = (
                    result.get("_unrecoverable_reason", "")
                    + f"; {name}: {step_result.get('message', '')}"
                )
                logger.error(
                    f"[LightRAGRepair] {name} 报 unrecoverable: {step_result.get('message', '')}，停止后续重建"
                )
                break  # 任一 unrecoverable 立即停止后续重建
        except Exception as e:  # noqa: BLE001
            logger.error(f"[LightRAGRepair] {name} 重建异常: {e}", exc_info=True)
            result[name] = {
                "status": "error",
                "expected": 0,
                "actual": 0,
                "lost": 0,
                "message": f"{name} 重建异常: {type(e).__name__}: {e}",
                "unrecoverable": True,
            }
            result["_unrecoverable"] = True
            result["_unrecoverable_reason"] = (
                result.get("_unrecoverable_reason", "")
                + f"; {name} 重建异常: {e}"
            )
            break

    return result


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

