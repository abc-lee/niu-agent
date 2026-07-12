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


def test_check_chunk_shared_by_too_many_entities_detects_anomaly(tmp_path):
    """一个 chunk 被超过阈值个 entity 共享 → 报错"""
    from niu_api.internal.lightrag_integrity import check_chunk_shared_by_too_many_entities

    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "脑区1": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区2": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区3": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区4": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区5": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        # 正常 entity 指向独立 chunk
        "实体A": {"chunk_ids": ["chunk-a-1"], "count": 1},
        "实体B": {"chunk_ids": ["chunk-b-1"], "count": 1},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        # 阈值设 3（测试用），5 个脑区共享 chunk-shared-xxx
        report = check_chunk_shared_by_too_many_entities(threshold=3)

    assert report["name"] == "chunk_shared_by_too_many_entities"
    assert len(report["errors"]) == 1
    err = report["errors"][0]
    assert err["chunk_id"] == "chunk-shared-xxx"
    assert err["entity_count"] == 5
    assert set(err["entities"]) == {"脑区1", "脑区2", "脑区3", "脑区4", "脑区5"}


def test_check_chunk_shared_by_too_many_entities_normal_ok(tmp_path):
    """每个 chunk 被不超过阈值个 entity 共享 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_chunk_shared_by_too_many_entities

    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "实体A": {"chunk_ids": ["chunk-shared"], "count": 1},
        "实体B": {"chunk_ids": ["chunk-shared"], "count": 1},
        "实体C": {"chunk_ids": ["chunk-other"], "count": 1},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_chunk_shared_by_too_many_entities(threshold=3)

    assert report["errors"] == []


def test_check_vdb_entities_orphan_detects_orphan_vectors(tmp_path):
    """vdb_entities 有向量但 GraphML 没 node → 报错"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_orphan

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "存在的脑区", "entity_type": "brainregion"},
    ])

    # vdb_entities 写一些向量（nano-vectordb 格式）
    # 注意：真实 vdb 顶层字段是 `data`（不是 `__data__`），entry 的向量字段是 `vector`（不是 `__vector__`），
    # 值是 base64 字符串（不是 list[float]）。这里简化用 base64 字符串占位。
    import json
    (tmp_path / "vdb_entities.json").write_text(json.dumps({
        "data": [
            {"__id__": "ent-存在的脑区", "vector": "AAAAAA==", "entity_name": "存在的脑区"},
            {"__id__": "ent-被删的脑区", "vector": "AAAAAA==", "entity_name": "被删的脑区"},
            {"__id__": "ent-另一个被删", "vector": "AAAAAA==", "entity_name": "另一个被删"},
        ],
        "file_hash": "fake_hash",
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_vdb_entities_orphan()

    assert report["name"] == "vdb_entities_orphan"
    assert len(report["errors"]) == 2
    orphan_names = [e["entity_name"] for e in report["errors"]]
    assert "被删的脑区" in orphan_names
    assert "另一个被删" in orphan_names


def test_check_vdb_entities_orphan_clean_ok(tmp_path):
    """vdb 和 GraphML 一致 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_orphan

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion"},
    ])

    import json
    (tmp_path / "vdb_entities.json").write_text(json.dumps({
        "data": [
            {"__id__": "ent-脑区a", "vector": "AAAAAA==", "entity_name": "脑区A"},
        ],
        "file_hash": "fake_hash",
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_vdb_entities_orphan()

    assert report["errors"] == []


def test_check_brainregion_orphan_chunks_detects_orphan(tmp_path):
    """text_chunks 有 source_id=brain_xxx 的 chunk 但 GraphML 没 brain_xxx node"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "存在的脑区", "entity_type": "brainregion"},
    ])

    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-AAA": {"content": "正常 chunk", "full_doc_id": "doc-xxx", "source_id": "doc-xxx"},
        "chunk-BBB": {"content": "脑区专属 chunk", "full_doc_id": "brain_被删的脑区", "source_id": "brain_被删的脑区"},
        "chunk-CCC": {"content": "存在脑区的 chunk", "full_doc_id": "brain_存在的脑区", "source_id": "brain_存在的脑区"},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()

    assert report["name"] == "brainregion_orphan_chunks"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["orphan_chunk_id"] == "chunk-BBB"
    assert report["errors"][0]["brain_name"] == "被删的脑区"


def test_check_brainregion_orphan_chunks_clean_ok(tmp_path):
    """所有 brain_xxx source_id 的 chunk 都对应存在的脑区 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion"},
    ])

    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-1": {"content": "脑区A 的 chunk", "full_doc_id": "brain_脑区A", "source_id": "brain_脑区A"},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()

    assert report["errors"] == []


def test_check_brainregion_orphan_chunks_detects_zombie_content(tmp_path):
    """brain_xxx chunk content 含'被删除'标记但 GraphML 仍有 brain_xxx node → chunk 侧僵尸信号"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks

    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    # 注意：脑区 node 仍在（这是僵尸脑区的特征：description 含标记但 node 还在）
    _write_test_graphml(graphml, nodes=[
        {"id": "智家僵尸脑区", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一。<SEP>brain_meta_size:0"},
    ])

    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        # 脑区专属 chunk content 含"被删除"标记 → 报错（chunk 侧僵尸信号）
        "chunk-zombie": {
            "content": "这是被删除的重复脑区实体之一的专属 chunk",
            "full_doc_id": "brain_智家僵尸脑区",
            "source_id": "brain_智家僵尸脑区",
        },
        # 正常脑区专属 chunk，content 不含标记 → 不报
        "chunk-normal": {
            "content": "智家僵尸脑区的正常内容",
            "full_doc_id": "brain_智家僵尸脑区",
            "source_id": "brain_智家僵尸脑区",
        },
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()

    assert report["name"] == "brainregion_orphan_chunks"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["orphan_chunk_id"] == "chunk-zombie"
    assert report["errors"][0]["brain_name"] == "智家僵尸脑区"
    assert "marker" in report["errors"][0]
    assert "被删除" in report["errors"][0]["marker"]
