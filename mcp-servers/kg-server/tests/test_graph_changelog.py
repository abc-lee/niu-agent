# mcp-servers/kg-server/tests/test_graph_changelog.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, graph_changelog, create_entity, link_entities


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_graph_changelog_returns_recent_changes():
    """Changelog should return recent entity and edge creations."""
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

            result = graph_changelog(limit=10)
        finally:
            niu_kg_server._conn = orig

        assert "changes" in result
        assert len(result["changes"]) == 3  # 2 entities + 1 edge
        types = [c["type"] for c in result["changes"]]
        assert "entity_created" in types
        assert "edge_created" in types


def test_graph_changelog_respects_limit():
    """Changelog should respect the limit parameter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_a", "实体A", "人物")
            create_entity("entity_b", "实体B", "人物")

            result = graph_changelog(limit=1)
        finally:
            niu_kg_server._conn = orig

        assert len(result["changes"]) <= 1


def test_graph_changelog_empty_graph():
    """Empty graph should return empty changes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            result = graph_changelog()
        finally:
            niu_kg_server._conn = orig

        assert result["changes"] == []


def test_graph_changelog_has_timestamps():
    """Changelog entries should have ISO 8601 timestamps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            create_entity("entity_x", "实体X", "人物")
            result = graph_changelog()
        finally:
            niu_kg_server._conn = orig

        assert len(result["changes"]) == 1
        change = result["changes"][0]
        assert "timestamp" in change
        assert change["timestamp"] is not None
        assert "T" in change["timestamp"]  # ISO 8601 format
