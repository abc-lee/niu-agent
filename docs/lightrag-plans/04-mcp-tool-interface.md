# 04 - LightRAG MCP Tool Interface Design

> 最后更新：2026-04-22
> 状态：✅ 方案已讨论确认

## Overview

This document designs the new MCP tool interface that replaces the current **vector-store** (7 tools) and **kg-server** (20 tools) with a unified **lightrag-server** built on LightRAG's API surface.

**Current state**: 27 tools across 2 MCP servers, backed by SQLite (vectors.db) + KuzuDB (knowledge.db).
**Target state**: ~12 tools in 1 MCP server, backed by LightRAG (NetworkX + NanoVectorDB + JsonKV).

---

## 1. Current Tool Inventory

### vector-store (7 tools)

| # | Tool | Purpose | LightRAG Equivalent |
|---|------|---------|---------------------|
| 1 | `add_document` | Add document + embedding to SQLite | `lightrag.insert()` |
| 2 | `search_documents` | Semantic vector search | `lightrag.aquery_data(mode="naive")` |
| 3 | `get_document` | Get doc by ID | `lightrag.aget_docs_by_ids()` |
| 4 | `delete_document` | Delete by ID/query/filter | `lightrag.adelete_by_doc_id()` |
| 5 | `list_documents` | List with metadata filter | `lightrag.get_docs_by_status()` |
| 6 | `count_documents` | Count total docs | `lightrag.get_processing_status()` |
| 7 | `update_metadata` | Merge-update metadata | No direct equivalent (see notes) |

### kg-server (20 tools)

| # | Tool | Purpose | LightRAG Equivalent |
|---|------|---------|---------------------|
| 1 | `create_document` | Create Document node in Kuzu | `lightrag.insert()` (auto-extracts entities) |
| 2 | `create_entity` | Create Entity node | `lightrag.acreate_entity()` |
| 3 | `create_concept` | Create Concept node | `lightrag.acreate_entity(entity_type="concept")` |
| 4 | `link_document_entity` | MENTIONS edge | Auto-created by `insert()` |
| 5 | `link_document_concept` | CONTAINS edge | Auto-created by `insert()` |
| 6 | `link_entities` | RELATED_TO edge | `lightrag.acreate_relation()` |
| 7 | `get_document` | Get document by URI | `lightrag.aget_docs_by_ids()` |
| 8 | `list_documents` | List documents | `lightrag.get_docs_by_status()` |
| 9 | `search_documents` | Keyword search in KG | `lightrag.aquery_data(mode="local")` |
| 10 | `get_related_entities` | Entities mentioned in doc | `lightrag.aquery_data(mode="local")` |
| 11 | `get_related_concepts` | Concepts in doc | `lightrag.aquery_data(mode="local")` |
| 12 | `query_graph` | Cypher query on Kuzu | **No equivalent** (LightRAG uses NetworkX, no Cypher) |
| 13 | `explore_node` | BFS from entity | `lightrag.get_knowledge_graph(node_label)` |
| 14 | `find_path` | Shortest path BFS | `lightrag.get_knowledge_graph()` + client-side BFS |
| 15 | `graph_stats` | Node/edge counts, density | `lightrag.get_processing_status()` + graph analysis |
| 16 | `hub_entities` | Degree centrality | `lightrag.get_knowledge_graph()` + client-side ranking |
| 17 | `surprising_connections` | Shared-neighbor discovery | `lightrag.get_knowledge_graph()` + client-side algorithm |
| 18 | `graph_changelog` | Recent changes by timestamp | `lightrag.get_docs_by_status()` (partial) |
| 19 | `list_entities` | List entities by type | `lightrag.get_graph_labels()` + filter |
| 20 | `list_concepts` | List concepts | `lightrag.get_graph_labels()` + filter |
| 21 | `graph_snapshot` | Full graph for visualization | `lightrag.get_knowledge_graph()` |
| 22 | `update_entity_status` | Doc processing status | `lightrag.get_processing_status()` (read-only) |
| 23 | `delete_entity` | Delete entity + edges | `lightrag.adelete_by_entity()` |

> Note: kg-server actually has 20 distinct tool entries in TOOL_SCHEMAS (some were added later like `graph_snapshot`, `update_entity_status`, `delete_entity` which brought it to 23 entries).

---

## 2. LightRAG Public API Surface

### Core Data Operations

```python
# Insert documents (auto-extracts entities + relations via LLM)
await lightrag.ainsert(input: str | list[str], ids=..., file_paths=...)
# Returns: track_id (str) for monitoring

# Query with multiple retrieval modes
await lightrag.aquery(query: str, param: QueryParam) -> str
# Modes: "local" | "global" | "hybrid" | "mix" | "naive" | "bypass"

# Data-only retrieval (no LLM generation)
await lightrag.aquery_data(query: str, param: QueryParam) -> dict
# Returns: {status, message, data: {entities, relationships, chunks, references}, metadata}
```

### Entity/Relation CRUD

```python
# Create
await lightrag.acreate_entity(entity_name: str, entity_data: dict) -> dict
await lightrag.acreate_relation(source_entity: str, target_entity: str, relation_data: dict) -> dict

# Read
await lightrag.get_entity_info(entity_name: str, include_vector_data=False) -> dict
await lightrag.get_relation_info(src_entity: str, tgt_entity: str, include_vector_data=False) -> dict

# Update
await lightrag.aedit_entity(entity_name: str, updated_data: dict, allow_rename=True, allow_merge=False) -> dict
await lightrag.aedit_relation(source_entity: str, target_entity: str, updated_data: dict) -> dict

# Delete
await lightrag.adelete_by_entity(entity_name: str) -> DeletionResult
await lightrag.adelete_by_relation(source_entity: str, target_entity: str) -> DeletionResult
await lightrag.adelete_by_doc_id(doc_id: str) -> DeletionResult

# Merge
await lightrag.amerge_entities(source_entities: list[str], target_entity: str, ...) -> dict
```

### Graph Inspection

```python
# Get subgraph for visualization
await lightrag.get_knowledge_graph(node_label: str, max_depth=3, max_nodes=None) -> KnowledgeGraph
# KnowledgeGraph = {nodes: [KnowledgeGraphNode], edges: [KnowledgeGraphEdge], is_truncated: bool}

# Get all entity labels/types
await lightrag.get_graph_labels() -> list[str]

# Document status tracking
await lightrag.get_docs_by_status(status: str) -> dict
await lightrag.aget_docs_by_ids(ids: list[str]) -> dict
await lightrag.get_processing_status() -> dict[str, int]
```

### QueryParam Configuration

```python
QueryParam(
    mode="mix",           # "local"|"global"|"hybrid"|"mix"|"naive"|"bypass"
    only_need_context=False,  # Return context only, no LLM generation
    top_k=...,            # Number of top items to retrieve
    chunk_top_k=...,      # Number of text chunks to retrieve
    max_entity_tokens=...,  # Token budget for entity context
    max_relation_tokens=..., # Token budget for relation context
    max_total_tokens=...,   # Total token budget
    hl_keywords=[],       # High-level keywords to prioritize
    ll_keywords=[],       # Low-level keywords to refine
    conversation_history=[],  # Past messages for context
    enable_rerank=True,   # Enable reranking of results
    response_type="Multiple Paragraphs",  # Response format
)
```

---

## 3. New Unified Tool Set: `lightrag-server`

### Design Principles

1. **Minimize tool count**: LLM context window is precious; fewer tools = better tool selection.
2. **Preserve core workflows**: Every current usage pattern must be achievable.
3. **Leverage LightRAG strengths**: Hybrid/mix search, automatic entity extraction, merge entities.
4. **Drop KuzuDB-specific tools**: No Cypher queries, no KuzuDB-specific confidence scores.
5. **Client-side graph analytics**: `explore_node`, `hub_entities`, `surprising_connections` computed from `get_knowledge_graph()` output.

### New Tools (12 total)

#### Group A: Document Operations (3 tools)

##### A1. `insert`

**Replaces**: `vector-store/add_document`, `kg-server/create_document`

```python
TOOL_SCHEMAS["insert"] = {
    "name": "insert",
    "description": "Insert document(s) into the knowledge base. LightRAG automatically extracts entities and relations. Use file_path for large content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Document text content (or use file_path)"
            },
            "doc_id": {
                "type": "string",
                "description": "Optional unique document ID (auto-generated if omitted)"
            },
            "file_path": {
                "type": "string",
                "description": "Path to file to read content from (avoids JSON size limits)"
            },
            "split_by_character": {
                "type": "string",
                "description": "Optional character to split content by (e.g., '\\n' for line-split)"
            }
        },
        "required": []
    }
}
```

**Implementation mapping**:
```python
def insert(content="", doc_id=None, file_path="", split_by_character=None):
    # Read file if file_path provided
    if file_path and not content:
        content = _read_file_content(file_path)

    # Call LightRAG sync insert
    track_id = lightrag.insert(
        input=content,
        ids=[doc_id] if doc_id else None,
        file_paths=[file_path] if file_path else None,
        split_by_character=split_by_character
    )
    return {"status": "inserted", "doc_id": doc_id, "track_id": track_id}
```

##### A2. `query`

**Replaces**: `vector-store/search_documents`, `kg-server/search_documents`, `kg-server/get_related_entities`, `kg-server/get_related_concepts`

```python
TOOL_SCHEMAS["query"] = {
    "name": "query",
    "description": "Search the knowledge base. Modes: 'local' (entity-centric), 'global' (relation-centric), 'hybrid' (both), 'mix' (KG + vector), 'naive' (vector only). Returns entities, relationships, and relevant text chunks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "mode": {
                "type": "string",
                "enum": ["local", "global", "hybrid", "mix", "naive"],
                "default": "mix",
                "description": "Retrieval mode"
            },
            "top_k": {
                "type": "integer",
                "default": 5,
                "description": "Number of top results to retrieve"
            },
            "only_context": {
                "type": "boolean",
                "default": True,
                "description": "Return data only (no LLM generation)"
            },
            "response_type": {
                "type": "string",
                "default": "Multiple Paragraphs",
                "description": "Response format when generating (e.g., 'Single Paragraph', 'Bullet Points')"
            }
        },
        "required": ["query"]
    }
}
```

**Implementation mapping**:
```python
def query(query, mode="mix", top_k=5, only_context=True, response_type="Multiple Paragraphs"):
    param = QueryParam(
        mode=mode,
        only_need_context=only_context,
        top_k=top_k,
        response_type=response_type
    )
    if only_context:
        result = lightrag.query_data(query, param=param)
    else:
        result = lightrag.query(query, param=param)
    return result
```

##### A3. `delete_document`

**Replaces**: `vector-store/delete_document`

```python
TOOL_SCHEMAS["delete_document"] = {
    "name": "delete_document",
    "description": "Delete a document and all its derived chunks, entities, and relations from the knowledge base.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Document ID to delete"}
        },
        "required": ["doc_id"]
    }
}
```

**Implementation mapping**:
```python
def delete_document(doc_id):
    result = lightrag.adelete_by_doc_id(doc_id)
    return {"status": result.status, "doc_id": result.doc_id, "message": result.message}
```

#### Group B: Entity & Relation Operations (4 tools)

##### B1. `manage_entity`

**Replaces**: `kg-server/create_entity`, `kg-server/create_concept`, `kg-server/delete_entity`, `kg-server/update_entity_status`

```python
TOOL_SCHEMAS["manage_entity"] = {
    "name": "manage_entity",
    "description": "Create, read, update, or delete an entity. action='create' adds new entity; 'get' retrieves info; 'edit' updates fields; 'delete' removes entity and its relations; 'merge' merges multiple entities into one.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "get", "edit", "delete", "merge"],
                "description": "Operation to perform"
            },
            "entity_name": {"type": "string", "description": "Entity name"},
            "entity_type": {"type": "string", "description": "Entity type (person, organization, technology, concept, location, ...)"},
            "description": {"type": "string", "description": "Entity description (for create/edit)"},
            "updated_data": {"type": "object", "description": "Fields to update (for edit), e.g. {\"description\": \"new\", \"entity_type\": \"PERSON\"}"},
            "source_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Entity names to merge (for merge action)"
            },
            "allow_rename": {"type": "boolean", "default": True, "description": "Allow renaming entity (for edit)"}
        },
        "required": ["action", "entity_name"]
    }
}
```

**Implementation mapping**:
```python
def manage_entity(action, entity_name, entity_type=None, description=None,
                  updated_data=None, source_entities=None, allow_rename=True):
    if action == "create":
        data = {"entity_type": entity_type or "other", "description": description or ""}
        return lightrag.create_entity(entity_name, data)
    elif action == "get":
        return lightrag.get_entity_info(entity_name)
    elif action == "edit":
        data = updated_data or {}
        if description: data["description"] = description
        if entity_type: data["entity_type"] = entity_type
        return lightrag.edit_entity(entity_name, data, allow_rename=allow_rename)
    elif action == "delete":
        return lightrag.delete_by_entity(entity_name)
    elif action == "merge":
        return lightrag.merge_entities(source_entities or [], entity_name)
```

##### B2. `manage_relation`

**Replaces**: `kg-server/link_entities`, `kg-server/link_document_entity`, `kg-server/link_document_concept`

```python
TOOL_SCHEMAS["manage_relation"] = {
    "name": "manage_relation",
    "description": "Create, read, edit, or delete a relation between entities. action='create' adds new relation; 'get' retrieves info; 'edit' updates fields; 'delete' removes relation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "get", "edit", "delete"],
                "description": "Operation to perform"
            },
            "source_entity": {"type": "string", "description": "Source entity name"},
            "target_entity": {"type": "string", "description": "Target entity name"},
            "description": {"type": "string", "description": "Relation description (for create)"},
            "keywords": {"type": "string", "description": "Relation keywords (for create/edit)"},
            "updated_data": {"type": "object", "description": "Fields to update (for edit)"}
        },
        "required": ["action", "source_entity", "target_entity"]
    }
}
```

**Implementation mapping**:
```python
def manage_relation(action, source_entity, target_entity, description=None,
                    keywords=None, updated_data=None):
    if action == "create":
        data = {"description": description or "", "keywords": keywords or ""}
        return lightrag.create_relation(source_entity, target_entity, data)
    elif action == "get":
        return lightrag.get_relation_info(source_entity, target_entity)
    elif action == "edit":
        data = updated_data or {}
        if description: data["description"] = description
        if keywords: data["keywords"] = keywords
        return lightrag.edit_relation(source_entity, target_entity, data)
    elif action == "delete":
        return lightrag.delete_by_relation(source_entity, target_entity)
```

##### B3. `list_entities`

**Replaces**: `kg-server/list_entities`, `kg-server/list_concepts`, `vector-store/list_documents`, `vector-store/count_documents`

```python
TOOL_SCHEMAS["list_entities"] = {
    "name": "list_entities",
    "description": "List entities or documents in the knowledge base. type='entities' lists graph entities; 'documents' lists indexed documents; 'labels' lists all entity type labels.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["entities", "documents", "labels"],
                "default": "entities",
                "description": "What to list"
            },
            "entity_type": {"type": "string", "description": "Filter by entity type (e.g., 'person', 'organization')"},
            "limit": {"type": "integer", "default": 50, "description": "Max results"},
            "status": {"type": "string", "description": "Document status filter (for documents): pending|processing|processed|failed"}
        },
        "required": []
    }
}
```

**Implementation mapping**:
```python
def list_entities(type="entities", entity_type=None, limit=50, status=None):
    if type == "labels":
        return lightrag.get_graph_labels()
    elif type == "documents":
        if status:
            return lightrag.get_docs_by_status(status)
        return lightrag.get_docs_by_status("processed")  # default: show processed docs
    elif type == "entities":
        # LightRAG doesn't have a direct list-all-entities API.
        # Use get_graph_labels() for types, or get_knowledge_graph() for full list.
        # Alternative: query with very broad term
        labels = lightrag.get_graph_labels()
        if entity_type and entity_type not in labels:
            return []
        # Get graph data for the entity_type label
        graph = lightrag.get_knowledge_graph(
            node_label=entity_type or "",
            max_depth=1,
            max_nodes=limit
        )
        return [{"id": n.id, "labels": n.labels, "properties": n.properties}
                for n in graph.nodes]
```

##### B4. `get_document`

**Replaces**: `vector-store/get_document`, `kg-server/get_document`

```python
TOOL_SCHEMAS["get_document"] = {
    "name": "get_document",
    "description": "Get a document by its ID, including content and processing status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Document ID"}
        },
        "required": ["doc_id"]
    }
}
```

**Implementation mapping**:
```python
def get_document(doc_id):
    result = lightrag.aget_docs_by_ids([doc_id])
    return result
```

#### Group C: Graph Exploration (3 tools)

##### C1. `explore_graph`

**Replaces**: `kg-server/explore_node`, `kg-server/graph_snapshot`, `kg-server/find_path`

```python
TOOL_SCHEMAS["explore_graph"] = {
    "name": "explore_graph",
    "description": "Explore the knowledge graph. action='node' returns N-layer neighbors of an entity; 'snapshot' returns full graph for visualization; 'path' finds shortest path between two entities.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["node", "snapshot", "path"],
                "description": "Exploration type"
            },
            "entity_name": {"type": "string", "description": "Center entity name (for node action)"},
            "max_depth": {"type": "integer", "default": 3, "description": "Traversal depth (1-5, default 3)"},
            "max_nodes": {"type": "integer", "default": 200, "description": "Max nodes to return (default 200)"},
            "from_entity": {"type": "string", "description": "Path start entity (for path action)"},
            "to_entity": {"type": "string", "description": "Path end entity (for path action)"},
            "node_label": {"type": "string", "description": "Label filter for snapshot (optional)"}
        },
        "required": ["action"]
    }
}
```

**Implementation mapping**:
```python
def explore_graph(action, entity_name=None, max_depth=3, max_nodes=200,
                  from_entity=None, to_entity=None, node_label=None):
    if action == "node":
        # Use get_knowledge_graph centered on entity
        label = node_label or entity_name or ""
        graph = lightrag.get_knowledge_graph(
            node_label=label,
            max_depth=max_depth,
            max_nodes=max_nodes
        )
        return _format_graph(graph)
    elif action == "snapshot":
        label = node_label or ""
        graph = lightrag.get_knowledge_graph(
            node_label=label,
            max_depth=max_depth,
            max_nodes=max_nodes
        )
        return _format_graph(graph)
    elif action == "path":
        # Get graph containing both entities, then find path client-side
        graph = lightrag.get_knowledge_graph(
            node_label="",  # broad search
            max_depth=max_depth,
            max_nodes=max_nodes
        )
        path = _find_shortest_path(graph, from_entity, to_entity)
        return path
```

**Client-side BFS for `find_path`**:
```python
def _find_shortest_path(graph: KnowledgeGraph, from_id: str, to_id: str) -> dict:
    """BFS shortest path on KnowledgeGraph nodes/edges."""
    # Build adjacency list
    adj = defaultdict(list)
    for edge in graph.edges:
        adj[edge.source].append((edge.target, edge))
        adj[edge.target].append((edge.source, edge))  # undirected

    # BFS
    visited = {from_id}
    queue = deque([(from_id, [])])
    while queue:
        current, path = queue.popleft()
        for neighbor, edge in adj[current]:
            if neighbor == to_id:
                return {"found": True, "hops": len(path) + 1, "path": path + [edge]}
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [edge]))
    return {"found": False, "hops": 0, "path": []}
```

##### C2. `graph_stats`

**Replaces**: `kg-server/graph_stats`, `kg-server/hub_entities`, `kg-server/surprising_connections`

```python
TOOL_SCHEMAS["graph_stats"] = {
    "name": "graph_stats",
    "description": "Get knowledge graph statistics. action='overview' returns node/edge counts; 'hubs' returns top entities by degree; 'surprising' finds entities that share many neighbors but lack direct edges.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["overview", "hubs", "surprising"],
                "default": "overview",
                "description": "Statistics type"
            },
            "limit": {"type": "integer", "default": 10, "description": "Top N results (for hubs)"},
            "min_shared": {"type": "integer", "default": 2, "description": "Min shared neighbors (for surprising)"},
            "max_nodes": {"type": "integer", "default": 200, "description": "Max nodes to analyze (prevents O(n^2))"}
        },
        "required": []
    }
}
```

**Implementation mapping**:
```python
def graph_stats(action="overview", limit=10, min_shared=2, max_nodes=200):
    # Get graph data
    graph = lightrag.get_knowledge_graph(
        node_label="",
        max_depth=1,
        max_nodes=max_nodes
    )
    processing = lightrag.get_processing_status()

    if action == "overview":
        return {
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "is_truncated": graph.is_truncated,
            "processing": processing,
            "node_types": _count_node_types(graph),
        }
    elif action == "hubs":
        return _compute_hub_entities(graph, limit)
    elif action == "surprising":
        return _compute_surprising_connections(graph, min_shared)
```

**Client-side analytics**:
```python
def _compute_hub_entities(graph: KnowledgeGraph, limit: int) -> dict:
    """Degree centrality on returned graph."""
    degree = defaultdict(int)
    for edge in graph.edges:
        degree[edge.source] += 1
        degree[edge.target] += 1
    hubs = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:limit]
    # Enrich with node properties
    node_map = {n.id: n for n in graph.nodes}
    return {"entities": [
        {"id": hid, "label": node_map[hid].labels if hid in node_map else [],
         "connections": cnt}
        for hid, cnt in hubs
    ]}

def _compute_surprising_connections(graph: KnowledgeGraph, min_shared: int) -> dict:
    """Find entity pairs with shared neighbors but no direct edge."""
    # Build adjacency + direct edges
    adj = defaultdict(set)
    direct = set()
    for edge in graph.edges:
        adj[edge.source].add(edge.target)
        adj[edge.target].add(edge.source)
        direct.add((min(edge.source, edge.target), max(edge.source, edge.target)))

    results = []
    nodes = [n.id for n in graph.nodes]
    for i, a in enumerate(nodes):
        for b in nodes[i+1:]:
            if (min(a, b), max(a, b)) in direct:
                continue
            shared = adj[a] & adj[b]
            if len(shared) >= min_shared:
                results.append({
                    "entity1": a, "entity2": b,
                    "shared_neighbors": len(shared),
                    "neighbors": list(shared)[:10]
                })
    results.sort(key=lambda x: x["shared_neighbors"], reverse=True)
    return {"connections": results[:20]}
```

##### C3. `changelog`

**Replaces**: `kg-server/graph_changelog`

```python
TOOL_SCHEMAS["changelog"] = {
    "name": "changelog",
    "description": "Get recent changes to the knowledge base. Returns document processing status changes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 50, "description": "Max results"},
            "status": {"type": "string", "description": "Filter by status: pending|processing|processed|failed"}
        },
        "required": []
    }
}
```

**Implementation mapping**:
```python
def changelog(limit=50, status=None):
    # LightRAG tracks document status but not granular entity/edge changes.
    # Best approximation: return recent document status changes.
    if status:
        docs = lightrag.get_docs_by_status(status)
    else:
        # Get all statuses
        counts = lightrag.get_processing_status()
        all_docs = {}
        for s in ["pending", "processing", "processed", "failed"]:
            all_docs[s] = lightrag.get_docs_by_status(s)
        docs = all_docs
    return docs
```

#### Group D: Utility (2 tools)

##### D1. `update_metadata`

**Replaces**: `vector-store/update_metadata`

```python
TOOL_SCHEMAS["update_metadata"] = {
    "name": "update_metadata",
    "description": "Update metadata fields on a document (merge update, preserves unmentioned fields).",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Document ID"},
            "metadata_updates": {"type": "object", "description": "Fields to update (merged with existing)"}
        },
        "required": ["doc_id", "metadata_updates"]
    }
}
```

**Implementation**: LightRAG stores document metadata via `doc_status` storage. This requires a custom extension since LightRAG's public API doesn't expose metadata updates. Options:
- **Option A**: Access `lightrag.doc_status` storage directly (in-process).
- **Option B**: Re-insert document with same ID (upsert behavior).
- **Recommendation**: Option A for Phase 1, with a PR to LightRAG for a public `aupdate_metadata()` method.

##### D2. `processing_status`

**Replaces**: `vector-store/count_documents`, `kg-server/update_entity_status` (read part)

```python
TOOL_SCHEMAS["processing_status"] = {
    "name": "processing_status",
    "description": "Get document processing status counts (pending, processing, processed, failed) and pipeline status.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
}
```

**Implementation mapping**:
```python
def processing_status():
    return lightrag.get_processing_status()
```

---

## 4. Tool Mapping Summary

### Current -> New (27 tools -> 12 tools)

| Current Tool | New Tool | Notes |
|---|---|---|
| `vector-store/add_document` | `lightrag/insert` | Auto-extracts entities |
| `vector-store/search_documents` | `lightrag/query` (mode="naive") | Hybrid search now available |
| `vector-store/get_document` | `lightrag/get_document` | Same |
| `vector-store/delete_document` | `lightrag/delete_document` | Cascading delete |
| `vector-store/list_documents` | `lightrag/list_entities` (type="documents") | Merged |
| `vector-store/count_documents` | `lightrag/processing_status` | Returns counts by status |
| `vector-store/update_metadata` | `lightrag/update_metadata` | Requires LightRAG extension |
| `kg-server/create_document` | `lightrag/insert` | Auto-extracts entities |
| `kg-server/create_entity` | `lightrag/manage_entity` (action="create") | Merged |
| `kg-server/create_concept` | `lightrag/manage_entity` (action="create", entity_type="concept") | Merged |
| `kg-server/link_document_entity` | *(automatic via insert)* | No manual tool needed |
| `kg-server/link_document_concept` | *(automatic via insert)* | No manual tool needed |
| `kg-server/link_entities` | `lightrag/manage_relation` (action="create") | Merged |
| `kg-server/get_document` | `lightrag/get_document` | Deduplicated |
| `kg-server/list_documents` | `lightrag/list_entities` (type="documents") | Deduplicated |
| `kg-server/search_documents` | `lightrag/query` (mode="local") | Better: KG-aware search |
| `kg-server/get_related_entities` | `lightrag/query` (mode="local") | Returns entities+relations |
| `kg-server/get_related_concepts` | `lightrag/query` (mode="local") | Returns entities+relations |
| `kg-server/query_graph` | *(removed)* | No Cypher in LightRAG |
| `kg-server/explore_node` | `lightrag/explore_graph` (action="node") | Merged |
| `kg-server/find_path` | `lightrag/explore_graph` (action="path") | Client-side BFS |
| `kg-server/graph_stats` | `lightrag/graph_stats` (action="overview") | Merged |
| `kg-server/hub_entities` | `lightrag/graph_stats` (action="hubs") | Client-side ranking |
| `kg-server/surprising_connections` | `lightrag/graph_stats` (action="surprising") | Client-side algorithm |
| `kg-server/graph_changelog` | `lightrag/changelog` | Approximated via doc status |
| `kg-server/list_entities` | `lightrag/list_entities` (type="entities") | Merged |
| `kg-server/list_concepts` | `lightrag/list_entities` (type="entities", entity_type="concept") | Merged |
| `kg-server/graph_snapshot` | `lightrag/explore_graph` (action="snapshot") | Merged |
| `kg-server/update_entity_status` | `lightrag/processing_status` (read) | Write removed (internal) |
| `kg-server/delete_entity` | `lightrag/manage_entity` (action="delete") | Merged |

### Tools with No Direct LightRAG Equivalent

| Tool | Issue | Mitigation |
|---|---|---|
| `query_graph` (Cypher) | LightRAG uses NetworkX, no query language | Use `explore_graph` for traversal; advanced queries need custom NetworkX access |
| `update_entity_status` (write) | LightRAG manages status internally | Remove manual status override; rely on pipeline |
| `link_document_entity` | Auto-created by `insert()` | Remove manual tool; insert handles it |
| `link_document_concept` | Auto-created by `insert()` | Remove manual tool; insert handles it |
| `update_metadata` | No public API | Custom extension via `doc_status` storage |

### New Capabilities Enabled by LightRAG

| Capability | Description | Tool |
|---|---|---|
| **Hybrid search** | Combines entity-centric + relation-centric retrieval | `query` (mode="hybrid") |
| **Mix search** | KG + vector retrieval combined | `query` (mode="mix") |
| **Auto entity extraction** | LLM extracts entities from inserted documents | `insert` |
| **Entity merge** | Merge duplicate/variant entities | `manage_entity` (action="merge") |
| **Entity rename** | Rename entity with cascade | `manage_entity` (action="edit") |
| **Relation edit** | Edit relation description/keywords | `manage_relation` (action="edit") |
| **Query with conversation** | Context-aware retrieval with history | `query` (via QueryParam.conversation_history) |
| **Reranking** | Automatic result reranking | `query` (enable_rerank=True) |

---

## 5. Backward Compatibility Strategy

### Problem

The LLM has been trained on current tool names like `kg-server/explore_node`, `vector-store/search_documents`. Abrupt renaming breaks existing conversations and tool selection patterns.

### Solution: Alias Layer + Deprecation Period

**Phase 1: Dual Registration (Week 1-2)**

Register both old and new tool names in ToolRegistry. Old names are thin wrappers that delegate to new implementations and add a deprecation warning.

```python
# In lightrag-server __init__.py

# New canonical tools
TOOL_SCHEMAS = {
    "insert": {...},
    "query": {...},
    "manage_entity": {...},
    # ... 12 new tools
}

# Backward-compatibility aliases
DEPRECATED_ALIASES = {
    # vector-store aliases
    "add_document": ("insert", {"content": "content", "id": "doc_id", "file_path": "file_path"}),
    "search_documents": ("query", {"query": "query", "limit": "top_k"}),
    "get_document": ("get_document", {"id": "doc_id"}),
    "delete_document": ("delete_document", {"id": "doc_id"}),
    "list_documents": ("list_entities", {}),  # type="documents"
    "count_documents": ("processing_status", {}),
    "update_metadata": ("update_metadata", {"id": "doc_id"}),

    # kg-server aliases
    "create_document": ("insert", {"uri": "doc_id", "title": "content"}),
    "create_entity": ("manage_entity", {"id": "entity_name", "name": "entity_name", "type": "entity_type"}),
    "create_concept": ("manage_entity", {"name": "entity_name"}),  # entity_type="concept"
    "link_entities": ("manage_relation", {"entity1_id": "source_entity", "entity2_id": "target_entity"}),
    "explore_node": ("explore_graph", {"entity_id": "entity_name"}),
    "find_path": ("explore_graph", {"from_id": "from_entity", "to_id": "to_entity"}),
    "graph_stats": ("graph_stats", {}),
    "hub_entities": ("graph_stats", {}),  # action="hubs"
    "surprising_connections": ("graph_stats", {}),  # action="surprising"
    "graph_changelog": ("changelog", {}),
    "list_entities": ("list_entities", {}),
    "list_concepts": ("list_entities", {}),  # entity_type="concept"
    "graph_snapshot": ("explore_graph", {}),  # action="snapshot"
    "delete_entity": ("manage_entity", {"id": "entity_name"}),  # action="delete"
}
```

**Phase 2: Deprecated Warnings (Week 3-6)**

Old tool names still work but tool descriptions include `[DEPRECATED, use lightrag/new-name]` prefix. LLM naturally learns to prefer new names.

**Phase 3: Remove Aliases (Week 7+)**

After LLM has adapted, remove deprecated aliases.

### Agent Definition Updates

Update `config/agents/niu.md`:

```yaml
# Before
mcpServers:
  - vector-store
  - kg-server

# After
mcpServers:
  - lightrag-server
```

No changes needed to `agent/handler.py` dispatch logic -- it already routes `server/tool` format via ToolRegistry generically.

### Tool Visibility Updates

Update `config/mcp-servers.yaml` visibility map:

```yaml
lightrag-server:
  visibility:
    insert: static           # Always available
    query: static            # Always available
    manage_entity: dynamic   # Injected on demand
    manage_relation: dynamic
    list_entities: dynamic
    get_document: dynamic
    explore_graph: dynamic
    graph_stats: dynamic
    changelog: hidden        # Rarely needed by LLM
    update_metadata: dynamic
    processing_status: hidden
```

---

## 6. Graph Visualization API (K6)

### Current State: `niu_api/kg_api.py`

9 FastAPI endpoints that call `niu_kg_server` functions directly:

| Endpoint | Current Implementation | LightRAG Replacement |
|---|---|---|
| `GET /api/kg/snapshot` | `kg.graph_snapshot()` | `lightrag.get_knowledge_graph()` |
| `GET /api/kg/stats` | `kg.graph_stats()` | `lightrag.get_processing_status()` + graph analysis |
| `GET /api/kg/hubs` | `kg.hub_entities()` | `_compute_hub_entities(graph)` |
| `POST /api/kg/explore` | `kg.explore_node()` | `lightrag.get_knowledge_graph(node_label=...)` |
| `POST /api/kg/find-path` | `kg.find_path()` | Client-side BFS on `get_knowledge_graph()` |
| `GET /api/kg/entities` | `kg.list_entities()` | `lightrag.get_knowledge_graph()` + filter |
| `GET /api/kg/concepts` | `kg.list_concepts()` | `lightrag.get_knowledge_graph()` + filter |
| `GET /api/kg/surprising` | `kg.surprising_connections()` | `_compute_surprising_connections(graph)` |
| `GET /api/kg/changelog` | `kg.graph_changelog()` | `lightrag.get_docs_by_status()` |

### New Implementation: `niu_api/kg_api.py`

```python
"""
Knowledge Graph API endpoints for the graph visualization UI.
Routes call LightRAG instance directly (same-process import).
"""

from typing import Literal, Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


class ExploreRequest(BaseModel):
    entity_name: str
    max_depth: int = Field(default=3, ge=1, le=5)
    max_nodes: int = Field(default=200, ge=1, le=500)


class FindPathRequest(BaseModel):
    from_entity: str
    to_entity: str
    max_depth: int = Field(default=5, ge=1, le=10)


def _get_lightrag():
    """Get LightRAG instance (singleton, same-process)."""
    from agent.lightrag_instance import get_lightrag
    return get_lightrag()


@router.get("/snapshot")
async def graph_snapshot(
    node_label: str = Query(default=""),
    max_depth: int = Query(default=3, ge=1, le=5),
    max_nodes: int = Query(default=200, ge=1, le=500),
):
    """Get graph snapshot for visualization."""
    rag = _get_lightrag()
    graph = await rag.get_knowledge_graph(node_label, max_depth, max_nodes)
    return _format_knowledge_graph(graph)


@router.get("/stats")
async def graph_stats():
    """Get knowledge graph statistics."""
    rag = _get_lightrag()
    processing = await rag.get_processing_status()
    graph = await rag.get_knowledge_graph("", max_depth=1, max_nodes=500)
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "is_truncated": graph.is_truncated,
        "processing": processing,
        "node_types": _count_node_types(graph),
    }


@router.get("/hubs")
async def hub_entities(
    limit: int = Query(default=20, ge=1, le=100),
    max_nodes: int = Query(default=200, ge=1, le=500),
):
    """Find hub entities by degree centrality."""
    rag = _get_lightrag()
    graph = await rag.get_knowledge_graph("", max_depth=1, max_nodes=max_nodes)
    return _compute_hub_entities(graph, limit)


@router.post("/explore")
async def explore_node(request: ExploreRequest):
    """Explore graph from a specific entity."""
    rag = _get_lightrag()
    graph = await rag.get_knowledge_graph(
        request.entity_name, request.max_depth, request.max_nodes
    )
    return _format_knowledge_graph(graph)


@router.post("/find-path")
async def find_path(request: FindPathRequest):
    """Find shortest path between two entities."""
    rag = _get_lightrag()
    graph = await rag.get_knowledge_graph(
        "", max_depth=request.max_depth, max_nodes=500
    )
    return _find_shortest_path(graph, request.from_entity, request.to_entity)


@router.get("/entities")
async def list_entities(
    entity_type: Optional[str] = Query(default=None),
    max_nodes: int = Query(default=100, ge=1, le=500),
):
    """List entities, optionally filtered by type."""
    rag = _get_lightrag()
    label = entity_type or ""
    graph = await rag.get_knowledge_graph(label, max_depth=1, max_nodes=max_nodes)
    # Filter by entity_type if specified
    nodes = graph.nodes
    if entity_type:
        nodes = [n for n in nodes if entity_type in n.labels]
    return [{"id": n.id, "labels": n.labels, "properties": n.properties} for n in nodes]


@router.get("/surprising")
async def surprising_connections(
    min_shared: int = Query(default=2, ge=1),
    max_nodes: int = Query(default=200, ge=1, le=1000),
):
    """Find surprising connections between entities."""
    rag = _get_lightrag()
    graph = await rag.get_knowledge_graph("", max_depth=1, max_nodes=max_nodes)
    return _compute_surprising_connections(graph, min_shared)


@router.get("/changelog")
async def graph_changelog(
    limit: int = Query(default=50, ge=1, le=200),
):
    """Get recent document processing changes."""
    rag = _get_lightrag()
    # Return all status buckets as proxy for changelog
    result = {}
    for status in ["pending", "processing", "processed", "failed"]:
        docs = await rag.get_docs_by_status(status)
        result[status] = docs
    return result


@router.get("/labels")
async def graph_labels():
    """Get all entity type labels in the graph."""
    rag = _get_lightrag()
    return await rag.get_graph_labels()
```

### Helper Functions

```python
def _format_knowledge_graph(graph: KnowledgeGraph) -> dict:
    """Convert LightRAG KnowledgeGraph to visualization format."""
    nodes = []
    for n in graph.nodes:
        nodes.append({
            "id": n.id,
            "label": n.labels[0] if n.labels else n.id,
            "nodeType": n.labels[0] if n.labels else "unknown",
            "properties": n.properties
        })
    edges = []
    for e in graph.edges:
        edges.append({
            "source": e.source,
            "target": e.target,
            "type": e.type,
            "properties": e.properties
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges)},
        "is_truncated": graph.is_truncated
    }

def _count_node_types(graph: KnowledgeGraph) -> dict[str, int]:
    """Count nodes by their primary label."""
    counts = {}
    for n in graph.nodes:
        label = n.labels[0] if n.labels else "unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts
```

---

## 7. Async/Sync Bridge Strategy

LightRAG is async-native. Our handler is sync-native. We need a bridge.

### Option: Dedicated Event Loop in Daemon Thread

```python
# agent/lightrag_instance.py

import asyncio
import threading
from lightrag import LightRAG

_instance: LightRAG | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _start_event_loop():
    """Start a dedicated asyncio event loop in a daemon thread."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def get_lightrag() -> LightRAG:
    """Get or create the global LightRAG instance."""
    global _instance, _loop
    if _instance is None:
        # Start daemon thread with event loop
        thread = threading.Thread(target=_start_event_loop, daemon=True)
        thread.start()

        # Initialize LightRAG (async)
        _instance = asyncio.run_coroutine_threadsafe(
            _initialize_lightrag(), _loop
        ).result()
    return _instance


def call_async(coro):
    """Call an async LightRAG method from sync context."""
    global _loop
    if _loop is None:
        raise RuntimeError("LightRAG event loop not started")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


async def _initialize_lightrag() -> LightRAG:
    """Create and initialize LightRAG instance."""
    from lightrag import LightRAG
    from lightrag.llm.openai import openai_complete_if_cache, openai_embed

    rag = LightRAG(
        working_dir=_get_working_dir(),
        llm_model_func=openai_complete_if_cache,
        embedding_func=EmbeddingFunc(
            embedding_dim=384,
            max_token_size=512,
            func=lambda texts: openai_embed(texts, model="text-embedding-3-small")
        ),
        graph_storage="NetworkXStorage",
        kv_storage="JsonKVStorage",
        vector_storage="NanoVectorDBStorage",
        doc_status_storage="JsonDocStatusStorage",
    )
    await rag.initialize_storages()
    return rag
```

### Usage in MCP Server (Sync Wrapper)

```python
# mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py

from agent.lightrag_instance import get_lightrag, call_async

def insert(content="", doc_id=None, file_path="", split_by_character=None):
    rag = get_lightrag()
    if file_path and not content:
        content = _read_file_content(file_path)
    track_id = call_async(rag.ainsert(
        input=content,
        ids=[doc_id] if doc_id else None,
        file_paths=[file_path] if file_path else None,
        split_by_character=split_by_character
    ))
    return {"status": "inserted", "doc_id": doc_id, "track_id": track_id}
```

---

## 8. 讨论确认的决策

| # | 问题 | 决策 | 理由 |
|---|------|------|------|
| 1 | manage_entity/manage_relation 合并CRUD | 先做起来验证，有问题再改 | 工具数量多反而让LLM失焦；合并后描述清晰，LLM更准确 |
| 2 | query_graph(Cypher) 移除 | 移除，用 explore_graph + manage_entity 替代 | LLM写Cypher容易出错；LightRAG用NetworkX不是Cypher；遍历能力够用 |
| 3 | 数据迁移 | 无历史数据迁移，直接替换代码 | LightRAG从空库开始 |

---

## 9. Implementation Checklist

### Phase 1: Core Server

- [ ] Create `mcp-servers/lightrag-server/` directory structure
- [ ] Implement `agent/lightrag_instance.py` (singleton + async bridge)
- [ ] Implement 12 tool functions with TOOL_SCHEMAS
- [ ] Implement `get_tool_schemas()` for ToolRegistry registration
- [ ] Implement `call_tool()` dispatch
- [ ] Add `__main__.py` entry point
- [ ] Update `config/mcp-servers.yaml` with lightrag-server config
- [ ] Register in `agent/mcp_loader.py`

### Phase 2: Backward Compatibility

- [ ] Implement DEPRECATED_ALIASES in lightrag-server
- [ ] Add deprecation warnings to old tool descriptions
- [ ] Update `config/agents/niu.md` to include lightrag-server
- [ ] Keep vector-store and kg-server as optional fallbacks
- [ ] Test all existing agent workflows with new tools

### Phase 3: Graph Visualization API

- [ ] Rewrite `niu_api/kg_api.py` to use LightRAG
- [ ] Implement client-side analytics helpers
- [ ] Update frontend graph visualization to handle new data format
- [ ] Test all 9 API endpoints

### Phase 4: Cleanup

- [ ] Remove deprecated aliases
- [ ] Remove vector-store and kg-server from config
- [ ] Update CLAUDE.md and AGENTS.md documentation
- [ ] Archive old MCP server code

---

## 10. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| LightRAG async/sync conflict | HIGH | Daemon thread event loop + `call_async()` bridge |
| Entity extraction quality differs | MEDIUM | A/B test with existing data; tune LightRAG prompts |
| `get_knowledge_graph()` performance with large graphs | MEDIUM | `max_nodes` parameter; pagination support |
| Tool name change confuses LLM | MEDIUM | Alias layer with deprecation period |
| `update_metadata` missing from LightRAG | LOW | Custom extension via `doc_status` storage |
| LightRAG insert is slow (LLM extraction) | MEDIUM | Background processing; track_id for async monitoring |
