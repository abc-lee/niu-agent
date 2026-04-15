# mcp-servers/kg-server/tests/test_hub_entities.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, hub_entities, create_entity, link_entities


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_hub_entities_returns_top_connected():
    """Hub entities should return entities sorted by connection count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            # Create hub: person_a connected to 3 others
            create_entity("person_a", "用户A", "人物")
            create_entity("person_b", "用户B", "人物")
            create_entity("person_c", "用户C", "人物")
            create_entity("person_d", "用户D", "人物")
            link_entities("person_a", "person_b", "KNOWS", confidence=0.9)
            link_entities("person_a", "person_c", "KNOWS", confidence=0.9)
            link_entities("person_a", "person_d", "KNOWS", confidence=0.9)
            # person_b only connected to person_a
            link_entities("person_b", "person_a", "KNOWS", confidence=0.9)

            result = hub_entities(limit=5)
        finally:
            niu_kg_server._conn = orig

        assert "entities" in result
        assert len(result["entities"]) == 4
        # person_a should be first (3 connections outgoing + 1 incoming = 4)
        assert result["entities"][0]["id"] == "person_a"
        assert result["entities"][0]["connections"] == 4


def test_hub_entities_respects_limit():
    """Hub entities should respect the limit parameter."""
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

            result = hub_entities(limit=1)
        finally:
            niu_kg_server._conn = orig

        assert len(result["entities"]) == 1


def test_hub_entities_empty_graph():
    """Hub entities on empty graph should return empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            result = hub_entities()
        finally:
            niu_kg_server._conn = orig

        assert result["entities"] == []


def test_hub_entities_filters_low_confidence():
    """Hub entities should filter by min_confidence."""
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
            link_entities("entity_a", "entity_b", "KNOWS", confidence=0.3)  # low
            link_entities("entity_a", "entity_c", "KNOWS", confidence=0.9)  # high

            # With high threshold, only 1 connection
            result = hub_entities(min_confidence=0.5)
        finally:
            niu_kg_server._conn = orig

        # entity_a has only 1 connection above 0.5
        assert len(result["entities"]) >= 1
        top = result["entities"][0]
        assert top["connections"] == 1
