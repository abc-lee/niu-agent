"""
Knowledge Graph API endpoints for the graph visualization UI.

Routes delegate to LightRAG adapter for queries (replacing KuzuDB direct calls).

Response format: all graph endpoints return structured {nodes: [...], edges: [...]}
to match the frontend force-graph renderer expectations.
"""

import threading
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


def _normalize_nodes(nodes: list) -> list:
    """Convert adapter node format {id, name, type, description, file_path, source_id} to frontend-expected format.

    Frontend expects: {id, label, name, nodeType, entityType, description, uri, source}
    Adapter returns:  {id, name, type, description, file_path, source_id}
    """
    result = []
    for n in nodes:
        result.append({
            "id": n.get("id", ""),
            "label": n.get("name", n.get("id", "")),
            "name": n.get("name", ""),
            "nodeType": n.get("type", "Other"),
            "entityType": n.get("type", "Other"),
            "description": n.get("description", ""),
            "uri": n.get("file_path", ""),
            "source": n.get("source_id", ""),
        })
    return result


def _normalize_edges(edges: list) -> list:
    """Convert adapter edge format to frontend-expected format.

    Frontend expects: {source, target, relation, edgeType, confidence}
    Adapter returns:  {source, target, relation, description, weight}
    """
    result = []
    for e in edges:
        result.append({
            "source": e.get("source", e.get("src_id", "")),
            "target": e.get("target", e.get("tgt_id", "")),
            "relation": e.get("relation", e.get("keywords", "")),
            "edgeType": e.get("type", "relation"),
            "confidence": e.get("confidence", e.get("weight", 1.0)),
        })
    return result


class ExploreRequest(BaseModel):
    entity_id: str
    depth: int = Field(default=2, ge=1, le=5)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    direction: Literal["both", "outgoing", "incoming"] = "both"


class FindPathRequest(BaseModel):
    from_id: str
    to_id: str
    max_depth: int = Field(default=5, ge=1, le=10)


def _get_graph():
    """Get LightRAG's NetworkX graph safely, with retry on concurrent modification."""
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return None
    try:
        return getattr(rag, "chunk_entity_relation_graph", None)
    except RuntimeError:
        logger.warning("LightRAG graph modified during read, retrying")
        return getattr(rag, "chunk_entity_relation_graph", None)


# ============== Singleton Adapter ==============

_adapter = None
_adapter_lock = threading.Lock()


def _get_adapter():
    """Get or create the singleton LightRAGAdapter instance (thread-safe)."""
    global _adapter
    if _adapter is not None:
        return _adapter
    with _adapter_lock:
        if _adapter is not None:
            return _adapter
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        _adapter = LightRAGAdapter()
    return _adapter


# ============== Endpoints ==============
# NOTE: All endpoints use `def` (not `async def`) because LightRAGAdapter.query()
# and call_async() are blocking.  FastAPI runs regular def endpoints in a thread
# pool, so they won't block the ASGI event loop.


@router.get("/snapshot")
def graph_snapshot(
    limit: int = Query(default=200, ge=1, le=500),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Get full graph snapshot for visualization.

    Returns ``{nodes: [...], edges: [...]}`` matching the frontend renderer.
    """
    adapter = _get_adapter()

    result = adapter.get_graph_snapshot(limit=limit)
    if result is None:
        return {"status": "error", "message": "LightRAG not available"}
    # Normalize adapter format to frontend-expected format
    if "nodes" in result:
        result["nodes"] = _normalize_nodes(result["nodes"])
    if "edges" in result:
        result["edges"] = _normalize_edges(result["edges"])
    # Apply min_confidence filter on edges
    if min_confidence > 0 and "edges" in result:
        result["edges"] = [e for e in result["edges"] if e.get("confidence", 1.0) >= min_confidence]
    return result


@router.get("/stats")
def graph_stats():
    """Get knowledge graph statistics."""
    from niu_api.internal.lightrag_manager import get_lightrag_status

    return get_lightrag_status()


@router.get("/hubs")
def hub_entities(
    limit: int = Query(default=20, ge=1, le=100),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Find hub entities by connection count.

    Returns ``{nodes: [...], edges: [...]}`` — the top ``limit`` entities
    ranked by edge count, plus edges between them.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        node_degrees = {n: g.degree(n) for n in g.nodes()}
        sorted_nodes = sorted(node_degrees.keys(), key=lambda n: node_degrees[n], reverse=True)
        top = sorted_nodes[:limit]
        top_set = set(top)

        nodes = []
        for node_name in top:
            attrs = g.nodes[node_name] if g.has_node(node_name) else {}
            nodes.append(
                {
                    "id": f"entity:{node_name}",
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                    "uri": attrs.get("file_path", ""),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        for u, v, data in g.edges(data=True):
            if u in top_set and v in top_set:
                confidence = data.get("weight", 1.0)
                if min_confidence > 0 and confidence < min_confidence:
                    continue
                edges.append(
                    {
                        "source": f"entity:{u}",
                        "target": f"entity:{v}",
                        "relation": data.get("keywords", ""),
                        "confidence": confidence,
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during hub_entities read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.post("/explore")
def explore_node(request: ExploreRequest):
    """Explore graph from a specific entity.

    Returns ``{nodes: [...], edges: [...]}`` — the entity's neighborhood
    up to ``request.depth`` hops.
    """
    adapter = _get_adapter()

    result = adapter.explore_node(
        entity_name=request.entity_id,
        depth=request.depth,
    )
    if result is None:
        return {"status": "error", "message": "LightRAG not available"}
    # Normalize adapter format to frontend-expected format
    if "nodes" in result:
        result["nodes"] = _normalize_nodes(result["nodes"])
    if "edges" in result:
        result["edges"] = _normalize_edges(result["edges"])
    # Apply min_confidence filter on edges
    if request.min_confidence > 0 and "edges" in result:
        result["edges"] = [e for e in result["edges"] if e.get("confidence", 1.0) >= request.min_confidence]
    if "center" in result and isinstance(result["center"], dict):
        c = result["center"]
        result["center"] = {
            "id": c.get("id", ""),
            "label": c.get("name", c.get("id", "")),
            "name": c.get("name", ""),
            "nodeType": c.get("type", "Other"),
            "entityType": c.get("type", "Other"),
            "description": c.get("description", ""),
        }
    return result


@router.post("/find-path")
def find_path(request: FindPathRequest):
    """Find shortest path between two entities.

    Returns ``{nodes: [...], edges: [...]}`` along the path, or an empty
    graph if no path exists.
    """
    import networkx as nx

    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    src = request.from_id.removeprefix("entity:")
    tgt = request.to_id.removeprefix("entity:")

    try:
        path_nodes = nx.shortest_path(g, source=src, target=tgt)
    except (nx.NodeNotFound, nx.NetworkXNoPath):
        return {"nodes": [], "edges": []}
    except RuntimeError:
        logger.warning("Graph modified during find_path read")
        return {"nodes": [], "edges": []}

    try:
        nodes = []
        for node_name in path_nodes:
            attrs = g.nodes[node_name] if g.has_node(node_name) else {}
            nodes.append(
                {
                    "id": f"entity:{node_name}",
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                    "uri": attrs.get("file_path", ""),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        # Only include edges between consecutive path nodes
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            # Check both directions since the graph is directed
            if g.has_edge(u, v):
                data = g.edges[u, v]
                edges.append(
                    {
                        "source": f"entity:{u}",
                        "target": f"entity:{v}",
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
            elif g.has_edge(v, u):
                data = g.edges[v, u]
                edges.append(
                    {
                        "source": f"entity:{v}",
                        "target": f"entity:{u}",
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during find_path iteration")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/entities")
def list_entities(
    limit: int = Query(default=100, ge=1, le=500),
    entity_type: Optional[str] = Query(default=None),
):
    """List all entities.

    Returns ``{nodes: [...], edges: [...]}`` — entity nodes (and edges
    between them) up to ``limit``.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        collected = []
        for node_name in g.nodes():
            attrs = g.nodes[node_name] if g.has_node(node_name) else {}
            # Filter by entity_type if requested
            if entity_type and attrs.get("entity_type", "").lower() != entity_type.lower():
                continue
            collected.append((node_name, attrs))
            if len(collected) >= limit:
                break

        node_names = {name for name, _ in collected}
        nodes = []
        for node_name, attrs in collected:
            nodes.append(
                {
                    "id": f"entity:{node_name}",
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                    "uri": attrs.get("file_path", ""),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        for u, v, data in g.edges(data=True):
            if u in node_names and v in node_names:
                edges.append(
                    {
                        "source": f"entity:{u}",
                        "target": f"entity:{v}",
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during list_entities read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/concepts")
def list_concepts(limit: int = Query(default=100, ge=1, le=500)):
    """List all concepts.

    Returns ``{nodes: [...], edges: [...]}`` — concept nodes (entities
    with entityType "concept") and their interconnecting edges.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        collected = []
        for node_name in g.nodes():
            attrs = g.nodes[node_name] if g.has_node(node_name) else {}
            if attrs.get("entity_type", "").lower() == "concept":
                collected.append((node_name, attrs))
            if len(collected) >= limit:
                break

        node_names = {name for name, _ in collected}
        nodes = []
        for node_name, attrs in collected:
            nodes.append(
                {
                    "id": f"entity:{node_name}",
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Concept",
                    "entityType": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                }
            )

        edges = []
        for u, v, data in g.edges(data=True):
            if u in node_names and v in node_names:
                edges.append(
                    {
                        "source": f"entity:{u}",
                        "target": f"entity:{v}",
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during list_concepts read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/surprising")
def surprising_connections(
    min_shared: int = Query(default=2, ge=1),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    max_entities: int = Query(default=200, ge=1, le=1000),
):
    """Find surprising connections between entities.

    Returns ``{nodes: [...], edges: [...]}`` — entity pairs that share
    at least ``min_shared`` common neighbors.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        # Build adjacency sets only for the requested number of entities (not the full graph)
        candidate_nodes = list(g.nodes())[:max_entities]
        adj: Dict[str, set] = {n: set(g.neighbors(n)) for n in candidate_nodes}

        surprising_pairs: List[tuple] = []  # (u, v, shared_count)
        node_set = set()

        for u in candidate_nodes:
            for v in adj[u]:
                if v <= u or v not in adj:
                    continue
                shared = len(adj[u] & adj[v])
                if shared >= min_shared:
                    surprising_pairs.append((u, v, shared))
                    node_set.add(u)
                    node_set.add(v)

        if not node_set:
            return {"nodes": [], "edges": []}

        nodes = []
        for node_name in node_set:
            attrs = g.nodes[node_name] if g.has_node(node_name) else {}
            nodes.append(
                {
                    "id": f"entity:{node_name}",
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                    "uri": attrs.get("file_path", ""),
                    "source": attrs.get("source_id", ""),
                }
            )

        # Include edges between the surprising pairs
        edges = []
        pair_set = {(u, v) for u, v, _ in surprising_pairs}
        for u, v, data in g.edges(data=True):
            if (u, v) in pair_set or (v, u) in pair_set:
                confidence = data.get("weight", 1.0)
                if min_confidence > 0 and confidence < min_confidence:
                    continue
                edges.append(
                    {
                        "source": f"entity:{u}",
                        "target": f"entity:{v}",
                        "relation": data.get("keywords", ""),
                        "confidence": confidence,
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during surprising_connections read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/changelog")
def graph_changelog(
    limit: int = Query(default=50, ge=1, le=200),
    since: Optional[str] = Query(default=None),
):
    """Get recent graph changes.

    Returns ``{changes: [...]}``.  LightRAG does not natively track
    a changelog, so this always returns an empty list for now.
    The frontend polls this endpoint for incremental updates.
    """
    return {"changes": []}
