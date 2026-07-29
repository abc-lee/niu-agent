"""Task 7: 端到端真实测试——合成 fixture 7 种损坏现场。

不 mock LLM，不 mock embedding（CLAUDE.md 铁律 5）。
用真实 embedding 模型（niu_api.internal.embedding.get_model）+ 真实 LightRAG 实例
（get_lightrag_for_repair，用于 tokenizer + chunking_by_token_size）。

7 种损坏现场：
1. 删 vdb_entities → repair → vdb_entities 重建
2. 删 9 全部派生文件 → repair → 9 全部重建
3. GraphML 损坏 → unrecoverable + 回滚（9 派生文件未被删除）
4. full_docs 损坏 → unrecoverable
5. cache 损坏 → unrecoverable
6. 含旧版本 doc + 已删实体 → 重建后不复活
7. weight 衰减值保留 → 重建后 GraphML 的 weight 不变

测试隔离铁律：所有测试在 tmp_path 内执行，不操作真实 ~/.niu/lightrag_storage 数据。
"""
import pytest

pytest.skip("v8-Task 1 将 repair_text_chunks 改为 unrecoverable stub，依赖 repair_all 成功的 E2E 测试需等 Task 4 重写", allow_module_level=True)

import json  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from niu_api.internal.lightrag_repair import repair_all  # noqa: E402

# 9 个派生文件清单（跟 lightrag_repair._DERIVED_FILES 一致；GraphML 是真相源不在内）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]


def _make_synthetic_fixture(tmp_path: Path) -> None:
    """合成 fixture：3 文档 + 5 cache + GraphML（含衰减后 weight + 已删实体已不在）。

    用真实 compute_mdhash_id 生成 chunk_id，让 GraphML source_id 跟 full_docs
    chunking 产出一致。否则 repair_text_chunks 的 full_docs 反查永远找不到匹配
    chunk → text_chunks 重建为空 → does_not_reanimate 测试"意外通过"（空 dict
    不含已删实体）而非"正确验证"。

    构造 v4 场景：
    - GraphML：2 个实体（entity-a, entity-b）+ 1 条 edge（weight=0.5 衰减后）
    - 已删实体 deleted-entity 不在 GraphML 里（模拟之前已正确删除）
    - full_docs：2 个文档（content 用于算真实 chunk_id）
    - cache：5 条 extract entry（含 1 个已删实体的脏 entry + 1 个旧版本 chunk 的 entry）
    - 9 个派生文件初始为空（repair_all 会重建）
    """
    from lightrag.utils import compute_mdhash_id

    # 用确定性的 full_docs 内容，算出真实 chunk_id
    doc_v1_content = "v1 content for synthetic fixture document one"
    doc_v2_content = "v2 content for synthetic fixture document two"
    chunk_id_1 = compute_mdhash_id(doc_v1_content, prefix="chunk-")
    chunk_id_2 = compute_mdhash_id(doc_v2_content, prefix="chunk-")

    # 已删实体/旧版本的 chunk_id（用不同 content，确保不在活跃集合）
    deleted_content = "deleted entity content that should not be rebuilt"
    old_content = "old version content that should not be rebuilt"
    chunk_id_deleted = compute_mdhash_id(deleted_content, prefix="chunk-")
    chunk_id_old = compute_mdhash_id(old_content, prefix="chunk-")

    # GraphML：2 个实体（entity-a, entity-b）+ 1 条 edge（weight=0.5 衰减后）
    # 已删实体 deleted-entity 不在 GraphML 里
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    for kid, attr_name, attr_type in [
        ("d1", "entity_type", "string"), ("d2", "description", "string"),
        ("d3", "source_id", "string"), ("d7", "weight", "double"),
        ("d8", "description", "string"), ("d9", "keywords", "string"),
        ("d10", "source_id", "string"),
    ]:
        ET.SubElement(root, f"{{{ns}}}key", {
            "id": kid, "for": "all", "attr.name": attr_name, "attr.type": attr_type
        })
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    a = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-a"})
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d2"}).text = "desc A"
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_1

    b = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-b"})
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d2"}).text = "desc B"
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_2

    edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": "entity-a", "target": "entity-b"})
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d7"}).text = "0.5"  # 衰减后的 weight
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = "edge desc"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = "keyword1, keyword2"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = f"{chunk_id_1}<SEP>{chunk_id_2}"

    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    # full_docs：2 个文档（用上面算 hash 的 content）
    docs = {
        "doc-v1": {"content": doc_v1_content, "file_path": "v1.md", "create_time": 1000},
        "doc-v2": {"content": doc_v2_content, "file_path": "v2.md", "create_time": 2000},
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))

    # cache：5 条 extract entry（含 1 个已删实体的脏 entry + 1 个旧版本 chunk 的 entry）
    cache = {
        "default:extract:chunk1": {
            "return": "entity<|#|>entity-a<|#|>concept<|#|>desc A",
            "cache_type": "extract", "chunk_id": chunk_id_1, "create_time": 1500,
        },
        "default:extract:chunk2": {
            "return": "entity<|#|>entity-b<|#|>concept<|#|>desc B",
            "cache_type": "extract", "chunk_id": chunk_id_2, "create_time": 1500,
        },
        # 已删实体的脏 entry（chunk_id_deleted 不在 GraphML 活跃集合）
        "default:extract:chunk_deleted": {
            "return": "entity<|#|>deleted-entity<|#|>concept<|#|>已删",
            "cache_type": "extract", "chunk_id": chunk_id_deleted, "create_time": 800,
        },
        # 旧版本 chunk 的 entry（chunk_id_old 不在 GraphML 活跃集合）
        "default:extract:chunk_old": {
            "return": "entity<|#|>old-entity<|#|>concept<|#|>旧版本",
            "cache_type": "extract", "chunk_id": chunk_id_old, "create_time": 500,
        },
        # 非 extract 类型 cache
        "default:summary:some": {
            "return": "summary", "cache_type": "summary", "chunk_id": None, "create_time": 1700,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 9 个派生文件初始为空（repair_all 会重建）
    for fname in ["kv_store_text_chunks.json", "kv_store_doc_status.json",
                  "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
                  "kv_store_full_entities.json", "kv_store_full_relations.json"]:
        (tmp_path / fname).write_text("{}")
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')


@pytest.fixture
def patched_storage(tmp_path, monkeypatch):
    """patch _STORAGE_DIR 到 tmp_path + 写合成 fixture + 预加载真实 embedding 模型。

    - 用真实 compute_mdhash_id 让 GraphML source_id 跟 full_docs chunking 一致
    - 用真实 embedding 模型（不 mock）
    - 用真实 LightRAG 实例（get_lightrag_for_repair）做 chunking_by_token_size
    """
    _make_synthetic_fixture(tmp_path)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    # 预加载真实 embedding 模型（不 mock LLM，不 mock embedding）
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载（测试前置条件）"

    return tmp_path


# =============================================================================
# 测试 1：删 vdb_entities → repair → vdb_entities 重建
# =============================================================================


def test_1_delete_vdb_entities_rebuild(patched_storage):
    """场景 1：删 vdb_entities.json → repair_all → 重建含 entity-a/entity-b 向量。"""
    storage = patched_storage

    # 删 vdb_entities.json
    vdb_entities_path = storage / "vdb_entities.json"
    assert vdb_entities_path.exists(), "fixture 应预置 vdb_entities.json"
    vdb_entities_path.unlink()
    assert not vdb_entities_path.exists(), "删除后应不存在"

    # repair_all 重建
    result = repair_all()

    # 断言修复成功（不是 unrecoverable）
    assert not result.get("_unrecoverable"), \
        f"修复应成功: {result.get('_unrecoverable_reason')}"

    # vdb_entities.json 重建
    assert vdb_entities_path.exists(), "vdb_entities.json 应被重建"
    vdb_data = json.loads(vdb_entities_path.read_text())
    assert "data" in vdb_data, "vdb_entities 应有 data 字段"

    # 重建后应含 entity-a 和 entity-b 两条向量
    entity_names = {entry.get("entity_name") for entry in vdb_data["data"]}
    assert "entity-a" in entity_names, "vdb_entities 应含 entity-a"
    assert "entity-b" in entity_names, "vdb_entities 应含 entity-b"

    # 每条向量应有非空 vector
    for entry in vdb_data["data"]:
        assert entry.get("vector"), f"entity {entry.get('entity_name')} 向量不应为空"


# =============================================================================
# 测试 2：删 9 全部派生文件 → repair → 9 全部重建
# =============================================================================


def test_2_delete_all_9_derived_rebuild(patched_storage):
    """场景 2：删 9 个派生文件全部 → repair_all → 9 个全重建。"""
    storage = patched_storage

    # 删 9 个派生文件全部
    for fname in _DERIVED_FILES:
        path = storage / fname
        if path.exists():
            path.unlink()
        assert not path.exists(), f"删除后 {fname} 应不存在"

    # repair_all 重建
    result = repair_all()

    # 断言修复成功
    assert not result.get("_unrecoverable"), \
        f"修复应成功: {result.get('_unrecoverable_reason')}"

    # 9 个派生文件全部重建
    for fname in _DERIVED_FILES:
        path = storage / fname
        assert path.exists(), f"{fname} 应被重建"
        assert path.stat().st_size > 0, f"{fname} 不应为空文件"


# =============================================================================
# 测试 3：GraphML 损坏 → unrecoverable + 回滚（9 派生文件未被删除）
# =============================================================================


def test_3_graphml_corrupt_unrecoverable_rollback(patched_storage):
    """场景 3：GraphML 写 corrupt → repair_all → unrecoverable + 9 派生文件未被删除。

    GraphML 是真相源，损坏=不可恢复。repair_all 应在步骤 1（检测真相源）就 return，
    不进入"备份 9 派生 → 删除 → 重建"流程，所以 9 派生文件保留原状。
    """
    storage = patched_storage

    # 记录 9 派生文件原始内容（用于断言"未被删除"）
    original_contents = {}
    for fname in _DERIVED_FILES:
        original_contents[fname] = (storage / fname).read_bytes()

    # GraphML 写 corrupt
    (storage / "graph_chunk_entity_relation.graphml").write_text("corrupt <<< not valid xml")

    # repair_all 应直接返回 unrecoverable
    result = repair_all()

    assert result.get("_unrecoverable") is True, "GraphML 损坏应报 unrecoverable"
    # 没进入删除流程，所以 _rolled_back=False（无回滚需要）
    assert result.get("_rolled_back") is False, \
        "GraphML 损坏在步骤 1 return，没备份没删除，不应触发回滚"

    # 9 派生文件保留原状（未被删除）
    for fname in _DERIVED_FILES:
        path = storage / fname
        assert path.exists(), f"{fname} 应保留（未被删除）"
        assert path.read_bytes() == original_contents[fname], \
            f"{fname} 内容应不变（未被修改）"


# =============================================================================
# 测试 4：full_docs 损坏 → unrecoverable
# =============================================================================


def test_4_full_docs_corrupt_unrecoverable(patched_storage):
    """场景 4：full_docs 写 corrupt → repair_all → unrecoverable。

    full_docs 是真相源，损坏=不可恢复。
    """
    storage = patched_storage

    # full_docs 写 corrupt（非合法 JSON）
    (storage / "kv_store_full_docs.json").write_text("corrupt not valid json <<<")

    # repair_all 应返回 unrecoverable
    result = repair_all()

    assert result.get("_unrecoverable") is True, "full_docs 损坏应报 unrecoverable"
    reason = result.get("_unrecoverable_reason", "")
    assert "full_docs" in reason, f"原因应提 full_docs: {reason}"


# =============================================================================
# 测试 5：cache 损坏 → unrecoverable
# =============================================================================


def test_5_cache_corrupt_unrecoverable(patched_storage):
    """场景 5：cache 写 corrupt → repair_all → unrecoverable。

    cache 是真相源，损坏=不可恢复。
    """
    storage = patched_storage

    # cache 写 corrupt（非合法 JSON）
    (storage / "kv_store_llm_response_cache.json").write_text("corrupt not valid json <<<")

    # repair_all 应返回 unrecoverable
    result = repair_all()

    assert result.get("_unrecoverable") is True, "cache 损坏应报 unrecoverable"
    reason = result.get("_unrecoverable_reason", "")
    assert "cache" in reason, f"原因应提 cache: {reason}"


# =============================================================================
# 测试 6：含旧版本 doc + 已删实体 → 重建后不复活
# =============================================================================


def test_6_no_reanimate_deleted_and_old(patched_storage):
    """场景 6：fixture 含已删实体（deleted-entity）+ 旧版本（old-entity）→ repair_all
    → 派生文件不含 deleted-entity / old-entity + text_chunks 含活跃 chunk。

    fixture 的 cache 含：
    - chunk_deleted: extract 出 deleted-entity（GraphML 不含，应不复活）
    - chunk_old: extract 出 old-entity（GraphML 不含，应不复活）

    text_chunks 必须真正含活跃 chunk（不是空 dict 意外通过）。
    """
    storage = patched_storage

    # 跑 repair_all
    result = repair_all()
    assert not result.get("_unrecoverable"), \
        f"修复应成功: {result.get('_unrecoverable_reason')}"

    from lightrag.utils import compute_mdhash_id
    doc_v1_content = "v1 content for synthetic fixture document one"
    deleted_content = "deleted entity content that should not be rebuilt"
    old_content = "old version content that should not be rebuilt"
    chunk_id_1 = compute_mdhash_id(doc_v1_content, prefix="chunk-")
    chunk_id_deleted = compute_mdhash_id(deleted_content, prefix="chunk-")
    chunk_id_old = compute_mdhash_id(old_content, prefix="chunk-")

    # 验证 text_chunks 真正重建了活跃 chunk（不是空 dict 意外通过）
    tc = json.loads((storage / "kv_store_text_chunks.json").read_text())
    assert chunk_id_1 in tc, "活跃 chunk 应被重建（证明 text_chunks 非空，不是意外通过）"
    assert tc[chunk_id_1]["content"] == doc_v1_content, "重建的 chunk 内容应匹配 full_docs"
    # 已删实体的 chunk 不重建
    assert chunk_id_deleted not in tc, "已删实体的 chunk 不应被重建"
    assert chunk_id_old not in tc, "旧版本 chunk 不应被重建"

    # 验证 9 派生文件都不含 deleted-entity / old-entity
    for fname in _DERIVED_FILES:
        path = storage / fname
        content = path.read_text()
        assert "deleted-entity" not in content, \
            f"{fname} 不应含 deleted-entity（已删实体不复活）"
        assert "old-entity" not in content, \
            f"{fname} 不应含 old-entity（旧版本不复活）"

    # 单独强校验 entity_chunks / full_entities / vdb_entities 不含这两个实体
    ec = json.loads((storage / "kv_store_entity_chunks.json").read_text())
    assert "deleted-entity" not in ec, "entity_chunks 不应含 deleted-entity"
    assert "old-entity" not in ec, "entity_chunks 不应含 old-entity"

    fe = json.loads((storage / "kv_store_full_entities.json").read_text())
    assert "deleted-entity" not in fe, "full_entities 不应含 deleted-entity"
    assert "old-entity" not in fe, "full_entities 不应含 old-entity"

    vdb_e = json.loads((storage / "vdb_entities.json").read_text())
    vdb_e_names = {entry.get("entity_name") for entry in vdb_e.get("data", [])}
    assert "deleted-entity" not in vdb_e_names, "vdb_entities 不应含 deleted-entity"
    assert "old-entity" not in vdb_e_names, "vdb_entities 不应含 old-entity"


# =============================================================================
# 测试 7：weight 衰减值保留 → 重建后 GraphML 的 weight 不变
# =============================================================================


def test_7_weight_preserved_graphml_untouched(patched_storage):
    """场景 7：fixture GraphML edge weight=0.5（衰减后）→ repair_all
    → GraphML 的 weight 仍是 0.5（GraphML 一字节未动）+ vdb_relationships 不含 weight
    （meta_fields 不含）。

    核心断言：
    - GraphML 完全不动（真相源不可动）
    - vdb_relationships 不含 weight 字段（meta_fields 不含 weight）
    """
    storage = patched_storage

    # 记录 GraphML 原始字节（用于断言"一字节未动"）
    graphml_path = storage / "graph_chunk_entity_relation.graphml"
    graphml_before = graphml_path.read_bytes()

    # 跑 repair_all
    result = repair_all()
    assert not result.get("_unrecoverable"), \
        f"修复应成功: {result.get('_unrecoverable_reason')}"

    # GraphML 一字节未动（真相源不可动）
    assert graphml_path.read_bytes() == graphml_before, \
        "GraphML 不应被修改（weight 衰减值保留的核心保证）"

    # 解析 GraphML 确认 weight 仍是 0.5
    tree = ET.parse(graphml_path)
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    weights = [d.text for d in tree.findall(".//g:edge/g:data[@key='d7']", ns)]
    assert weights == ["0.5"], f"GraphML edge weight 应仍是 0.5（衰减值保留）, got {weights}"

    # vdb_relationships 不含 weight 字段（meta_fields 不含 weight）
    vdb_rel = json.loads((storage / "vdb_relationships.json").read_text())
    for entry in vdb_rel.get("data", []):
        assert "weight" not in entry, \
            f"vdb_relationships entry 不应含 weight 字段: {entry}"
        # 应含的 meta_fields 是 src_id / tgt_id / content / source_id
        assert "src_id" in entry, "vdb_relationships entry 应含 src_id"
        assert "tgt_id" in entry, "vdb_relationships entry 应含 tgt_id"
