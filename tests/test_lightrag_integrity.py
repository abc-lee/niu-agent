"""LightRAG 因果链引用完整性检测测试

每项检查一个 PASS 场景 + 一个 FAIL 场景（共 20 个）。
+ 空文件场景（所有文件不存在 → ok=True）
+ 单文件损坏场景（只有 vdb_entities 空，其他文件有数据 → #6 报 major）

测试用 tempfile.TemporaryDirectory + monkeypatch _STORAGE_DIR 隔离，不碰用户真实数据。
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


# =============================================================================
# 工具函数：构造测试数据
# =============================================================================


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_graphml(nodes: list[str], edges: list[tuple[str, str]]) -> str:
    """构造 GraphML 字符串。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<graph edgedefault="undirected">',
    ]
    for n in nodes:
        lines.append(f'<node id="{n}"/>')
    for src, tgt in edges:
        lines.append(f'<edge source="{src}" target="{tgt}"/>')
    lines.append("</graph>")
    lines.append("</graphml>")
    return "\n".join(lines)


def _compute_md5_id(content: str, prefix: str) -> str:
    """复用 LightRAG 的 compute_mdhash_id。"""
    from lightrag.utils import compute_mdhash_id

    return compute_mdhash_id(content, prefix=prefix)


def _make_vdb(data_list: list[dict]) -> dict:
    """构造 vdb 文件内容（只关心 __id__ 字段，matrix 留空，_load_vdb 容错）。"""
    return {"embedding_dim": 4, "data": data_list, "matrix": ""}


# =============================================================================
# 测试 fixture：隔离的 storage_dir
# =============================================================================


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """隔离的 storage_dir，monkeypatch _STORAGE_DIR。"""
    from niu_api.internal import lightrag_integrity

    sd = tmp_path / "lightrag_storage"
    sd.mkdir()
    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", str(sd))
    return sd


# =============================================================================
# 空文件场景
# =============================================================================


def test_empty_storage_ok(storage_dir):
    """所有文件不存在 → ok=True（新用户合法启动）"""
    from niu_api.internal.lightrag_integrity import check_all

    report = check_all()
    assert report["ok"] is True, f"空 storage 应通过: {report['errors']}"
    assert report["critical_errors"] == 0
    assert report["major_errors"] == 0
    assert report["minor_errors"] == 0


def test_empty_dicts_ok(storage_dir):
    """所有 JSON 文件存在但内容是空 dict → ok=True"""
    from niu_api.internal.lightrag_integrity import check_all

    for fname in [
        "kv_store_doc_status.json",
        "kv_store_entity_chunks.json",
        "kv_store_full_docs.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
        "kv_store_relation_chunks.json",
        "kv_store_text_chunks.json",
        "kv_store_llm_response_cache.json",
        "vdb_entities.json",
        "vdb_relationships.json",
        "vdb_chunks.json",
    ]:
        _write_json(storage_dir / fname, {})
    # GraphML 空文件（只有 graphml 根元素，无 node/edge）
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                '<?xml version="1.0"?><graphml xmlns="http://graphml.graphdrawing.org/xmlns"><graph/></graphml>')

    report = check_all()
    assert report["ok"] is True, f"空 dict 应通过: {report['errors']}"


# =============================================================================
# 检查 #1: entity_chunks 引用悬空
# =============================================================================


def test_entity_chunks_dangling_pass(storage_dir):
    """#1 PASS: entity_chunks 的 key 都在 GraphML node 里"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_dangling

    _write_json(storage_dir / "kv_store_entity_chunks.json", {
        "alice": {}, "bob": {},
    })
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], []))

    report = check_entity_chunks_dangling()
    assert report["errors"] == []


def test_entity_chunks_dangling_fail(storage_dir):
    """#1 FAIL: entity_chunks 有 'charlie' 但 GraphML 没有"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_dangling

    _write_json(storage_dir / "kv_store_entity_chunks.json", {
        "alice": {}, "charlie": {},
    })
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], []))

    report = check_entity_chunks_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "entity_chunks_dangling"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["ref_key"] == "charlie"


# =============================================================================
# 检查 #2: relation_chunks 引用悬空
# =============================================================================


def test_relation_chunks_dangling_pass(storage_dir):
    """#2 PASS: relation_chunks 的 key 拆分后 (src, tgt) 在 GraphML edge 中存在（无向）"""
    from niu_api.internal.lightrag_integrity import check_relation_chunks_dangling

    # GraphML edge: alice->bob；relation_chunks key 用 <SEP> 排序后 = "alice<SEP>bob"
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    _write_json(storage_dir / "kv_store_relation_chunks.json", {
        "alice<SEP>bob": {},
    })

    report = check_relation_chunks_dangling()
    assert report["errors"] == []


def test_relation_chunks_dangling_fail(storage_dir):
    """#2 FAIL: relation_chunks 有 'alice<SEP>charlie' 但 GraphML 没有 edge"""
    from niu_api.internal.lightrag_integrity import check_relation_chunks_dangling

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    _write_json(storage_dir / "kv_store_relation_chunks.json", {
        "alice<SEP>charlie": {},
    })

    report = check_relation_chunks_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "relation_chunks_dangling"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["ref_src"] == "alice"
    assert report["errors"][0]["ref_tgt"] == "charlie"


# =============================================================================
# 检查 #3: text_chunks 文档悬空（critical）
# =============================================================================


def test_text_chunks_doc_dangling_pass(storage_dir):
    """#3 PASS: text_chunks 的 full_doc_id 都在 full_docs 里"""
    from niu_api.internal.lightrag_integrity import check_text_chunks_doc_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "content": "..."},
        "chunk-002": {"full_doc_id": "doc-001", "content": "..."},
    })
    _write_json(storage_dir / "kv_store_full_docs.json", {
        "doc-001": {"content": "..."},
    })

    report = check_text_chunks_doc_dangling()
    assert report["errors"] == []


def test_text_chunks_doc_dangling_fail(storage_dir):
    """#3 FAIL: text_chunks 引用了 doc-missing，但 full_docs 没有"""
    from niu_api.internal.lightrag_integrity import check_text_chunks_doc_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-missing"},
    })
    _write_json(storage_dir / "kv_store_full_docs.json", {
        "doc-001": {"content": "..."},
    })

    report = check_text_chunks_doc_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "text_chunks_doc_dangling"
    assert report["errors"][0]["severity"] == "critical"
    assert report["errors"][0]["full_doc_id"] == "doc-missing"


def test_text_chunks_doc_dangling_skip_custom_kg(storage_dir):
    """#3 PASS: 自定义 KG chunk（brain_*/custom_kg_*/skill:// 等 full_doc_id）跳过检查。

    LightRAG ainsert_custom_kg 写入的 chunk 的 full_doc_id 是自定义字符串
    （brain_*/custom_kg_*/skill://*/文件路径等），不写入 full_docs，跳过检查以避免误报。
    """
    from niu_api.internal.lightrag_integrity import check_text_chunks_doc_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        # 普通文档 chunk，在 full_docs 里 → 不报错
        "chunk-doc-001": {"full_doc_id": "doc-aaa", "content": "..."},
        # refined chunk，在 full_docs 里 → 不报错
        "chunk-refined-001": {"full_doc_id": "refined:bbb", "content": "..."},
        # 自定义 KG chunk，不在 full_docs 里 → 跳过（不报错）
        "chunk-brain-001": {"full_doc_id": "brain_memory_001", "content": "..."},
        "chunk-custom-001": {"full_doc_id": "custom_kg_xxx", "content": "..."},
        "chunk-skill-001": {"full_doc_id": "skill://some-skill", "content": "..."},
        "chunk-path-001": {"full_doc_id": "/some/file/path", "content": "..."},
    })
    _write_json(storage_dir / "kv_store_full_docs.json", {
        "doc-aaa": {"content": "..."},
        "refined:bbb": {"content": "..."},
    })

    report = check_text_chunks_doc_dangling()
    assert report["errors"] == []


# =============================================================================
# 检查 #4: text_chunks 缓存悬空（minor）
# =============================================================================


def test_text_chunks_cache_dangling_pass(storage_dir):
    """#4 PASS: text_chunks 的 llm_cache_list 引用都在 llm_response_cache 里"""
    from niu_api.internal.lightrag_integrity import check_text_chunks_cache_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "llm_cache_list": ["cache-a", "cache-b"]},
    })
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {
        "cache-a": {}, "cache-b": {},
    })

    report = check_text_chunks_cache_dangling()
    assert report["errors"] == []


def test_text_chunks_cache_dangling_pass_no_field(storage_dir):
    """#4 PASS: llm_cache_list 字段不存在 → 通过"""
    from niu_api.internal.lightrag_integrity import check_text_chunks_cache_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001"},  # 无 llm_cache_list
    })
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    report = check_text_chunks_cache_dangling()
    assert report["errors"] == []


def test_text_chunks_cache_dangling_fail(storage_dir):
    """#4 FAIL: llm_cache_list 引用 'cache-x' 但 llm_response_cache 没有"""
    from niu_api.internal.lightrag_integrity import check_text_chunks_cache_dangling

    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "llm_cache_list": ["cache-a", "cache-x"]},
    })
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {
        "cache-a": {},
    })

    report = check_text_chunks_cache_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "text_chunks_cache_dangling"
    assert report["errors"][0]["severity"] == "minor"
    assert report["errors"][0]["cache_key"] == "cache-x"


# =============================================================================
# 检查 #5: doc_status chunks 悬空
# =============================================================================


def test_doc_status_chunks_dangling_pass(storage_dir):
    """#5 PASS: doc_status 的 chunks_list 都在 text_chunks 里"""
    from niu_api.internal.lightrag_integrity import check_doc_status_chunks_dangling

    _write_json(storage_dir / "kv_store_doc_status.json", {
        "doc-001": {"chunks_list": ["chunk-001", "chunk-002"]},
    })
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {}, "chunk-002": {},
    })

    report = check_doc_status_chunks_dangling()
    assert report["errors"] == []


def test_doc_status_chunks_dangling_fail(storage_dir):
    """#5 FAIL: doc_status 引用 chunk-x 但 text_chunks 没有"""
    from niu_api.internal.lightrag_integrity import check_doc_status_chunks_dangling

    _write_json(storage_dir / "kv_store_doc_status.json", {
        "doc-001": {"chunks_list": ["chunk-001", "chunk-x"]},
    })
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {},
    })

    report = check_doc_status_chunks_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "doc_status_chunks_dangling"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["chunk_id"] == "chunk-x"


# =============================================================================
# 检查 #6: vdb_entities 向量缺失
# =============================================================================


def test_vdb_entities_missing_pass(storage_dir):
    """#6 PASS: GraphML 每个 node 的 ent-{md5(name)} 都在 vdb_entities.data 里"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_missing

    nodes = ["alice", "bob"]
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(nodes, []))
    vdb_data = [{"__id__": _compute_md5_id(n, "ent-")} for n in nodes]
    _write_json(storage_dir / "vdb_entities.json", _make_vdb(vdb_data))

    report = check_vdb_entities_missing()
    assert report["errors"] == []


def test_vdb_entities_missing_fail(storage_dir):
    """#6 FAIL: GraphML 有 'charlie' 但 vdb_entities 没有 ent-{md5(charlie)}"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_missing

    nodes = ["alice", "bob", "charlie"]
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(nodes, []))
    # 只给 alice + bob 的向量，不给 charlie
    vdb_data = [
        {"__id__": _compute_md5_id("alice", "ent-")},
        {"__id__": _compute_md5_id("bob", "ent-")},
    ]
    _write_json(storage_dir / "vdb_entities.json", _make_vdb(vdb_data))

    report = check_vdb_entities_missing()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "vdb_entities_missing"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["ref_node"] == "charlie"


# =============================================================================
# 检查 #7: vdb_relationships 向量缺失
# =============================================================================


def test_vdb_relationships_missing_pass(storage_dir):
    """#7 PASS: GraphML 每个 edge 的 make_relation_vdb_ids 候选 ID 至少一个在 vdb_relationships 里"""
    from niu_api.internal.lightrag_integrity import check_vdb_relationships_missing
    from lightrag.utils import make_relation_vdb_ids

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    # 正序 ID
    candidate_ids = make_relation_vdb_ids("alice", "bob")
    vdb_data = [{"__id__": candidate_ids[0]}]
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb(vdb_data))

    report = check_vdb_relationships_missing()
    assert report["errors"] == []


def test_vdb_relationships_missing_pass_reverse(storage_dir):
    """#7 PASS: 候选 ID 中的逆序 ID 在 vdb 里也算通过（兼容历史数据）"""
    from niu_api.internal.lightrag_integrity import check_vdb_relationships_missing
    from lightrag.utils import make_relation_vdb_ids

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    candidate_ids = make_relation_vdb_ids("alice", "bob")
    # 如果正序 != 逆序，用逆序 ID
    if len(candidate_ids) > 1:
        vdb_data = [{"__id__": candidate_ids[1]}]
    else:
        vdb_data = [{"__id__": candidate_ids[0]}]
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb(vdb_data))

    report = check_vdb_relationships_missing()
    assert report["errors"] == []


def test_vdb_relationships_missing_fail(storage_dir):
    """#7 FAIL: GraphML edge 'alice->bob' 但 vdb_relationships 没有候选 ID"""
    from niu_api.internal.lightrag_integrity import check_vdb_relationships_missing

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb([
        {"__id__": "rel-XXXXXXXX"},  # 不相关的 ID
    ]))

    report = check_vdb_relationships_missing()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "vdb_relationships_missing"
    assert report["errors"][0]["severity"] == "major"


# =============================================================================
# 检查 #8: vdb_chunks 向量缺失
# =============================================================================


def test_vdb_chunks_missing_pass(storage_dir):
    """#8 PASS: text_chunks 每个 chunk 的 chunk-{md5(content)} 都在 vdb_chunks 里"""
    from niu_api.internal.lightrag_integrity import check_vdb_chunks_missing

    chunks = {
        "chunk-001": {"content": "hello"},
        "chunk-002": {"content": "world"},
    }
    _write_json(storage_dir / "kv_store_text_chunks.json", chunks)
    vdb_data = [
        {"__id__": _compute_md5_id("hello", "chunk-")},
        {"__id__": _compute_md5_id("world", "chunk-")},
    ]
    _write_json(storage_dir / "vdb_chunks.json", _make_vdb(vdb_data))

    report = check_vdb_chunks_missing()
    assert report["errors"] == []


def test_vdb_chunks_missing_fail(storage_dir):
    """#8 FAIL: text_chunks 有 content='missing' 但 vdb_chunks 没有 chunk-{md5(missing)}"""
    from niu_api.internal.lightrag_integrity import check_vdb_chunks_missing

    chunks = {
        "chunk-001": {"content": "hello"},
        "chunk-002": {"content": "missing-content"},
    }
    _write_json(storage_dir / "kv_store_text_chunks.json", chunks)
    vdb_data = [
        {"__id__": _compute_md5_id("hello", "chunk-")},
    ]
    _write_json(storage_dir / "vdb_chunks.json", _make_vdb(vdb_data))

    report = check_vdb_chunks_missing()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "vdb_chunks_missing"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["ref_key"] == "chunk-002"


# =============================================================================
# 检查 #9: GraphML edge 端点悬空
# =============================================================================


def test_graphml_edge_dangling_pass(storage_dir):
    """#9 PASS: GraphML edge 的 source/target 都在 node 集合中"""
    from niu_api.internal.lightrag_integrity import check_graphml_edge_dangling

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))

    report = check_graphml_edge_dangling()
    assert report["errors"] == []


def test_graphml_edge_dangling_fail(storage_dir):
    """#9 FAIL: GraphML edge source='alice' target='charlie'，但 charlie 不在 node 集合"""
    from niu_api.internal.lightrag_integrity import check_graphml_edge_dangling

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "charlie")]))

    report = check_graphml_edge_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "graphml_edge_dangling_target"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["target"] == "charlie"


# =============================================================================
# 检查 #10: vdb_relationships 端点悬空
# =============================================================================


def test_vdb_relationships_endpoint_dangling_pass(storage_dir):
    """#10 PASS: vdb_relationships 的 src_id/tgt_id 都在 GraphML node 里"""
    from niu_api.internal.lightrag_integrity import check_vdb_relationships_endpoint_dangling

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], []))
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb([
        {"__id__": "rel-1", "src_id": "alice", "tgt_id": "bob"},
    ]))

    report = check_vdb_relationships_endpoint_dangling()
    assert report["errors"] == []


def test_vdb_relationships_endpoint_dangling_fail(storage_dir):
    """#10 FAIL: vdb_relationships 的 src_id='charlie' 但 GraphML 没有 charlie node"""
    from niu_api.internal.lightrag_integrity import check_vdb_relationships_endpoint_dangling

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], []))
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb([
        {"__id__": "rel-1", "src_id": "charlie", "tgt_id": "bob"},
    ]))

    report = check_vdb_relationships_endpoint_dangling()
    assert len(report["errors"]) == 1
    assert report["errors"][0]["check"] == "vdb_relationships_endpoint_dangling"
    assert report["errors"][0]["severity"] == "major"
    assert report["errors"][0]["src_id"] == "charlie"


# =============================================================================
# 文件级 critical 场景
# =============================================================================


def test_json_parse_failure_critical(storage_dir):
    """JSON 解析失败 → 文件级 critical"""
    from niu_api.internal.lightrag_integrity import check_all

    # 写一个截断的 JSON 文件
    _write_text(storage_dir / "kv_store_full_docs.json", '{"doc-001": {"content":')
    # 其他相关文件正常（但 text_chunks 引用 doc-001）
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001"},
    })

    report = check_all()
    assert report["ok"] is False
    assert report["critical_errors"] >= 1
    # text_chunks_doc_dangling 应该报 json_parse critical（因为 full_docs 解析失败）
    json_errors = [e for e in report["errors"] if e.get("check") == "json_parse"]
    assert len(json_errors) >= 1
    assert json_errors[0]["severity"] == "critical"
    assert json_errors[0]["file"] == "kv_store_full_docs.json"


def test_json_not_dict_critical(storage_dir):
    """JSON 解析为 list 而非 dict → 文件级 critical"""
    from niu_api.internal.lightrag_integrity import check_all

    _write_json(storage_dir / "kv_store_full_docs.json", ["not", "a", "dict"])

    report = check_all()
    assert report["ok"] is False
    assert report["critical_errors"] >= 1
    json_errors = [e for e in report["errors"] if e.get("check") == "json_not_dict"]
    assert len(json_errors) >= 1


def test_vdb_matrix_size_mismatch_critical(storage_dir):
    """vdb matrix 维度不匹配 → 文件级 critical"""
    from niu_api.internal.lightrag_integrity import check_all
    import base64

    # 构造 matrix 字节数 != 4 * embedding_dim * data_len
    matrix_bytes = base64.b64encode(b"\x00" * 10).decode()  # 10 bytes，不可能匹配任何 4*dim*n
    vdb = {
        "embedding_dim": 4,
        "data": [{"__id__": "e1"}],  # 1 条，期望 4*4*1=16 bytes
        "matrix": matrix_bytes,
    }
    _write_json(storage_dir / "vdb_entities.json", vdb)
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice"], []))

    report = check_all()
    assert report["ok"] is False
    assert report["critical_errors"] >= 1
    matrix_errors = [e for e in report["errors"] if e.get("check") == "matrix_size_mismatch"]
    assert len(matrix_errors) >= 1


# =============================================================================
# 单文件损坏场景：只有 vdb_entities 空，其他文件有数据 → #6 报 major
# =============================================================================


def test_single_file_corrupt_only_vdb_entities_empty(storage_dir):
    """只有 vdb_entities 空，其他文件有数据 → #6 报 major"""
    from niu_api.internal.lightrag_integrity import check_all

    # GraphML 有节点
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))
    # vdb_entities 为空（文件不存在或空 dict）
    # 其他 vdb 文件有数据
    from lightrag.utils import make_relation_vdb_ids

    candidate_ids = make_relation_vdb_ids("alice", "bob")
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb([
        {"__id__": candidate_ids[0], "src_id": "alice", "tgt_id": "bob"},
    ]))
    # text_chunks + full_docs + doc_status 自洽
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc-001": {"content": "..."}})
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "content": "hello"},
    })
    _write_json(storage_dir / "kv_store_doc_status.json", {
        "doc-001": {"chunks_list": ["chunk-001"]},
    })
    _write_json(storage_dir / "vdb_chunks.json", _make_vdb([
        {"__id__": _compute_md5_id("hello", "chunk-")},
    ]))

    # 不写 vdb_entities.json → 文件不存在 → 通过（无 GraphML 引用？）
    # 但 GraphML 有 nodes alice/bob → #6 会发现 vdb 缺失
    report = check_all()
    assert report["ok"] is False
    assert report["major_errors"] >= 2  # alice 和 bob 两条 major
    assert report["critical_errors"] == 0
    # 至少有 vdb_entities_missing 错误
    missing_errors = [e for e in report["errors"] if e.get("check") == "vdb_entities_missing"]
    assert len(missing_errors) == 2
    assert all(e["severity"] == "major" for e in missing_errors)


# =============================================================================
# check_all 整体集成
# =============================================================================


def test_check_all_full_pass(storage_dir):
    """完整 PASS 场景：所有文件自洽，无引用悬空"""
    from niu_api.internal.lightrag_integrity import check_all
    from lightrag.utils import make_relation_vdb_ids

    # GraphML: alice -> bob
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml",
                _make_graphml(["alice", "bob"], [("alice", "bob")]))

    # vdb_entities: alice + bob
    _write_json(storage_dir / "vdb_entities.json", _make_vdb([
        {"__id__": _compute_md5_id("alice", "ent-")},
        {"__id__": _compute_md5_id("bob", "ent-")},
    ]))

    # vdb_relationships: alice -> bob
    candidate_ids = make_relation_vdb_ids("alice", "bob")
    _write_json(storage_dir / "vdb_relationships.json", _make_vdb([
        {"__id__": candidate_ids[0], "src_id": "alice", "tgt_id": "bob"},
    ]))

    # text_chunks + full_docs + doc_status + vdb_chunks 自洽
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc-001": {"content": "doc"}})
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "content": "hello"},
    })
    _write_json(storage_dir / "kv_store_doc_status.json", {
        "doc-001": {"chunks_list": ["chunk-001"]},
    })
    _write_json(storage_dir / "vdb_chunks.json", _make_vdb([
        {"__id__": _compute_md5_id("hello", "chunk-")},
    ]))

    # entity_chunks + relation_chunks 自洽
    _write_json(storage_dir / "kv_store_entity_chunks.json", {
        "alice": {}, "bob": {},
    })
    _write_json(storage_dir / "kv_store_relation_chunks.json", {
        "alice<SEP>bob": {},
    })

    # llm_response_cache + text_chunks.llm_cache_list 自洽
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {
        "cache-a": {},
    })
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "content": "hello", "llm_cache_list": ["cache-a"]},
    })

    report = check_all()
    assert report["ok"] is True, f"完整自洽场景应通过: {report['errors']}"
    assert report["critical_errors"] == 0
    assert report["major_errors"] == 0
    assert report["minor_errors"] == 0


def test_check_all_minor_does_not_affect_ok(storage_dir):
    """minor 错误不影响 ok（critical==0 and major==0 → ok=True）"""
    from niu_api.internal.lightrag_integrity import check_all

    # 只构造一个 minor 错误：text_chunks.llm_cache_list 引用不存在的 cache
    _write_json(storage_dir / "kv_store_text_chunks.json", {
        "chunk-001": {"full_doc_id": "doc-001", "llm_cache_list": ["cache-x"]},
    })
    _write_json(storage_dir / "kv_store_full_docs.json", {
        "doc-001": {"content": "..."},
    })
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    report = check_all()
    # minor 错误不影响 ok
    assert report["ok"] is True, f"minor 错误不应影响 ok: {report['errors']}"
    assert report["critical_errors"] == 0
    assert report["major_errors"] == 0
    assert report["minor_errors"] >= 1
