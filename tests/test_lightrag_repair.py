"""LightRAG 外挂修复测试（按依赖链从真相源重建版）

每个 repair 函数一个 PASS 场景 + 一个 FAIL 场景（22 个测试）。
用 tempfile.TemporaryDirectory + monkeypatch _STORAGE_DIR + monkeypatch _embed_text/_embed_batch
（返回固定向量，避免加载真实模型）。

测试覆盖：
- 11 个 repair 函数 × 2 场景 = 22 个
- unrecoverable 场景（full_docs 损坏 / text_chunks 损坏 / llm_response_cache 损坏）
- embedding 失败 >10% → status=error 不写文件
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest


# =============================================================================
# 工具函数：构造测试数据
# =============================================================================


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_graphml(
    nodes: list[tuple[str, str, str]] | None = None,
    edges: list[tuple[str, str, str, str, str]] | None = None,
) -> str:
    """构造 GraphML 字符串。

    Args:
        nodes: list of (id, description, source_id)
        edges: list of (src, tgt, description, source_id, keywords)
               keywords 可为空字符串（模拟无 keywords 的 edge）

    注意：GraphML 边数据 key 定义跟真实 LightRAG 一致：
        d8=description, d9=keywords, d10=source_id（真实文件头）
        节点 key：d2=description, d3=source_id
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<graph edgedefault="undirected">',
    ]
    for nid, desc, src in nodes or []:
        lines.append(f'<node id="{xml_escape(nid)}">')
        if desc:
            lines.append(f'<data key="d2">{xml_escape(desc)}</data>')
        if src:
            lines.append(f'<data key="d3">{xml_escape(src)}</data>')
        lines.append('</node>')
    for edge in edges or []:
        if len(edge) == 5:
            src, tgt, desc, sid, keywords = edge
        else:
            # 兼容旧 4-tuple（无 keywords）
            src, tgt, desc, sid = edge
            keywords = ""
        lines.append(f'<edge source="{xml_escape(src)}" target="{xml_escape(tgt)}">')
        if desc:
            lines.append(f'<data key="d8">{xml_escape(desc)}</data>')
        if keywords:
            lines.append(f'<data key="d9">{xml_escape(keywords)}</data>')
        if sid:
            lines.append(f'<data key="d10">{xml_escape(sid)}</data>')
        lines.append('</edge>')
    lines.append('</graph>')
    lines.append('</graphml>')
    return "\n".join(lines)


def _fixed_vec(text: str) -> list[float]:
    """用 text 的 hash 生成固定 4 维向量（避免加载真实模型）。"""
    h = hash(text) & 0xFFFF
    return [(h & 0xFF) / 255.0, ((h >> 8) & 0xFF) / 255.0, 0.5, 0.5]


def _vdb_text_to_chunks(text: str, chunk_size: int = 50) -> list[str]:
    """简单分块（测试用，不调真实 tokenizer）。"""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# =============================================================================
# fixture：隔离的 storage_dir + monkeypatch embedding
# =============================================================================


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """隔离的 storage_dir，monkeypatch _STORAGE_DIR。"""
    from niu_api.internal import lightrag_repair

    sd = tmp_path / "lightrag_storage"
    sd.mkdir()
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(sd))
    return sd


@pytest.fixture
def patched_embed(monkeypatch):
    """monkeypatch _embed_text 和 _embed_batch 返回固定向量。"""
    from niu_api.internal import lightrag_repair

    def fake_embed_text(text: str):
        return _fixed_vec(text)

    def fake_embed_batch(texts: list[str]):
        return [_fixed_vec(t) for t in texts]

    def fake_get_dim():
        return 4

    monkeypatch.setattr(lightrag_repair, "_embed_text", fake_embed_text)
    monkeypatch.setattr(lightrag_repair, "_embed_batch", fake_embed_batch)
    monkeypatch.setattr(lightrag_repair, "_get_embedding_dim", fake_get_dim)
    return lightrag_repair


# =============================================================================
# 1. repair_text_chunks
# =============================================================================


def test_repair_text_chunks_pass(storage_dir, patched_embed, monkeypatch):
    """v4: GraphML 活跃 chunk + text_chunks 缺失 → 从 full_docs 按需提取重建"""
    from niu_api.internal import lightrag_repair
    import xml.etree.ElementTree as ET
    from lightrag.utils import compute_mdhash_id

    # full_docs 含 1 个文档
    doc_content = "a" * 200
    expected_chunk_id = compute_mdhash_id(doc_content, prefix="chunk-")
    full_docs = {
        "doc-001": {"content": doc_content, "summary": "test doc"},
    }
    _write_json(storage_dir / "kv_store_full_docs.json", full_docs)
    # text_chunks 空（强制走 full_docs scan）
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    # GraphML：1 个实体引用 expected_chunk_id
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-1"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = "desc"
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = expected_chunk_id
    ET.ElementTree(root).write(
        storage_dir / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    # mock chunking_by_token_size 返回单块（整段不切，让 compute_mdhash_id 匹配 expected_chunk_id）
    def fake_chunking(tokenizer, content, **kwargs):
        return [
            {"content": content, "tokens": len(content), "chunk_order_index": 0}
        ]

    monkeypatch.setattr("lightrag.operate.chunking_by_token_size", fake_chunking)

    # mock get_lightrag_for_repair 返回带 tokenizer 的对象（v4 用 get_lightrag_for_repair）
    class FakeRag:
        tokenizer = "fake_tokenizer"

    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag_for_repair", lambda: FakeRag()
    )
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager._get_lightrag_config",
        lambda: {"chunk_token_size": 50, "chunk_overlap_token_size": 5},
    )

    result = lightrag_repair.repair_text_chunks()

    assert result["status"] == "ok"
    assert result["actual"] > 0
    assert result["source"] == "GraphML + full_docs"

    # 验证文件写入
    tc_data = json.loads((storage_dir / "kv_store_text_chunks.json").read_text())
    assert len(tc_data) == result["actual"]
    # expected_chunk_id 应在重建结果里（从 full_docs chunking 产出）
    assert expected_chunk_id in tc_data
    # 每条都有 full_doc_id 指向 full_docs
    for chunk_id, chunk_value in tc_data.items():
        assert chunk_value["full_doc_id"] in full_docs


def test_repair_text_chunks_fail_full_docs_corrupt(storage_dir, patched_embed, monkeypatch):
    """v4: GraphML 活跃 chunk + full_docs 损坏 + text_chunks 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair
    import xml.etree.ElementTree as ET

    # GraphML：1 个实体引用 chunk-x（活跃 chunk）
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-z"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = "desc"
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-z"
    ET.ElementTree(root).write(
        storage_dir / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    # text_chunks 损坏（非法 JSON）
    _write_text(storage_dir / "kv_store_text_chunks.json", '{"truncated":')
    # full_docs 损坏（非法 JSON）
    _write_text(storage_dir / "kv_store_full_docs.json", '{"truncated":')
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", storage_dir)

    result = lightrag_repair.repair_text_chunks()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "无法重建" in result["message"] or "损坏" in result["message"]


# =============================================================================
# 2. repair_doc_status
# =============================================================================


def test_repair_doc_status_pass(storage_dir, patched_embed):
    """text_chunks + full_docs 完好 → 重建 doc_status"""
    from niu_api.internal import lightrag_repair

    text_chunks = {
        "chunk-aaa": {"content": "chunk a", "full_doc_id": "doc-1"},
        "chunk-bbb": {"content": "chunk b", "full_doc_id": "doc-1"},
        "chunk-ccc": {"content": "chunk c", "full_doc_id": "doc-2"},
    }
    full_docs = {
        "doc-1": {"content": "doc 1 content"},
        "doc-2": {"content": "doc 2 content"},
    }
    _write_json(storage_dir / "kv_store_text_chunks.json", text_chunks)
    _write_json(storage_dir / "kv_store_full_docs.json", full_docs)

    result = lightrag_repair.repair_doc_status()

    assert result["status"] == "ok"
    assert result["actual"] == 2  # 2 个 doc
    assert result["source"] == "kv_store_text_chunks + kv_store_full_docs"

    ds_data = json.loads((storage_dir / "kv_store_doc_status.json").read_text())
    assert "doc-1" in ds_data
    assert "doc-2" in ds_data
    assert set(ds_data["doc-1"]["chunks_list"]) == {"chunk-aaa", "chunk-bbb"}
    assert ds_data["doc-2"]["chunks_list"] == ["chunk-ccc"]


def test_repair_doc_status_fail_text_chunks_corrupt(storage_dir, patched_embed):
    """text_chunks 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "kv_store_text_chunks.json", '{"truncated":')

    result = lightrag_repair.repair_doc_status()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "text_chunks 损坏" in result["message"]


# =============================================================================
# 3. repair_graphml
# =============================================================================


def test_repair_graphml_pass(storage_dir, patched_embed, monkeypatch):
    """LightRAG 实例可用 + cache 完好 → 调 apipeline 重建"""
    from niu_api.internal import lightrag_repair

    # 完好的 llm_response_cache
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {"key1": "value1"})
    # doc_status 有 2 个 PROCESSED 文档
    # 注：status 用大写模拟历史损坏数据，repair 后应转为小写
    _write_json(
        storage_dir / "kv_store_doc_status.json",
        {
            "doc-1": {"status": "PROCESSED", "chunks_list": ["chunk-a"]},
            "doc-2": {"status": "PROCESSED", "chunks_list": ["chunk-b"]},
        },
    )
    # 假装有一个旧 GraphML
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", "<graphml/>")

    # mock get_lightrag 返回 fake rag
    class FakeGraph:
        async def drop(self):
            return {"status": "success"}

    class FakeRag:
        force_llm_summary_on_merge = 8
        chunk_entity_relation_graph = FakeGraph()

        async def apipeline_process_enqueue_documents(self):
            # 模拟 apipeline 写一个新 GraphML
            graphml_content = _make_graphml(
                nodes=[("ent1", "desc1", "chunk-a"), ("ent2", "desc2", "chunk-b")],
                edges=[("ent1", "ent2", "edge desc", "chunk-a<SEP>chunk-b", "edge_kw")],
            )
            _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)

    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag", lambda: FakeRag()
    )

    result = lightrag_repair.repair_graphml()

    assert result["status"] == "ok"
    assert result["actual"] > 0  # nodes + edges
    # 验证 GraphML 已生成
    assert (storage_dir / "graph_chunk_entity_relation.graphml").exists()
    # 验证 doc_status 改为 pending（小写）
    ds_data = json.loads((storage_dir / "kv_store_doc_status.json").read_text())
    assert ds_data["doc-1"]["status"] == "pending"
    assert ds_data["doc-2"]["status"] == "pending"
    # 验证 monkeypatch 已恢复
    assert FakeRag.force_llm_summary_on_merge == 8


def test_repair_graphml_fail_cache_corrupt(storage_dir, patched_embed):
    """llm_response_cache 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "kv_store_llm_response_cache.json", '{"truncated":')

    result = lightrag_repair.repair_graphml()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "llm_response_cache 损坏" in result["message"]


def test_repair_graphml_fail_no_rag(storage_dir, patched_embed, monkeypatch):
    """LightRAG 实例未初始化 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_json(storage_dir / "kv_store_llm_response_cache.json", {"key1": "value1"})

    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag", lambda: None
    )

    result = lightrag_repair.repair_graphml()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "LightRAG 实例未初始化" in result["message"]


# =============================================================================
# 4. repair_vdb_chunks
# =============================================================================


def test_repair_vdb_chunks_pass(storage_dir, patched_embed):
    """text_chunks 完好 → 重新 embedding 重建 vdb_chunks"""
    from niu_api.internal import lightrag_repair

    text_chunks = {
        "chunk-aaa": {"content": "chunk a content", "full_doc_id": "doc-1"},
        "chunk-bbb": {"content": "chunk b content", "full_doc_id": "doc-1"},
    }
    _write_json(storage_dir / "kv_store_text_chunks.json", text_chunks)

    result = lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "ok"
    assert result["actual"] == 2
    assert result["source"] == "kv_store_text_chunks"

    # 验证 vdb 文件写入
    vdb_data = json.loads((storage_dir / "vdb_chunks.json").read_text())
    assert vdb_data["embedding_dim"] == 4
    assert len(vdb_data["data"]) == 2
    assert all("__id__" in item for item in vdb_data["data"])
    assert all("vector" in item for item in vdb_data["data"])


def test_repair_vdb_chunks_fail_text_chunks_corrupt(storage_dir, patched_embed):
    """text_chunks 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "kv_store_text_chunks.json", '{"truncated":')

    result = lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "text_chunks 损坏" in result["message"]


def test_repair_vdb_chunks_fail_embedding_high_failure(storage_dir, monkeypatch):
    """embedding 失败 >10% → status=error 不写文件"""
    from niu_api.internal import lightrag_repair

    # 10 个 chunk，全部让 embedding 失败
    text_chunks = {
        f"chunk-{i}": {"content": f"chunk {i} content", "full_doc_id": "doc-1"}
        for i in range(10)
    }
    _write_json(storage_dir / "kv_store_text_chunks.json", text_chunks)

    # mock embedding 返回 None（失败）
    monkeypatch.setattr(lightrag_repair, "_embed_batch", lambda texts: None)
    monkeypatch.setattr(lightrag_repair, "_get_embedding_dim", lambda: 4)

    result = lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "error"
    assert "embedding" in result["message"]
    # 文件不应写入
    assert not (storage_dir / "vdb_chunks.json").exists()


# =============================================================================
# 5. repair_vdb_entities
# =============================================================================


def test_repair_vdb_entities_pass(storage_dir, patched_embed):
    """GraphML nodes 完好 → 重新 embedding 重建 vdb_entities"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[
            ("ent1", "description 1", "chunk-a"),
            ("ent2", "description 2", "chunk-b"),
        ],
        edges=[],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)

    result = lightrag_repair.repair_vdb_entities()

    assert result["status"] == "ok"
    assert result["actual"] == 2
    assert result["source"] == "GraphML"

    vdb_data = json.loads((storage_dir / "vdb_entities.json").read_text())
    assert vdb_data["embedding_dim"] == 4
    assert len(vdb_data["data"]) == 2
    # __id__ 应该是 ent-{md5(name)}
    from lightrag.utils import compute_mdhash_id

    expected_ids = {compute_mdhash_id("ent1", prefix="ent-"), compute_mdhash_id("ent2", prefix="ent-")}
    actual_ids = {item["__id__"] for item in vdb_data["data"]}
    assert actual_ids == expected_ids
    # content 格式：f"{node_id}\n{desc}"（跟 LightRAG operate.py L1160 一致）
    contents = {item["entity_name"]: item["content"] for item in vdb_data["data"]}
    assert contents["ent1"] == "ent1\ndescription 1"
    assert contents["ent2"] == "ent2\ndescription 2"


def test_repair_vdb_entities_fail_graphml_corrupt(storage_dir, patched_embed):
    """GraphML 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", "<not valid xml>")

    result = lightrag_repair.repair_vdb_entities()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


# =============================================================================
# 6. repair_vdb_relationships
# =============================================================================


def test_repair_vdb_relationships_pass(storage_dir, patched_embed):
    """GraphML edges 完好 → 重新 embedding 重建 vdb_relationships

    content 格式跟 LightRAG operate.py L1601 一致：
        f"{keywords}\t{src}\n{tgt}\n{desc}"
    """
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[("ent1", "desc1", ""), ("ent2", "desc2", "")],
        edges=[
            ("ent1", "ent2", "edge desc", "chunk-a<SEP>chunk-b", "edge_keywords"),
        ],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)

    result = lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok"
    assert result["actual"] == 1
    assert result["source"] == "GraphML"

    vdb_data = json.loads((storage_dir / "vdb_relationships.json").read_text())
    assert len(vdb_data["data"]) == 1
    # __id__ 应该用 make_relation_vdb_ids 的第一个（正序）
    from lightrag.utils import make_relation_vdb_ids

    expected_ids = set(make_relation_vdb_ids("ent1", "ent2"))
    assert vdb_data["data"][0]["__id__"] in expected_ids
    # src_id/tgt_id 应该是 sorted 后的值
    src, tgt = sorted(("ent1", "ent2"))
    assert vdb_data["data"][0]["src_id"] == src
    assert vdb_data["data"][0]["tgt_id"] == tgt
    # content 格式：f"{keywords}\t{sorted_src}\n{sorted_tgt}\n{desc}"
    expected_content = f"edge_keywords\t{src}\n{tgt}\nedge desc"
    assert vdb_data["data"][0]["content"] == expected_content


def test_repair_vdb_relationships_fail_graphml_corrupt(storage_dir, patched_embed):
    """GraphML 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", "<not valid xml>")

    result = lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


# =============================================================================
# 7. repair_entity_chunks
# =============================================================================


def test_repair_entity_chunks_pass(storage_dir, patched_embed):
    """GraphML nodes source_id 完好 → 重建 entity_chunks"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[
            ("ent1", "desc1", "chunk-a<SEP>chunk-b"),
            ("ent2", "desc2", "chunk-c"),
        ],
        edges=[],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)

    result = lightrag_repair.repair_entity_chunks()

    assert result["status"] == "ok"
    assert result["actual"] == 2
    assert result["source"] == "GraphML node source_id"

    ec_data = json.loads((storage_dir / "kv_store_entity_chunks.json").read_text())
    assert ec_data["ent1"]["chunk_ids"] == ["chunk-a", "chunk-b"]
    assert ec_data["ent1"]["count"] == 2
    assert ec_data["ent2"]["chunk_ids"] == ["chunk-c"]
    assert ec_data["ent2"]["count"] == 1


def test_repair_entity_chunks_fail_graphml_corrupt(storage_dir, patched_embed):
    """GraphML 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", "<not valid xml>")

    result = lightrag_repair.repair_entity_chunks()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


# =============================================================================
# 8. repair_relation_chunks
# =============================================================================


def test_repair_relation_chunks_pass(storage_dir, patched_embed):
    """GraphML edges source_id 完好 → 重建 relation_chunks"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[("ent1", "", ""), ("ent2", "", ""), ("ent3", "", "")],
        edges=[
            ("ent1", "ent2", "edge desc", "chunk-a<SEP>chunk-b", "keyword1"),
            ("ent2", "ent3", "edge desc 2", "chunk-c", "keyword2"),
        ],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)

    result = lightrag_repair.repair_relation_chunks()

    assert result["status"] == "ok"
    assert result["actual"] == 2
    assert result["source"] == "GraphML edge source_id"

    rc_data = json.loads((storage_dir / "kv_store_relation_chunks.json").read_text())
    # key 应该是 make_relation_chunk_key 格式
    from lightrag.utils import make_relation_chunk_key

    expected_key1 = make_relation_chunk_key("ent1", "ent2")
    expected_key2 = make_relation_chunk_key("ent2", "ent3")
    assert expected_key1 in rc_data
    assert expected_key2 in rc_data
    assert rc_data[expected_key1]["chunk_ids"] == ["chunk-a", "chunk-b"]
    assert rc_data[expected_key1]["count"] == 2
    assert rc_data[expected_key2]["chunk_ids"] == ["chunk-c"]
    assert rc_data[expected_key2]["count"] == 1


def test_repair_relation_chunks_fail_graphml_corrupt(storage_dir, patched_embed):
    """GraphML 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", "<not valid xml>")

    result = lightrag_repair.repair_relation_chunks()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


# =============================================================================
# 9. repair_full_entities
# =============================================================================


def test_repair_full_entities_pass(storage_dir, patched_embed):
    """GraphML source_id + doc_status 完好 → 重建 full_entities"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[
            ("ent1", "desc1", "chunk-a<SEP>chunk-b"),
            ("ent2", "desc2", "chunk-c"),
        ],
        edges=[],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)
    # doc_status 提供 chunk→doc 映射
    _write_json(
        storage_dir / "kv_store_doc_status.json",
        {
            "doc-1": {"chunks_list": ["chunk-a", "chunk-b"]},
            "doc-2": {"chunks_list": ["chunk-c"]},
        },
    )

    result = lightrag_repair.repair_full_entities()

    assert result["status"] == "ok"
    assert result["actual"] == 2  # 2 个 doc
    assert "GraphML source_id + doc_status chunks_list" in result["source"]

    fe_data = json.loads((storage_dir / "kv_store_full_entities.json").read_text())
    assert "doc-1" in fe_data
    assert "ent1" in fe_data["doc-1"]
    assert "doc-2" in fe_data
    assert "ent2" in fe_data["doc-2"]


def test_repair_full_entities_fail_doc_status_corrupt(storage_dir, patched_embed):
    """doc_status 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[("ent1", "desc1", "chunk-a")],
        edges=[],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)
    _write_text(storage_dir / "kv_store_doc_status.json", '{"truncated":')

    result = lightrag_repair.repair_full_entities()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "doc_status 损坏" in result["message"]


# =============================================================================
# 10. repair_full_relations
# =============================================================================


def test_repair_full_relations_pass(storage_dir, patched_embed):
    """GraphML edge source_id + doc_status 完好 → 重建 full_relations"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[("ent1", "", ""), ("ent2", "", "")],
        edges=[
            ("ent1", "ent2", "edge desc", "chunk-a<SEP>chunk-b", "kw"),
        ],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)
    _write_json(
        storage_dir / "kv_store_doc_status.json",
        {
            "doc-1": {"chunks_list": ["chunk-a", "chunk-b"]},
        },
    )

    result = lightrag_repair.repair_full_relations()

    assert result["status"] == "ok"
    assert result["actual"] == 1  # 1 个 doc
    assert "GraphML edge source_id + doc_status chunks_list" in result["source"]

    fr_data = json.loads((storage_dir / "kv_store_full_relations.json").read_text())
    assert "doc-1" in fr_data
    from lightrag.utils import make_relation_chunk_key

    expected_key = make_relation_chunk_key("ent1", "ent2")
    assert expected_key in fr_data["doc-1"]


def test_repair_full_relations_fail_doc_status_corrupt(storage_dir, patched_embed):
    """doc_status 损坏 → unrecoverable"""
    from niu_api.internal import lightrag_repair

    graphml_content = _make_graphml(
        nodes=[("ent1", "", ""), ("ent2", "", "")],
        edges=[("ent1", "ent2", "desc", "chunk-a", "kw")],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)
    _write_text(storage_dir / "kv_store_doc_status.json", '{"truncated":')

    result = lightrag_repair.repair_full_relations()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "doc_status 损坏" in result["message"]


# =============================================================================
# 11. repair_llm_response_cache
# =============================================================================


def test_repair_llm_response_cache_pass(storage_dir, patched_embed):
    """cache 损坏 → 清空 + 清空 text_chunks.llm_cache_list"""
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "kv_store_llm_response_cache.json", '{"truncated":')
    _write_json(
        storage_dir / "kv_store_text_chunks.json",
        {
            "chunk-a": {"content": "a", "llm_cache_list": ["cache_key_1", "cache_key_2"]},
            "chunk-b": {"content": "b", "llm_cache_list": ["cache_key_3"]},
        },
    )

    result = lightrag_repair.repair_llm_response_cache()

    assert result["status"] == "ok"
    assert "不可重建" in result["message"]

    # cache 应被清空为 {}
    cache_data = json.loads((storage_dir / "kv_store_llm_response_cache.json").read_text())
    assert cache_data == {}

    # text_chunks.llm_cache_list 应被清空
    tc_data = json.loads((storage_dir / "kv_store_text_chunks.json").read_text())
    assert tc_data["chunk-a"]["llm_cache_list"] == []
    assert tc_data["chunk-b"]["llm_cache_list"] == []


def test_repair_llm_response_cache_pass_empty(storage_dir, patched_embed):
    """cache 文件不存在 → 写空 dict"""
    from niu_api.internal import lightrag_repair

    # 不创建 cache 文件
    result = lightrag_repair.repair_llm_response_cache()

    assert result["status"] == "ok"
    cache_data = json.loads((storage_dir / "kv_store_llm_response_cache.json").read_text())
    assert cache_data == {}


# =============================================================================
# repair_all 集成测试
# =============================================================================


def test_repair_all_empty_storage_ok(storage_dir, patched_embed, monkeypatch):
    """空 storage（所有文件不存在）→ check_all 全过 → repair_all 全部跳过。

    v2: repair_all 改为按 check 结果选择性调用。空 storage 没报错 → 全跳过。
    """
    from niu_api.internal import lightrag_repair

    # mock get_lightrag 返回 None（不会被调到，因为 check_all 全过）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag", lambda: None
    )

    result = lightrag_repair.repair_all()

    # 没有 repair 被调用（check_all 全过）
    actual_repair_keys = {k for k in result.keys() if not k.startswith("_")}
    assert actual_repair_keys == set(), f"空 storage 不应调用任何 repair，实际调了: {actual_repair_keys}"
    # _skipped 应包含全部 13 个（含 brainregion_zombies）
    assert set(result.get("_skipped", [])) == {
        "brainregion_zombies",
        "text_chunks", "doc_status", "graphml", "graphml_orphan_edges",
        "vdb_chunks", "vdb_entities",
        "vdb_relationships", "entity_chunks", "relation_chunks", "full_entities",
        "full_relations", "llm_response_cache",
    }
    # _check_summary 应存在且 ok=True
    assert result["_check_summary"]["ok"] is True
    assert result["_check_summary"]["critical_errors"] == 0
    assert result["_check_summary"]["major_errors"] == 0
    # 没有 _unrecoverable
    assert result.get("_unrecoverable") is None


def test_repair_all_unrecoverable_propagates(storage_dir, patched_embed, monkeypatch):
    """full_docs 损坏 → _check_truth_sources_intact 报 full_docs 损坏 → _unrecoverable=True。

    v4: full_docs 是 3 真相源之一，损坏 = 不可恢复。repair_all 直接返回
    _unrecoverable=True + _truth_source_check，不调任何 repair 函数。
    """
    from niu_api.internal import lightrag_repair

    _write_text(storage_dir / "kv_store_full_docs.json", '{"truncated":')

    result = lightrag_repair.repair_all()

    # v4: 真相源损坏 → _unrecoverable=True，不调任何 repair 函数
    assert result.get("_unrecoverable") is True
    assert "full_docs" in result["_truth_source_check"]
    assert result["_truth_source_check"]["full_docs"]["intact"] is False
    assert "full_docs" in result.get("_unrecoverable_reason", "")
    # 没有任何重建子项（9 派生文件 repair 全部跳过）
    for key in ("text_chunks", "doc_status", "vdb_chunks", "vdb_entities",
                "vdb_relationships", "entity_chunks", "relation_chunks",
                "full_entities", "full_relations"):
        assert key not in result


def test_repair_all_returns_expected_actual_lost_fields(storage_dir, patched_embed, monkeypatch):
    """repair_all 每个返回值都有 expected/actual/lost 字段。

    v2: 准备数据让 check 报 vdb_entities_missing（GraphML 有 node 但 vdb 缺）→
    调 repair_vdb_entities，返回值应有 status/expected/actual/lost/source/message。
    """
    from niu_api.internal import lightrag_repair

    # 准备数据：GraphML 有 1 个 node，vdb_entities 不存在 → check vdb_entities_missing 报错
    graphml_content = _make_graphml(
        nodes=[("ent1", "desc1", "chunk-a")],
        edges=[],
    )
    _write_text(storage_dir / "graph_chunk_entity_relation.graphml", graphml_content)
    # text_chunks 完整（避免 text_chunks 相关 check 误报）
    _write_json(
        storage_dir / "kv_store_text_chunks.json",
        {"chunk-a": {"content": "a", "full_doc_id": "doc-1"}},
    )
    _write_json(
        storage_dir / "kv_store_full_docs.json",
        {"doc-1": {"content": "a"}},
    )
    _write_json(
        storage_dir / "kv_store_doc_status.json",
        {"doc-1": {"status": "PROCESSED", "chunks_list": ["chunk-a"]}},
    )
    _write_json(
        storage_dir / "kv_store_entity_chunks.json",
        {"ent1": {"chunk_ids": ["chunk-a"], "count": 1}},
    )

    # mock get_lightrag 返回 None（不应该被调到，因为 check 报的 vdb_entities_missing 走 repair_vdb_entities，不依赖 LightRAG）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag", lambda: None
    )

    result = lightrag_repair.repair_all()

    # vdb_entities 应被调用并返回完整字段
    assert "vdb_entities" in result, f"vdb_entities 应被调用，实际 keys: {list(result.keys())}"
    r = result["vdb_entities"]
    assert r["status"] == "ok"
    assert "expected" in r
    assert "actual" in r
    assert "lost" in r
    assert "source" in r
    assert "message" in r
    # text_chunks/graphml 不应被调用（check 没报错）
    assert "text_chunks" not in result
    assert "graphml" not in result
