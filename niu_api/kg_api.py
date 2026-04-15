"""
Knowledge Graph API endpoints for the graph visualization UI.

Routes call niu_kg_server functions directly (same-process import, like ToolRegistry).
"""

from typing import Literal, Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


class ExploreRequest(BaseModel):
    entity_id: str
    depth: int = Field(default=2, ge=1, le=5)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    direction: Literal["both", "outgoing", "incoming"] = "both"


class FindPathRequest(BaseModel):
    from_id: str
    to_id: str
    max_depth: int = Field(default=5, ge=1, le=10)


def _get_kg():
    """Import niu_kg_server module (in-process, same as ToolRegistry)."""
    import niu_kg_server
    return niu_kg_server


@router.get("/snapshot")
async def graph_snapshot(
    limit: int = Query(default=200, ge=1, le=500),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Get full graph snapshot for visualization."""
    kg = _get_kg()
    return kg.graph_snapshot(limit=limit, min_confidence=min_confidence)


@router.get("/stats")
async def graph_stats():
    """Get knowledge graph statistics."""
    kg = _get_kg()
    return kg.graph_stats()


@router.get("/hubs")
async def hub_entities(
    limit: int = Query(default=20, ge=1, le=100),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
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
async def list_entities(
    limit: int = Query(default=100, ge=1, le=500),
    entity_type: Optional[str] = Query(default=None),
):
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
async def graph_changelog(
    limit: int = Query(default=50, ge=1, le=200),
    since: Optional[str] = Query(default=None),
):
    """Get recent graph changes."""
    kg = _get_kg()
    return kg.graph_changelog(limit=limit, since=since)
