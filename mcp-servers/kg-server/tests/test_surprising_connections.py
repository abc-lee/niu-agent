# mcp-servers/kg-server/tests/test_surprising_connections.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, surprising_connections, create_entity, link_entities


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_surprising_connections_finds_shared_neighbors():
    """Find entities that share neighbors but aren't directly connected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # Create: A -> C, B -> C, A and B NOT directly connected
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")
            create_entity("entity_c", "实体C", "人物")
            link_entities("entity_a", "entity_c", "KNOWS", confidence=0.9)
            link_entities("entity_b", "entity_c", "KNOWS", confidence=0.9)

            result = surprising_connections(min_shared=1)
        finally:
            niu_kg_server._conn = orig

        assert "connections" in result
        # A and B share C as neighbor
        assert len(result["connections"]) == 1
        conn_info = result["connections"][0]
        assert conn_info["shared_neighbors"] == 1
        assert conn_info["neighbors"][0]["id"] == "entity_c"


def test_surprising_connections_excludes_directly_connected():
    """Should not return pairs that are already directly connected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")
            create_entity("entity_c", "实体C", "人物")
            # A connected to B and C
            link_entities("entity_a", "entity_b", "KNOWS", confidence=0.9)
            link_entities("entity_a", "entity_c", "KNOWS", confidence=0.9)
            # B also connected to C
            link_entities("entity_b", "entity_c", "KNOWS", confidence=0.9)

            result = surprising_connections(min_shared=1)
        finally:
            niu_kg_server._conn = orig

        # A-B and A-C and B-C are all directly connected, so no surprising connections
        assert result["connections"] == []


def test_surprising_connections_empty_graph():
    """Empty graph should return empty connections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            result = surprising_connections()
        finally:
            niu_kg_server._conn = orig

        assert result["connections"] == []


def test_surprising_connections_respects_min_shared():
    """Should only return pairs with >= min_shared neighbors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("a", "实体A", "人物")
            create_entity("b", "实体B", "人物")
            create_entity("c", "实体C", "人物")
            link_entities("a", "c", "KNOWS", confidence=0.9)
            link_entities("b", "c", "KNOWS", confidence=0.9)

            # min_shared=2 should return nothing (only 1 shared neighbor)
            result = surprising_connections(min_shared=2)
        finally:
            niu_kg_server._conn = orig

        assert result["connections"] == []
