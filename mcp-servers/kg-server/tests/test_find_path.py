# mcp-servers/kg-server/tests/test_find_path.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, find_path, create_entity, link_entities


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_find_path_shortest():
    """Find shortest path between two entities."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # Create test graph: A -[KNOWS]-> B -[WORKS_AT]-> C
            create_entity("person_a", "用户A", "人物")
            create_entity("person_b", "用户B", "人物")
            create_entity("org_c", "公司C", "组织")
            link_entities("person_a", "person_b", "KNOWS", confidence=0.9)
            link_entities("person_b", "org_c", "WORKS_AT", confidence=1.0)

            # Find path from A to C
            result = find_path("person_a", "org_c", max_depth=5)

            assert result["found"] == True
            assert result["hops"] == 2
            assert len(result["path"]) == 3  # A, B, C
        finally:
            niu_kg_server._conn = orig


def test_find_path_no_path():
    """Find path when no path exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # Create isolated entities
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")

            # No path between them
            result = find_path("entity_a", "entity_b", max_depth=5)

            assert result["found"] == False
        finally:
            niu_kg_server._conn = orig


def test_find_path_source_not_found():
    """Find path when source doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_b", "实体B", "人物")

            result = find_path("nonexistent", "entity_b", max_depth=5)

            assert result["found"] == False
            assert "error" in result
        finally:
            niu_kg_server._conn = orig


def test_find_path_direct_relation():
    """Find path for directly connected entities."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # Create directly connected entities
            create_entity("person_a", "用户A", "人物")
            create_entity("person_b", "用户B", "人物")
            link_entities("person_a", "person_b", "KNOWS", confidence=0.9)

            result = find_path("person_a", "person_b", max_depth=5)

            assert result["found"] == True
            assert result["hops"] == 1
            assert len(result["path"]) == 2
        finally:
            niu_kg_server._conn = orig


def test_find_path_self_loop():
    """Find path from entity to itself should return 0 hops."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("person_a", "用户A", "人物")

            result = find_path("person_a", "person_a", max_depth=5)

            assert result["found"] == True
            assert result["hops"] == 0
            assert len(result["path"]) == 1
            assert result["path"][0]["id"] == "person_a"
        finally:
            niu_kg_server._conn = orig
