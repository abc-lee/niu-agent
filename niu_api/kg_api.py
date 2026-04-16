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


@router.post("/cleanup")
async def cleanup_graph():
    """Clean up test data and fix entity types in the knowledge graph."""
    kg = _get_kg()
    conn = kg.get_connection()
    results = {}

    # 1. Delete test data entities
    test_ids = [
        "person_zhang", "person_li", "person_a", "person_b",
        "entity_a", "entity_b", "node_a", "node_b", "node_c", "org_c",
    ]
    deleted = 0
    for eid in test_ids:
        try:
            r = conn.execute(f"MATCH (e:Entity {{id: '{eid}'}}) DETACH DELETE e").get_all()
            deleted += 1
        except Exception:
            pass
    results["deleted_test_entities"] = deleted

    # 2. Fix Chinese type labels -> English
    type_fixes = {"人物": "person", "组织": "organization"}
    fixed = 0
    for old_type, new_type in type_fixes.items():
        try:
            r = conn.execute(
                f"MATCH (e:Entity {{type: '{old_type}'}}) SET e.type = '{new_type}' RETURN count(e) as cnt"
            ).get_all()
            cnt = r[0]["cnt"] if r else 0
            fixed += cnt
        except Exception:
            pass
    results["fixed_type_labels"] = fixed

    # 2.5. Fix misclassified other -> technology (known tech entities)
    # ID prefix is wrong (other:xxx should be technology:xxx), need delete+recreate
    tech_fixes = {
        "other:PageRank": ("technology:PageRank", "PageRank", "technology"),
        "other:MCP": ("technology:MCP", "MCP", "technology"),
        "other:Cypher": ("technology:Cypher", "Cypher", "technology"),
        "other:NetworkX": ("technology:NetworkX", "NetworkX", "technology"),
        "other:KuzuDB": ("technology:KuzuDB", "KuzuDB", "technology"),
    }
    tech_fixed = 0
    for old_id, (new_id, name, new_type) in tech_fixes.items():
        try:
            # Check if old entity exists (use parameterized query to avoid escaping issues)
            r = conn.execute(
                "MATCH (e:Entity {id: $id}) RETURN e.name as name, e.description as desc",
                {"id": old_id},
            ).get_all()
            if r:
                desc = r[0].get("desc", "") or ""
                # Delete old
                conn.execute(
                    "MATCH (e:Entity {id: $id}) DETACH DELETE e",
                    {"id": old_id},
                ).get_all()
                # Create new with correct ID
                conn.execute(
                    "CREATE (e:Entity {id: $new_id, name: $name, type: $type, description: $desc, created_at: timestamp()})",
                    {"new_id": new_id, "name": name, "type": new_type, "desc": desc},
                ).get_all()
                tech_fixed += 1
        except Exception as ex:
            logger.warning(f"[KG Cleanup] Failed to fix {old_id}: {ex}")
    results["fixed_other_to_technology"] = tech_fixed

    # 3. Delete misclassified entities
    misclassified = ["person:游戏", "technology:chat-with-file-processor", "mcp_tool:chat-with-file-processor"]
    mis_deleted = 0
    for eid in misclassified:
        try:
            conn.execute(f"MATCH (e:Entity {{id: '{eid}'}}) DETACH DELETE e").get_all()
            mis_deleted += 1
        except Exception:
            pass
    results["deleted_misclassified"] = mis_deleted

    # 4. Delete orphan Document nodes (no MENTIONS edges)
    try:
        r = conn.execute(
            "MATCH (d:Document) WHERE NOT (d)-[:MENTIONS]->() DETACH DELETE d RETURN count(d) as cnt"
        ).get_all()
        results["deleted_orphan_documents"] = r[0]["cnt"] if r else 0
    except Exception:
        results["deleted_orphan_documents"] = 0

    return {"status": "ok", "results": results}
