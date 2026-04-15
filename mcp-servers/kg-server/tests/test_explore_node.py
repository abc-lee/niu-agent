# mcp-servers/kg-server/tests/test_explore_node.py
import sys
import tempfile
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kuzu
from niu_kg_server import _init_schema, explore_node, link_entities, create_entity


def test_explore_node_basic():
    """Explore node should return neighbors and edges."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        # Create test entities
        create_entity("person_zhang", "张三", "人物")
        create_entity("person_li", "李四", "人物")
        link_entities("person_zhang", "person_li", "KNOWS", confidence=0.9)

        # Explore from 张三
        result = explore_node("person_zhang", depth=1, min_confidence=0.5)

        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 1  # 李四
        assert abs(result["edges"][0]["confidence"] - 0.9) < 0.001, f"Confidence should be ~0.9, got {result['edges'][0]['confidence']}"


def test_explore_node_filters_by_confidence():
    """Explore should filter by min_confidence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        # Create test entities with low confidence
        create_entity("person_a", "用户A", "人物")
        create_entity("person_b", "用户B", "人物")
        link_entities("person_a", "person_b", "KNOWS", confidence=0.3)

        # Explore with high min_confidence - should return no nodes
        result = explore_node("person_a", depth=1, min_confidence=0.5)
        assert len(result["nodes"]) == 0

        # Explore with low min_confidence - should return node
        result = explore_node("person_a", depth=1, min_confidence=0.1)
        assert len(result["nodes"]) == 1


def test_explore_node_not_found():
    """Explore should return error for non-existent entity."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        result = explore_node("nonexistent", depth=1)
        assert "error" in result


def test_explore_node_depth():
    """Explore should respect depth parameter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        _init_schema(conn)

        # Create chain: A -> B -> C
        create_entity("node_a", "节点A", "人物")
        create_entity("node_b", "节点B", "人物")
        create_entity("node_c", "节点C", "人物")
        link_entities("node_a", "node_b", "KNOWS", confidence=0.9)
        link_entities("node_b", "node_c", "KNOWS", confidence=0.9)

        # Depth 1: should only find B
        result = explore_node("node_a", depth=1)
        assert len(result["nodes"]) == 1

        # Depth 2: should find B and C
        result = explore_node("node_a", depth=2)
        assert len(result["nodes"]) == 2
