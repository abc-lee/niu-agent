"""语义完整性检查的 TDD 测试。"""
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_integrity import _load_graphml, _parse_brain_meta


def _write_test_graphml(path: Path, nodes: list[dict], edges: list[dict] = None):
    """生成测试用 GraphML 文件。nodes 是 [{id, entity_type, description, source_id}, ...]"""
    edges = edges or []
    ns = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for n in nodes:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": n["id"]})
        if "entity_type" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = n["entity_type"]
        if "description" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = n["description"]
        if "source_id" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = n["source_id"]
    for e in edges:
        edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": e["source"], "target": e["target"]})
        if "keywords" in e:
            ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = e["keywords"]
    tree = ET.ElementTree(root)
    tree.write(path, xml_declaration=True, encoding="utf-8")


def test_load_graphml_returns_node_metadata(tmp_path):
    """_load_graphml 应返回 node 的 description 和 entity_type（不只 id）"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:0<SEP>brain_meta_shrink_count:1", "source_id": "brain_聊天历史"},
        {"id": "Python", "entity_type": "concept", "description": "编程语言", "source_id": "doc-abc"},
    ])

    node_ids, edges, node_meta, err = _load_graphml(graphml)

    assert err is None
    assert node_ids == {"聊天历史脑区", "Python"}
    assert edges == []
    assert node_meta["聊天历史脑区"]["entity_type"] == "brainregion"
    assert "brain_meta_shrink_count:1" in node_meta["聊天历史脑区"]["description"]
    assert node_meta["聊天历史脑区"]["source_id"] == "brain_聊天历史"
    assert node_meta["Python"]["entity_type"] == "concept"


def test_load_graphml_node_without_metadata(tmp_path):
    """GraphML node 没有 d1/d2/d3 字段时，meta 字段是空字符串而非 KeyError"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[{"id": "bare_node"}])

    _, _, node_meta, err = _load_graphml(graphml)

    assert err is None
    assert node_meta["bare_node"] == {"entity_type": "", "description": "", "source_id": ""}


def test_load_graphml_backward_compat_node_ids(tmp_path):
    """扩展后 _load_graphml 仍兼容原 (node_ids, edges, err) 三元组签名调用方式"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[{"id": "X"}], edges=[{"source": "X", "target": "Y"}])

    result = _load_graphml(graphml)

    assert len(result) == 4
    node_ids, edges, node_meta, err = result
    assert "X" in node_ids
    assert ("X", "Y") in edges
    assert err is None


def test_parse_brain_meta_standard_fields():
    """_parse_brain_meta 解析标准 brain_meta_* 字段"""
    desc = "brain_meta_region_id:default_聊天历史<SEP>brain_meta_size:89<SEP>brain_meta_shrink_count:1"
    result = _parse_brain_meta(desc)
    assert result == {
        "region_id": "default_聊天历史",
        "size": "89",
        "shrink_count": "1",
    }


def test_parse_brain_meta_mixed_with_normal_text():
    """description 含普通文本 + brain_meta_* 字段（真实数据格式）"""
    desc = "日常对话中提炼的偏好<SEP>brain_meta_region_id:default_聊天历史<SEP>brain_meta_size:89"
    result = _parse_brain_meta(desc)
    # 普通文本"日常对话中提炼的偏好"不含 brain_meta_ 前缀，被过滤
    assert result == {
        "region_id": "default_聊天历史",
        "size": "89",
    }


def test_parse_brain_meta_empty_value_field():
    """brain_meta_representative: 这种空值字段应保留为空字符串"""
    desc = "brain_meta_region_id:<SEP>brain_meta_size:0<SEP>brain_meta_representative:"
    result = _parse_brain_meta(desc)
    assert result == {
        "region_id": "",
        "size": "0",
        "representative": "",
    }


def test_parse_brain_meta_empty_description():
    """空 description 返回空 dict"""
    assert _parse_brain_meta("") == {}
    assert _parse_brain_meta(None) == {}


def test_check_brainregion_semantic_zombie_detects_zombie(tmp_path):
    """检测 description 含'被删除'但 node 仍存在的脑区"""
    from niu_api.internal.lightrag_integrity import check_brainregion_semantic_zombie

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        # 僵尸脑区 1：description 含"被删除的重复脑区实体之一"
        {"id": "智家全维资料脑区", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一。<SEP>brain_meta_size:0<SEP>brain_meta_shrink_count:1"},
        # 僵尸脑区 2：description 含"已删除"
        {"id": "智家使用运维脑区", "entity_type": "brainregion",
         "description": "已删除的脑区。<SEP>brain_meta_size:0"},
        # 正常脑区
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10"},
        # 非脑区实体（即使 description 含"被删除"也不该报）
        {"id": "普通实体", "entity_type": "concept",
         "description": "被删除的文档内容"},
    ])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_semantic_zombie()

    assert report["name"] == "brainregion_semantic_zombie"
    assert len(report["errors"]) == 2
    zombie_names = [e["ref_key"] for e in report["errors"]]
    assert "智家全维资料脑区" in zombie_names
    assert "智家使用运维脑区" in zombie_names
    # 非脑区实体不报
    assert "普通实体" not in zombie_names
    # severity 应该是 major
    assert all(e["severity"] == "major" for e in report["errors"])


def test_check_brainregion_semantic_zombie_clean_data_ok(tmp_path):
    """正常脑区（description 不含'被删除'标记）→ 0 errors"""
    from niu_api.internal.lightrag_integrity import check_brainregion_semantic_zombie

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10"},
        {"id": "文档库脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:5"},
    ])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_semantic_zombie()

    assert report["errors"] == []


def test_check_entity_chunks_source_id_mismatch_detects_inconsistency(tmp_path):
    """entity_chunks 的 chunk_ids 跟 GraphML node 的 d3 source_id 不一致"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_source_id_mismatch

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        # 僵尸脑区：d3 source_id = chunk-A，但 entity_chunks 指向 chunk-B
        {"id": "智家脑区X", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一",
         "source_id": "chunk-AAAAAAAA"},
        # 正常脑区：d3 source_id = chunk-C，entity_chunks 也指向 chunk-C
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10",
         "source_id": "chunk-CCCCCCCC"},
    ])

    # 写 entity_chunks
    ec_path = tmp_path / "kv_store_entity_chunks.json"
    import json
    ec_path.write_text(json.dumps({
        "智家脑区X": {"chunk_ids": ["chunk-BBBBBBBB"], "count": 1},  # 不一致
        "聊天历史脑区": {"chunk_ids": ["chunk-CCCCCCCC"], "count": 1},  # 一致
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_entity_chunks_source_id_mismatch()

    assert report["name"] == "entity_chunks_source_id_mismatch"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["ref_key"] == "智家脑区X"
    assert report["errors"][0]["graphml_source_id"] == "chunk-AAAAAAAA"
    assert "chunk-BBBBBBBB" in report["errors"][0]["entity_chunks_ids"]


def test_check_entity_chunks_source_id_mismatch_consistent_ok(tmp_path):
    """entity_chunks 跟 GraphML d3 source_id 一致 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_source_id_mismatch

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion",
         "source_id": "chunk-AAAA"},
    ])

    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "脑区A": {"chunk_ids": ["chunk-AAAA"], "count": 1},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_entity_chunks_source_id_mismatch()

    assert report["errors"] == []
