"""
LightRAG Unified MCP Server

Replaces vector-store (7 tools) and kg-server (20 tools) with 16 unified tools
that delegate to LightRAGAdapter and LightRAGIngester.

Tool groups:
- Query (5): lightrag_query, lightrag_query_data, lightrag_search_entities, lightrag_get_graph, lightrag_timeline_query
- Insert (5): lightrag_insert, lightrag_insert_file, lightrag_insert_custom_kg, lightrag_insert_entity, lightrag_insert_relation
- Manage (6): lightrag_delete_document, lightrag_delete_entity, lightrag_document_status, lightrag_get_document, lightrag_list_entities, lightrag_merge_entities
- Edit/Detail (7): lightrag_edit_entity, lightrag_edit_relation, lightrag_delete_relation, lightrag_get_entity_info, lightrag_get_relation_info, lightrag_create_entity, lightrag_create_relation
"""

import re

from typing import Any, Dict, List, Optional
import inspect
import threading
import sys as _sys

from loguru import logger

from niu_api.internal.lightrag_adapter import LightRAGAdapter


# ============== Eager Import (module load, single-threaded) ==============
# pipeline_enqueue_file requires sys.argv=["lightrag"] workaround.
# LightRAG's auth.py → config.py → parse_args() parses sys.argv and exits
# with SystemExit:2 when called outside the API server.
# We import at module load time to avoid the race condition of modifying
# sys.argv in a multi-threaded API server at runtime. A global lock guards
# the sys.argv swap in case this module is imported after threads start.

_argv_swap_lock = threading.Lock()

_pipeline_enqueue_file = None

with _argv_swap_lock:
    _saved_argv = _sys.argv[:]
    _sys.argv = ["lightrag"]
    try:
        from lightrag.api.routers.document_routes import pipeline_enqueue_file
        _pipeline_enqueue_file = pipeline_enqueue_file
    finally:
        _sys.argv = _saved_argv
        del _saved_argv


def _get_pipeline_enqueue_file():
    """Return the eagerly-imported pipeline_enqueue_file (no runtime sys.argv swap)."""
    if _pipeline_enqueue_file is None:
        raise RuntimeError("pipeline_enqueue_file was not imported at module load time")
    return _pipeline_enqueue_file


# ============== Entity Name Normalization ==============

# Legacy colon-prefix patterns (brain:, person:, photo:, etc.) are DEPRECATED.
# All entity names now follow LightRAG's natural language naming system.
# This normalization is kept as a no-op identity function for backward compat.


def _normalize_entity_name(name: str) -> str:
    """Normalize entity name.

    All entity names now use natural language format (e.g., "Niu", "任飞",
    "影像记忆脑区") instead of colon-prefix format (e.g., "brain:Niu",
    "person:uuid", "brain:region:影像记忆").

    This function is kept as identity for backward compatibility.
    New code should pass natural language names directly.
    """
    return name


# ============== Singleton Accessors ==============

_adapter = None
_ingester = None
_adapter_lock = threading.Lock()
_ingester_lock = threading.Lock()


def _get_adapter():
    """Get or create the singleton LightRAGAdapter (thread-safe)."""
    global _adapter
    if _adapter is None:
        with _adapter_lock:
            if _adapter is None:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                _adapter = LightRAGAdapter()
    return _adapter


def _get_ingester():
    """Get or create the singleton LightRAGIngester (thread-safe)."""
    global _ingester
    if _ingester is None:
        with _ingester_lock:
            if _ingester is None:
                from niu_api.internal.lightrag_adapter import LightRAGIngester
                _ingester = LightRAGIngester()
    return _ingester


# ============== TOOL_SCHEMAS ==============

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # --- Query Group ---
    "lightrag_query": {
        "name": "lightrag_query",
        "description": (
            "Search the knowledge base using LightRAG. "
            "Modes: 'local' (entity-centric), 'global' (overview), 'hybrid' (balanced), "
            "'mix' (KG + vector combined), 'naive' (vector only). "
            "Returns generated text answer or raw context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "mode": {
                    "type": "string",
                    "enum": ["naive", "local", "global", "hybrid", "mix", "bypass"],
                    "default": "mix",
                    "description": "Retrieval mode ('bypass' skips retrieval, uses LLM only)",
                },
                "only_need_context": {
                    "type": "boolean",
                    "default": True,
                    "description": "Return context only without LLM generation",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "Number of top results to retrieve",
                },
                "response_type": {
                    "type": "string",
                    "default": "Multiple Paragraphs",
                    "description": "Response format when generating",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "提供keywords时跳过LLM关键词提取，近即时返回（<1秒）；不提供时由LightRAG自动提取（5-30秒，依赖LLM可用）。推荐提供keywords以获得最佳性能。从查询中提取核心名词/术语作为keywords。",
                },
            },
            "required": ["query"],
        },
    },

    "lightrag_query_data": {
        "name": "lightrag_query_data",
        "description": (
            "Query the knowledge base returning structured data (entities + relationships + chunks). "
            "MODES: 'local' (entity-centric graph traversal, RECOMMENDED for most queries), "
            "'global' (community-level overview), 'hybrid' (local+global combined, slower), "
            "'naive' (vector-only, NO graph data), 'mix' (all combined, slowest). "
            "KEY OPTIMIZATION: When you provide 'keywords', the query skips LLM keyword extraction "
            "and uses your keywords directly — this eliminates LLM latency (~10-100s -> <1s) while "
            "keeping full graph traversal capability. ALWAYS provide keywords when you know the search "
            "terms (e.g., query='便签' keywords=['便签']). Only omit keywords for complex natural "
            "language queries that need LLM interpretation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "mode": {
                    "type": "string",
                    "enum": ["naive", "local", "global", "hybrid", "mix", "bypass"],
                    "default": "local",
                    "description": "Retrieval mode. 'local' is best for finding specific entities. 'hybrid' adds community context but is slower. 'naive' skips graph entirely.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Pre-provided keywords to skip LLM extraction. DRAMATICALLY faster. Use the core nouns/terms from your query. E.g., query='查看便签' -> keywords=['便签']. For 'local' mode these become ll_keywords; for 'global'/'hybrid' they become both hl and ll keywords.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Number of top results to retrieve",
                },
            },
            "required": ["query"],
        },
    },

    "lightrag_search_entities": {
        "name": "lightrag_search_entities",
        "description": (
            "Search for entities of a specific type in the knowledge graph. "
            "Uses local mode (entity-focused) and filters by entity_type. "
            "Common types: skill, tool, knowledge, person, photo, concept."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "entity_type": {
                    "type": "string",
                    "default": "",
                    "description": "Entity type to filter (skill, tool, knowledge, person, photo, concept)",
                },
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max results",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "提供keywords时跳过LLM关键词提取，近即时返回（<1秒）；不提供时由LightRAG自动提取（5-30秒，依赖LLM可用）。推荐提供keywords以获得最佳性能。从查询中提取核心名词/术语作为keywords。",
                },
            },
            "required": ["query"],
        },
    },

    "lightrag_get_graph": {
        "name": "lightrag_get_graph",
        "description": (
            "Get a subgraph from the knowledge graph. "
            "action='explore' returns N-layer neighbors of an entity; "
            "action='snapshot' returns full graph for visualization."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["explore", "snapshot"],
                    "default": "explore",
                    "description": "Graph retrieval type",
                },
                "entity_name": {
                    "type": "string",
                    "default": "",
                    "description": "Center entity name (for explore action)",
                },
                "depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "BFS traversal depth 1-5 (for explore)",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "description": "Max nodes to return (for snapshot)",
                },
                "edge_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter edges by relation type (e.g., ['followed_by', 'corrected_by']). None returns all.",
                },
            },
            "required": ["action"],
        },
    },

    "lightrag_timeline_query": {
        "name": "lightrag_timeline_query",
        "description": (
            "时间线查询：向量匹配内容 → 遍历时间链 → 按时间戳排序。"
            "用于追踪事件演变、纠正链、因果关系。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本（与 start_entities 二选一）",
                },
                "start_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "直接指定起始实体名列表，跳过向量匹配",
                },
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward"],
                    "default": "backward",
                    "description": "排序方向：backward=最近优先，forward=最早优先",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "时间链遍历深度",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "向量搜索返回实体数",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                    "description": "返回结果最大数量",
                },
            },
            "required": [],
        },
    },

    # --- Insert Group ---
    "lightrag_insert": {
        "name": "lightrag_insert",
        "description": (
            "Insert document(s) into the knowledge base. LightRAG automatically "
            "extracts entities and relations via LLM. Use file_path for large content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Document text content",
                },
                "doc_id": {
                    "type": "string",
                    "description": "Optional unique document ID (auto-generated if omitted)",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional file path for citation",
                },
            },
            "required": ["content"],
        },
    },

    "lightrag_insert_file": {
        "name": "lightrag_insert_file",
        "description": (
            "Insert a file into the knowledge base by file path. "
            "LightRAG reads and parses the file automatically (supports DOCX, PDF, PPTX, XLSX, TXT, MD, etc.), "
            "then extracts entities and builds the knowledge graph asynchronously."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to insert into the knowledge base",
                },
                "doc_id": {
                    "type": "string",
                    "description": "Optional unique document ID for dedup",
                },
            },
            "required": ["file_path"],
        },
    },

    "lightrag_insert_custom_kg": {
        "name": "lightrag_insert_custom_kg",
        "description": (
            "Inject structured knowledge (entities + relationships + chunks) directly. "
            "Bypasses LLM extraction for precise control. Use for skills, tools, "
            "photo names, and other data that must be exact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of entity dicts: {entity_name, entity_type, description}",
                },
                "relationships": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of relationship dicts: {src_id, tgt_id, keywords, description}",
                },
                "chunks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of chunk dicts: {content, source_id, file_path}",
                },
                "source_id": {
                    "type": "string",
                    "default": "custom_kg",
                    "description": "Default source ID",
                },
            },
            "required": [],
        },
    },

        "lightrag_insert_entity": {
        "name": "lightrag_insert_entity",
        "description": "Insert an entity into the knowledge graph using structured injection (ainsert_custom_kg). Entity name and type are preserved exactly — no LLM auto-extraction. Also creates a Niu anchor edge for reachability. Entity names must use natural language (e.g., 'Python', '任飞'), NOT colon-prefix format (e.g., NOT 'skill:Python', NOT 'person:uuid').",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entity name (e.g., 'Python', '数据分析')"},
                "entity_type": {"type": "string", "description": "Entity type (e.g., 'person', 'concept', 'skill', 'tool')"},
                "description": {"type": "string", "default": "", "description": "Entity description"},
                "source_id": {"type": "string", "default": "custom_kg", "description": "Deprecated — ignored. Kept for backward compatibility."},
                "file_path": {"type": "string", "default": "custom_kg", "description": "File path for citation"},
                "skip_llm_extraction": {
                    "type": "boolean",
                    "default": True,
                    "description": "Deprecated — custom_kg never triggers LLM. Kept for backward compatibility.",
                },
            },
            "required": ["name", "entity_type"],
        },
    },

    "lightrag_insert_relation": {
        "name": "lightrag_insert_relation",
        "description": "Insert a relation between two entities using structured injection (ainsert_custom_kg). Relation src/tgt/keywords are preserved exactly — no LLM auto-extraction. Entity names must use natural language (e.g., 'Python', '任飞'), NOT colon-prefix format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "src_id": {"type": "string", "description": "Source entity name"},
                "tgt_id": {"type": "string", "description": "Target entity name"},
                "relation": {"type": "string", "description": "Relation type (e.g., USED_FOR, OFTEN_WITH)"},
                "description": {"type": "string", "default": "", "description": "Relation description"},
                "source_id": {"type": "string", "default": "custom_kg", "description": "Deprecated — ignored. Kept for backward compatibility."},
                "file_path": {"type": "string", "default": "custom_kg", "description": "File path for citation"},
            },
            "required": ["src_id", "tgt_id", "relation"],
        },
    },

    # --- Manage Group ---
    "lightrag_delete_entity": {
        "name": "lightrag_delete_entity",
        "description": "Delete an entity and all its relations from the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Entity name to delete"},
            },
            "required": ["entity_name"],
        },
    },

    "lightrag_document_status": {
        "name": "lightrag_document_status",
        "description": "Get document processing status counts (pending, processing, processed, failed).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "lightrag_get_document": {
        "name": "lightrag_get_document",
        "description": "获取完整文档内容及其处理状态。对应旧 vector-store/get_document。",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "文档ID"}
            },
            "required": ["doc_id"]
        }
    },

    "lightrag_delete_document": {
        "name": "lightrag_delete_document",
        "description": "级联删除文档及其关联的 chunks、entities、relationships。对应旧 vector-store/delete_document，但执行完整级联删除而非仅删实体。",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "要删除的文档ID"}
            },
            "required": ["doc_id"]
        }
    },

    "lightrag_list_entities": {
        "name": "lightrag_list_entities",
        "description": (
            "List entities or documents in the knowledge base. "
            "list_type='entities' lists graph entities; 'documents' lists indexed documents; "
            "'labels' lists all entity type labels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_type": {
                    "type": "string",
                    "enum": ["entities", "documents", "labels"],
                    "default": "entities",
                    "description": "What to list",
                },
                "entity_type": {
                    "type": "string",
                    "default": "",
                    "description": "Filter by entity type (e.g., person, concept)",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max results",
                },
            },
            "required": [],
        },
    },

    "lightrag_merge_entities": {
        "name": "lightrag_merge_entities",
        "description": "Merge multiple entities into one, consolidating all relations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity names to merge",
                },
                "target_entity": {
                    "type": "string",
                    "description": "Name of the merged target entity",
                },
            },
            "required": ["source_entities", "target_entity"],
        },
    },

    # --- Edit/Detail Group ---
    "lightrag_edit_entity": {
        "name": "lightrag_edit_entity",
        "description": "Edit entity information in the knowledge graph. Can update description, type, or rename entity. Set allow_rename=True to enable renaming. Set allow_merge=True to merge into existing entity when renaming to an existing name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Entity name to edit"},
                "description": {"type": "string", "description": "New description (overwrites existing)"},
                "entity_type": {"type": "string", "description": "New entity type"},
                "new_name": {"type": "string", "description": "New entity name (requires allow_rename=True)"},
                "allow_rename": {"type": "boolean", "default": False, "description": "Allow renaming entity"},
                "allow_merge": {"type": "boolean", "default": False, "description": "Allow merging into existing entity when renaming"},
            },
            "required": ["entity_name"],
        },
    },

    "lightrag_edit_relation": {
        "name": "lightrag_edit_relation",
        "description": "Edit relation (edge) information between two entities. Can update description, keywords, or weight.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_entity": {"type": "string", "description": "Source entity name"},
                "target_entity": {"type": "string", "description": "Target entity name"},
                "keywords": {"type": "string", "description": "Current keywords (used to identify the relation)"},
                "new_keywords": {"type": "string", "description": "New keywords"},
                "new_description": {"type": "string", "description": "New description"},
                "new_weight": {"type": "number", "description": "New weight"},
            },
            "required": ["source_entity", "target_entity"],
        },
    },

    "lightrag_delete_relation": {
        "name": "lightrag_delete_relation",
        "description": "Delete a relation between two entities. Both entities are kept, only the relation is removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_entity": {"type": "string", "description": "Source entity name"},
                "target_entity": {"type": "string", "description": "Target entity name"},
                "keywords": {"type": "string", "description": "Relation keywords (optional, if not specified deletes all relations between the two entities)"},
            },
            "required": ["source_entity", "target_entity"],
        },
    },

    "lightrag_get_entity_info": {
        "name": "lightrag_get_entity_info",
        "description": "Get detailed information of a single entity, including graph data and optionally vector data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Entity name to query"},
                "include_vector_data": {"type": "boolean", "default": False, "description": "Include vector database information"},
            },
            "required": ["entity_name"],
        },
    },

    "lightrag_get_relation_info": {
        "name": "lightrag_get_relation_info",
        "description": "Get detailed information of a relationship between two entities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_entity": {"type": "string", "description": "Source entity name"},
                "target_entity": {"type": "string", "description": "Target entity name"},
                "include_vector_data": {"type": "boolean", "default": False, "description": "Include vector database information"},
            },
            "required": ["source_entity", "target_entity"],
        },
    },

    "lightrag_create_entity": {
        "name": "lightrag_create_entity",
        "description": "Create a new entity in the knowledge graph. Fails if entity already exists. Use lightrag_insert_entity for upsert behavior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Entity name (must be unique)"},
                "entity_type": {"type": "string", "description": "Entity type (e.g., person, concept, skill, tool)"},
                "description": {"type": "string", "default": "", "description": "Entity description"},
                "source_id": {"type": "string", "default": "manual_creation", "description": "Source chunk ID"},
                "file_path": {"type": "string", "default": "manual_creation", "description": "File path for citation"},
            },
            "required": ["entity_name", "entity_type"],
        },
    },

    "lightrag_create_relation": {
        "name": "lightrag_create_relation",
        "description": "Create a new relation between two entities. Both entities must exist. Fails if relation already exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_entity": {"type": "string", "description": "Source entity name"},
                "target_entity": {"type": "string", "description": "Target entity name"},
                "keywords": {"type": "string", "description": "Relation keywords (required)"},
                "description": {"type": "string", "default": "", "description": "Relation description"},
                "weight": {"type": "number", "default": 1.0, "description": "Relation weight"},
                "source_id": {"type": "string", "default": "manual_creation", "description": "Source chunk ID"},
                "file_path": {"type": "string", "default": "manual_creation", "description": "File path for citation"},
            },
            "required": ["source_entity", "target_entity", "keywords"],
        },
    },
}


# ============== Public API ==============


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return all tool schemas for ToolRegistry registration."""
    return list(TOOL_SCHEMAS.values())


# ============== Tool Implementations ==============


def lightrag_query(
    query: str,
    mode: str = "mix",
    only_need_context: bool = True,
    top_k: int = 5,
    response_type: str = "Multiple Paragraphs",
    keywords: Optional[list] = None,
):
    """Search the knowledge base using LightRAG."""
    valid_modes = {"naive", "local", "global", "hybrid", "mix", "bypass"}
    if mode not in valid_modes:
        return {"status": "error", "message": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"}
    try:
        adapter = _get_adapter()
        result = adapter.query(
            query=query,
            mode=mode,
            only_need_context=only_need_context,
            top_k=top_k,
            response_type=response_type,
            keywords=keywords,
        )
        if result is None:
            return {"status": "error", "message": "No results from LightRAG"}
        return result
    except Exception as e:
        logger.error(f"lightrag_query failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_query_data(
    query: str,
    mode: str = "local",
    keywords: Optional[list] = None,
    top_k: int = 10,
):
    """Query returning structured data (entities + relationships + chunks).

    When keywords are provided, skips LLM keyword extraction for near-instant
    results while keeping full graph traversal. Without keywords, LLM extraction
    adds 5-30s latency.
    """
    valid_modes = {"naive", "local", "global", "hybrid", "mix", "bypass"}
    if mode not in valid_modes:
        return {"status": "error", "message": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"}
    try:
        adapter = _get_adapter()
        result = adapter.query_data(
            query=query, mode=mode, top_k=top_k, keywords=keywords,
        )
        if LightRAGAdapter._is_no_result(result):
            return {"status": "no_results", "message": "No relevant results found in knowledge graph"}
        return result
    except Exception as e:
        logger.error(f"lightrag_query_data failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_search_entities(
    query: str,
    entity_type: str = "",
    top_k: int = 10,
    keywords: Optional[list] = None,
) -> Dict[str, Any]:
    """Search for entities of a specific type."""
    try:
        adapter = _get_adapter()
        result = adapter.query_data(query=query, mode="local", top_k=top_k, keywords=keywords)
        if LightRAGAdapter._is_no_result(result):
            return {"status": "no_results", "message": "No relevant results found in knowledge graph"}
        if entity_type:
            entities = adapter.filter_by_entity_type(result, entity_type)
            return {"status": "ok", "data": entities}
        # No filter: return all entities from result
        data = result.get("data", result) if isinstance(result, dict) else {}
        if isinstance(data, list):
            return {"status": "ok", "data": data}
        entities = data.get("entities", []) if isinstance(data, dict) else []
        return {"status": "ok", "data": entities}
    except Exception as e:
        logger.error(f"lightrag_search_entities failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_get_graph(
    action: str = "explore",
    entity_name: str = "",
    depth: int = 2,
    limit: int = 200,
    edge_types: Optional[List[str]] = None,
):
    """Get a subgraph from the knowledge graph."""
    valid_actions = {"explore", "snapshot"}
    if action not in valid_actions:
        return {"status": "error", "message": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}", "nodes": [], "edges": [], "center": None, "stats": {}}
    try:
        adapter = _get_adapter()
        if action == "explore":
            if not entity_name:
                return {"status": "error", "message": "entity_name required for explore", "nodes": [], "edges": [], "center": None, "stats": {}}
            return adapter.explore_node(entity_name=entity_name, depth=depth, edge_types=edge_types)
        else:  # snapshot
            return adapter.get_graph_snapshot(limit=limit)
    except Exception as e:
        logger.error(f"lightrag_get_graph failed: {e}")
        return {"status": "error", "message": str(e), "nodes": [], "edges": [], "center": None, "stats": {}}


def lightrag_timeline_query(
    query: str = "",
    start_entities: Optional[List[str]] = None,
    direction: str = "backward",
    max_depth: int = 2,
    top_k: int = 5,
    max_results: int = 10,
) -> Dict[str, Any]:
    """Query timeline: vector-match then traverse time-chain relations."""
    try:
        adapter = _get_adapter()
        result = adapter.timeline_query(
            query=query,
            start_entities=start_entities,
            direction=direction,
            max_depth=max_depth,
            top_k=top_k,
            max_results=max_results,
        )
        return {"status": "ok", "timeline": result}
    except Exception as e:
        logger.error(f"lightrag_timeline_query failed: {e}")
        return {"status": "error", "message": str(e), "timeline": []}


def lightrag_insert(
    content: str,
    doc_id: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert document(s) into the knowledge base."""
    try:
        ingester = _get_ingester()
        return ingester.inject_document(content=content, doc_id=doc_id, file_path=file_path)
    except Exception as e:
        logger.error(f"lightrag_insert failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_file(
    file_path: str,
    doc_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert a file into the knowledge base by file path.

    LightRAG reads the file, extracts text (supports DOCX/PDF/PPTX/XLSX/txt/md etc.),
    chunks it, and builds the knowledge graph.
    The original file is never modified or moved — a temp copy is used.
    After enqueuing, file_path in doc_status/full_docs is patched to the original
    path so that entities/relations in the KG carry the correct source reference.
    """
    from niu_api.internal.lightrag_manager import get_lightrag, call_async
    from pathlib import Path as _Path
    import tempfile
    import shutil

    original_path = str(_Path(file_path).resolve())
    file = _Path(file_path)
    if not file.is_file():
        return {"status": "error", "message": f"File not found: {file_path}"}

    rag = get_lightrag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    # Copy file to a temp directory so pipeline_enqueue_file moves
    # the copy (not the user's original file).
    tmp_dir = _Path(tempfile.mkdtemp(prefix="lightrag_ingest_"))
    tmp_file = tmp_dir / file.name
    try:
        shutil.copy2(str(file), str(tmp_file))
    except Exception:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        raise

    try:
        pipeline_fn = _get_pipeline_enqueue_file()

        # Use original path as track_id so we can find the doc later.
        effective_track_id = doc_id or original_path

        enqueue_kwargs: dict[str, Any] = {
            "rag": rag,
            "file_path": tmp_file,
            "track_id": effective_track_id,
        }

        success, track_id = call_async(
            pipeline_fn(**enqueue_kwargs),
            timeout=600,
        )

        # Patch file_path in doc_status and full_docs to the original path.
        # pipeline_enqueue_file stores only file_path.name (basename),
        # but we want the full original path for source traceability.
        #
        # Timing is safe: both the patch coroutine and the pipeline coroutine
        # are submitted to the same asyncio event loop via run_coroutine_threadsafe.
        # asyncio schedules coroutines in FIFO order, so the patch (submitted
        # first) completes before the pipeline (submitted later via fire_and_forget).
        if success:
            try:
                docs = call_async(
                    rag.doc_status.get_docs_by_track_id(effective_track_id),
                    timeout=30,
                )
                for doc_key, doc_data in docs.items():
                    # Patch file_path on the dataclass instance first,
                    # then convert to dict for upsert. This preserves
                    # enum types (e.g. DocStatus) that asdict() would
                    # serialize correctly from the dataclass but might
                    # break if constructed from a plain dict.
                    doc_data.file_path = original_path
                    from dataclasses import asdict as _asdict
                    status_dict = _asdict(doc_data)
                    call_async(rag.doc_status.upsert({doc_key: status_dict}), timeout=30)

                    # Update full_docs file_path
                    full_doc = call_async(rag.full_docs.get_by_id(doc_key), timeout=30)
                    if full_doc:
                        if isinstance(full_doc, dict):
                            full_doc["file_path"] = original_path
                            call_async(rag.full_docs.upsert({doc_key: full_doc}), timeout=30)
                        else:
                            full_doc.file_path = original_path
                            from dataclasses import asdict as _asdict2
                            full_doc_dict = _asdict2(full_doc)
                            call_async(rag.full_docs.upsert({doc_key: full_doc_dict}), timeout=30)
            except Exception as patch_err:
                logger.warning(
                    f"[lightrag_insert_file] file_path patch failed: {patch_err}"
                )

            # Trigger entity extraction pipeline after enqueuing.
            # pipeline_enqueue_file only stores the document (PENDING status).
            # apipeline_process_enqueue_documents splits, calls LLM for
            # entity/relation extraction, and builds the knowledge graph.
            #
            # Fire-and-forget: the pipeline runs in LightRAG's event loop
            # and can take minutes (LLM calls for entity extraction).
            # We must NOT block the caller (sub-agent → main agent → _chat_lock)
            # waiting for this to complete. The enqueue + patch above is
            # sufficient to guarantee the document will be processed.
            try:
                from niu_api.internal.lightrag_manager import fire_and_forget

                async def _process_and_handle_failure(rag_instance, tid, cleanup_dir=None):
                    """Run pipeline and mark docs as FAILED on error or cancellation."""
                    import asyncio as _asyncio
                    try:
                        await rag_instance.apipeline_process_enqueue_documents()
                        # Pipeline succeeded — LLM extracted entities/edges that are
                        # NOT reported via changelog (they go through LightRAG's
                        # internal merge_nodes_and_edges, not our wrapper).
                        # Signal the frontend to re-fetch the full snapshot.
                        try:
                            from niu_api.internal.lightrag_manager import get_change_log
                            get_change_log().record_change("snapshot_refresh", {
                                "reason": "pipeline_completed",
                                "track_id": tid,
                            })
                        except Exception as _cl_err:
                            logger.debug(
                                f"[lightrag_insert_file] snapshot_refresh changelog skipped: {_cl_err}"
                            )
                        # 查询入库统计数据并推送（用数据说话，不替用户下结论）
                        try:
                            docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                            total_docs = len(docs)
                            success_docs = 0
                            failed_docs = 0
                            total_chunks = 0
                            entities_count = 0
                            relations_count = 0
                            errors = []
                            from lightrag.base import DocStatus
                            for _did, _dinfo in docs.items():
                                total_chunks += _dinfo.chunks_count or 0
                                if _dinfo.status == DocStatus.FAILED:
                                    failed_docs += 1
                                    if _dinfo.error_msg:
                                        errors.append(_dinfo.error_msg)
                                    continue
                                # PROCESSED / PREPROCESSED — 查询实际产出
                                _ent = await rag_instance.full_entities.get_by_id(_did)
                                _rel = await rag_instance.full_relations.get_by_id(_did)
                                _ent_count = _ent.get("count", 0) if _ent else 0
                                _rel_count = _rel.get("count", 0) if _rel else 0
                                if _ent_count == 0:
                                    # 标记为成功但零实体产出 → 视为失败
                                    failed_docs += 1
                                    errors.append(f"{_dinfo.file_path or _did}: 未提取到实体")
                                else:
                                    success_docs += 1
                                    entities_count += _ent_count
                                    relations_count += _rel_count
                            from niu_api.chat import push_ingest_result
                            await push_ingest_result(
                                file_path=original_path,
                                total_docs=total_docs,
                                success_docs=success_docs,
                                failed_docs=failed_docs,
                                total_chunks=total_chunks,
                                entities_count=entities_count,
                                relations_count=relations_count,
                                errors=errors if errors else None,
                            )
                        except Exception as _push_err:
                            logger.debug(f"[lightrag_insert_file] ingest result push skipped: {_push_err}")
                    except (_asyncio.CancelledError, Exception) as pipeline_err:
                        is_cancelled = isinstance(pipeline_err, _asyncio.CancelledError)
                        if is_cancelled:
                            logger.warning(
                                f"[lightrag_insert_file] pipeline cancelled: track_id={tid}"
                            )
                        else:
                            logger.error(
                                f"[lightrag_insert_file] pipeline processing failed: "
                                f"track_id={tid} error={pipeline_err}"
                            )
                        # Mark documents as FAILED so they don't stay PENDING forever.
                        # When the outer task is cancelled, bare await re-raises
                        # CancelledError immediately. We use create_task to spawn
                        # the marking as a separate task, then await it directly.
                        # If the event loop is shutting down, the marking may not
                        # complete — this is best-effort.
                        try:
                            from dataclasses import asdict as _asdict
                            from lightrag.api.routers.document_routes import DocStatus

                            async def _mark_failed():
                                docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                                for dk, dd in docs.items():
                                    dd.status = DocStatus.FAILED
                                    status_dict = _asdict(dd)
                                    await rag_instance.doc_status.upsert({dk: status_dict})

                            inner = _asyncio.create_task(_mark_failed())
                            try:
                                await inner
                            except _asyncio.CancelledError:
                                # Outer task cancelled while waiting for marking.
                                # inner is an independent Task — it continues
                                # running in the background regardless.
                                pass
                        except (_asyncio.CancelledError, Exception) as mark_err:
                            logger.debug(
                                f"[lightrag_insert_file] mark-failed skipped "
                                f"(best-effort): track_id={tid} error={mark_err}"
                            )
                        # 推送入库失败结果（用户主动取消不算失败）
                        if not is_cancelled:
                            try:
                                docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                                total_docs = len(docs)
                                total_chunks = sum(d.chunks_count or 0 for d in docs.values())
                                _err_msgs = [d.error_msg for d in docs.values() if d.error_msg]
                                from niu_api.chat import push_ingest_result
                                await push_ingest_result(
                                    file_path=original_path,
                                    total_docs=total_docs,
                                    success_docs=0,
                                    failed_docs=total_docs,
                                    total_chunks=total_chunks,
                                    errors=_err_msgs if _err_msgs else [str(pipeline_err)],
                                )
                            except _asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
                        if is_cancelled:
                            raise pipeline_err
                    finally:
                        # Clean up temp directory after pipeline completes (or fails).
                        # The file may have been moved to __enqueued__/ by
                        # pipeline_enqueue_file, so we remove the entire temp dir.
                        if cleanup_dir:
                            try:
                                shutil.rmtree(str(cleanup_dir), ignore_errors=True)
                            except Exception:
                                pass

                fire_and_forget(
                    _process_and_handle_failure(rag, effective_track_id, cleanup_dir=tmp_dir),
                    context=f"track_id={track_id}",
                )
                logger.info(
                    f"[lightrag_insert_file] pipeline processing scheduled "
                    f"(fire-and-forget), track_id={track_id}"
                )
            except Exception as proc_err:
                logger.warning(
                    f"[lightrag_insert_file] process_enqueue schedule failed: {proc_err}"
                )
                # fire_and_forget failed — pipeline won't run, so clean up temp dir now.
                try:
                    shutil.rmtree(str(tmp_dir), ignore_errors=True)
                except Exception:
                    pass

            # Record changelog only on successful enqueue
            try:
                from niu_api.internal.lightrag_manager import get_change_log
                get_change_log().record_change("document_created", {
                    "id": effective_track_id,
                    "uri": file_path,
                    "title": file.name,
                    "source": "lightrag_insert_file",
                })
            except Exception as e:
                logger.debug(f"[lightrag_insert_file] changelog skipped: {e}")
        else:
            # Enqueue returned success=False (not exception) — pipeline won't
            # run, so clean up the temp copy now.
            try:
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
            except Exception:
                pass

        return {"status": "ok" if success else "error", "track_id": track_id}
    except Exception as e:
        # Enqueue failed — pipeline won't run, so clean up temp dir now.
        try:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass
        logger.error(f"lightrag_insert_file failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_custom_kg(
    entities: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    source_id: str = "custom_kg",
) -> Dict[str, Any]:
    """Inject structured knowledge directly.

    Entity names must follow LightRAG's natural language naming system.
    No colon prefixes (e.g., use "Niu" not "brain:Niu", use "任飞" not "person:uuid").

    Dedup: entities/relationships that already exist in the graph are skipped,
    and the return message reports which items were skipped.
    """
    try:
        adapter = _get_adapter()

        # --- Dedup entities ---
        input_entities = entities or []
        new_entities = []
        skipped_entity_names = []
        for ent in input_entities:
            ent_name = ent.get("entity_name", "")
            if ent_name and adapter.has_entity(ent_name):
                skipped_entity_names.append(ent_name)
            else:
                new_entities.append(ent)

        # --- Dedup relationships ---
        input_rels = relationships or []
        new_rels = []
        skipped_rel_labels = []
        for rel in input_rels:
            src = rel.get("src_id", "")
            tgt = rel.get("tgt_id", "")
            kw = rel.get("keywords", "")
            if src and tgt and adapter.has_edge(src, tgt, keywords=kw):
                skipped_rel_labels.append(f"{src}->{tgt}({kw})")
            else:
                new_rels.append(rel)

        # Build skip info string
        skip_parts = []
        if skipped_entity_names:
            for ent_name in skipped_entity_names:
                skip_parts.append(
                    f"实体'{ent_name}'已存在。可选操作：\n"
                    f"  - 追加描述：disk(\"/lightrag/lightrag_insert '新描述'\")\n"
                    f"  - 修改描述：disk(\"/lightrag/lightrag_edit_entity '{ent_name}' --description '新描述'\")\n"
                    f"  - 删除重建：disk(\"/lightrag/lightrag_delete_entity '{ent_name}'\")"
                )
        if skipped_rel_labels:
            for rel_label in skipped_rel_labels:
                match = re.match(r"(.+?)->(.+?)\((.+)\)", rel_label)
                if match:
                    src, tgt, kw = match.groups()
                    skip_parts.append(
                        f"关系'{src}'→'{tgt}'({kw})已存在。可选操作：\n"
                        f"  - 修改关系：disk(\"/lightrag/lightrag_edit_relation '{src}' '{tgt}' --keywords '{kw}' --new_description '新描述'\")\n"
                        f"  - 删除关系：disk(\"/lightrag/lightrag_delete_relation '{src}' '{tgt}' --keywords '{kw}'\")"
                    )
                else:
                    skip_parts.append(f"关系'{rel_label}'已存在")
        skip_info = "\n".join(skip_parts)

        # If everything was skipped, return early
        if not new_entities and not new_rels:
            if skip_info:
                return {"status": "ok", "message": f"所有数据已存在，跳过插入。{skip_info}。", "skipped": True}
            # No data was provided at all
            return {"status": "ok", "message": "未提供任何实体或关系数据。"}

        ingester = _get_ingester()
        result = ingester.inject_custom_kg(
            entities=new_entities,
            relationships=new_rels,
            chunks=chunks or [],
            source_id=source_id,
        )

        # Enrich result with skip info
        inserted_e = len(new_entities)
        inserted_r = len(new_rels)
        msg = f"已插入 {inserted_e} 个实体, {inserted_r} 个关系。"
        if skip_info:
            msg += skip_info + "。"

        if isinstance(result, dict):
            result["message"] = msg
            if skipped_entity_names:
                result["skipped_entities"] = skipped_entity_names
            if skipped_rel_labels:
                result["skipped_relationships"] = skipped_rel_labels
            return result
        return {"status": "ok", "message": msg}
    except Exception as e:
        logger.error(f"lightrag_insert_custom_kg failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_entity(
    name: str,
    entity_type: str,
    description: str = "",
    source_id: str = "custom_kg",  # kept for MCP schema compat (deprecated)
    file_path: str = "custom_kg",
    skip_llm_extraction: bool = True,  # deprecated — custom_kg never triggers LLM
) -> Dict[str, Any]:
    """Insert a single entity via ainsert_custom_kg (structured injection).

    Uses inject_custom_kg to bypass LLM auto-extraction, ensuring the
    entity name and type are preserved exactly as specified. Also creates
    a Niu -> entity anchor relationship so the entity is reachable
    from the root.

    Entity names must follow LightRAG's natural language naming system.
    No colon prefixes (e.g., use "Python" not "skill:Python").

    Args:
        name: Entity name (e.g., 'Python', '数据分析', '任飞').
        entity_type: Entity type (e.g., 'Person', 'Concept', 'Skill', 'Tool').
        description: Entity description.
        source_id: Deprecated, kept for backward compatibility (ignored).
        file_path: File path for citation.
        skip_llm_extraction: Deprecated — custom_kg never triggers LLM
            (kept for backward compatibility, callers still pass it).
    """
    # Deprecated params kept for MCP schema compatibility; consumed here to
    # satisfy static analysis (callers still pass them by keyword).
    _ = source_id, skip_llm_extraction
    try:
        # Dedup: skip if entity already exists in the graph
        adapter = _get_adapter()
        if adapter.has_entity(name):
            info = adapter.get_entity_info(name)
            current_desc = ""
            if info.get("status") == "ok":
                data = info.get("data")
                if isinstance(data, dict):
                    current_desc = str(data.get("graph_data", {}).get("description", ""))[:100]
            return {
                "status": "ok",
                "message": f"实体'{name}'已存在（当前描述：{current_desc}）。可选操作：\n"
                           f"1. 追加描述：disk(\"/lightrag/lightrag_insert '新描述内容'\")\n"
                           f"2. 删除重建：disk(\"/lightrag/lightrag_delete_entity '{name}'\") 后重新插入\n"
                           f"3. 修改描述：disk(\"/lightrag/lightrag_edit_entity '{name}' --description '新描述'\")",
                "skipped": True,
                "entity_name": name,
            }

        niu_relation_map = {
            "person": "remembers",
            "skill": "skilled_in",
            "concept": "knows_about",
            "tool": "uses",
            "preference": "prefers",
        }
        niu_relation = niu_relation_map.get((entity_type or "").lower())

        # Build entity dict for inject_custom_kg
        entity = {
            "entity_name": name,
            "entity_type": entity_type,
            "description": description,
            "source_id": file_path,
            "file_path": file_path,
        }

        # Build Niu -> entity anchor relationship (only for types that
        # semantically connect to Niu — Person/Skill/Concept/Tool/Preference)
        relationships = []
        if niu_relation:
            anchor_rel = {
                "src_id": "Niu",
                "tgt_id": name,
                "keywords": niu_relation,
                "description": f"Niu {niu_relation} {name}",
                "source_id": file_path,
                "file_path": file_path,
            }
            relationships.append(anchor_rel)

        ingester = _get_ingester()
        return ingester.inject_custom_kg(
            entities=[entity],
            relationships=relationships,
            chunks=[],
            source_id=file_path,
        )
    except Exception as e:
        logger.error(f"lightrag_insert_entity failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_relation(
    src_id: str,
    tgt_id: str,
    relation: str,
    description: str = "",
    source_id: str = "custom_kg",  # kept for MCP schema compat (deprecated)
    file_path: str = "custom_kg",
) -> Dict[str, Any]:
    """Insert a single relation via ainsert_custom_kg (structured injection).

    Entity names must follow LightRAG's natural language naming system.
    No colon prefixes (e.g., use "Niu" not "brain:Niu").

    Args:
        src_id: Source entity name (natural language).
        tgt_id: Target entity name (natural language).
        relation: Relation type (e.g., skilled_in, is_identical_to).
        description: Relation description.
        source_id: Deprecated, kept for backward compatibility (ignored).
        file_path: File path for citation.
    """
    _ = source_id  # kept for MCP schema compatibility (deprecated)
    try:
        # Dedup: skip if edge (with matching keywords) already exists in the graph
        adapter = _get_adapter()
        if adapter.has_edge(src_id, tgt_id, keywords=relation):
            return {
                "status": "ok",
                "message": f"关系'{src_id}'→'{tgt_id}'({relation})已存在。可选操作：\n"
                           f"1. 修改关系：disk(\"/lightrag/lightrag_edit_relation '{src_id}' '{tgt_id}' --keywords '{relation}' --new_description '新描述'\")\n"
                           f"2. 删除关系：disk(\"/lightrag/lightrag_delete_relation '{src_id}' '{tgt_id}' --keywords '{relation}'\")",
                "skipped": True,
                "source_entity": src_id,
                "target_entity": tgt_id,
                "keywords": relation,
            }

        # Build relationship dict for inject_custom_kg
        rel = {
            "src_id": src_id,
            "tgt_id": tgt_id,
            "keywords": relation,
            "description": description,
            "source_id": file_path,
            "file_path": file_path,
        }

        ingester = _get_ingester()
        return ingester.inject_custom_kg(
            entities=[],
            relationships=[rel],
            chunks=[],
            source_id=file_path,
        )
    except Exception as e:
        logger.error(f"lightrag_insert_relation failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_delete_entity(entity_name: str) -> Dict[str, Any]:
    """Delete an entity and all its relations."""
    try:
        adapter = _get_adapter()
        return adapter.delete_entity(entity_name)
    except Exception as e:
        logger.error(f"lightrag_delete_entity failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_document_status() -> Dict[str, Any]:
    """Get document processing status counts."""
    try:
        adapter = _get_adapter()
        return adapter.document_status()
    except Exception as e:
        logger.error(f"lightrag_document_status failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_get_document(doc_id: str) -> Dict[str, Any]:
    """Get full document content and its processing status."""
    try:
        from niu_api.internal.lightrag_manager import call_async

        adapter = _get_adapter()
        rag = adapter._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not initialized"}
        full_doc = call_async(rag.full_docs.get_by_id(doc_id), timeout=30)
        if full_doc is None:
            return {"status": "not_found", "doc_id": doc_id}
        doc_status_obj = call_async(rag.doc_status.get_by_id(doc_id), timeout=30)
        content = getattr(full_doc, "content", None) or str(full_doc)
        status_str = getattr(doc_status_obj, "status", "unknown") if doc_status_obj else "unknown"
        return {
            "status": "ok",
            "doc_id": doc_id,
            "content": content,
            "doc_status": status_str,
        }
    except Exception as e:
        logger.error(f"lightrag_get_document failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_delete_document(doc_id: str) -> Dict[str, Any]:
    """Cascade delete a document and its associated chunks, entities, relationships."""
    try:
        from niu_api.internal.lightrag_manager import call_async

        adapter = _get_adapter()
        rag = adapter._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not initialized"}
        result = call_async(rag.adelete_by_doc_id(doc_id), timeout=600)
        return {"status": "ok", "doc_id": doc_id, "result": result}
    except Exception as e:
        logger.error(f"lightrag_delete_document failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_list_entities(
    list_type: str = "entities",
    entity_type: str = "",
    limit: int = 50,
) -> Dict[str, Any]:
    """List entities or documents in the knowledge base."""
    valid_list_types = {"entities", "documents", "labels"}
    if list_type not in valid_list_types:
        return {"status": "error", "message": f"Invalid list_type '{list_type}'. Must be one of: {', '.join(sorted(valid_list_types))}"}
    try:
        adapter = _get_adapter()
        return adapter.list_entities(
            list_type=list_type,
            entity_type=entity_type,
            limit=limit,
        )
    except Exception as e:
        logger.error(f"lightrag_list_entities failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_merge_entities(
    source_entities: List[str],
    target_entity: str,
) -> Dict[str, Any]:
    """Merge multiple entities into one."""
    try:
        adapter = _get_adapter()
        return adapter.merge_entities(
            source_entities=source_entities,
            target_entity=target_entity,
        )
    except Exception as e:
        logger.error(f"lightrag_merge_entities failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_edit_entity(
    entity_name: str,
    description: Optional[str] = None,
    entity_type: Optional[str] = None,
    new_name: Optional[str] = None,
    allow_rename: bool = False,
    allow_merge: bool = False,
) -> Dict[str, Any]:
    """Edit entity information in the knowledge graph."""
    try:
        adapter = _get_adapter()
        updated_data = {}
        if description is not None:
            updated_data["description"] = description
        if entity_type is not None:
            updated_data["entity_type"] = entity_type
        if new_name is not None:
            updated_data["entity_name"] = new_name
        if not updated_data:
            return {"status": "error", "message": "No update fields provided"}
        result = adapter.edit_entity(entity_name=entity_name, updated_data=updated_data, allow_rename=allow_rename, allow_merge=allow_merge)
        if result.get("status") == "ok":
            data = result.get("data")
            op_summary = data.get("operation_summary", {}) if isinstance(data, dict) else {}
            msg = f"实体 '{entity_name}' 编辑成功"
            if op_summary.get("renamed"):
                msg = f"实体 '{entity_name}' 已重命名为 '{op_summary.get('final_entity')}'"
            if op_summary.get("merged"):
                msg = f"实体 '{entity_name}' 已合并到 '{op_summary.get('target_entity')}'"
            return {"status": "ok", "message": msg, "data": data}
        return result
    except Exception as e:
        logger.error(f"lightrag_edit_entity failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_edit_relation(
    source_entity: str,
    target_entity: str,
    keywords: Optional[str] = None,
    new_keywords: Optional[str] = None,
    new_description: Optional[str] = None,
    new_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """Edit relation (edge) information between two entities."""
    try:
        adapter = _get_adapter()
        updated_data = {}
        if new_keywords is not None:
            updated_data["keywords"] = new_keywords
        if new_description is not None:
            updated_data["description"] = new_description
        if new_weight is not None:
            updated_data["weight"] = new_weight
        if not updated_data:
            return {"status": "error", "message": "No update fields provided"}
        result = adapter.edit_relation(source_entity=source_entity, target_entity=target_entity, updated_data=updated_data)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}' 编辑成功", "data": result.get("data")}
        return result
    except Exception as e:
        logger.error(f"lightrag_edit_relation failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_delete_relation(
    source_entity: str,
    target_entity: str,
    keywords: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a relation between two entities."""
    try:
        adapter = _get_adapter()
        if keywords:
            info = adapter.get_relation_info(source_entity, target_entity)
            if info.get("status") == "ok":
                data = info.get("data")
                edge_data = data.get("graph_data", {}) if isinstance(data, dict) else {}
                edge_keywords = edge_data.get("keywords", "")
                if keywords not in edge_keywords:
                    return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}' 的 keywords 不匹配 '{keywords}'，未删除", "skipped": True}
        result = adapter.delete_relation(source_entity=source_entity, target_entity=target_entity)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}' 已删除"}
        return result
    except Exception as e:
        logger.error(f"lightrag_delete_relation failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_get_entity_info(
    entity_name: str,
    include_vector_data: bool = False,
) -> Dict[str, Any]:
    """Get detailed information of a single entity."""
    try:
        adapter = _get_adapter()
        result = adapter.get_entity_info(entity_name=entity_name, include_vector_data=include_vector_data)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"实体 '{entity_name}' 信息查询成功", "data": result.get("data")}
        return result
    except Exception as e:
        logger.error(f"lightrag_get_entity_info failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_get_relation_info(
    source_entity: str,
    target_entity: str,
    include_vector_data: bool = False,
) -> Dict[str, Any]:
    """Get detailed information of a relationship between two entities."""
    try:
        adapter = _get_adapter()
        result = adapter.get_relation_info(source_entity=source_entity, target_entity=target_entity, include_vector_data=include_vector_data)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}' 信息查询成功", "data": result.get("data")}
        return result
    except Exception as e:
        logger.error(f"lightrag_get_relation_info failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_create_entity(
    entity_name: str,
    entity_type: str,
    description: str = "",
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
) -> Dict[str, Any]:
    """Create a new entity in the knowledge graph."""
    try:
        adapter = _get_adapter()
        if adapter.has_entity(entity_name):
            return {"status": "ok", "message": f"实体 '{entity_name}' 已存在，无法创建。如需修改请使用 lightrag_edit_entity。", "skipped": True}
        result = adapter.create_entity(entity_name=entity_name, entity_type=entity_type, description=description, source_id=source_id, file_path=file_path)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"实体 '{entity_name}' 创建成功", "data": result.get("data")}
        return result
    except Exception as e:
        logger.error(f"lightrag_create_entity failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_create_relation(
    source_entity: str,
    target_entity: str,
    keywords: str,
    description: str = "",
    weight: float = 1.0,
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
) -> Dict[str, Any]:
    """Create a new relation between two entities."""
    try:
        adapter = _get_adapter()
        if not adapter.has_entity(source_entity):
            return {"status": "error", "message": f"源实体 '{source_entity}' 不存在，请先创建该实体"}
        if not adapter.has_entity(target_entity):
            return {"status": "error", "message": f"目标实体 '{target_entity}' 不存在，请先创建该实体"}
        if adapter.has_edge(source_entity, target_entity, keywords=keywords):
            return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}'({keywords}) 已存在，无法创建。如需修改请使用 lightrag_edit_relation。", "skipped": True}
        result = adapter.create_relation(source_entity=source_entity, target_entity=target_entity, keywords=keywords, description=description, weight=weight, source_id=source_id, file_path=file_path)
        if result.get("status") == "ok":
            return {"status": "ok", "message": f"关系 '{source_entity}'→'{target_entity}'({keywords}) 创建成功", "data": result.get("data")}
        return result
    except Exception as e:
        logger.error(f"lightrag_create_relation failed: {e}")
        return {"status": "error", "message": str(e)}


# ============== call_tool Dispatcher ==============

_TOOL_FUNCTIONS = {
    "lightrag_query": lightrag_query,
    "lightrag_query_data": lightrag_query_data,
    "lightrag_search_entities": lightrag_search_entities,
    "lightrag_get_graph": lightrag_get_graph,
    "lightrag_insert": lightrag_insert,
    "lightrag_insert_file": lightrag_insert_file,
    "lightrag_insert_custom_kg": lightrag_insert_custom_kg,
    "lightrag_insert_entity": lightrag_insert_entity,
    "lightrag_insert_relation": lightrag_insert_relation,
    "lightrag_delete_entity": lightrag_delete_entity,
    "lightrag_delete_document": lightrag_delete_document,
    "lightrag_document_status": lightrag_document_status,
    "lightrag_get_document": lightrag_get_document,
    "lightrag_list_entities": lightrag_list_entities,
    "lightrag_merge_entities": lightrag_merge_entities,
    "lightrag_timeline_query": lightrag_timeline_query,
    "lightrag_edit_entity": lightrag_edit_entity,
    "lightrag_edit_relation": lightrag_edit_relation,
    "lightrag_delete_relation": lightrag_delete_relation,
    "lightrag_get_entity_info": lightrag_get_entity_info,
    "lightrag_get_relation_info": lightrag_get_relation_info,
    "lightrag_create_entity": lightrag_create_entity,
    "lightrag_create_relation": lightrag_create_relation,
}


def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    """Dispatch a tool call by name.

    Args:
        name: Tool name (e.g., "lightrag_query").
        arguments: Dict of tool arguments.

    Returns:
        Tool result.

    Raises:
        ValueError: If tool name is unknown.
    """
    fn = _TOOL_FUNCTIONS.get(name)
    if fn is None:
        raise ValueError(f"Unknown lightrag-server tool: {name}")
    # Filter arguments to only include params the function accepts
    sig = inspect.signature(fn)
    valid_params = set(sig.parameters.keys())
    filtered_args = {k: v for k, v in arguments.items() if k in valid_params}
    return fn(**filtered_args)


# ============== Backward Compatibility Aliases ==============

# DEPRECATED_ALIASES: Mapping of deprecated tool names to current lightrag-server
# tool names. DOCUMENTATION ONLY — does not affect runtime routing.
# Runtime alias resolution is handled by handler.py's _TOOL_ALIASES which uses
# the "server-name/tool-name" format. The keys here use bare tool names for
# documentation and migration reference purposes.
DEPRECATED_ALIASES: Dict[str, str] = {
    # vector-store aliases
    "add_document": "lightrag_insert",
    "search_documents": "lightrag_query",
    "get_document": "lightrag_get_document",
    "delete_document": "lightrag_delete_document",
    "list_documents": "lightrag_list_entities",
    "count_documents": "lightrag_document_status",

    # kg-server aliases
    "create_document": "lightrag_insert",
    "create_entity": "lightrag_insert_entity",
    "create_concept": "lightrag_insert_entity",
    "link_entities": "lightrag_insert_relation",
    "link_document_entity": "lightrag_insert_relation",
    "link_document_concept": "lightrag_insert_relation",
    "explore_node": "lightrag_get_graph",
    "find_path": "lightrag_get_graph",
    "graph_stats": "lightrag_document_status",
    "hub_entities": "lightrag_get_graph",
    "surprising_connections": "lightrag_get_graph",
    "graph_changelog": "lightrag_document_status",
    "list_entities": "lightrag_list_entities",
    "list_concepts": "lightrag_list_entities",
    "graph_snapshot": "lightrag_get_graph",
    "delete_entity": "lightrag_delete_entity",
    "query_graph": "lightrag_query",
    "search_entities": "lightrag_search_entities",
    "get_related_entities": "lightrag_query_data",
    "get_related_concepts": "lightrag_query_data",
    "update_entity_status": "lightrag_document_status",
}
