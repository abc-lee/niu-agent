"""Tests for list_entities, list_concepts, and graph_snapshot functions."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import (
    _init_schema, create_entity, create_concept, create_document,
    link_document_entity, link_document_concept, link_entities,
)


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_list_entities_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            result = niu_kg_server.list_entities()
            assert result == []
        finally:
            niu_kg_server._conn = orig


def test_list_entities_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            create_entity("e1", "Alice", "person")
            create_entity("e2", "Bob", "person")
            create_entity("org1", "Acme", "organization")

            result = niu_kg_server.list_entities()
            assert len(result) == 3
            names = {e["name"] for e in result}
            assert names == {"Alice", "Bob", "Acme"}

            persons = niu_kg_server.list_entities(entity_type="person")
            assert len(persons) == 2
            assert all(e["type"] == "person" for e in persons)
        finally:
            niu_kg_server._conn = orig


def test_list_concepts_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            create_concept("Machine Learning", description="ML concepts")
            create_concept("Deep Learning", description="DL subset of ML")

            result = niu_kg_server.list_concepts()
            assert len(result) == 2
            names = {c["name"] for c in result}
            assert names == {"Machine Learning", "Deep Learning"}
        finally:
            niu_kg_server._conn = orig


def test_graph_snapshot_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            result = niu_kg_server.graph_snapshot()
            assert result["nodes"] == []
            assert result["edges"] == []
            assert result["stats"]["nodes"] == 0
            assert result["stats"]["edges"] == 0
        finally:
            niu_kg_server._conn = orig


def test_graph_snapshot_with_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = kuzu.Database(str(Path(tmpdir) / "test.db"))
        conn = kuzu.Connection(db)
        _init_schema(conn)
        orig = _override_conn(conn)
        try:
            create_entity("alice", "Alice", "person")
            create_entity("bob", "Bob", "person")
            create_entity("acme", "Acme Corp", "organization")
            link_entities("alice", "bob", "KNOWS", confidence=0.9)
            link_entities("alice", "acme", "WORKS_AT", confidence=1.0)

            create_document("doc1", "Meeting Notes", content="Alice met Bob")
            link_document_entity("doc1", "alice", confidence=0.8)

            create_concept("Project X")
            link_document_concept("doc1", "Project X", confidence=0.7)

            result = niu_kg_server.graph_snapshot()

            node_types = {n["nodeType"] for n in result["nodes"]}
            assert "Entity" in node_types
            assert "Document" in node_types
            assert "Concept" in node_types

            edge_types = {e["edgeType"] for e in result["edges"]}
            assert "RELATED_TO" in edge_types
            assert "MENTIONS" in edge_types
            assert "CONTAINS" in edge_types

            assert result["stats"]["nodes"] == len(result["nodes"])
            assert result["stats"]["edges"] == len(result["edges"])
        finally:
            niu_kg_server._conn = orig
