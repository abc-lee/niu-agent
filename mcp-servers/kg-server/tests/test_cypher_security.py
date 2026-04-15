# mcp-servers/kg-server/tests/test_cypher_security.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
import niu_kg_server
from niu_kg_server import _init_schema, _validate_cypher_readonly, query_graph


def _override_conn(conn):
    original = niu_kg_server._conn
    niu_kg_server._conn = conn
    return original


def test_validate_blocks_create():
    """Should block CREATE statements."""
    assert _validate_cypher_readonly("CREATE (e:Entity {id: 'test'})") == False
    assert _validate_cypher_readonly("create (e:Entity)") == False


def test_validate_blocks_delete():
    """Should block DELETE statements."""
    assert _validate_cypher_readonly("DELETE (e:Entity)") == False
    assert _validate_cypher_readonly("MATCH (e) DELETE e") == False


def test_validate_allows_keywords_in_property_names():
    """Should allow write keywords as substrings in property names (word boundary)."""
    # created_at contains CREATE, removed contains REMOVE, etc.
    assert _validate_cypher_readonly("MATCH (e:Entity) WHERE e.created_at >= '2024-01-01' RETURN e") == True
    assert _validate_cypher_readonly("MATCH (n) WHERE n.removed = true RETURN n") == True
    assert _validate_cypher_readonly("MATCH (n) RETURN count(n) AS deletion_count") == True
    assert _validate_cypher_readonly("MATCH (n) RETURN n.merged_score") == True


def test_validate_blocks_set():
    """Should block SET statements."""
    assert _validate_cypher_readonly("SET e.name = 'test'") == False
    # SET with no trailing whitespace (e.g., SET( or SET.) should also be blocked
    assert _validate_cypher_readonly("SET(e.name = 'test')") == False
    # But SETTLEMENT should not be blocked (word boundary)
    assert _validate_cypher_readonly("MATCH (n) WHERE n.type = 'SETTLEMENT' RETURN n") == True


def test_validate_blocks_remove():
    """Should block REMOVE statements."""
    assert _validate_cypher_readonly("REMOVE e.name") == False


def test_validate_blocks_merge():
    """Should block MERGE statements."""
    assert _validate_cypher_readonly("MERGE (e:Entity {id: 'test'})") == False


def test_validate_blocks_drop():
    """Should block DROP statements."""
    assert _validate_cypher_readonly("DROP TABLE Entity") == False
    assert _validate_cypher_readonly("drop table if exists Entity") == False


def test_validate_allows_match():
    """Should allow MATCH statements."""
    assert _validate_cypher_readonly("MATCH (e:Entity) RETURN e.id") == True


def test_validate_allows_match_with_where():
    """Should allow MATCH with WHERE."""
    assert _validate_cypher_readonly("MATCH (e:Entity) WHERE e.id = 'test' RETURN e") == True


def test_validate_allows_match_with_order_by():
    """Should allow MATCH with ORDER BY."""
    assert _validate_cypher_readonly("MATCH (e:Entity) RETURN e ORDER BY e.name") == True


def test_validate_allows_match_with_limit():
    """Should allow MATCH with LIMIT."""
    assert _validate_cypher_readonly("MATCH (e:Entity) RETURN e LIMIT 10") == True


def test_query_graph_blocks_write():
    """query_graph should block write operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            result = query_graph("CREATE (e:Entity {id: 'test'})")
            assert "error" in result
            assert "read-only" in result["error"].lower()
        finally:
            niu_kg_server._conn = orig


def test_query_graph_allows_read():
    """query_graph should allow read operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        orig = _override_conn(conn)
        try:
            result = query_graph("MATCH (e:Entity) RETURN e.id LIMIT 5")
            # Should succeed (returns list, may be empty)
            assert isinstance(result, list)
        finally:
            niu_kg_server._conn = orig


def test_validate_allows_keywords_in_string_literals():
    """Should allow write keywords inside string literals."""
    # 'DELETE ME' is a string value, not a DELETE command
    assert _validate_cypher_readonly("MATCH (n) WHERE n.name = 'DELETE ME' RETURN n") == True
    assert _validate_cypher_readonly("MATCH (n) WHERE n.name = 'CREATE TABLE' RETURN n") == True


def test_validate_blocks_foreach():
    """Should block FOREACH (can be used for mutation)."""
    assert _validate_cypher_readonly("FOREACH (x IN [1,2,3] | CREATE (n:Test {id: x}))") == False


def test_validate_allows_keywords_in_comments():
    """Should allow write keywords inside comments."""
    assert _validate_cypher_readonly("MATCH (n) // CREATE something\nRETURN n") == True
