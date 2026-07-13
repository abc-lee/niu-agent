"""语义修复的 TDD 测试。"""
import json
import xml.etree.ElementTree as ET
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_brainregion_zombies
from niu_api.internal.lightrag_integrity import check_all


def _make_test_storage(tmp_path: Path, zombies: list[str], normal_regions: list[str] = None):
    """生成测试用 LightRAG 存储，含僵尸脑区。"""
    normal_regions = normal_regions or []
    storage = tmp_path
    ns = "http://graphml.graphdrawing.org/xmlns"

    # 1. GraphML
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for zname in zombies:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": zname})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = f"被删除的重复脑区实体之一。<SEP>brain_meta_size:0<SEP>brain_meta_shrink_count:1"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = f"brain_{zname}"
    for nname in normal_regions:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": nname})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = f"brain_meta_size:10"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = f"chunk-{nname}"
    ET.ElementTree(root).write(storage / "graph_chunk_entity_relation.graphml", xml_declaration=True, encoding="utf-8")

    # 2. vdb_entities
    vdb_data = []
    for name in zombies + normal_regions:
        vdb_data.append({"__id__": f"ent-{name.lower()}", "entity_name": name, "vector": "AAAAAA=="})
    (storage / "vdb_entities.json").write_text(json.dumps({
        "data": vdb_data, "file_hash": "fake", "embedding_dim": 8,
    }, ensure_ascii=False))

    # 3. vdb_relationships
    rel_data = []
    for zname in zombies:
        rel_data.append({
            "__id__": f"rel-{zname.lower()}",
            "src_id": "知识图谱系统维护",
            "tgt_id": zname,
            "vector": "AAAAAA==",
        })
    (storage / "vdb_relationships.json").write_text(json.dumps({
        "data": rel_data, "file_hash": "fake", "embedding_dim": 8,
    }, ensure_ascii=False))

    # 4. kv_store_entity_chunks
    shared_chunk_id = "chunk-shared-deletion-log"
    ec_data = {zname: {"chunk_ids": [shared_chunk_id], "count": 1} for zname in zombies}
    for nname in normal_regions:
        ec_data[nname] = {"chunk_ids": [f"chunk-{nname}"], "count": 1}
    (storage / "kv_store_entity_chunks.json").write_text(json.dumps(ec_data, ensure_ascii=False))

    # 5. kv_store_text_chunks
    tc_data = {shared_chunk_id: {"content": "删除日志", "source_id": "refined:2026-07-06:001"}}
    for zname in zombies:
        tc_data[f"chunk-{zname}"] = {"content": f"{zname} 的 chunk", "source_id": f"brain_{zname}"}
    for nname in normal_regions:
        tc_data[f"chunk-{nname}"] = {"content": f"{nname} 的 chunk", "source_id": f"brain_{nname}"}
    (storage / "kv_store_text_chunks.json").write_text(json.dumps(tc_data, ensure_ascii=False))

    # 6. vdb_chunks
    chunk_data = []
    for cid in [shared_chunk_id] + [f"chunk-{n}" for n in zombies + normal_regions]:
        chunk_data.append({"__id__": cid, "vector": "AAAAAA=="})
    (storage / "vdb_chunks.json").write_text(json.dumps({
        "data": chunk_data, "file_hash": "fake", "embedding_dim": 8,
    }, ensure_ascii=False))

    # 7. kv_store_full_entities (form 2: dict[doc_id] -> {entity_name, description, source_id})
    fe_data = {}
    for i, name in enumerate(zombies + normal_regions):
        is_zombie = name in zombies
        desc = "被删除的重复脑区实体之一。<SEP>brain_meta_size:0" if is_zombie else "brain_meta_size:10"
        fe_data[f"doc-{i+1}"] = {
            "entity_name": name,
            "description": desc,
            "source_id": f"brain_{name}",
        }
    (storage / "kv_store_full_entities.json").write_text(json.dumps(fe_data, ensure_ascii=False))

    # 8. kv_store_full_relations
    pairs = [["知识图谱系统维护", z, "删除操作"] for z in zombies]
    (storage / "kv_store_full_relations.json").write_text(json.dumps({
        "doc-1": {
            "relation_pairs": pairs,
            "count": len(pairs),
            "create_time": "2026-07-06T00:00:00",
            "update_time": "2026-07-06T00:00:00",
            "_id": "doc-1",
        },
    }, ensure_ascii=False))

    # 9. kv_store_relation_chunks (Bug #3: 第 8 个存储)
    rc_data = {}
    for z in zombies:
        rc_data[f"{z}<SEP>知识图谱系统维护"] = {"chunk_ids": [f"chunk-rel-{z}"], "count": 1}
    for n in normal_regions:
        rc_data[f"{n}<SEP>知识图谱系统维护"] = {"chunk_ids": [f"chunk-rel-{n}"], "count": 1}
    (storage / "kv_store_relation_chunks.json").write_text(json.dumps(rc_data, ensure_ascii=False))


def test_repair_brainregion_zombies_cleans_all_8_storages(tmp_path):
    """repair_brainregion_zombies 应清理 8 个存储的僵尸脑区残留"""
    _make_test_storage(tmp_path, zombies=["智家脑区A", "智家脑区B"], normal_regions=["聊天历史脑区"])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    assert result["status"] == "ok"
    assert result["cleaned_count"] == 2

    # 1. GraphML
    tree = ET.parse(tmp_path / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家脑区A" not in node_ids
    assert "智家脑区B" not in node_ids
    assert "聊天历史脑区" in node_ids

    # 2. vdb_entities
    vdb = json.loads((tmp_path / "vdb_entities.json").read_text())
    names = [e["entity_name"] for e in vdb["data"]]
    assert "智家脑区A" not in names
    assert "智家脑区B" not in names
    assert "聊天历史脑区" in names

    # 3. vdb_relationships
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    rel_tgt = [e.get("tgt_id") for e in vdb_r["data"]]
    assert "智家脑区A" not in rel_tgt
    assert "智家脑区B" not in rel_tgt

    # 4. kv_store_entity_chunks
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    assert "智家脑区A" not in ec
    assert "智家脑区B" not in ec
    assert "聊天历史脑区" in ec

    # 5. kv_store_text_chunks
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-智家脑区A" not in tc
    assert "chunk-智家脑区B" not in tc
    assert "chunk-聊天历史脑区" in tc

    # 6. vdb_chunks
    vdb_c = json.loads((tmp_path / "vdb_chunks.json").read_text())
    chunk_ids = [e["__id__"] for e in vdb_c["data"]]
    assert "chunk-智家脑区A" not in chunk_ids
    assert "chunk-智家脑区B" not in chunk_ids

    # 7. kv_store_full_entities (form 2)
    fe = json.loads((tmp_path / "kv_store_full_entities.json").read_text())
    zombie_docs = [doc_id for doc_id, ent in fe.items()
                   if isinstance(ent, dict) and ent.get("entity_name") in ["智家脑区A", "智家脑区B"]]
    assert len(zombie_docs) == 0, f"僵尸 entity 的 doc 仍存在: {zombie_docs}"
    normal_docs = [doc_id for doc_id, ent in fe.items()
                   if isinstance(ent, dict) and ent.get("entity_name") == "聊天历史脑区"]
    assert len(normal_docs) == 1, f"正常 entity 的 doc 应保留 1 个，实际 {len(normal_docs)}"

    # 8. kv_store_relation_chunks (Bug #3)
    rc = json.loads((tmp_path / "kv_store_relation_chunks.json").read_text())
    zombie_rc_keys = [k for k in rc.keys()
                      if any(z in k.split("<SEP>") for z in ["智家脑区A", "智家脑区B"])]
    assert len(zombie_rc_keys) == 0, f"僵尸关系 chunk key 仍存在: {zombie_rc_keys}"
    normal_rc_keys = [k for k in rc.keys() if "聊天历史脑区" in k.split("<SEP>")]
    assert len(normal_rc_keys) == 1, f"正常关系 chunk 应保留 1 个，实际 {len(normal_rc_keys)}"


def test_repair_brainregion_zombies_check_ok_after_repair(tmp_path):
    """repair 后 check_all 应该不再报僵尸脑区错误"""
    _make_test_storage(tmp_path, zombies=["智家脑区A", "智家脑区B"], normal_regions=["聊天历史脑区"])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        before = check_all()
        assert before["ok"] is False

        repair_brainregion_zombies()

        after = check_all()
        zombie_errors = after["checks"].get("brainregion_semantic_zombie", {}).get("errors", [])
        assert zombie_errors == [], f"仍有僵尸脑区: {zombie_errors}"


def test_repair_brainregion_zombies_zombies_not_in_full_entities(tmp_path):
    """边界情况：僵尸脑区不在 full_entities keys 时，repair 仍正确执行（不报错）。"""
    _make_test_storage(tmp_path, zombies=["智家脑区A"], normal_regions=["聊天历史脑区"])

    import json
    fe_path = tmp_path / "kv_store_full_entities.json"
    fe_path.write_text(json.dumps({
        "doc-1": {"entity_name": "其他实体", "description": "...", "source_id": "doc-1"},
    }, ensure_ascii=False))

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    assert result["status"] == "ok", f"status 应为 ok，实际 {result.get('status')}"
    assert result["cleaned_count"] == 1, f"cleaned_count 应为 1，实际 {result.get('cleaned_count')}"
    fe = json.loads(fe_path.read_text())
    assert "doc-1" in fe, "其他 entity 的 doc 应保留"
    assert fe["doc-1"]["entity_name"] == "其他实体", "其他 entity 内容应保持不变"


def test_repair_all_calls_brainregion_zombies_when_zombies_exist(tmp_path):
    """repair_all 在检测到僵尸脑区时应调用 repair_brainregion_zombies"""
    from niu_api.internal.lightrag_repair import repair_all
    _make_test_storage(tmp_path, zombies=["智家脑区A"], normal_regions=["聊天历史脑区"])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_all()

    # 应该调用了 brainregion_zombies
    assert "brainregion_zombies" in result
    assert result["brainregion_zombies"]["status"] == "ok"
    assert result["brainregion_zombies"]["cleaned_count"] == 1
    # 应该不在 _skipped 里
    assert "brainregion_zombies" not in result.get("_skipped", [])


def test_repair_all_skips_brainregion_zombies_when_no_zombies(tmp_path):
    """无僵尸脑区时 repair_all 应跳过 brainregion_zombies"""
    from niu_api.internal.lightrag_repair import repair_all
    _make_test_storage(tmp_path, zombies=[], normal_regions=["聊天历史脑区"])

    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_all()

    assert "brainregion_zombies" in result.get("_skipped", [])
