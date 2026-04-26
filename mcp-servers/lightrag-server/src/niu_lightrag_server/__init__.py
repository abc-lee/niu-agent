"""
LightRAG Unified MCP Server

Replaces vector-store (7 tools) and kg-server (20 tools) with 14 unified tools
that delegate to LightRAGAdapter and LightRAGIngester.

Tool groups:
- Query (4): lightrag_query, lightrag_query_data, lightrag_search_entities, lightrag_get_graph
- Insert (4): lightrag_insert, lightrag_insert_custom_kg, lightrag_insert_entity, lightrag_insert_relation
- Manage (6): lightrag_delete_document, lightrag_delete_entity, lightrag_document_status, lightrag_get_document, lightrag_list_entities, lightrag_merge_entities
"""

from typing import Any, Dict, List, Optional
import inspect
import threading

from loguru import logger


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
            "Query the knowledge base returning structured data (entities + relationships). "
            "Unlike lightrag_query which returns text, this returns structured JSON "
            "with entity_type, description, and relationship details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "mode": {
                    "type": "string",
                    "enum": ["naive", "local", "global", "hybrid", "mix", "bypass"],
                    "default": "local",
                    "description": "Retrieval mode (default: local for entity-focused; 'bypass' skips retrieval)",
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
            },
            "required": ["action"],
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
    top_k: int = 10,
):
    """Query returning structured data (entities + relationships)."""
    valid_modes = {"naive", "local", "global", "hybrid", "mix", "bypass"}
    if mode not in valid_modes:
        return {"status": "error", "message": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"}
    try:
        adapter = _get_adapter()
        result = adapter.query_data(query=query, mode=mode, top_k=top_k)
        if result is None:
            return {"status": "ok", "data": {}}
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
        if result is None:
            return {"status": "ok", "data": []}
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
            return adapter.explore_node(entity_name=entity_name, depth=depth)
        else:  # snapshot
            return adapter.get_graph_snapshot(limit=limit)
    except Exception as e:
        logger.error(f"lightrag_get_graph failed: {e}")
        return {"status": "error", "message": str(e), "nodes": [], "edges": [], "center": None, "stats": {}}


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
    """Insert a single entity."""
    try:
        ingester = _get_ingester()
        return ingester.inject_entity(
            name=name,
            entity_type=entity_type,
            description=description,
            source_id=source_id,
            file_path=file_path,
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
    """Insert a relation between two entities."""
    try:
        ingester = _get_ingester()
        return ingester.inject_relation(
            src_id=src_id,
            tgt_id=tgt_id,
            relation=relation,
            description=description,
            source_id=source_id,
            file_path=file_path,
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
        result = call_async(rag.adelete_by_doc_id(doc_id))
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
    "lightrag_insert_custom_kg": lightrag_insert_custom_kg,
    "lightrag_insert_entity": lightrag_insert_entity,
    "lightrag_insert_relation": lightrag_insert_relation,
    "lightrag_delete_entity": lightrag_delete_entity,
    "lightrag_delete_document": lightrag_delete_document,
    "lightrag_document_status": lightrag_document_status,
    "lightrag_get_document": lightrag_get_document,
    "lightrag_list_entities": lightrag_list_entities,
    "lightrag_merge_entities": lightrag_merge_entities,
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
