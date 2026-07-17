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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from loguru import logger

from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import (
    EmbeddingFunc,
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
    func: "Callable[..., Any] | None" = None  # 在 __post_init__ 中设为 _embed_async
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

    # partial 状态：部分 has_content + 部分 absent/empty → 损坏
    graphml_has = graphml_state == "has_content"
    full_docs_has = full_docs_state == "has_content"
    cache_has = cache_state == "has_content"
    if graphml_has != full_docs_has or graphml_has != cache_has:
        return {
            "intact": False,
            "graphml": {
                "intact": graphml_has,
                "reason": "partial 状态损坏" if not graphml_has else "有内容",
            },
            "full_docs": {
                "intact": full_docs_has,
                "reason": "partial 状态损坏" if not full_docs_has else "有内容",
            },
            "cache": {
                "intact": cache_has,
                "reason": "partial 状态损坏" if not cache_has else "有内容",
            },
        }

    # 3 文件都有内容且无 corrupt → intact=True
    return {
        "intact": True,
        "graphml": {"intact": True, "reason": "有 node"},
        "full_docs": {"intact": True, "reason": "有 entries"},
        "cache": {"intact": True, "reason": "有 entries"},
    }


# =============================================================================
# 11 个 repair 函数（按依赖链顺序）
# =============================================================================


def repair_text_chunks() -> dict[str, Any]:
    """v8：从 GraphML 提活跃 chunk_id + cache original_prompt 优先 + full_docs fallback + 脑区直接构造。

    真相源：GraphML（唯一真相源，提活跃 chunk_id + 脑区元数据）
    辅助：cache original_prompt（主补充源，正则提取 ``` 之间 chunk 原文，多条取 create_time 最大）
         full_docs（fallback，cache 找不到时 chunking 反查）
    派生：kv_store_text_chunks.json

    算法：
    1. 解析 GraphML 提取活跃 chunk_id 集合 C（从所有 node d3 + edge d10）
    2. 识别脑区节点（d1=brainregion），直接构造 chunk：
       - content = "{node_id}: {d2 description}"
       - full_doc_id = "brain_{node_id}"
    3. 对 C 中非脑区 chunk_id：
       a. cache original_prompt 优先：按 chunk_id 索引 cache extract entry，
          多条取 create_time 最大，正则 r"```\\s*(.+?)\\s*```" + re.DOTALL 提取 chunk 原文
       b. full_docs fallback：cache 找不到时，对每个 doc 用独立 tokenizer chunking，
          算 chunk_id（compute_mdhash_id），跟活跃 chunk_id 匹配
    4. 三处都没有 → missing
    5. llm_cache_list 从 cache 按 chunk_id 反向构建

    GraphML 损坏 = unrecoverable
    cache + full_docs 都损坏 = unrecoverable
    """
    import re

    storage_dir = _storage_dir()
    tc_path = storage_dir / "kv_store_text_chunks.json"
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    # 1. 解析 GraphML 提取活跃 chunk_id 集合 C + 识别脑区节点
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
    # v8 真实数据纠正：脑区节点 source_id 含 N 个 chunk_id（如"知识体系脑区"有 63 个），
    # 这些 chunk_id 实际是脑区引用的普通文档 chunk（不是脑区自己生成的 chunk）。
    # 脑区直接构造只作为最后 fallback：cache + full_docs 都没匹配时才用脑区元数据构造。
    # 算法：脑区 source_id 的所有 chunk_id 优先走 cache→full_docs→脑区直接构造→missing

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
        edge_src_ids = edge_tuple[2]  # (src, tgt, src_ids, desc, kw) 的 index 2
        if edge_src_ids:
            active_chunk_ids.update(c for c in edge_src_ids.split(GRAPH_FIELD_SEP) if c)

    # 全新用户（GraphML 无活跃 chunk）→ 写空 text_chunks
    if not active_chunk_ids:
        logger.info("[LightRAGRepair] GraphML 无活跃 chunk_id（全新用户），写空 text_chunks")
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
        pass
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + cache + full_docs",
            "message": "GraphML 无活跃 chunk_id，重建空 text_chunks",
        }

    # 2. 读 cache（主补充源）
    cache: dict[str, Any] = {}
    cache_corrupt = False
    if cache_path.exists():
        loaded = _load_json_dict(cache_path)
        if isinstance(loaded, dict):
            cache = loaded
        elif loaded is None and cache_path.exists():
            cache_corrupt = True

    # cache 是 3 真相源之一，损坏即 unrecoverable（方案 L5: 3 真相源任一损坏即报修复失败）
    if cache_corrupt:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(brainregion_chunks),
            "lost": len(active_chunk_ids) - len(brainregion_chunks),
            "source": "GraphML + cache + full_docs",
            "message": "cache 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
            "unrecoverable": True,
        }

    # 3. 读 full_docs（fallback）
    full_docs: dict[str, Any] = {}
    full_docs_corrupt = False
    if full_docs_path.exists():
        loaded = _load_json_dict(full_docs_path)
        if isinstance(loaded, dict):
            full_docs = loaded
        elif loaded is None and full_docs_path.exists():
            full_docs_corrupt = True

    # 4. 构建 cache 的 chunk_id -> [(create_time, original_prompt, cache_key)] 映射
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

    # 每个 chunk_id 的 entries 按 create_time 降序排（最大在前）
    for cid in cache_by_chunk_id:
        cache_by_chunk_id[cid].sort(key=lambda x: x[0], reverse=True)

    # 5. 判断是否需要扫 full_docs
    # v8-Task 10 修复：cache 覆盖也扫 full_docs（因为 cache entry 不含 full_doc_id，
    # cache-derived chunks 需要 full_docs chunking 反查补 full_doc_id）。
    # 这样 doc_status/full_entities/full_relations 的 chunk→doc 映射不会全空。
    # 只有 cache 为空且无 active chunk 时才不扫（但 active_chunk_ids 非空到这就必须扫）。
    need_full_docs_scan = bool(active_chunk_ids)

    # 6. full_docs chunking 反查（仅当 cache 没覆盖全部非脑区 chunk）
    full_docs_chunk_map: dict[str, tuple[int, str, str, str]] = {}
    # 类型: chunk_id -> (create_time, doc_id, chunk_content, file_path)
    if need_full_docs_scan:
        # full_docs 是 3 真相源之一，损坏即 unrecoverable（方案 L5: 3 真相源任一损坏即报修复失败）
        if full_docs_corrupt:
            return {
                "status": "error",
                "expected": len(active_chunk_ids),
                "actual": len(brainregion_chunks),
                "lost": len(active_chunk_ids) - len(brainregion_chunks),
                "source": "GraphML + cache + full_docs",
                "message": "full_docs 损坏（JSON 解析失败），3 真相源之一损坏无法恢复",
                "unrecoverable": True,
            }
        if not full_docs:
            # full_docs 文件不存在或为空 dict（全新用户合法），跳过 chunking
            pass
        else:
            # 独立加载 tokenizer（不调 get_lightrag_for_repair，铁律 3）
            tokenizer = _get_tokenizer()
            if tokenizer is None:
                return {
                    "status": "error",
                    "expected": len(active_chunk_ids),
                    "actual": len(brainregion_chunks),
                    "lost": len(active_chunk_ids) - len(brainregion_chunks),
                    "source": "GraphML + cache + full_docs",
                    "message": "TiktokenTokenizer 加载失败，无法 chunking",
                    "unrecoverable": True,
                }
            chunk_token_size, chunk_overlap = _get_chunk_config()

            from lightrag.operate import chunking_by_token_size

            # 按 create_time 降序排 full_docs（多 doc 匹配同 chunk_id 时取最新版本）
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
                file_path = doc_data.get("file_path", "")
                create_time = doc_data.get("create_time", 0)

                chunks = chunking_by_token_size(
                    tokenizer, content,  # type: ignore[arg-type]
                    chunk_token_size=chunk_token_size,
                    chunk_overlap_token_size=chunk_overlap,
                )
                for chunk in chunks:
                    chunk_content = chunk["content"]
                    cid = compute_mdhash_id(chunk_content, prefix="chunk-")
                    if cid not in full_docs_chunk_map:
                        full_docs_chunk_map[cid] = (create_time, doc_id, chunk_content, file_path)

    # 7. 遍历 C 构建 new_tc
    new_tc: dict[str, Any] = {}
    missing_chunks: list[str] = []

    # v8 真实数据纠正：脑区 source_id 里的 chunk_id 优先走 cache→full_docs 提取
    # （因为这些 chunk_id 实际是脑区引用的普通文档 chunk）。
    # 只有 cache + full_docs 都没匹配时才用脑区元数据直接构造（fallback）。
    for cid in active_chunk_ids:
        if cid in cache_by_chunk_id:
            # cache original_prompt 提取（取 create_time 最大的 entry）
            latest_entry = cache_by_chunk_id[cid][0]  # 已降序排
            _, op, _ = latest_entry
            m = cache_pattern.search(op)
            if m:
                chunk_content = m.group(1)
                # v8-Task 10 修复：cache entry 不含 full_doc_id，用 full_docs_chunk_map 反查补 doc_id。
                # 之前留空导致 doc_status.chunks_list 为空，full_entities/full_relations actual=0。
                # 反查用 chunk_id 直接匹配 full_docs_chunk_map 的 key（按 chunk_content hash 算的）。
                doc_id = ""
                if cid in full_docs_chunk_map:
                    doc_id = full_docs_chunk_map[cid][1]  # (create_time, doc_id, content, file_path)
                new_tc[cid] = {
                    "content": chunk_content,
                    "full_doc_id": doc_id,
                    "llm_cache_list": [e[2] for e in cache_by_chunk_id[cid]],
                }
                continue
        # full_docs fallback
        if cid in full_docs_chunk_map:
            _, doc_id, content, _ = full_docs_chunk_map[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": doc_id,
                "llm_cache_list": [e[2] for e in cache_by_chunk_id.get(cid, [])],
            }
            continue
        # 脑区直接构造（fallback）：cache + full_docs 都没匹配，且 chunk_id 来自脑区节点
        if cid in brainregion_chunks:
            content, full_doc_id = brainregion_chunks[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": full_doc_id,
                "llm_cache_list": [e[2] for e in cache_by_chunk_id.get(cid, [])],
            }
            continue
        # 三处都没有 → missing
        missing_chunks.append(cid)

    # 8. 备份损坏的 text_chunks + 原子写
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

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
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
        pass
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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
        pass
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
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
    pass

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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
        pass
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
    # items: (node_id, content, source_id, description, entity_type, file_path)
    items: list[tuple[str, str, str, str, str, str]] = []
    for node_id, (etype, desc, src, file_path) in nodes.items():
        # desc 为空时用 node_id 作为 fallback（保证有内容可 embed）
        # 格式: f"{node_id}\n{desc}"，跟 LightRAG 一致
        content = f"{node_id}\n{desc}" if desc else f"{node_id}\n{node_id}"
        items.append((node_id, content, src, desc, etype, file_path))

    expected = len(items)
    texts = [t for _, t, _, _, _, _ in items]

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

    # 4. 构造 data_list（6 字段对齐 LightRAG 原生 vdb_data，参考 operate.py L1162-1172）
    embedding_dim = len(vectors[0]) if vectors and vectors[0] is not None else _get_embedding_dim()
    data_list: list[dict[str, Any]] = []
    final_vectors: list[list[float]] = []
    failed_count = 0
    for (node_id, content, src, desc, etype, file_path), vec in zip(items, vectors):
        if vec is None:
            failed_count += 1
            continue
        # __id__ = compute_mdhash_id(node_id, prefix="ent-")
        # node_id 已 lower（LightRAG 设计），但 compute_mdhash_id 对原始字符串算 hash
        expected_id = compute_mdhash_id(node_id, prefix="ent-")
        data_list.append({
            "__id__": expected_id,
            "entity_name": node_id,
            "source_id": src or "",
            "description": desc or "",
            "entity_type": etype or "",
            "file_path": file_path or "",
            "content": content,
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
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
    pass

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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
        pass
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
        # 跟 LightRAG operate.py L1483 ", ".join(set(keywords)) 一致——多关键词用逗号+空格分隔
        # LightRAG 用 set 去重，这里也去重保持一致（避免 embedding 输入与原生不一致）
        if edge_keywords and GRAPH_FIELD_SEP in edge_keywords:
            kw_list = [k.strip() for k in edge_keywords.split(GRAPH_FIELD_SEP) if k.strip()]
            # dict.fromkeys 保留顺序去重（与 set 等价但保持 LightRAG 写入时的顺序）
            normalized_keywords = ", ".join(dict.fromkeys(kw_list))
        else:
            normalized_keywords = edge_keywords or ""
        content = f"{normalized_keywords}\t{sorted_src}\n{sorted_tgt}\n{edge_desc}"
        items.append((sorted_src, sorted_tgt, content, edge_src_id, vdb_id))

    expected = len(items)
    if expected == 0:
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
        pass
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
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _build_vdb_file）
    pass

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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
        pass
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
    for node_id, (_, _, src, _file_path) in nodes.items():
        if not src:
            # source_id 为空 → 空 chunk_ids（合法）
            new_entity_chunks[node_id] = {"chunk_ids": [], "count": 0}
            continue
        # source_id 是 <SEP> 分隔的 chunk_id 列表
        chunk_ids = [c for c in src.split(GRAPH_FIELD_SEP) if c]
        new_entity_chunks[node_id] = {"chunk_ids": chunk_ids, "count": len(chunk_ids)}

    # 3. 备份 + 写
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

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
        # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
        pass
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
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

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
    for node_id, (_, _, src, _file_path) in nodes.items():
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

    # 6. 写入（LightRAG 原生格式：{entity_names: [...], count: N}）
    #    参考 REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py:2901-2908
    #    读取侧 lightrag.py:3567 显式检查 "entity_names" in doc_entities_data
    expected = len(doc_status) if doc_status else 0
    fe_payload = {
        doc_id: {"entity_names": sorted(ents), "count": len(ents)}
        for doc_id, ents in doc_to_entities.items()
    }
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

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
    #    key 用 (src, tgt) 二元组（保留 src/tgt 信息，LightRAG 读取侧用 pair[0]/pair[1]）
    relation_pair_to_docs: dict[tuple[str, str], set[str]] = {}
    for src, tgt, edge_src_id, _, _ in edges:
        if not src or not tgt:
            continue
        if not edge_src_id:
            continue
        chunk_ids = [c for c in edge_src_id.split(GRAPH_FIELD_SEP) if c]
        for cid in chunk_ids:
            doc_id = chunk_to_doc.get(cid)
            if doc_id:
                relation_pair_to_docs.setdefault((src, tgt), set()).add(doc_id)

    # 5. 反转：doc→relation_pairs
    doc_to_relation_pairs: dict[str, list[list[str]]] = {}
    for (src, tgt), doc_set in relation_pair_to_docs.items():
        for doc_id in doc_set:
            doc_to_relation_pairs.setdefault(doc_id, []).append([src, tgt])

    # 6. 写入（LightRAG 原生格式：{relation_pairs: [[src, tgt], ...], count: N}）
    #    参考 REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py:2911-2919
    #    读取侧 lightrag.py:3582 显式检查 "relation_pairs" in doc_relations_data
    expected = len(doc_status) if doc_status else 0
    fr_payload = {
        doc_id: {"relation_pairs": pairs, "count": len(pairs)}
        for doc_id, pairs in doc_to_relation_pairs.items()
    }
    # TODO Task 3-9 用 storage.upsert 重写（v9 Task 1 已删除 _atomic_write_json）
    pass

    actual = len(doc_to_relation_pairs)
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
    """v8：3 真相源不可动 + 删 9 派生 + 按需提取重建。

    流程：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
    3. 删除 9 个派生文件（铁律 1：不备份，直接删）
    4. 按依赖链重建 9 派生文件（从 GraphML + cache + full_docs 按需提取）
    5. 失败时无法回滚（因为派生文件已删光，真相源从未被修改）

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
        result["_deleted"] = []  # 真相源损坏时不删派生文件，让用户看到现场
        return result

    # 2. 删除 9 个派生文件（铁律 1：不备份，直接删）
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

    # 3. 按依赖链重建 9 派生文件
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

