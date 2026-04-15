# mcp-servers/kg-server/tests/test_graph_stats.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, graph_stats, create_entity, link_entities


def _override_conn(conn):
    """Override global connection so graph_stats() uses the right DB."""
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_graph_stats_basic():
    """Graph stats should return node/edge counts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        # Override BEFORE calling functions that use the global connection
        orig = _override_conn(conn)
        try:
            # Create test data
            create_entity("person_a", "用户A", "人物")
            create_entity("org_b", "公司B", "组织")
            link_entities("person_a", "org_b", "WORKS_AT", confidence=0.9)

            stats = graph_stats()
        finally:
            niu_kg_server._conn = orig

        assert stats["nodes"]["total"] == 2
        assert "人物" in stats["nodes"]["by_type"]
        assert "组织" in stats["nodes"]["by_type"]
        assert stats["edges"]["total"] == 1


def test_graph_stats_has_confidence_distribution():
    """Graph stats should include confidence distribution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")
            link_entities("entity_a", "entity_b", "KNOWS", confidence=0.9)
            stats = graph_stats()
        finally:
            niu_kg_server._conn = orig

        assert "by_confidence" in stats["edges"]
        assert "high (0.7-1.0)" in stats["edges"]["by_confidence"]


def test_graph_stats_has_density():
    """Graph stats should include density."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")
            stats = graph_stats()
        finally:
            niu_kg_server._conn = orig

        assert "density" in stats
        assert isinstance(stats["density"], float)


def test_graph_stats_empty_graph():
    """Graph stats on empty graph should return zeros."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            stats = graph_stats()
        finally:
            niu_kg_server._conn = orig

        assert stats["nodes"]["total"] == 0
        assert stats["edges"]["total"] == 0
        assert stats["density"] == 0.0
