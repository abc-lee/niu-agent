# mcp-servers/kg-server/tests/test_confidence_mechanism.py
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
from niu_kg_server import _init_schema, _infer_confidence, _get_timestamp


def test_get_timestamp_format():
    """Timestamp should be ISO 8601 format."""
    ts = _get_timestamp()
    # Should be parseable as ISO format
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    assert dt.tzinfo is not None, "Timestamp should have timezone"


def test_infer_confidence_default():
    """Without argument, should return 1.0 (highest confidence)."""
    conf = _infer_confidence(None)
    assert conf == 1.0, f"Default confidence should be 1.0, got {conf}"


def test_infer_confidence_bounds():
    """Confidence should be clamped to 0.0-1.0."""
    assert _infer_confidence(1.5) == 1.0, "Should clamp to 1.0"
    assert _infer_confidence(-0.5) == 0.0, "Should clamp to 0.0"
    assert _infer_confidence(0.75) == 0.75, "Should keep valid value"


def test_create_entity_has_timestamps():
    """Entity creation should include timestamps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)

        # Initialize schema
        _init_schema(conn)

        # Create entity
        ts_before = datetime.now(timezone.utc)
        conn.execute(
            "MERGE (e:Entity {id: 'test_entity'}) SET e.name = 'Test', e.type = '测试', e.description = '', e.created_at = $ts, e.updated_at = $ts",
            {"ts": _get_timestamp()}
        )

        # Verify
        result = conn.execute("MATCH (e:Entity {id: 'test_entity'}) RETURN e.created_at, e.updated_at")
        row = list(result)[0]
        created_at = row[0]
        updated_at = row[1]

        assert created_at is not None, "created_at should not be None"
        assert updated_at is not None, "updated_at should not be None"


def test_link_has_confidence_and_timestamp():
    """Link creation should include confidence and created_at."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)

        # Initialize schema
        _init_schema(conn)

        # Create entities
        conn.execute(
            "CREATE (d:Document {uri: 'doc1', title: 'Test', content: '', source: '', created_at: ''})"
        )
        conn.execute(
            "MERGE (e:Entity {id: 'entity1', name: 'Entity1', type: 'Test', description: '', created_at: '', updated_at: ''})"
        )

        # Link with confidence and timestamp
        confidence = 0.9
        ts = _get_timestamp()
        conn.execute(
            "MATCH (d:Document {uri: 'doc1'}), (e:Entity {id: 'entity1'}) CREATE (d)-[:MENTIONS {confidence: $conf, created_at: $ts}]->(e)",
            {"conf": confidence, "ts": ts}
        )

        # Verify
        result = conn.execute("MATCH ()-[r:MENTIONS]->() RETURN r.confidence, r.created_at")
        row = list(result)[0]
        conf = row[0]
        created_at = row[1]

        assert abs(conf - confidence) < 0.001, f"Confidence should be ~{confidence}, got {conf}"
        assert created_at == ts, f"created_at should be {ts}, got {created_at}"
