"""
LightRAG Unified MCP Server

Replaces vector-store (7 tools) and kg-server (20 tools) with 16 unified tools
that delegate to LightRAGAdapter and LightRAGIngester.

Tool groups:
- Query (5): lightrag_query, lightrag_query_data, lightrag_search_entities, lightrag_get_graph, lightrag_timeline_query
- Insert (5): lightrag_insert, lightrag_insert_file, lightrag_insert_custom_kg, lightrag_insert_entity, lightrag_insert_relation
- Manage (6): lightrag_delete_document, lightrag_delete_entity, lightrag_document_status, lightrag_get_document, lightrag_list_entities, lightrag_merge_entities
"""

from typing import Any, Dict, List, Optional
import inspect
import threading

from loguru import logger

from niu_api.internal.lightrag_adapter import LightRAGAdapter


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
        "description": "Insert a single entity into the knowledge graph with precise control.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entity name"},
                "entity_type": {"type": "string", "description": "Entity type (e.g., person, concept, skill)"},
                "description": {"type": "string", "default": "", "description": "Entity description"},
                "source_id": {"type": "string", "default": "custom_kg", "description": "Source ID"},
                "file_path": {"type": "string", "default": "custom_kg", "description": "File path for citation"},
            },
            "required": ["name", "entity_type"],
        },
    },

    "lightrag_insert_relation": {
        "name": "lightrag_insert_relation",
        "description": "Insert a relation between two entities in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "src_id": {"type": "string", "description": "Source entity name"},
                "tgt_id": {"type": "string", "description": "Target entity name"},
                "relation": {"type": "string", "description": "Relation type (e.g., has_framework, USED_FOR)"},
                "description": {"type": "string", "default": "", "description": "Relation description"},
                "source_id": {"type": "string", "default": "custom_kg", "description": "Source ID"},
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
) -> Dict[str, Any]:
    """Search for entities of a specific type."""
    try:
        adapter = _get_adapter()
        result = adapter.query_data(query=query, mode="local", top_k=top_k)
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
    chunks it, and builds the knowledge graph asynchronously.
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag, call_async
        from pathlib import Path as _Path

        rag = get_lightrag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        from lightrag.api.routers.document_routes import pipeline_enqueue_file

        success, track_id = call_async(
            pipeline_enqueue_file(rag, _Path(file_path), track_id=doc_id or ""),
            timeout=600,
        )

        # Record changelog
        try:
            from niu_api.internal.lightrag_manager import get_change_log
            get_change_log().record_change("document_created", {
                "id": doc_id or "",
                "uri": file_path,
                "title": _Path(file_path).name,
                "source": "lightrag_insert_file",
            })
        except Exception:
            pass

        return {"status": "ok" if success else "error", "track_id": track_id}
    except Exception as e:
        logger.error(f"lightrag_insert_file failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_custom_kg(
    entities: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    source_id: str = "custom_kg",
) -> Dict[str, Any]:
    """Inject structured knowledge directly."""
    try:
        ingester = _get_ingester()
        return ingester.inject_custom_kg(
            entities=entities or [],
            relationships=relationships or [],
            chunks=chunks or [],
            source_id=source_id,
        )
    except Exception as e:
        logger.error(f"lightrag_insert_custom_kg failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_entity(
    name: str,
    entity_type: str,
    description: str = "",
    source_id: str = "custom_kg",
    file_path: str = "custom_kg",
) -> Dict[str, Any]:
    """Insert a single entity via inject_custom_kg (inject_entity removed)."""
    try:
        ingester = _get_ingester()
        return ingester.inject_custom_kg(
            entities=[{
                "entity_name": name,
                "entity_type": entity_type,
                "description": description,
            }],
            relationships=[],
            chunks=[],
            source_id=source_id,
        )
    except Exception as e:
        logger.error(f"lightrag_insert_entity failed: {e}")
        return {"status": "error", "message": str(e)}


def lightrag_insert_relation(
    src_id: str,
    tgt_id: str,
    relation: str,
    description: str = "",
    source_id: str = "custom_kg",
    file_path: str = "custom_kg",
) -> Dict[str, Any]:
    """Insert a single relation via inject_custom_kg (inject_relation removed)."""
    try:
        ingester = _get_ingester()
        return ingester.inject_custom_kg(
            entities=[],
            relationships=[{
                "src_id": src_id,
                "tgt_id": tgt_id,
                "keywords": relation,
                "description": description,
                "source_id": source_id,
                "file_path": file_path,
            }],
            chunks=[],
            source_id=source_id,
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
        full_doc = call_async(rag.full_docs.get_by_id(doc_id))
        if full_doc is None:
            return {"status": "not_found", "doc_id": doc_id}
        doc_status_obj = call_async(rag.doc_status.get_by_id(doc_id))
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
