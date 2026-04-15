# mcp-servers/kg-server/tests/test_integration.py
"""Integration test: all tools working together in a realistic scenario."""
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import (
    _init_schema, create_entity, link_entities, graph_stats,
    explore_node, find_path, hub_entities, surprising_connections,
    graph_changelog
)


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_full_workflow():
    """Test complete workflow: create graph, query stats, explore, path finding."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # === Build graph ===
            # Hub: CEO connected to many
            create_entity("ceo", "张总", "人物")
            create_entity("cto", "李CTO", "人物")
            create_entity("coo", "王COO", "人物")
            create_entity("engineer_a", "工程师A", "人物")
            create_entity("engineer_b", "工程师B", "人物")
            create_entity("company_x", "X公司", "组织")

            link_entities("ceo", "cto", "MANAGES", confidence=1.0)
            link_entities("ceo", "coo", "MANAGES", confidence=1.0)
            link_entities("cto", "engineer_a", "MANAGES", confidence=0.9)
            link_entities("cto", "engineer_b", "MANAGES", confidence=0.9)
            link_entities("ceo", "company_x", "OWNS", confidence=1.0)
            link_entities("engineer_a", "engineer_b", "KNOWS", confidence=0.7)
            link_entities("engineer_b", "engineer_a", "KNOWS", confidence=0.7)

            # === Graph Stats ===
            stats = graph_stats()
            assert stats["nodes"]["total"] == 6
            assert stats["edges"]["total"] == 7
            assert stats["density"] > 0.0
            assert "high (0.7-1.0)" in stats["edges"]["by_confidence"]
            assert stats["edges"]["by_confidence"]["high (0.7-1.0)"] == 7  # All high confidence

            # === Explore ===
            ceo_explore = explore_node("ceo", depth=2)
            assert ceo_explore["center"]["id"] == "ceo"
            assert ceo_explore["stats"]["nodes"] >= 3  # cto, coo, company_x

            # === Hub Entities ===
            hubs = hub_entities(limit=3)
            assert len(hubs["entities"]) == 3
            # engineer_a has 3 connections (outgoing to b, incoming from b and cto)
            # ceo has 3 connections (outgoing to cto/coo/company, incoming from cto)
            # cto has 2 connections
            assert hubs["entities"][0]["connections"] >= 2  # engineer_a or ceo top

            # === Find Path ===
            path = find_path("engineer_a", "company_x", max_depth=5)
            assert path["found"] == True
            assert path["hops"] == 3  # engineer_a -> cto -> ceo -> company_x

            # === Surprising Connections ===
            # engineer_a and engineer_b KNOW each other, but also both connected to cto
            # They are directly connected so NOT surprising
            surprising = surprising_connections(min_shared=2)
            # The pair (engineer_a, engineer_b) has min_shared=2 via cto and ceo
            # Actually: engineer_a -> cto, engineer_b -> cto, and engineer_a <-> engineer_b
            # So they ARE directly connected - no surprising connections
            # Let's check a different scenario:
            # engineer_a and coo share cto (engineer_a->cto<-coo) and ceo (engineer_a->cto->ceo->coo)
            # ... but are they directly connected? No!
            # Wait: engineer_a -> cto, cto -> engineer_a (but that's KNOWS, not from cto to engineer_a)
            # engineer_a is connected to: cto (MANAGES), engineer_b (KNOWS)
            # coo is connected to: ceo (MANAGES)
            # So coo and engineer_a share... no shared neighbors!
            # So surprising should be empty
            assert surprising["connections"] == []

            # === Changelog ===
            changelog = graph_changelog(limit=20)
            assert len(changelog["changes"]) == 13  # 6 entities + 7 edges
            # Most recent first
            assert changelog["changes"][0]["type"] in ("entity_created", "edge_created")
            assert "timestamp" in changelog["changes"][0]

        finally:
            niu_kg_server._conn = orig
