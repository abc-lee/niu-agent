# Knowledge Graph Frontend Data Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the graph visualization frontend to real kg-server backend data, replacing hardcoded sample data.

**Architecture:** Add FastAPI routes that call niu_kg_server functions directly (same-process import, like ToolRegistry). Add Electron IPC bridge (preload.js + main.js) following the assistant window pattern. Replace renderer.js hardcoded data with async API calls.

**Tech Stack:** Python/FastAPI (backend routes), Electron/Node.js (IPC bridge), AntV G6 v4 (graph visualization), KuzuDB (graph database)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `mcp-servers/kg-server/src/niu_kg_server/__init__.py` | Modify | Add list_entities, list_concepts, graph_snapshot functions + TOOL_SCHEMAS + call_tool entries |
| `mcp-servers/kg-server/tests/test_new_functions.py` | Create | Tests for list_entities, list_concepts, graph_snapshot |
| `niu_api/kg_api.py` | Create | FastAPI routes that call kg-server functions directly |
| `niu_api/__main__.py` | Modify | Register kg_api router |
| `ui/graph/preload.js` | Rewrite | contextBridge API for kg endpoints |
| `ui/graph/main.js` | Rewrite | IPC handlers + HTTP requests to FastAPI |
| `ui/graph/renderer.js` | Modify | Replace sampleData with API calls, update node/edge processing |

---

### Task 1: Add list_entities and list_concepts to kg-server

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py` (after line 1297, after `list_documents`)
- Create: `mcp-servers/kg-server/tests/test_new_functions.py`

- [ ] **Step 1: Write the failing tests**

```python
# mcp-servers/kg-server/tests/test_new_functions.py
"""Tests for list_entities, list_concepts, and graph_snapshot functions."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import (
    _init_schema, create_entity, create_concept, create_document,
    link_document_entity, link_document_concept, link_entities,
)


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_list_entities_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            result = niu_kg_server.list_entities()
            assert result == []
        finally:
            niu_kg_server._conn = orig


def test_list_entities_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            create_entity("e1", "Alice", "person")
            create_entity("e2", "Bob", "person")
            create_entity("org1", "Acme", "organization")

            result = niu_kg_server.list_entities()
            assert len(result) == 3
            assert result[0]["name"] in ("Alice", "Bob", "Acme")

            # Filter by type
            persons = niu_kg_server.list_entities(entity_type="person")
            assert len(persons) == 2
            assert all(e["type"] == "person" for e in persons)
        finally:
            niu_kg_server._conn = orig


def test_list_concepts_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            create_concept("Machine Learning", description="ML concepts")
            create_concept("Deep Learning", description="DL subset of ML")

            result = niu_kg_server.list_concepts()
            assert len(result) == 2
            assert result[0]["name"] in ("Machine Learning", "Deep Learning")
        finally:
            niu_kg_server._conn = orig


def test_graph_snapshot_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            result = niu_kg_server.graph_snapshot()
            assert result["nodes"] == []
            assert result["edges"] == []
            assert result["stats"]["nodes"] == 0
            assert result["stats"]["edges"] == 0
        finally:
            niu_kg_server._conn = orig


def test_graph_snapshot_with_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            # Create entities and relations
            create_entity("alice", "Alice", "person")
            create_entity("bob", "Bob", "person")
            create_entity("acme", "Acme Corp", "organization")
            link_entities("alice", "bob", "KNOWS", confidence=0.9)
            link_entities("alice", "acme", "WORKS_AT", confidence=1.0)

            # Create document linked to entity
            create_document("doc1", "Meeting Notes", content="Alice met Bob")
            link_document_entity("doc1", "alice", confidence=0.8)

            # Create concept linked to document
            create_concept("Project X")
            link_document_concept("doc1", "Project X", confidence=0.7)

            result = niu_kg_server.graph_snapshot()

            # Should have Entity, Document, and Concept nodes
            node_types = {n["nodeType"] for n in result["nodes"]}
            assert "Entity" in node_types
            assert "Document" in node_types
            assert "Concept" in node_types

            # Should have RELATED_TO, MENTIONS, CONTAINS edges
            edge_types = {e["edgeType"] for e in result["edges"]}
            assert "RELATED_TO" in edge_types
            assert "MENTIONS" in edge_types
            assert "CONTAINS" in edge_types

            # Stats should match
            assert result["stats"]["nodes"] == len(result["nodes"])
            assert result["stats"]["edges"] == len(result["edges"])
        finally:
            niu_kg_server._conn = orig
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd E:/tools/ai-bot/mcp-servers/kg-server && python -m pytest tests/test_new_functions.py -v`
Expected: FAIL (functions not defined)

- [ ] **Step 3: Implement list_entities, list_concepts, and graph_snapshot**

Add after `list_documents` (line 1297) in `mcp-servers/kg-server/src/niu_kg_server/__init__.py`:

```python
def list_entities(limit: int = 100, entity_type: str | None = None) -> list[dict[str, Any]]:
    """List all entities, optionally filtered by type.

    Args:
        limit: Maximum results (default: 100, max: 500)
        entity_type: Optional entity type filter (e.g., "person", "organization")

    Returns:
        List of entity dicts with id, name, type, description, created_at
    """
    conn = get_connection()
    limit = max(1, min(500, int(limit)))

    if entity_type:
        result = conn.execute(
            "MATCH (e:Entity) WHERE e.type = $etype RETURN e.id, e.name, e.type, e.description, e.created_at ORDER BY e.created_at DESC LIMIT $limit",
            {"etype": entity_type, "limit": limit},
        )
    else:
        result = conn.execute(
            f"MATCH (e:Entity) RETURN e.id, e.name, e.type, e.description, e.created_at ORDER BY e.created_at DESC LIMIT {limit}"
        )

    entities = []
    while result.has_next():
        row = result.get_next()
        entities.append({
            "id": row[0], "name": row[1], "type": row[2],
            "description": row[3], "created_at": row[4]
        })
    return entities


def list_concepts(limit: int = 100) -> list[dict[str, Any]]:
    """List all concepts.

    Args:
        limit: Maximum results (default: 100, max: 500)

    Returns:
        List of concept dicts with name, description, created_at
    """
    conn = get_connection()
    limit = max(1, min(500, int(limit)))
    result = conn.execute(
        f"MATCH (c:Concept) RETURN c.name, c.description, c.created_at ORDER BY c.created_at DESC LIMIT {limit}"
    )

    concepts = []
    while result.has_next():
        row = result.get_next()
        concepts.append({"name": row[0], "description": row[1], "created_at": row[2]})
    return concepts


def graph_snapshot(limit: int = 200, min_confidence: float = 0.0) -> dict[str, Any]:
    """Get a full graph snapshot for visualization.

    Returns Entity nodes with RELATED_TO edges, Document nodes connected
    via MENTIONS, and Concept nodes connected via CONTAINS.

    Args:
        limit: Max entity nodes to return (default: 200)
        min_confidence: Minimum confidence filter (0.0-1.0)

    Returns:
        {"nodes": [...], "edges": [...], "stats": {"nodes": N, "edges": M}}
    """
    conn = get_connection()
    min_confidence = max(0.0, min(1.0, float(min_confidence)))
    nodes = []
    edges = []
    node_ids = set()

    # 1. Entity nodes
    entity_result = conn.execute(
        f"MATCH (e:Entity) RETURN e.id, e.name, e.type, e.description LIMIT {limit}"
    )
    while entity_result.has_next():
        row = entity_result.get_next()
        node_id = f"entity:{row[0]}"
        nodes.append({
            "id": node_id, "label": row[1], "nodeType": "Entity",
            "entityType": row[2], "description": row[3] or ""
        })
        node_ids.add(node_id)

    # 2. RELATED_TO edges (Entity -> Entity)
    rel_result = conn.execute(
        "MATCH (e1:Entity)-[r:RELATED_TO]->(e2:Entity) "
        "WHERE r.confidence >= $min_conf "
        "RETURN e1.id, e2.id, r.relation, r.confidence",
        {"min_conf": min_confidence}
    )
    while rel_result.has_next():
        row = rel_result.get_next()
        src = f"entity:{row[0]}"
        tgt = f"entity:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "relation": row[2], "confidence": row[3],
                "edgeType": "RELATED_TO"
            })

    # 3. Document nodes (connected to entities via MENTIONS)
    doc_result = conn.execute(
        "MATCH (d:Document)-[:MENTIONS]->(e:Entity) "
        "RETURN DISTINCT d.uri, d.title, d.source"
    )
    while doc_result.has_next():
        row = doc_result.get_next()
        doc_id = f"doc:{row[0]}"
        nodes.append({
            "id": doc_id, "label": row[1] or row[0], "nodeType": "Document",
            "source": row[2] or ""
        })
        node_ids.add(doc_id)

    # 4. MENTIONS edges (Document -> Entity)
    mentions_result = conn.execute(
        "MATCH (d:Document)-[r:MENTIONS]->(e:Entity) "
        "WHERE r.confidence >= $min_conf "
        "RETURN d.uri, e.id, r.confidence",
        {"min_conf": min_confidence}
    )
    while mentions_result.has_next():
        row = mentions_result.get_next()
        src = f"doc:{row[0]}"
        tgt = f"entity:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "confidence": row[2], "edgeType": "MENTIONS"
            })

    # 5. Concept nodes (connected to documents via CONTAINS)
    concept_result = conn.execute(
        "MATCH (d:Document)-[:CONTAINS]->(c:Concept) "
        "RETURN DISTINCT c.name, c.description"
    )
    while concept_result.has_next():
        row = concept_result.get_next()
        concept_id = f"concept:{row[0]}"
        nodes.append({
            "id": concept_id, "label": row[0], "nodeType": "Concept",
            "description": row[1] or ""
        })
        node_ids.add(concept_id)

    # 6. CONTAINS edges (Document -> Concept)
    contains_result = conn.execute(
        "MATCH (d:Document)-[r:CONTAINS]->(c:Concept) "
        "WHERE r.confidence >= $min_conf "
        "RETURN d.uri, c.name, r.confidence",
        {"min_conf": min_confidence}
    )
    while contains_result.has_next():
        row = contains_result.get_next()
        src = f"doc:{row[0]}"
        tgt = f"concept:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "confidence": row[2], "edgeType": "CONTAINS"
            })

    return {
        "nodes": nodes, "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges)}
    }
```

Add to `TOOL_SCHEMAS` dict (after `graph_changelog` entry):

```python
    "list_entities": {
        "name": "list_entities",
        "description": "List all entities in the knowledge graph, optionally filtered by type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                "entity_type": {"type": "string", "description": "Optional entity type filter (e.g., person, organization)"},
            },
        },
    },
    "list_concepts": {
        "name": "list_concepts",
        "description": "List all concepts in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
            },
        },
    },
    "graph_snapshot": {
        "name": "graph_snapshot",
        "description": "Get a full graph snapshot for visualization. Returns nodes and edges across all types.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entity nodes (default: 200)"},
                "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
            },
        },
    },
```

Add to `list_tools()` (after `graph_changelog` Tool):

```python
        Tool(
            name="list_entities",
            description="List all entities in the knowledge graph, optionally filtered by type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                    "entity_type": {"type": "string", "description": "Optional entity type filter"},
                },
            },
        ),
        Tool(
            name="list_concepts",
            description="List all concepts in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                },
            },
        ),
        Tool(
            name="graph_snapshot",
            description="Get a full graph snapshot for visualization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max entity nodes (default: 200)"},
                    "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
                },
            },
        ),
```

Add to `call_tool()` handler (before the `else: return [TextContent...]` line):

```python
        elif name == "list_entities":
            result = list_entities(
                limit=arguments.get("limit", 100),
                entity_type=arguments.get("entity_type"),
            )
        elif name == "list_concepts":
            result = list_concepts(limit=arguments.get("limit", 100))
        elif name == "graph_snapshot":
            result = graph_snapshot(
                limit=arguments.get("limit", 200),
                min_confidence=arguments.get("min_confidence", 0.0),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd E:/tools/ai-bot/mcp-servers/kg-server && python -m pytest tests/test_new_functions.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `cd E:/tools/ai-bot/mcp-servers/kg-server && python -m pytest tests/ -v`
Expected: All tests PASS (52 existing + 5 new = 57)

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/kg-server/src/niu_kg_server/__init__.py mcp-servers/kg-server/tests/test_new_functions.py
git commit -m "feat(kg-server): add list_entities, list_concepts, graph_snapshot for graph visualization"
```

---

### Task 2: Add FastAPI routes for kg-server

**Files:**
- Create: `niu_api/kg_api.py`
- Modify: `niu_api/__main__.py` (add import at line 30, add router at line 166)

- [ ] **Step 1: Create kg_api.py with all routes**

```python
"""
Knowledge Graph API endpoints for the graph visualization UI.

Routes call niu_kg_server functions directly (same-process import, like ToolRegistry).
"""

from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel
from loguru import logger

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


class ExploreRequest(BaseModel):
    entity_id: str
    depth: int = 2
    min_confidence: float = 0.0
    direction: str = "both"


class FindPathRequest(BaseModel):
    from_id: str
    to_id: str
    max_depth: int = 5


def _get_kg():
    """Import niu_kg_server module (in-process, same as ToolRegistry)."""
    import niu_kg_server
    return niu_kg_server


@router.get("/snapshot")
async def graph_snapshot(limit: int = Query(default=200, ge=1, le=500), min_confidence: float = Query(default=0.0, ge=0.0, le=1.0)):
    """Get full graph snapshot for visualization."""
    kg = _get_kg()
    return kg.graph_snapshot(limit=limit, min_confidence=min_confidence)


@router.get("/stats")
async def graph_stats():
    """Get knowledge graph statistics."""
    kg = _get_kg()
    return kg.graph_stats()


@router.get("/hubs")
async def hub_entities(limit: int = Query(default=20, ge=1, le=100), min_confidence: float = Query(default=0.0, ge=0.0, le=1.0)):
    """Find hub entities by connection count."""
    kg = _get_kg()
    return kg.hub_entities(limit=limit, min_confidence=min_confidence)


@router.post("/explore")
async def explore_node(request: ExploreRequest):
    """Explore graph from a specific entity."""
    kg = _get_kg()
    return kg.explore_node(
        entity_id=request.entity_id,
        depth=request.depth,
        min_confidence=request.min_confidence,
        direction=request.direction,
    )


@router.post("/find-path")
async def find_path(request: FindPathRequest):
    """Find shortest path between two entities."""
    kg = _get_kg()
    return kg.find_path(
        from_id=request.from_id,
        to_id=request.to_id,
        max_depth=request.max_depth,
    )


@router.get("/entities")
async def list_entities(limit: int = Query(default=100, ge=1, le=500), entity_type: Optional[str] = Query(default=None)):
    """List all entities."""
    kg = _get_kg()
    return kg.list_entities(limit=limit, entity_type=entity_type)


@router.get("/concepts")
async def list_concepts(limit: int = Query(default=100, ge=1, le=500)):
    """List all concepts."""
    kg = _get_kg()
    return kg.list_concepts(limit=limit)


@router.get("/surprising")
async def surprising_connections(
    min_shared: int = Query(default=2, ge=1),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    max_entities: int = Query(default=200, ge=1, le=1000),
):
    """Find surprising connections between entities."""
    kg = _get_kg()
    return kg.surprising_connections(
        min_shared=min_shared,
        min_confidence=min_confidence,
        max_entities=max_entities,
    )


@router.get("/changelog")
async def graph_changelog(limit: int = Query(default=50, ge=1, le=200), since: Optional[str] = Query(default=None)):
    """Get recent graph changes."""
    kg = _get_kg()
    return kg.graph_changelog(limit=limit, since=since)
```

- [ ] **Step 2: Register the router in __main__.py**

Add import at line 30 (after `from niu_api.alerts_api import router as alerts_router`):

```python
from niu_api.kg_api import router as kg_router
```

Add router registration at line 166 (after `app.include_router(alerts_router)`):

```python
app.include_router(kg_router)  # Knowledge Graph API
```

- [ ] **Step 3: Verify the API starts**

Run: `cd E:/tools/ai-bot && python -c "from niu_api.kg_api import router; print(f'KG API routes: {len(router.routes)}')"` 
Expected: `KG API routes: 9`

- [ ] **Step 4: Commit**

```bash
git add niu_api/kg_api.py niu_api/__main__.py
git commit -m "feat(api): add /api/kg routes for knowledge graph visualization"
```

---

### Task 3: Add Electron IPC bridge (preload.js + main.js)

**Files:**
- Rewrite: `ui/graph/preload.js`
- Rewrite: `ui/graph/main.js`

- [ ] **Step 1: Rewrite preload.js with contextBridge**

```javascript
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Graph data
  getGraphSnapshot: (limit, minConfidence) =>
    ipcRenderer.invoke('kg-snapshot', limit, minConfidence),
  getGraphStats: () => ipcRenderer.invoke('kg-stats'),
  getHubEntities: (limit) => ipcRenderer.invoke('kg-hubs', limit),
  exploreNode: (entityId, depth, minConfidence, direction) =>
    ipcRenderer.invoke('kg-explore', entityId, depth, minConfidence, direction),
  findPath: (fromId, toId) => ipcRenderer.invoke('kg-find-path', fromId, toId),
  listEntities: (limit, entityType) => ipcRenderer.invoke('kg-entities', limit, entityType),
  listConcepts: (limit) => ipcRenderer.invoke('kg-concepts', limit),
  getSurprisingConnections: (minShared) => ipcRenderer.invoke('kg-surprising', minShared),
});
```

- [ ] **Step 2: Rewrite main.js with IPC handlers**

```javascript
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const http = require('http');

const API_HOST = '127.0.0.1';
const API_PORT = 9876;

function apiRequest(method, apiPath, body = null) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: API_HOST,
      port: API_PORT,
      path: apiPath,
      method: method,
      headers: { 'Content-Type': 'application/json' },
      timeout: 30000,
    };
    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error(`Parse error: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile('index.html');
}

// ========== IPC Handlers ==========

ipcMain.handle('kg-snapshot', async (event, limit, minConfidence) => {
  const params = new URLSearchParams({ limit: limit || 200, min_confidence: minConfidence || 0 });
  return apiRequest('GET', `/api/kg/snapshot?${params}`);
});

ipcMain.handle('kg-stats', async () => {
  return apiRequest('GET', '/api/kg/stats');
});

ipcMain.handle('kg-hubs', async (event, limit) => {
  return apiRequest('GET', `/api/kg/hubs?limit=${limit || 20}`);
});

ipcMain.handle('kg-explore', async (event, entityId, depth, minConfidence, direction) => {
  return apiRequest('POST', '/api/kg/explore', {
    entity_id: entityId, depth: depth || 2,
    min_confidence: minConfidence || 0, direction: direction || 'both',
  });
});

ipcMain.handle('kg-find-path', async (event, fromId, toId) => {
  return apiRequest('POST', '/api/kg/find-path', { from_id: fromId, to_id: toId });
});

ipcMain.handle('kg-entities', async (event, limit, entityType) => {
  const params = new URLSearchParams({ limit: limit || 100 });
  if (entityType) params.set('entity_type', entityType);
  return apiRequest('GET', `/api/kg/entities?${params}`);
});

ipcMain.handle('kg-concepts', async (event, limit) => {
  return apiRequest('GET', `/api/kg/concepts?limit=${limit || 100}`);
});

ipcMain.handle('kg-surprising', async (event, minShared) => {
  return apiRequest('GET', `/api/kg/surprising?min_shared=${minShared || 2}`);
});

// ========== App Lifecycle ==========

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
```

- [ ] **Step 3: Commit**

```bash
git add ui/graph/preload.js ui/graph/main.js
git commit -m "feat(graph): add Electron IPC bridge for kg-server API"
```

---

### Task 4: Replace hardcoded data in renderer.js with API calls

**Files:**
- Modify: `ui/graph/renderer.js`

This is the largest change. The key modifications:

1. Delete `sampleData` (lines 76-124)
2. Add node type mapping from backend format to frontend visual types
3. Replace immediate `graph.data(processedData); graph.render()` with async `loadGraphSnapshot()`
4. Update `processNodes` and `processEdges` to work with backend data format
5. Update detail panel to use `_originalData` stored on G6 node model
6. Update search and filter to work with backend data
7. Add double-click to expand node neighborhood via `exploreNode`
8. Add loading/empty states

- [ ] **Step 1: Replace renderer.js with API-driven version**

The new renderer.js keeps the same G6 config and nodeConfigs, but replaces data loading and updates all references from `sampleData` to `currentData`:

```javascript
// Node type configuration (unchanged)
const nodeConfigs = {
  人物: { shape: 'circle', size: 50, color: '#78b2be', stroke: '#5a8c96', label: '人物' },
  文档: { shape: 'rect', size: [60, 40], color: '#e7ca4a', stroke: '#c9af39', label: '文档' },
  照片: { shape: 'roundedRect', size: [70, 45], color: '#f8a7c8', stroke: '#f07aa8', label: '照片' },
  便签: { shape: 'polygon', size: 60, color: '#a3f0c2', stroke: '#76d8a0',
    points: [[0,0],[100,0],[100,70],[15,80],[0,70]], label: '便签' },
  链接: { shape: 'hexagon', size: 50, color: '#c4ddc8', stroke: '#a3c2a7', label: '链接' },
  组织: { shape: 'rect', size: [90, 55], color: '#9bc295', stroke: '#7da677', label: '组织' }
};

// Backend type -> Frontend visual type mapping
const typeMapping = {
  'person': '人物', 'organization': '组织',
  'location': '链接', 'event': '便签',
  'technology': '链接', 'product': '链接',
};

function mapNodeType(node) {
  if (node.nodeType === 'Document') return '文档';
  if (node.nodeType === 'Concept') return '便签';
  return typeMapping[node.entityType] || '链接';
}

const getIconForType = (type) => {
  const icons = { '人物': '👤', '文档': '📄', '照片': '📷', '便签': '📝', '链接': '🔗', '组织': '🏢' };
  return icons[type] || '📌';
};

// Current graph data (loaded from API)
let currentData = { nodes: [], edges: [] };

// Process nodes to add styles
const processNodes = (data) => {
  return data.nodes.map(node => {
    const visualType = mapNodeType(node);
    const config = nodeConfigs[visualType];
    const nodeConfig = {
      id: node.id,
      label: node.label || node.name || node.id,
      type: config.shape,
      size: config.size,
      style: {
        fill: config.color,
        stroke: config.stroke,
        lineWidth: 2,
        fillOpacity: 0.8
      },
      labelCfg: {
        style: {
          fontFamily: 'Ma Shan Zheng, Caveat, cursive',
          fontSize: 16,
          fill: '#2c2c2c',
          textShadow: '0.5px 0.5px 0.5px rgba(0,0,0,0.2)'
        }
      },
      _originalData: node,
      _visualType: visualType,
    };
    if (config.shape === 'polygon' && config.points) {
      nodeConfig.points = config.points;
    }
    return nodeConfig;
  });
};

// Process edges - confidence (0-1) maps to line width (1-6)
const processEdges = (data) => {
  return data.edges.map(edge => {
    const width = Math.max(1, Math.round((edge.confidence || 0.5) * 6));
    return {
      source: edge.source,
      target: edge.target,
      label: edge.relation || '',
      style: {
        stroke: '#888888',
        lineWidth: width,
        strokeOpacity: 0.7
      },
      labelCfg: {
        autoRotate: true,
        style: {
          fontFamily: 'Caveat, cursive',
          fontSize: 14,
          fill: '#555555',
          background: {
            fill: '#faf8f0',
            padding: [2, 4, 2, 4],
            radius: 4
          }
        }
      }
    };
  });
};

// Initialize G6
const container = document.getElementById('graph-container');
const width = container.offsetWidth;
const height = container.offsetHeight;

const graph = new G6.Graph({
  container: 'graph-container',
  width,
  height,
  renderer: 'canvas',
  modes: {
    default: ['drag-canvas', 'zoom-canvas', 'drag-node', 'click-select']
  },
  layout: {
    type: 'force',
    linkDistance: 120,
    center: [width / 2, height / 2],
    nodeStrength: -300,
    edgeStrength: 0.8,
    preventOverlap: true,
    nodeSize: 50
  },
  defaultNode: { type: 'circle', size: 50 },
  animate: true,
  enableOptimization: true,
  optimize: { enable: true, zoomThreshold: 0.5, showLabel: false }
});

// Loading state
const showLoading = () => {
  const el = document.getElementById('loading-overlay');
  if (el) el.classList.remove('hidden');
};
const hideLoading = () => {
  const el = document.getElementById('loading-overlay');
  if (el) el.classList.add('hidden');
};
const showEmpty = () => {
  const el = document.getElementById('empty-state');
  if (el) el.classList.remove('hidden');
};
const hideEmpty = () => {
  const el = document.getElementById('empty-state');
  if (el) el.classList.add('hidden');
};

// Load graph data from backend
async function loadGraphSnapshot() {
  showLoading();
  hideEmpty();

  try {
    const snapshot = await window.electronAPI.getGraphSnapshot(200, 0);
    currentData = { nodes: snapshot.nodes || [], edges: snapshot.edges || [] };

    if (currentData.nodes.length === 0) {
      showEmpty();
      return;
    }

    const processed = {
      nodes: processNodes(currentData),
      edges: processEdges(currentData)
    };

    graph.data(processed);
    graph.render();
    updateStats();
  } catch (error) {
    console.error('Failed to load graph:', error);
    showEmpty();
  } finally {
    hideLoading();
  }
}

loadGraphSnapshot();

// Update statistics
const updateStats = () => {
  const typeCounts = {};
  currentData.nodes.forEach(node => {
    const visualType = mapNodeType(node);
    typeCounts[visualType] = (typeCounts[visualType] || 0) + 1;
  });

  const statsEl = document.getElementById('stats');
  let html = '';
  Object.entries(typeCounts).forEach(([type, count]) => {
    html += `<span class="stat-item"><strong>${count}</strong> ${type}</span>`;
  });
  html += `<span class="stat-item"><strong>${currentData.edges.length}</strong> 关系</span>`;
  statsEl.innerHTML = html;
};

// Detail panel handling
let currentSelectedNode = null;
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailContent = document.getElementById('detail-content');
const closeDetail = document.getElementById('close-detail');
const focusNodeBtn = document.getElementById('focus-node');

const showDetail = (node) => {
  currentSelectedNode = node.getModel();
  const orig = currentSelectedNode._originalData;
  if (!orig) return;

  const visualType = currentSelectedNode._visualType || mapNodeType(orig);
  detailTitle.textContent = `${getIconForType(visualType)} ${orig.label || orig.name || orig.id}`;

  let html = '';
  html += `<div class="detail-row"><span class="detail-label">类型：</span> ${visualType}</div>`;

  // Show available fields
  if (orig.entityType) html += `<div class="detail-row"><span class="detail-label">实体类型：</span> ${orig.entityType}</div>`;
  if (orig.description) html += `<div class="detail-row"><span class="detail-label">描述：</span> ${orig.description}</div>`;
  if (orig.source) html += `<div class="detail-row"><span class="detail-label">来源：</span> ${orig.source}</div>`;

  // Count related edges
  const relatedEdges = currentData.edges.filter(e => e.source === orig.id || e.target === orig.id);
  if (relatedEdges.length > 0) {
    html += `<div class="detail-row"><br/><strong>关系 (${relatedEdges.length})：</strong></div>`;
    html += `<div class="relation-list">`;
    relatedEdges.forEach(edge => {
      const otherId = edge.source === orig.id ? edge.target : edge.source;
      const otherNode = currentData.nodes.find(n => n.id === otherId);
      if (otherNode) {
        const otherType = mapNodeType(otherNode);
        html += `<div class="detail-row">${getIconForType(otherType)} <strong>${edge.relation || edge.edgeType}：</strong> ${otherNode.label || otherNode.name}</div>`;
      }
    });
    html += `</div>`;
  }

  detailContent.innerHTML = html;
  detailPanel.classList.remove('hidden');
};

const hideDetail = () => {
  detailPanel.classList.add('hidden');
  currentSelectedNode = null;
};

closeDetail.addEventListener('click', hideDetail);

focusNodeBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  graph.focusItem(currentSelectedNode.id);
});

// Node click handler
graph.on('node:click', (e) => {
  showDetail(e.item);
});

// Double-click to expand neighborhood
graph.on('node:dblclick', async (e) => {
  const node = e.item.getModel();
  const orig = node._originalData;
  if (!orig || orig.nodeType !== 'Entity') return;

  // Extract entity ID from prefixed ID (e.g., "entity:alice" -> "alice")
  const entityId = orig.id.replace(/^entity:/, '');

  try {
    const result = await window.electronAPI.exploreNode(entityId, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) return;

    // Add new nodes and edges not already in graph
    const existingIds = new Set(currentData.nodes.map(n => n.id));
    let addedCount = 0;

    result.nodes.forEach(n => {
      const nodeId = n.id.startsWith('entity:') ? n.id : `entity:${n.id}`;
      if (!existingIds.has(nodeId)) {
        const newNode = {
          id: nodeId, label: n.name, nodeType: 'Entity',
          entityType: n.type, description: n.description || ''
        };
        currentData.nodes.push(newNode);
        const processed = processNodes({ nodes: [newNode], edges: [] });
        graph.addItem('node', processed[0]);
        addedCount++;
      }
    });

    result.edges.forEach(edge => {
      const srcId = edge.source.startsWith('entity:') ? edge.source : `entity:${edge.source}`;
      const tgtId = edge.target.startsWith('entity:') ? edge.target : `entity:${edge.target}`;
      const edgeKey = `${srcId}-${tgtId}`;
      if (!existingIds.has(srcId) || !existingIds.has(tgtId)) return;
      // Check if edge already exists
      const edgeExists = currentData.edges.some(e => e.source === srcId && e.target === tgtId);
      if (!edgeExists) {
        const newEdge = { source: srcId, target: tgtId, relation: edge.relation, confidence: edge.confidence, edgeType: 'RELATED_TO' };
        currentData.edges.push(newEdge);
        const processed = processEdges({ nodes: [], edges: [newEdge] });
        graph.addItem('edge', processed[0]);
      }
    });

    if (addedCount > 0) updateStats();
  } catch (err) {
    console.error('Failed to expand node:', err);
  }
});

graph.on('canvas:click', () => {
  hideDetail();
});

// Search functionality
const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase();
  if (!query) {
    graph.getNodes().forEach(node => graph.showItem(node.getID()));
    graph.getEdges().forEach(edge => graph.showItem(edge.getID()));
    return;
  }

  graph.getNodes().forEach(node => {
    const model = node.getModel();
    const orig = model._originalData;
    const label = (orig?.label || orig?.name || '').toLowerCase();
    const desc = (orig?.description || '').toLowerCase();
    if (label.includes(query) || desc.includes(query)) {
      graph.showItem(node.getID());
    } else {
      graph.hideItem(node.getID());
    }
  });

  graph.getEdges().forEach(edge => {
    const model = edge.getModel();
    const sourceNode = graph.findById(model.source);
    const targetNode = graph.findById(model.target);
    const sourceVisible = sourceNode && !sourceNode.getModel().hidden;
    const targetVisible = targetNode && !targetNode.getModel().hidden;
    if (sourceVisible && targetVisible) {
      graph.showItem(edge.getID());
    } else {
      graph.hideItem(edge.getID());
    }
  });
});

// Filter buttons
const filterBtns = document.querySelectorAll('.filter-btn');
filterBtns.forEach(btn => {
  btn.addEventListener('click', (e) => {
    filterBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const filterType = btn.dataset.type;

    graph.getNodes().forEach(node => {
      const model = node.getModel();
      const visualType = model._visualType;
      if (filterType === 'all' || visualType === filterType) {
        graph.showItem(node.getID());
      } else {
        graph.hideItem(node.getID());
      }
    });

    graph.getEdges().forEach(edge => {
      const model = edge.getModel();
      const sourceNode = graph.findById(model.source);
      const targetNode = graph.findById(model.target);
      const sourceVisible = sourceNode && !sourceNode.getModel().hidden;
      const targetVisible = targetNode && !targetNode.getModel().hidden;
      if (sourceVisible && targetVisible) {
        graph.showItem(edge.getID());
      } else {
        graph.hideItem(edge.getID());
      }
    });
  });
});

// Handle window resize
window.addEventListener('resize', () => {
  const newWidth = container.offsetWidth;
  const newHeight = container.offsetHeight;
  graph.changeSize(newWidth, newHeight);
});
```

- [ ] **Step 2: Add loading and empty-state elements to index.html**

Add inside `<body>` before `<div class="toolbar">`:

```html
  <div id="loading-overlay" class="hidden" style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(250,248,240,0.9);display:flex;align-items:center;justify-content:center;z-index:1000;">
    <div style="text-align:center;font-family:'Ma Shan Zheng',cursive;font-size:24px;color:#5a8c96;">加载中...</div>
  </div>
  <div id="empty-state" class="hidden" style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(250,248,240,0.95);display:flex;align-items:center;justify-content:center;z-index:999;">
    <div style="text-align:center;font-family:'Ma Shan Zheng',cursive;color:#888;">
      <div style="font-size:48px;margin-bottom:16px;">🕸️</div>
      <div style="font-size:20px;">知识图谱暂无数据</div>
      <div style="font-size:14px;margin-top:8px;">开始对话以构建你的知识网络</div>
    </div>
  </div>
```

Note: The `.hidden` class already exists in styles.css with `display: none !important`. The inline `display:flex` will be overridden by `.hidden`. This works because `.hidden` uses `!important`.

- [ ] **Step 3: Commit**

```bash
git add ui/graph/renderer.js ui/graph/index.html
git commit -m "feat(graph): replace hardcoded data with kg-server API calls"
```

---

## Verification

1. Start the full application: `go run main.go`
2. Open the graph window
3. **Empty database**: Should see "知识图谱暂无数据" empty state
4. **With data**: Should see real nodes and edges from KuzuDB
5. Click a node → detail panel shows type, description, relationships
6. Double-click an Entity node → neighborhood expands with new nodes
7. Search filters nodes by label/description
8. Type filter buttons work correctly
9. Verify no hardcoded sample data remains in renderer.js
10. Run kg-server tests: `cd mcp-servers/kg-server && python -m pytest tests/ -v` (all 57 pass)
