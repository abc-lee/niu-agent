"""LightRAG 检测逻辑单元测试 v2——验证"派生文件缺失不是损坏"。

修复 bug：脑区+Skills 已注入但未入库文档时，5 个派生 kv_store 文件缺失
被误判为 major 损坏，触发修复弹窗。正确行为：派生缺失让 LightRAG 内部
按需 lazy 加载为空 dict，不阻断启动，不主动重建。

场景矩阵见 docs/superpowers/plans/2026-07-28-lightrag-integrity-check-fix.md 第 3 节。
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from niu_api.internal.lightrag_integrity import check_all


def _write_graphml(path: Path, nodes: list[tuple[str, str]] | None = None,
                   edges: list[tuple[str, str]] | None = None) -> None:
    """写一个最小合法 GraphML 文件。"""
    nsmap = {"g": "http://graphml.graphdrawing.org/xmlns"}
    ET.register_namespace("", nsmap["g"])
    root = ET.Element("{%s}graphml" % nsmap["g"])
    key = ET.SubElement(root, "{%s}key" % nsmap["g"])
    key.set("id", "d1")
    key.set("for", "node")
    key.set("attr.name", "entity_type")
    key.set("attr.type", "string")
    graph = ET.SubElement(root, "{%s}graph" % nsmap["g"])
    graph.set("id", "G")
    graph.set("edgedefault", "undirected")
    for node_id, entity_type in (nodes or []):
        node = ET.SubElement(graph, "{%s}node" % nsmap["g"])
        node.set("id", node_id)
        data = ET.SubElement(node, "{%s}data" % nsmap["g"])
        data.set("key", "d1")
        data.text = entity_type
    for src, tgt in (edges or []):
        edge = ET.SubElement(graph, "{%s}edge" % nsmap["g"])
        edge.set("source", src)
        edge.set("target", tgt)
    path.write_text(ET.tostring(root, encoding="unicode"))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False))


def _write_vdb(path: Path, entries: list[dict]) -> None:
    _write_json(path, {"data": entries, "__type__": "NanoVectorDB"})


def _make_vdb_entity(name: str) -> dict:
    return {"id": hash(name) & 0xFFFFFF, "entity_name": name, "vector": [0.1] * 768}


def _make_vdb_relation(src: str, tgt: str) -> dict:
    s, t = sorted([src, tgt])
    return {"id": hash(s + t) & 0xFFFFFF, "src_id": s, "tgt_id": t, "vector": [0.1] * 768}


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """重定向 _STORAGE_DIR 到 tmp_path。"""
    from niu_api.internal import lightrag_integrity
    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr(lightrag_integrity, "_resolve_storage_dir", lambda: tmp_path)
    return tmp_path


# ============================================================================
# 场景 1: 全新用户（啥都没有）→ ok=True
# ============================================================================

def test_scenario_1_empty_user(storage_dir):
    """3 真相源全 absent + 9 派生全 absent → ok=True, major=0。

    storage_dir fixture 不直接使用，仅依赖其 monkeypatch 副作用重定向 _STORAGE_DIR。
    """
    _ = storage_dir  # 请求 fixture 触发 monkeypatch（不用变量值）
    result = check_all()
    assert result["ok"] is True
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 2: 脑区+Skills 已注入（用户当前现场）→ ok=True
# ============================================================================

def test_scenario_2_brainregion_skills_injected_no_full_docs(storage_dir):
    """GraphML 有 node + 3 vdb 存在且一致 + 6 派生 kv_store 缺失 + full_docs/cache 缺失。

    这是脑区/Skills 注入后的合法中间状态，不应判为损坏。
    """
    nodes = [("脑区_记忆", "brain_region"), ("skill_code_review", "skill")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_chunks.json", [_make_vdb_entity("chunk_1")])
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("脑区_记忆"), _make_vdb_entity("skill_code_review")])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json",
                {"chunk_1": {"content": "xxx", "full_doc_id": "brain_脑区_记忆"}})
    # full_docs / cache / doc_status / entity_chunks / relation_chunks /
    # full_entities / full_relations 都不写（脑区路径不触发）

    result = check_all()
    assert result["ok"] is True, f"脑区+Skills 注入后不应判损坏，errors={result['errors']}"
    assert result["major_errors"] == 0, f"不应有 major，errors={result['errors']}"
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 3: 文档入库后正常状态 → ok=True
# ============================================================================

def test_scenario_3_full_document_ingested(storage_dir):
    """3 真相源全有内容 + 6 派生 kv_store + 3 vdb 全存在且一致 → ok=True。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    edges = [("entity_1", "entity_2")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=edges)
    _write_json(storage_dir / "kv_store_full_docs.json",
                {"doc_1": {"content": "doc text"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json",
                {"resp_1": {"return": "cache text"}})
    _write_json(storage_dir / "kv_store_text_chunks.json",
                {"chunk_1": {"content": "chunk text", "full_doc_id": "doc_1"}})
    _write_json(storage_dir / "kv_store_doc_status.json",
                {"doc_1": {"content": "doc text", "chunks_list": ["chunk_1"]}})
    _write_json(storage_dir / "kv_store_entity_chunks.json",
                {"entity_1": ["chunk_1"]})
    _write_json(storage_dir / "kv_store_relation_chunks.json",
                {f"entity_1##entity_2": ["chunk_1"]})
    _write_json(storage_dir / "kv_store_full_entities.json",
                {"entity_1": {"entity_name": "entity_1"}})
    _write_json(storage_dir / "kv_store_full_relations.json",
                {f"entity_1##entity_2": {"src_id": "entity_1", "tgt_id": "entity_2"}})
    _write_vdb(storage_dir / "vdb_chunks.json", [_make_vdb_entity("chunk_1")])
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("entity_1"), _make_vdb_entity("entity_2")])
    _write_vdb(storage_dir / "vdb_relationships.json",
               [_make_vdb_relation("entity_1", "entity_2")])

    result = check_all()
    assert result["ok"] is True, f"errors={result['errors']}"
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 4: vdb_entities 缺向量（真损坏）→ major≥1
# ============================================================================

def test_scenario_4_vdb_entities_missing_is_real_corruption(storage_dir):
    """GraphML 有 node 但 vdb_entities 缺对应向量 → major≥1（真损坏，数据不一致）。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    # vdb_entities 只有 entity_1，缺 entity_2
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    # 派生 kv_store 全存在（避免被派生缺失干扰）
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})
    _write_json(storage_dir / "kv_store_entity_chunks.json", {})
    _write_json(storage_dir / "kv_store_relation_chunks.json", {})
    _write_json(storage_dir / "kv_store_full_entities.json", {})
    _write_json(storage_dir / "kv_store_full_relations.json", {})
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    result = check_all()
    assert result["ok"] is False, "vdb_entities 缺向量应判损坏"
    assert result["major_errors"] >= 1
    vdb_errors = [e for e in result["errors"] if e.get("check") == "vdb_entities_missing"]
    assert len(vdb_errors) >= 1


# ============================================================================
# 场景 5: vdb_relationships 缺向量（真损坏）→ major≥1
# ============================================================================

def test_scenario_5_vdb_relationships_missing_is_real_corruption(storage_dir):
    """GraphML 有 edge 但 vdb_relationships 缺对应向量 → major≥1（真损坏）。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    edges = [("entity_1", "entity_2")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=edges)
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("entity_1"), _make_vdb_entity("entity_2")])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})
    _write_json(storage_dir / "kv_store_entity_chunks.json", {})
    _write_json(storage_dir / "kv_store_relation_chunks.json", {})
    _write_json(storage_dir / "kv_store_full_entities.json", {})
    _write_json(storage_dir / "kv_store_full_relations.json", {})
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    result = check_all()
    assert result["ok"] is False, "vdb_relationships 缺向量应判损坏"
    assert result["major_errors"] >= 1
    vdb_errors = [e for e in result["errors"] if e.get("check") == "vdb_relationships_missing"]
    assert len(vdb_errors) >= 1


# ============================================================================
# 场景 6: GraphML corrupt（critical）
# ============================================================================

def test_scenario_6_graphml_corrupt_is_critical(storage_dir):
    """GraphML XML 解析失败 → critical（真损坏，不可恢复）。"""
    (storage_dir / "graph_chunk_entity_relation.graphml").write_text(
        "<?xml version='1.0'?><not-valid-graphml><broken"
    )
    result = check_all()
    assert result["ok"] is False
    assert result["critical_errors"] >= 1


# ============================================================================
# 场景 7: full_docs corrupt（critical）
# ============================================================================

def test_scenario_7_full_docs_corrupt_is_critical(storage_dir):
    """kv_store_full_docs.json JSON 解析失败 → critical。"""
    nodes = [("entity_1", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    (storage_dir / "kv_store_full_docs.json").write_text("{not valid json")
    result = check_all()
    assert result["ok"] is False
    assert result["critical_errors"] >= 1


# ============================================================================
# 场景 8: 派生文件部分缺失（混合）→ ok=True（派生缺失不报）
# ============================================================================

def test_scenario_8_partial_derived_missing_is_ok(storage_dir):
    """3 真相源有内容 + vdb 一致 + 6 派生 kv_store 中 3 个缺失 → ok=True。

    派生 kv_store 缺失不是损坏，LightRAG 内部按需 lazy 加载为空 dict。
    本方案不主动重建、不写空文件。
    """
    nodes = [("entity_1", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})
    # 只写 text_chunks + doc_status，缺 entity_chunks / relation_chunks / full_entities / full_relations
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})

    result = check_all()
    assert result["ok"] is True, f"派生缺失不应判损坏，errors={result['errors']}"
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 9: partial 真相源 + 修复入口不再 unrecoverable
# ============================================================================

def test_scenario_9_partial_truth_sources_not_unrecoverable(storage_dir, monkeypatch):
    """GraphML 有 node + full_docs/cache absent → _check_truth_sources_intact 应返回 intact=True。

    这是用户上次"尝试修复"失败的根因：partial 状态被误判为 unrecoverable。
    修复后脑区/Skills 合法状态应允许 repair_all 进入重建分支。
    """
    nodes = [("脑区_记忆", "brain_region")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("脑区_记忆")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    # full_docs / cache 不写（脑区路径合法状态）

    # 重定向 lightrag_repair 的 storage_dir
    from niu_api.internal import lightrag_repair
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", storage_dir)
    if hasattr(lightrag_repair, "_storage_dir"):
        monkeypatch.setattr(lightrag_repair, "_storage_dir", lambda: storage_dir)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    assert result["intact"] is True, \
        f"partial 真相源（GraphML 有 + full_docs/cache 缺）应判 intact=True，reason: {result}"
