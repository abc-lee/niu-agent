# kg-server Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add confidence mechanism, graph traversal tools, and surprising connections discovery to kg-server

**Architecture:** Schema redesign with confidence + timestamps on relations, plus 6 new MCP tools for graph analysis. KuzuDB doesn't support ALTER TABLE, so we'll drop and recreate tables.

**Tech Stack:** KuzuDB (graph database), Python MCP server, NetworkX (PageRank), Cypher query language

---

## Phase 1: Schema Enhancement + Confidence Mechanism

### Task 1: Backup and Schema Migration

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:1-150`
- Test: Create `mcp-servers/kg-server/tests/test_schema_migration.py`

- [ ] **Step 1: Write failing test for schema validation**

```python
# mcp-servers/kg-server/tests/test_schema_migration.py
import pytest
from niu_kg_server import KGServer

def test_entity_has_timestamps():
    """Entity table must have created_at and updated_at fields."""
    server = KGServer()
    result = server.conn.execute("CALL TABLE_INFO('Entity') RETURN *")
    columns = {row['property name'] for row in result}
    assert 'created_at' in columns, "Entity missing created_at"
    assert 'updated_at' in columns, "Entity missing updated_at"

def test_relation_has_confidence():
    """MENTIONS relation must have confidence and created_at."""
    server = KGServer()
    result = server.conn.execute("CALL TABLE_INFO('MENTIONS') RETURN *")
    columns = {row['property name'] for row in result}
    assert 'confidence' in columns, "MENTIONS missing confidence"
    assert 'created_at' in columns, "MENTIONS missing created_at"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_schema_migration.py -v`
Expected: FAIL with "Entity missing created_at" or similar

- [ ] **Step 3: Update schema initialization**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def _init_schema(self):
    """Initialize database schema with confidence and timestamps."""
    
    # Drop existing tables (KuzuDB doesn't support ALTER TABLE)
    self.conn.execute("DROP TABLE IF EXISTS RELATED_TO")
    self.conn.execute("DROP TABLE IF EXISTS CONTAINS")
    self.conn.execute("DROP TABLE IF EXISTS MENTIONS")
    self.conn.execute("DROP TABLE IF EXISTS Concept")
    self.conn.execute("DROP TABLE IF EXISTS Entity")
    self.conn.execute("DROP TABLE IF EXISTS Document")
    
    # Create node tables with timestamps
    self.conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Document (
            uri STRING,
            title STRING,
            content STRING,
            source STRING,
            created_at STRING,
            PRIMARY KEY (uri)
        )
    """)
    
    self.conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Entity (
            id STRING,
            name STRING,
            type STRING,
            description STRING,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (id)
        )
    """)
    
    self.conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Concept (
            name STRING,
            description STRING,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (name)
        )
    """)
    
    # Create relationship tables with confidence + timestamps
    self.conn.execute("""
        CREATE REL TABLE IF NOT EXISTS MENTIONS (
            FROM Document TO Entity,
            confidence FLOAT,
            created_at STRING
        )
    """)
    
    self.conn.execute("""
        CREATE REL TABLE IF NOT EXISTS CONTAINS (
            FROM Document TO Concept,
            confidence FLOAT,
            created_at STRING
        )
    """)
    
    self.conn.execute("""
        CREATE REL TABLE IF NOT EXISTS RELATED_TO (
            FROM Entity TO Entity,
            relation STRING,
            confidence FLOAT,
            created_at STRING
        )
    """)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_schema_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit schema changes**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_schema_migration.py
git commit -m "feat(kg-server): add confidence + timestamps to schema"
```

---

### Task 2: Add Confidence Helper Function

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:150-200`

- [ ] **Step 1: Add confidence inference helper**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

import inspect
from datetime import datetime, timezone

def _infer_confidence(self, confidence: float | None = None) -> float:
    """Infer confidence level based on call stack or use provided value.
    
    Confidence levels:
    - 1.0: User manually created (default for backward compatibility)
    - 0.7-0.9: LLM extracted from documents
    - 0.4-0.6: Agent inferred from context
    - 0.1-0.3: Algorithm discovered (clustering, co-occurrence)
    """
    if confidence is not None:
        return max(0.0, min(1.0, confidence))
    
    # Inspect call stack to infer source
    frame = inspect.currentframe()
    try:
        # Go up 2 levels: _infer_confidence -> link_* -> actual caller
        caller_frame = frame.f_back.f_back
        caller_name = caller_frame.f_code.co_name
        
        # Heuristics based on caller function name
        if 'user' in caller_name.lower() or 'manual' in caller_name.lower():
            return 1.0
        elif 'agent' in caller_name.lower() or 'infer' in caller_name.lower():
            return 0.5
        elif 'algorithm' in caller_name.lower() or 'cluster' in caller_name.lower():
            return 0.3
        else:
            return 1.0  # Default: highest confidence for backward compatibility
    finally:
        del frame

def _get_timestamp(self) -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 2: Update create_entity to use timestamps**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def create_entity(self, id: str, name: str, type: str, description: str = "") -> dict:
    """Create an entity with automatic timestamps."""
    created_at = self._get_timestamp()
    
    self.conn.execute(
        "CREATE (e:Entity {id: $id, name: $name, type: $type, description: $description, created_at: $created_at, updated_at: $created_at})",
        {"id": id, "name": name, "type": type, "description": description, "created_at": created_at}
    )
    
    return {"id": id, "name": name, "type": type, "description": description, "created_at": created_at, "updated_at": created_at}
```

- [ ] **Step 3: Update link_document_entity to use confidence**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def link_document_entity(self, doc_uri: str, entity_id: str, confidence: float | None = None) -> dict:
    """Link document to entity with confidence score."""
    confidence = self._infer_confidence(confidence)
    created_at = self._get_timestamp()
    
    self.conn.execute(
        "MATCH (d:Document {uri: $doc_uri}), (e:Entity {id: $entity_id}) "
        "CREATE (d)-[:MENTIONS {confidence: $confidence, created_at: $created_at}]->(e)",
        {"doc_uri": doc_uri, "entity_id": entity_id, "confidence": confidence, "created_at": created_at}
    )
    
    return {"doc_uri": doc_uri, "entity_id": entity_id, "confidence": confidence, "created_at": created_at}
```

- [ ] **Step 4: Update link_entities to use confidence**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def link_entities(self, from_id: str, to_id: str, relation: str, confidence: float | None = None) -> dict:
    """Link two entities with relation and confidence."""
    confidence = self._infer_confidence(confidence)
    created_at = self._get_timestamp()
    
    self.conn.execute(
        "MATCH (e1:Entity {id: $from_id}), (e2:Entity {id: $to_id}) "
        "CREATE (e1)-[:RELATED_TO {relation: $relation, confidence: $confidence, created_at: $created_at}]->(e2)",
        {"from_id": from_id, "to_id": to_id, "relation": relation, "confidence": confidence, "created_at": created_at}
    )
    
    return {"from_id": from_id, "to_id": to_id, "relation": relation, "confidence": confidence, "created_at": created_at}
```

- [ ] **Step 5: Commit confidence mechanism**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py
git commit -m "feat(kg-server): add confidence inference + timestamp auto-fill"
```

---

## Phase 2: Graph Traversal Tools

### Task 3: Implement explore_node Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:300-400`
- Test: Create `mcp-servers/kg-server/tests/test_explore_node.py`

- [ ] **Step 1: Write failing test for explore_node**

```python
# mcp-servers/kg-server/tests/test_explore_node.py
import pytest
from niu_kg_server import KGServer

def test_explore_node_basic():
    """Explore node should return neighbors and edges."""
    server = KGServer()
    
    # Create test data
    server.create_entity("person_zhang", "张三", "人物")
    server.create_entity("person_li", "李四", "人物")
    server.link_entities("person_zhang", "person_li", "KNOWS", confidence=0.9)
    
    # Explore from 张三
    result = server.explore_node("person_zhang", depth=1, min_confidence=0.5)
    
    assert "nodes" in result
    assert "edges" in result
    assert len(result["nodes"]) == 1  # 李四
    assert result["edges"][0]["confidence"] == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_explore_node.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'explore_node'"

- [ ] **Step 3: Implement explore_node**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def explore_node(self, entity_id: str, depth: int = 2, min_confidence: float = 0.0, direction: str = "both") -> dict:
    """Explore graph from entity, returning N-layer neighbors.
    
    Args:
        entity_id: Entity ID or name (supports fuzzy matching)
        depth: Traversal depth (1-5)
        min_confidence: Minimum confidence filter (0.0-1.0)
        direction: "both" | "outgoing" | "incoming"
    
    Returns:
        {
            "center": {"id": "...", "name": "...", "type": "..."},
            "nodes": [{"id": "...", "name": "...", "type": "...", "distance": 1}],
            "edges": [{"source": "...", "target": "...", "relation": "...", "confidence": 0.9}],
            "stats": {"nodes": N, "edges": M, "max_depth": D}
        }
    """
    depth = max(1, min(5, depth))
    
    # Find center node (fuzzy match by name or exact ID)
    center_result = self.conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name, e.type LIMIT 1",
        {"id": entity_id}
    )
    center_rows = list(center_result)
    if not center_rows:
        return {"error": f"Entity '{entity_id}' not found"}
    
    center = {
        "id": center_rows[0]["e.id"],
        "name": center_rows[0]["e.name"],
        "type": center_rows[0]["e.type"]
    }
    
    # Build direction filter
    if direction == "outgoing":
        dir_pattern = "->"
    elif direction == "incoming":
        dir_pattern = "<-"
    else:
        dir_pattern = "-"
    
    # BFS traversal with confidence filter
    nodes = []
    edges = []
    visited = {center["id"]}
    frontier = [center["id"]]
    
    for d in range(1, depth + 1):
        next_frontier = []
        for node_id in frontier:
            # Get neighbors with confidence filter
            query = f"""
                MATCH (n {{id: $node_id}}){dir_pattern}[r]{dir_pattern}-(neighbor)
                WHERE r.confidence >= $min_confidence
                RETURN neighbor.id, neighbor.name, neighbor.type, type(r) as rel_type, 
                       r.confidence, r.relation, startNode(r).id as source, endNode(r).id as target
            """
            result = self.conn.execute(query, {"node_id": node_id, "min_confidence": min_confidence})
            
            for row in result:
                neighbor_id = row["neighbor.id"]
                if neighbor_id not in visited:
                    visited.add(neighbor_id)
                    next_frontier.append(neighbor_id)
                    nodes.append({
                        "id": neighbor_id,
                        "name": row["neighbor.name"],
                        "type": row["neighbor.type"],
                        "distance": d
                    })
                
                # Add edge (avoid duplicates)
                edge_key = (row["source"], row["target"], row["rel_type"])
                if edge_key not in {(e["source"], e["target"], e["relation"]) for e in edges}:
                    edges.append({
                        "source": row["source"],
                        "target": row["target"],
                        "relation": row.get("relation") or row["rel_type"],
                        "confidence": row["r.confidence"]
                    })
        
        frontier = next_frontier
    
    return {
        "center": center,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "max_depth": depth
        }
    }
```

- [ ] **Step 4: Update TOOL_SCHEMAS with explore_node**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "explore_node": {
        "name": "explore_node",
        "description": "从指定实体出发探索N层邻居和边，支持置信度过滤",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "实体ID或名称（支持模糊匹配）"},
                "depth": {"type": "integer", "default": 2, "description": "遍历深度（1-5）"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "最小置信度过滤（0.0-1.0）"},
                "direction": {"type": "string", "default": "both", "enum": ["both", "outgoing", "incoming"], "description": "方向过滤"}
            },
            "required": ["entity_id"]
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_explore_node.py -v`
Expected: PASS

- [ ] **Step 6: Commit explore_node**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_explore_node.py
git commit -m "feat(kg-server): add explore_node tool for graph traversal"
```

---

### Task 4: Implement find_path Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:400-450`
- Test: Create `mcp-servers/kg-server/tests/test_find_path.py`

- [ ] **Step 1: Write failing test for find_path**

```python
# mcp-servers/kg-server/tests/test_find_path.py
import pytest
from niu_kg_server import KGServer

def test_find_path_shortest():
    """Find shortest path between two entities."""
    server = KGServer()
    
    # Create test graph: A -[KNOWS]-> B -[WORKS_AT]-> C
    server.create_entity("person_a", "用户A", "人物")
    server.create_entity("person_b", "用户B", "人物")
    server.create_entity("org_c", "公司C", "组织")
    server.link_entities("person_a", "person_b", "KNOWS", confidence=0.9)
    server.link_entities("person_b", "org_c", "WORKS_AT", confidence=1.0)
    
    # Find path from A to C
    result = server.find_path("person_a", "org_c", max_depth=5)
    
    assert result["found"] == True
    assert result["hops"] == 2
    assert len(result["path"]) == 3  # A, B, C
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_find_path.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'find_path'"

- [ ] **Step 3: Implement find_path**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def find_path(self, from_id: str, to_id: str, max_depth: int = 5) -> dict:
    """Find shortest path between two entities.
    
    Args:
        from_id: Source entity ID or name
        to_id: Target entity ID or name
        max_depth: Maximum hops to search (1-10)
    
    Returns:
        {
            "found": bool,
            "hops": int,
            "path": [{"id": "...", "name": "...", "relation": "...", "confidence": 0.9}]
        }
    """
    max_depth = max(1, min(10, max_depth))
    
    # Find source and target nodes
    source_result = self.conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name LIMIT 1",
        {"id": from_id}
    )
    source_rows = list(source_result)
    if not source_rows:
        return {"found": False, "error": f"Source entity '{from_id}' not found"}
    
    target_result = self.conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name LIMIT 1",
        {"id": to_id}
    )
    target_rows = list(target_result)
    if not target_rows:
        return {"found": False, "error": f"Target entity '{to_id}' not found"}
    
    source_id = source_rows[0]["e.id"]
    target_id = target_rows[0]["e.id"]
    
    # Use KuzuDB's SHORTESTPATH
    query = f"""
        MATCH path = SHORTESTPATH(
            (a {{id: $source_id}})-[*1..{max_depth}]-(b {{id: $target_id}})
        )
        RETURN [node in nodes(path) | node.id] as node_ids,
               [node in nodes(path) | node.name] as node_names,
               [rel in relationships(path) | {{type: type(rel), confidence: rel.confidence, relation: rel.relation}}] as rels
    """
    
    result = self.conn.execute(query, {"source_id": source_id, "target_id": target_id})
    path_rows = list(result)
    
    if not path_rows:
        return {"found": False, "hops": 0, "path": []}
    
    # Build path with edge info
    node_ids = path_rows[0]["node_ids"]
    node_names = path_rows[0]["node_names"]
    rels = path_rows[0]["rels"]
    
    path = []
    for i, (node_id, node_name) in enumerate(zip(node_ids, node_names)):
        if i == 0:
            path.append({"id": node_id, "name": node_name})
        else:
            rel = rels[i - 1]
            path.append({
                "id": node_id,
                "name": node_name,
                "relation": rel.get("relation") or rel["type"],
                "confidence": rel.get("confidence")
            })
    
    return {
        "found": True,
        "hops": len(node_ids) - 1,
        "path": path
    }
```

- [ ] **Step 4: Update TOOL_SCHEMAS with find_path**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "find_path": {
        "name": "find_path",
        "description": "查找两个实体之间的最短路径",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_id": {"type": "string", "description": "起点实体ID或名称"},
                "to_id": {"type": "string", "description": "终点实体ID或名称"},
                "max_depth": {"type": "integer", "default": 5, "description": "最大跳数（1-10）"}
            },
            "required": ["from_id", "to_id"]
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_find_path.py -v`
Expected: PASS

- [ ] **Step 6: Commit find_path**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_find_path.py
git commit -m "feat(kg-server): add find_path tool for shortest path search"
```

---

## Phase 3: Graph Analysis Tools

### Task 5: Implement graph_stats Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:450-500`
- Test: Create `mcp-servers/kg-server/tests/test_graph_stats.py`

- [ ] **Step 1: Write failing test for graph_stats**

```python
# mcp-servers/kg-server/tests/test_graph_stats.py
import pytest
from niu_kg_server import KGServer

def test_graph_stats_basic():
    """Graph stats should return node/edge counts by type."""
    server = KGServer()
    
    # Create test data
    server.create_entity("person_a", "用户A", "人物")
    server.create_entity("org_b", "公司B", "组织")
    server.link_entities("person_a", "org_b", "WORKS_AT", confidence=0.9)
    
    stats = server.graph_stats()
    
    assert stats["nodes"]["total"] == 2
    assert "人物" in stats["nodes"]["by_type"]
    assert "组织" in stats["nodes"]["by_type"]
    assert stats["edges"]["total"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_graph_stats.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'graph_stats'"

- [ ] **Step 3: Implement graph_stats**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def graph_stats(self) -> dict:
    """Return comprehensive graph statistics.
    
    Returns:
        {
            "nodes": {"total": N, "by_type": {"人物": M, "组织": K}},
            "edges": {"total": M, "by_relation": {"MENTIONS": X, "RELATED_TO": Y}, "by_confidence": {...}},
            "density": 0.028,
            "connected_components": 3
        }
    """
    # Node stats
    node_result = self.conn.execute("MATCH (e:Entity) RETURN e.type, count(e) as count")
    nodes_by_type = {}
    total_nodes = 0
    for row in node_result:
        nodes_by_type[row["e.type"]] = row["count"]
        total_nodes += row["count"]
    
    # Edge stats
    edge_result = self.conn.execute("MATCH ()-[r]->() RETURN type(r) as rel_type, count(r) as count, avg(r.confidence) as avg_conf")
    edges_by_relation = {}
    edges_by_confidence = {"high (0.7-1.0)": 0, "medium (0.4-0.7)": 0, "low (0.0-0.4)": 0}
    total_edges = 0
    for row in edge_result:
        rel_type = row["rel_type"]
        count = row["count"]
        avg_conf = row["avg_conf"] or 0.0
        
        edges_by_relation[rel_type] = count
        total_edges += count
        
        # Classify by confidence
        if avg_conf >= 0.7:
            edges_by_confidence["high (0.7-1.0)"] += count
        elif avg_conf >= 0.4:
            edges_by_confidence["medium (0.4-0.7)"] += count
        else:
            edges_by_confidence["low (0.0-0.4)"] += count
    
    # Graph density
    max_edges = total_nodes * (total_nodes - 1) / 2 if total_nodes > 1 else 1
    density = round(total_edges / max_edges, 4) if max_edges > 0 else 0.0
    
    # Connected components (simplified: count isolates)
    isolate_result = self.conn.execute("MATCH (e:Entity) WHERE NOT (e)--() RETURN count(e) as isolates")
    isolates = list(isolate_result)[0]["isolates"]
    
    return {
        "nodes": {
            "total": total_nodes,
            "by_type": nodes_by_type
        },
        "edges": {
            "total": total_edges,
            "by_relation": edges_by_relation,
            "by_confidence": edges_by_confidence
        },
        "density": density,
        "connected_components": isolates + 1  # Approximation
    }
```

- [ ] **Step 4: Update TOOL_SCHEMAS with graph_stats**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "graph_stats": {
        "name": "graph_stats",
        "description": "返回图统计概览：节点数、边数、置信度分布、密度",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_graph_stats.py -v`
Expected: PASS

- [ ] **Step 6: Commit graph_stats**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_graph_stats.py
git commit -m "feat(kg-server): add graph_stats tool for overview statistics"
```

---

### Task 6: Implement hub_entities Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:500-580`
- Test: Create `mcp-servers/kg-server/tests/test_hub_entities.py`

- [ ] **Step 1: Write failing test for hub_entities**

```python
# mcp-servers/kg-server/tests/test_hub_entities.py
import pytest
from niu_kg_server import KGServer

def test_hub_entities_degree():
    """Hub entities should return most connected nodes by degree."""
    server = KGServer()
    
    # Create test graph: hub -> many neighbors
    server.create_entity("hub", "中心节点", "人物")
    for i in range(5):
        server.create_entity(f"node_{i}", f"节点{i}", "人物")
        server.link_entities("hub", f"node_{i}", "KNOWS", confidence=0.9)
    
    hubs = server.hub_entities(top_n=3)
    
    assert len(hubs["entities"]) == 3
    assert hubs["entities"][0]["id"] == "hub"
    assert hubs["entities"][0]["degree"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_hub_entities.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'hub_entities'"

- [ ] **Step 3: Implement hub_entities**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def hub_entities(self, top_n: int = 10, entity_type: str | None = None) -> dict:
    """Return most central entities by degree centrality.
    
    Args:
        top_n: Number of top entities to return
        entity_type: Filter by entity type (optional)
    
    Returns:
        {
            "entities": [
                {"id": "...", "name": "...", "type": "...", "degree": 15, "pagerank": 0.12}
            ]
        }
    """
    top_n = max(1, min(50, top_n))
    
    # Build type filter
    type_filter = f"WHERE e.type = '{entity_type}'" if entity_type else ""
    
    # Degree centrality query
    query = f"""
        MATCH (e:Entity)
        {type_filter}
        MATCH (e)-[r]-()
        RETURN e.id, e.name, e.type, count(r) AS degree
        ORDER BY degree DESC
        LIMIT {top_n}
    """
    
    result = self.conn.execute(query)
    entities = []
    for row in result:
        entities.append({
            "id": row["e.id"],
            "name": row["e.name"],
            "type": row["e.type"],
            "degree": row["degree"],
            "pagerank": None  # Will be added later if needed
        })
    
    return {"entities": entities}
```

- [ ] **Step 4: Update TOOL_SCHEMAS with hub_entities**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "hub_entities": {
        "name": "hub_entities",
        "description": "返回最核心的实体（按度中心度排序）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "default": 10, "description": "返回数量"},
                "entity_type": {"type": "string", "description": "实体类型过滤（可选）"}
            }
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_hub_entities.py -v`
Expected: PASS

- [ ] **Step 6: Commit hub_entities**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_hub_entities.py
git commit -m "feat(kg-server): add hub_entities tool for degree centrality"
```

---

### Task 7: Implement surprising_connections Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:580-700`
- Test: Create `mcp-servers/kg-server/tests/test_surprising_connections.py`

- [ ] **Step 1: Write failing test for surprising_connections**

```python
# mcp-servers/kg-server/tests/test_surprising_connections.py
import pytest
from niu_kg_server import KGServer

def test_surprising_connections_basic():
    """Find entities that co-occur but have no direct relationship."""
    server = KGServer()
    
    # Create test data: A and B appear in same document but no direct edge
    server.create_entity("person_a", "用户A", "人物")
    server.create_entity("person_b", "用户B", "人物")
    server.create_document("doc_1", "文档1", "content", "test")
    server.link_document_entity("doc_1", "person_a", confidence=0.9)
    server.link_document_entity("doc_1", "person_b", confidence=0.9)
    
    # Find surprising connections
    result = server.surprising_connections(top_n=5, min_co_occurrence=1)
    
    assert len(result["candidates"]) >= 1
    assert any(c["entity_a"]["id"] == "person_a" and c["entity_b"]["id"] == "person_b" for c in result["candidates"])
    assert result["candidates"][0]["co_occurrence_count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_surprising_connections.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'surprising_connections'"

- [ ] **Step 3: Implement surprising_connections**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def surprising_connections(self, top_n: int = 5, min_co_occurrence: int = 2, types: list[str] | None = None) -> dict:
    """Discover hidden relationships between entities.
    
    Finds entity pairs that:
    - Co-occur in >= N documents
    - But have no direct RELATED_TO edge
    
    Args:
        top_n: Number of candidates to return
        min_co_occurrence: Minimum co-occurrence count
        types: Filter by entity type pairs (e.g., ["人物", "组织"])
    
    Returns:
        {
            "candidates": [
                {
                    "entity_a": {"id": "...", "name": "...", "type": "..."},
                    "entity_b": {"id": "...", "name": "...", "type": "..."},
                    "co_occurrence_count": 5,
                    "shared_documents": ["doc_1", "doc_2"],
                    "score": 0.85,
                    "reason": "共同出现在 5 篇文档中，但没有直接关系边"
                }
            ]
        }
    """
    top_n = max(1, min(20, top_n))
    min_co_occurrence = max(1, min(10, min_co_occurrence))
    
    # Build type filter
    type_filter = ""
    if types and len(types) == 2:
        type_filter = f"AND e1.type = '{types[0]}' AND e2.type = '{types[1]}'"
    
    # Find co-occurring entity pairs without direct relationship
    query = f"""
        MATCH (d:Document)-[:MENTIONS]->(e1:Entity)
        MATCH (d)-[:MENTIONS]->(e2:Entity)
        WHERE e1.id < e2.id
        {type_filter}
        WITH e1, e2, count(d) AS co_occurrence, collect(d.uri) AS shared_docs
        WHERE co_occurrence >= $min_co_occurrence
        AND NOT EXISTS {{
            MATCH (e1)-[:RELATED_TO]-(e2)
        }}
        RETURN e1.id, e1.name, e1.type, e2.id, e2.name, e2.type, co_occurrence, shared_docs
        ORDER BY co_occurrence DESC
        LIMIT $top_n
    """
    
    result = self.conn.execute(query, {
        "min_co_occurrence": min_co_occurrence,
        "top_n": top_n
    })
    
    candidates = []
    for row in result:
        # Calculate score based on co-occurrence and type diversity
        type_diversity_bonus = 0.5
        if row["e1.type"] != row["e2.type"]:
            if "人物" in [row["e1.type"], row["e2.type"]] and "组织" in [row["e1.type"], row["e2.type"]]:
                type_diversity_bonus = 1.0
            elif "人物" in [row["e1.type"], row["e2.type"]] and "技术概念" in [row["e1.type"], row["e2.type"]]:
                type_diversity_bonus = 1.5
        
        score = row["co_occurrence"] * 0.4 + type_diversity_bonus * 0.3
        
        candidates.append({
            "entity_a": {
                "id": row["e1.id"],
                "name": row["e1.name"],
                "type": row["e1.type"]
            },
            "entity_b": {
                "id": row["e2.id"],
                "name": row["e2.name"],
                "type": row["e2.type"]
            },
            "co_occurrence_count": row["co_occurrence"],
            "shared_documents": row["shared_docs"][:5],  # Limit to 5 docs
            "score": round(score, 2),
            "reason": f"共同出现在 {row['co_occurrence']} 篇文档中，但没有直接关系边"
        })
    
    return {"candidates": candidates}
```

- [ ] **Step 4: Update TOOL_SCHEMAS with surprising_connections**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "surprising_connections": {
        "name": "surprising_connections",
        "description": "发现隐藏的意外关联：共现但没有直接关系的实体对",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "default": 5, "description": "返回数量"},
                "min_co_occurrence": {"type": "integer", "default": 2, "description": "最小共现次数"},
                "types": {"type": "array", "items": {"type": "string"}, "description": "跨类型过滤（如 ['人物', '组织']）"}
            }
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_surprising_connections.py -v`
Expected: PASS

- [ ] **Step 6: Commit surprising_connections**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_surprising_connections.py
git commit -m "feat(kg-server): add surprising_connections for hidden relationships"
```

---

### Task 8: Implement graph_changelog Tool

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:700-750`
- Test: Create `mcp-servers/kg-server/tests/test_graph_changelog.py`

- [ ] **Step 1: Write failing test for graph_changelog**

```python
# mcp-servers/kg-server/tests/test_graph_changelog.py
import pytest
from datetime import datetime, timedelta, timezone
from niu_kg_server import KGServer

def test_graph_changelog_recent():
    """Changelog should return entities created since date."""
    server = KGServer()
    
    # Create entity now
    server.create_entity("new_person", "新用户", "人物")
    
    # Get changelog for last 7 days
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    changelog = server.graph_changelog(since)
    
    assert len(changelog["new_entities"]) >= 1
    assert any(e["id"] == "new_person" for e in changelog["new_entities"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/kg-server && pytest tests/test_graph_changelog.py -v`
Expected: FAIL with "AttributeError: 'KGServer' object has no attribute 'graph_changelog'"

- [ ] **Step 3: Implement graph_changelog**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

def graph_changelog(self, since_date: str | None = None) -> dict:
    """Return all changes since specified date.
    
    Args:
        since_date: ISO date string (default: 7 days ago)
    
    Returns:
        {
            "new_entities": [{"id": "...", "name": "...", "created_at": "..."}],
            "new_relations": [{"from": "...", "to": "...", "relation": "...", "confidence": 0.9, "created_at": "..."}],
            "summary": "新增 5 个实体，12 条关系"
        }
    """
    if not since_date:
        # Default: 7 days ago
        from datetime import datetime, timedelta, timezone
        since_date = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    
    # Find new entities
    entity_query = """
        MATCH (e:Entity)
        WHERE e.created_at >= $since_date
        RETURN e.id, e.name, e.type, e.created_at
        ORDER BY e.created_at DESC
    """
    entity_result = self.conn.execute(entity_query, {"since_date": since_date})
    new_entities = []
    for row in entity_result:
        new_entities.append({
            "id": row["e.id"],
            "name": row["e.name"],
            "type": row["e.type"],
            "created_at": row["e.created_at"]
        })
    
    # Find new relations (all relation types)
    rel_query = """
        MATCH (a)-[r]->(b)
        WHERE r.created_at >= $since_date
        RETURN a.id as from_id, b.id as to_id, type(r) as rel_type, 
               r.confidence, r.relation, r.created_at
        ORDER BY r.created_at DESC
    """
    rel_result = self.conn.execute(rel_query, {"since_date": since_date})
    new_relations = []
    for row in rel_result:
        new_relations.append({
            "from": row["from_id"],
            "to": row["to_id"],
            "relation": row.get("relation") or row["rel_type"],
            "confidence": row["r.confidence"],
            "created_at": row["r.created_at"]
        })
    
    return {
        "new_entities": new_entities,
        "new_relations": new_relations,
        "summary": f"新增 {len(new_entities)} 个实体，{len(new_relations)} 条关系"
    }
```

- [ ] **Step 4: Update TOOL_SCHEMAS with graph_changelog**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

TOOL_SCHEMAS = {
    # ... existing tools ...
    "graph_changelog": {
        "name": "graph_changelog",
        "description": "返回指定时间后的所有变更（新增实体和关系）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_date": {"type": "string", "description": "ISO 日期字符串（默认：7 天前）"}
            }
        }
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/kg-server && pytest tests/test_graph_changelog.py -v`
Expected: PASS

- [ ] **Step 6: Commit graph_changelog**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_graph_changelog.py
git commit -m "feat(kg-server): add graph_changelog for temporal tracking"
```

---

### Task 9: Add Cypher Security Check

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py:750-800`

- [ ] **Step 1: Add Cypher security validation**

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

import re

def _validate_cypher_readonly(self, query: str) -> bool:
    """Validate that Cypher query is read-only.
    
    Blocks: CREATE, DELETE, SET, REMOVE, MERGE, DROP
    Allows: MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT
    """
    blocked_keywords = ['CREATE', 'DELETE', 'SET ', 'REMOVE', 'MERGE', 'DROP']
    query_upper = query.upper()
    
    for keyword in blocked_keywords:
        if keyword in query_upper:
            return False
    
    return True

def query_graph(self, cypher: str) -> dict:
    """Execute read-only Cypher query with security check."""
    if not self._validate_cypher_readonly(cypher):
        return {"error": "Only read-only queries are allowed (MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT)"}
    
    try:
        result = self.conn.execute(cypher)
        rows = [dict(row) for row in result]
        return {"success": True, "data": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}
```

- [ ] **Step 2: Add test for Cypher security**

```python
# mcp-servers/kg-server/tests/test_cypher_security.py
import pytest
from niu_kg_server import KGServer

def test_cypher_blocks_write_operations():
    """Cypher queries should block CREATE/DELETE/SET operations."""
    server = KGServer()
    
    # Should block CREATE
    result = server.query_graph("CREATE (e:Entity {id: 'test'})")
    assert "error" in result
    assert "read-only" in result["error"].lower()
    
    # Should allow MATCH
    result = server.query_graph("MATCH (e:Entity) RETURN e.id LIMIT 5")
    assert "success" in result
```

- [ ] **Step 3: Run test**

Run: `cd mcp-servers/kg-server && pytest tests/test_cypher_security.py -v`
Expected: PASS

- [ ] **Step 4: Commit security check**

```bash
cd mcp-servers/kg-server
git add src/niu_kg_server/__init__.py tests/test_cypher_security.py
git commit -m "feat(kg-server): add Cypher read-only security validation"
```

---

## Final Integration

### Task 10: Update Documentation

**Files:**
- Modify: `mcp-servers/kg-server/README.md`

- [ ] **Step 1: Update README with new tools**

```markdown
# kg-server

Knowledge graph server with confidence mechanism and graph analysis tools.

## New Features (v2.0)

### Confidence Mechanism
- All relations now have `confidence` (0.0-1.0) and `created_at` timestamps
- Automatic confidence inference based on call context
- Confidence levels:
  - 1.0: User manually created
  - 0.7-0.9: LLM extracted from documents
  - 0.4-0.6: Agent inferred
  - 0.1-0.3: Algorithm discovered

### Graph Traversal Tools
- `explore_node`: BFS traversal from entity with confidence filter
- `find_path`: Shortest path between two entities

### Graph Analysis Tools
- `graph_stats`: Overview statistics (nodes, edges, density)
- `hub_entities`: Most central entities by degree centrality
- `surprising_connections`: Hidden relationships discovery
- `graph_changelog`: Temporal change tracking

## Migration

KuzuDB doesn't support ALTER TABLE, so schema changes require database rebuild:

1. Backup existing data: `cp ~/.niu/kg.db ~/.niu/kg.db.backup`
2. Restart kg-server (will auto-recreate schema)
3. Re-import data if needed
```

- [ ] **Step 2: Commit documentation**

```bash
cd mcp-servers/kg-server
git add README.md
git commit -m "docs(kg-server): document v2.0 features and migration"
```

---

### Task 11: Integration Test

**Files:**
- Create: `mcp-servers/kg-server/tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# mcp-servers/kg-server/tests/test_integration.py
import pytest
from niu_kg_server import KGServer

def test_full_workflow():
    """Test complete workflow: create -> link -> analyze."""
    server = KGServer()
    
    # 1. Create entities
    server.create_entity("person_a", "张三", "人物")
    server.create_entity("person_b", "李四", "人物")
    server.create_entity("org_x", "公司X", "组织")
    
    # 2. Create relationships with confidence
    server.link_entities("person_a", "person_b", "KNOWS", confidence=0.9)
    server.link_entities("person_b", "org_x", "WORKS_AT", confidence=1.0)
    
    # 3. Traverse graph
    result = server.explore_node("person_a", depth=2)
    assert len(result["nodes"]) == 2  # 李四 and 公司X
    
    # 4. Find path
    path = server.find_path("person_a", "org_x")
    assert path["found"] == True
    assert path["hops"] == 2
    
    # 5. Get stats
    stats = server.graph_stats()
    assert stats["nodes"]["total"] >= 3
    assert stats["edges"]["total"] >= 2
    
    # 6. Get changelog
    changelog = server.graph_changelog()
    assert len(changelog["new_entities"]) >= 3
```

- [ ] **Step 2: Run integration test**

Run: `cd mcp-servers/kg-server && pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit integration test**

```bash
cd mcp-servers/kg-server
git add tests/test_integration.py
git commit -m "test(kg-server): add full workflow integration test"
```

---

## Summary

**Total Tasks**: 11
**Estimated Time**: 4-6 days

**Phase 1** (1-2 days):
- Schema migration with confidence + timestamps
- Confidence inference mechanism

**Phase 2** (1 day):
- Graph traversal tools (explore_node, find_path)
- Cypher security check

**Phase 3** (2-3 days):
- Graph analysis tools (stats, hubs, connections, changelog)
- Integration tests

**Key Files Modified**:
- `mcp-servers/kg-server/src/niu_kg_server/__init__.py` — All tool implementations
- `mcp-servers/kg-server/tests/` — Test coverage for all new features
- `mcp-servers/kg-server/README.md` — Updated documentation

**Migration Note**: Requires database rebuild (KuzuDB limitation).
