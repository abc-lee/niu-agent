# mcp-servers/kg-server/tests/test_schema_migration.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
from niu_kg_server import _init_schema


def test_entity_has_timestamps():
    """Entity table must have created_at and updated_at fields."""
    # Create temporary database
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)

        # Initialize schema
        _init_schema(conn)

        result = conn.execute("CALL TABLE_INFO('Entity') RETURN *")
        # row format: [index, property_name, type, default, is_primary]
        columns = {row[1] for row in result}
        assert 'created_at' in columns, f"Entity missing created_at. Columns: {columns}"
        assert 'updated_at' in columns, f"Entity missing updated_at. Columns: {columns}"


def test_relation_has_confidence():
    """MENTIONS relation must have confidence and created_at."""
    # Create temporary database
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)

        # Initialize schema
        _init_schema(conn)

        result = conn.execute("CALL TABLE_INFO('MENTIONS') RETURN *")
        # row format: [index, property_name, type, default, is_primary]
        columns = {row[1] for row in result}
        assert 'confidence' in columns, f"MENTIONS missing confidence. Columns: {columns}"
        assert 'created_at' in columns, f"MENTIONS missing created_at. Columns: {columns}"
