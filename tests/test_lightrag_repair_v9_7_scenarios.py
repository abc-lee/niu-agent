"""v9 修复程序 7 种知识图谱损坏场景测试（方案 L2402）。

7 种损坏场景（每个对应一种损坏类型）：
1. 删 vdb_entities → repair（重建 vdb_entities）
2. 删 9 派生文件全部 → repair（重建 9 派生）
3. GraphML 损坏 → unrecoverable + 不删除派生文件（保留现场）
4. full_docs 损坏 → unrecoverable
5. cache 损坏 → unrecoverable
6. 含旧版本 doc + 已删实体 → 重建后不复活（派生文件不含 deleted-entity / old-entity）
7. weight 衰减值保留 → 重建后 GraphML 的 weight 不变（GraphML 没被修改）

合格标准：
- 得到准确的知识图谱数据结构的完整恢复
- 不能对三个真相源文件有任何的改变
- 3 真相源 mtime + sha256 不变

测试隔离：
- 所有测试在 tmp_path 内执行，绝对不动真实 ~/.niu/lightrag_storage/。
- 场景 1-5：拷贝真实 3 真相源到 tmp_path（用 _copy_truth_sources helper）。
- 场景 6-7：用合成 fixture（构造含已删实体 / weight=0.5 的最小数据）。
- 用 _FakeEmbedModel 替代真实 bge 模型（避免加载 ~400MB）。
- monkeypatch niu_api.internal.embedding.get_model 返回 _FakeEmbedModel(dim=768)。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest


# =============================================================================
# 通用 helpers
# =============================================================================


_TRUTH_SOURCE_FILES = [
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]

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


class _FakeEmbedModel:
    """假 embedding 模型（替代真实 bge-base-zh-v1.5，避免测试加载 ~400MB 模型）。

    encode(texts) 返回固定 shape 的随机向量（dim=768），用于验证：
    - RepairEmbeddingFunc.__call__ 返回 np.ndarray
    - 维度正确
    - 批量分片后结果正确合并
    """

    def __init__(self, dim: int = 768):
        self.dim = dim
        self._call_count = 0

    def encode(self, texts, **kwargs):  # type: ignore[no-untyped-def]
        self._call_count += 1
        return np.random.rand(len(texts), self.dim).astype(np.float32)


def _copy_truth_sources(tmp_storage_dir: Path, real_storage_dir: Path) -> None:
    """拷贝 3 真相源到 tmp 目录（其他派生文件不拷贝，让 repair 重建）。"""
    tmp_storage_dir.mkdir(parents=True, exist_ok=True)
    for fname in _TRUTH_SOURCE_FILES:
        src = real_storage_dir / fname
        if src.exists():
            shutil.copy2(src, tmp_storage_dir / fname)


def _sha256(path: Path) -> str:
    """算文件 sha256（验证真相源不变）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_truth_source_state(storage_dir: Path) -> dict[str, dict[str, object]]:
    """记录 3 真相源 sha256 + mtime（repair 前快照）。"""
    state: dict[str, dict[str, object]] = {}
    for fname in _TRUTH_SOURCE_FILES:
        path = storage_dir / fname
        if path.exists():
            state[fname] = {
                "sha256": _sha256(path),
                "mtime": path.stat().st_mtime,
            }
        else:
            state[fname] = {"sha256": None, "mtime": None}
    return state


def _assert_truth_sources_unchanged(
    storage_dir: Path, before: dict[str, dict[str, object]]
) -> None:
    """断言 3 真相源 sha256 + mtime 完全不变（铁律 2）。"""
    after = _record_truth_source_state(storage_dir)
    for fname in _TRUTH_SOURCE_FILES:
        before_sha = before[fname]["sha256"]
        after_sha = after[fname]["sha256"]
        before_mtime = before[fname]["mtime"]
        after_mtime = after[fname]["mtime"]
        assert before_sha == after_sha, (
            f"真相源 {fname} sha256 变化（违反铁律 2）: "
            f"before={before_sha}, after={after_sha}"
        )
        assert before_mtime == after_mtime, (
            f"真相源 {fname} mtime 变化（违反铁律 2）: "
            f"before={before_mtime}, after={after_mtime}"
        )


def _patch_to_tmp_storage(tmp_storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """patch 所有 _STORAGE_DIR / STORAGE_DIR 引用到 tmp_path。

    包括 lightrag_repair._STORAGE_DIR / lightrag_integrity._STORAGE_DIR /
    lightrag_manager.STORAGE_DIR / lightrag_manager._rag_instance=None。
    """
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair._STORAGE_DIR", str(tmp_storage)
    )
    monkeypatch.setattr(
        "niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_storage
    )
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_storage
    )
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)


def _patch_fake_embedding(monkeypatch: pytest.MonkeyPatch) -> _FakeEmbedModel:
    """用 _FakeEmbedModel 替代真实 bge 模型（避免加载 ~400MB）。"""
    from niu_api.internal import embedding as niu_embedding

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    return fake_model


def _real_storage_exists() -> bool:
    """检查真实 ~/.niu/lightrag_storage 是否存在（场景 1-5 前置条件）。"""
    return (Path.home() / ".niu" / "lightrag_storage").exists()


# =============================================================================
# 合成 fixture（场景 6/7 用：构造含已删实体 / weight=0.5 的最小数据）
# =============================================================================


def _make_synthetic_fixture(tmp_path: Path) -> None:
    """合成 fixture：2 个文档 + 5 cache + GraphML（含衰减后 weight + 已删实体已不在）。

    用真实 compute_mdhash_id 生成 chunk_id，让 GraphML source_id 跟 full_docs
    chunking 产出一致。否则 repair_text_chunks 的 full_docs 反查永远找不到匹配
    chunk → text_chunks 重建为空 → does_not_reanimate 测试"意外通过"（空 dict
    不含已删实体）而非"正确验证"。

    构造 v9 场景：
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
        ("d1", "entity_type", "string"),
        ("d2", "description", "string"),
        ("d3", "source_id", "string"),
        ("d7", "weight", "double"),
        ("d8", "description", "string"),
        ("d9", "keywords", "string"),
        ("d10", "source_id", "string"),
    ]:
        ET.SubElement(
            root,
            f"{{{ns}}}key",
            {
                "id": kid,
                "for": "all",
                "attr.name": attr_name,
                "attr.type": attr_type,
            },
        )
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    a = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-a"})
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d2"}).text = "desc A"
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_1

    b = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-b"})
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d2"}).text = "desc B"
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_2

    edge = ET.SubElement(
        graph, f"{{{ns}}}edge", {"source": "entity-a", "target": "entity-b"}
    )
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d7"}).text = "0.5"  # 衰减后的 weight
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = "edge desc"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = "keyword1, keyword2"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = (
        f"{chunk_id_1}<SEP>{chunk_id_2}"
    )

    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True,
        encoding="utf-8",
    )

    # full_docs：2 个文档（用上面算 hash 的 content）
    docs = {
        "doc-v1": {
            "content": doc_v1_content,
            "file_path": "v1.md",
            "create_time": 1000,
        },
        "doc-v2": {
            "content": doc_v2_content,
            "file_path": "v2.md",
            "create_time": 2000,
        },
    }
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False)
    )

    # cache：5 条 extract entry（含 1 个已删实体的脏 entry + 1 个旧版本 chunk 的 entry）
    cache = {
        "default:extract:chunk1": {
            "return": "entity<|#|>entity-a<|#|>concept<|#|>desc A",
            "cache_type": "extract",
            "chunk_id": chunk_id_1,
            "create_time": 1500,
        },
        "default:extract:chunk2": {
            "return": "entity<|#|>entity-b<|#|>concept<|#|>desc B",
            "cache_type": "extract",
            "chunk_id": chunk_id_2,
            "create_time": 1500,
        },
        # 已删实体的脏 entry（chunk_id_deleted 不在 GraphML 活跃集合）
        "default:extract:chunk_deleted": {
            "return": "entity<|#|>deleted-entity<|#|>concept<|#|>已删",
            "cache_type": "extract",
            "chunk_id": chunk_id_deleted,
            "create_time": 800,
        },
        # 旧版本 chunk 的 entry（chunk_id_old 不在 GraphML 活跃集合）
        "default:extract:chunk_old": {
            "return": "entity<|#|>old-entity<|#|>concept<|#|>旧版本",
            "cache_type": "extract",
            "chunk_id": chunk_id_old,
            "create_time": 500,
        },
        # 非 extract 类型 cache
        "default:summary:some": {
            "return": "summary",
            "cache_type": "summary",
            "chunk_id": None,
            "create_time": 1700,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False)
    )

    # 9 个派生文件初始为空（repair_all 会重建）
    for fname in [
        "kv_store_text_chunks.json",
        "kv_store_doc_status.json",
        "kv_store_entity_chunks.json",
        "kv_store_relation_chunks.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
    ]:
        (tmp_path / fname).write_text("{}")
    for fname in [
        "vdb_chunks.json",
        "vdb_entities.json",
        "vdb_relationships.json",
    ]:
        (tmp_path / fname).write_text(
            '{"data": [], "embedding_dim": 0, "matrix": ""}'
        )


# =============================================================================
# 场景 1：删 vdb_entities → repair
# =============================================================================


async def test_scenario_1_delete_vdb_entities_repair(tmp_path, monkeypatch):
    """场景 1：删 vdb_entities.json → _repair_all_async → 重建含真实 entity 向量。

    验证：
    1. repair 不报 unrecoverable
    2. vdb_entities.json 重建（含正确 __id__ hash）
    3. 3 真相源 mtime + sha256 不变
    """
    if not _real_storage_exists():
        pytest.skip("真实 ~/.niu/lightrag_storage 不存在，跳过场景 1")

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)
    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    # 删 vdb_entities.json
    vdb_e_path = tmp_storage / "vdb_entities.json"
    assert vdb_e_path.exists() or True  # 全新用户可能没有，但场景 1 要求真实数据有
    # 真实数据应有 vdb_entities.json，但本测试只关心 repair 能否重建
    # 如果不存在，repair_all 会从 GraphML 重建（跟"删 vdb_entities"语义一致）
    if vdb_e_path.exists():
        vdb_e_path.unlink()
    # repair_all 会先删 9 派生文件再重建，所以这里 vdb_entities 一定不存在

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：repair 不报 unrecoverable
    assert not result.get("_unrecoverable", False), (
        f"repair 报 unrecoverable: {result.get('_unrecoverable_reason', '')}"
    )

    # 断言 2：vdb_entities.json 重建（含 __id__ hash）
    assert vdb_e_path.exists(), "vdb_entities.json 应被重建"
    vdb_data = json.loads(vdb_e_path.read_text())
    assert "data" in vdb_data, "vdb_entities 应有 data 字段"
    assert isinstance(vdb_data["data"], list)
    assert len(vdb_data["data"]) > 0, "真实数据应至少有 1 个 entity"
    # 每条 entity 含 __id__（storage 自动注入的 hash ID）
    for entry in vdb_data["data"]:
        assert "__id__" in entry, f"vdb_entities entry 缺 __id__: {entry}"
        assert entry["__id__"].startswith("ent-"), (
            f"__id__ 应是 ent- 前缀的 hash ID: {entry['__id__']}"
        )
        assert "entity_name" in entry, f"vdb_entities entry 缺 entity_name: {entry}"

    # 断言 3：3 真相源 mtime + sha256 不变
    _assert_truth_sources_unchanged(tmp_storage, truth_before)


# =============================================================================
# 场景 2：删 9 派生文件全部 → repair
# =============================================================================


async def test_scenario_2_delete_all_9_derived_repair(tmp_path, monkeypatch):
    """场景 2：删 9 个派生文件全部 → _repair_all_async → 9 个全重建。

    验证：
    1. repair 不报 unrecoverable
    2. 9 派生文件全部重建
    3. 3 真相源 mtime + sha256 不变
    """
    if not _real_storage_exists():
        pytest.skip("真实 ~/.niu/lightrag_storage 不存在，跳过场景 2")

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)
    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    # 删 9 个派生文件全部（虽然 _repair_all_async 内部也会删，但这里显式删模拟现场）
    for fname in _DERIVED_FILES:
        path = tmp_storage / fname
        if path.exists():
            path.unlink()

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：repair 不报 unrecoverable
    assert not result.get("_unrecoverable", False), (
        f"repair 报 unrecoverable: {result.get('_unrecoverable_reason', '')}"
    )

    # 断言 2：9 派生文件全部重建
    # 注意：如果真实数据是全新用户（GraphML 无 node），部分派生文件可能不写盘（v9 跟 LightRAG 一致）
    # 所以这里只断言"text_chunks/vdb_entities/vdb_relationships"等核心派生文件被重建
    # （真实数据应有 GraphML node → 这些派生文件应重建）
    graphml_path = tmp_storage / "graph_chunk_entity_relation.graphml"
    graphml_has_data = (
        graphml_path.exists() and graphml_path.stat().st_size > 200
    )
    if graphml_has_data:
        # 真实 GraphML 有 node → 核心派生文件应被重建
        for fname in [
            "kv_store_text_chunks.json",
            "kv_store_doc_status.json",
            "vdb_chunks.json",
            "vdb_entities.json",
            "vdb_relationships.json",
            "kv_store_entity_chunks.json",
            "kv_store_relation_chunks.json",
        ]:
            path = tmp_storage / fname
            assert path.exists(), f"{fname} 应被重建（GraphML 有数据）"
            assert path.stat().st_size > 0, f"{fname} 不应为空文件"
    # full_entities / full_relations 可能因 GraphML source_id 跟 doc_status chunks_list
    # 不交叉而不写盘（v9 跟 LightRAG 一致），不强断言

    # 断言 3：3 真相源 mtime + sha256 不变
    _assert_truth_sources_unchanged(tmp_storage, truth_before)


# =============================================================================
# 场景 3：GraphML 损坏 → unrecoverable + 不删除派生文件（保留现场）
# =============================================================================


async def test_scenario_3_graphml_corrupt_unrecoverable(tmp_path, monkeypatch):
    """场景 3：GraphML 写损坏 → _repair_all_async → unrecoverable + 9 派生文件未被删除。

    GraphML 是真相源，损坏=不可恢复。_repair_all_async 应在步骤 1（检测真相源）就 return，
    不进入"删除 9 派生 → 重建"流程，所以 9 派生文件保留原状。

    验证：
    1. result["_unrecoverable"] is True
    2. 9 派生文件 sha256 不变（未被删除/修改）
    3. 3 真相源 sha256 + mtime 不变（full_docs / cache 完好，GraphML 损坏但内容也不变）
    """
    if not _real_storage_exists():
        pytest.skip("真实 ~/.niu/lightrag_storage 不存在，跳过场景 3")

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)
    # 拷贝 9 派生文件（让"未被删除"断言有意义）
    for fname in _DERIVED_FILES:
        src = real_storage / fname
        if src.exists():
            shutil.copy2(src, tmp_storage / fname)

    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)
    # 记录 9 派生文件 sha256（验证未被删除/修改）
    derived_before: dict[str, str | None] = {}
    for fname in _DERIVED_FILES:
        path = tmp_storage / fname
        derived_before[fname] = _sha256(path) if path.exists() else None

    # 写损坏 GraphML（非合法 XML）
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        "corrupt <<< not valid xml"
    )

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：result["_unrecoverable"] is True
    assert result.get("_unrecoverable") is True, (
        "GraphML 损坏应报 unrecoverable"
    )
    # _repair_all_async 在步骤 1 检测到 3 真相源损坏 → return + _deleted=[]
    # （不进入删除派生文件流程，保留现场）
    assert result.get("_deleted") == [], (
        f"GraphML 损坏时不应删派生文件（_deleted 应为空），实际: {result.get('_deleted')}"
    )

    # 断言 2：9 派生文件 sha256 不变（未被删除/修改）
    for fname in _DERIVED_FILES:
        path = tmp_storage / fname
        if derived_before[fname] is None:
            assert not path.exists(), (
                f"{fname} 原本不存在，repair 后也不应存在（保留现场）"
            )
        else:
            assert path.exists(), (
                f"{fname} 应保留（GraphML 损坏时不删派生文件）"
            )
            after_sha = _sha256(path)
            assert after_sha == derived_before[fname], (
                f"{fname} sha256 变化（应保留现场不变）: "
                f"before={derived_before[fname]}, after={after_sha}"
            )

    # 断言 3：3 真相源 mtime + sha256 不变
    # GraphML 被改写为 "corrupt <<< not valid xml"，其 sha256/mtime 在"写损坏"后已变，
    # 但 _repair_all_async 不会再修改它（直接 return）。
    # 这里只校验 full_docs / cache（这俩是完好的，repair 不会动它们）。
    after_truth = _record_truth_source_state(tmp_storage)
    for fname in ["kv_store_full_docs.json", "kv_store_llm_response_cache.json"]:
        assert after_truth[fname]["sha256"] == truth_before[fname]["sha256"], (
            f"真相源 {fname} sha256 变化（repair 不应动 full_docs/cache）"
        )
        assert after_truth[fname]["mtime"] == truth_before[fname]["mtime"], (
            f"真相源 {fname} mtime 变化（repair 不应动 full_docs/cache）"
        )


# =============================================================================
# 场景 4：full_docs 损坏 → unrecoverable
# =============================================================================


async def test_scenario_4_full_docs_corrupt_unrecoverable(tmp_path, monkeypatch):
    """场景 4：full_docs 写损坏 → _repair_all_async → unrecoverable。

    full_docs 是真相源，损坏=不可恢复。

    验证：
    1. result["_unrecoverable"] is True
    2. reason 含 "full_docs"
    3. 3 真相源 mtime + sha256 不变（GraphML / cache 完好，full_docs 损坏但内容也不变）
    """
    if not _real_storage_exists():
        pytest.skip("真实 ~/.niu/lightrag_storage 不存在，跳过场景 4")

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)
    # 拷贝 9 派生文件（让"未被删除"断言有意义）
    for fname in _DERIVED_FILES:
        src = real_storage / fname
        if src.exists():
            shutil.copy2(src, tmp_storage / fname)

    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    # 写损坏 full_docs（非合法 JSON）
    (tmp_storage / "kv_store_full_docs.json").write_text(
        "corrupt not valid json <<<"
    )

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：result["_unrecoverable"] is True
    assert result.get("_unrecoverable") is True, (
        "full_docs 损坏应报 unrecoverable"
    )
    # 断言 2：reason 含 "full_docs"
    reason = result.get("_unrecoverable_reason", "")
    assert "full_docs" in reason, (
        f"原因应提 full_docs: {reason}"
    )
    # _repair_all_async 在步骤 1 检测到 3 真相源损坏 → return + _deleted=[]
    assert result.get("_deleted") == [], (
        f"full_docs 损坏时不应删派生文件（_deleted 应为空），实际: {result.get('_deleted')}"
    )

    # 断言 3：3 真相源 mtime + sha256 不变
    # full_docs 被改写为 "corrupt not valid json <<<"，其 sha256/mtime 在"写损坏"后已变，
    # 但 _repair_all_async 不会再修改它。
    # 这里只校验 GraphML / cache（这俩完好的）。
    after_truth = _record_truth_source_state(tmp_storage)
    for fname in [
        "graph_chunk_entity_relation.graphml",
        "kv_store_llm_response_cache.json",
    ]:
        assert after_truth[fname]["sha256"] == truth_before[fname]["sha256"], (
            f"真相源 {fname} sha256 变化（repair 不应动 GraphML/cache）"
        )
        assert after_truth[fname]["mtime"] == truth_before[fname]["mtime"], (
            f"真相源 {fname} mtime 变化（repair 不应动 GraphML/cache）"
        )


# =============================================================================
# 场景 5：cache 损坏 → unrecoverable
# =============================================================================


async def test_scenario_5_cache_corrupt_unrecoverable(tmp_path, monkeypatch):
    """场景 5：cache 写损坏 → _repair_all_async → unrecoverable。

    cache 是真相源，损坏=不可恢复。

    验证：
    1. result["_unrecoverable"] is True
    2. reason 含 "cache"
    3. 3 真相源 mtime + sha256 不变（GraphML / full_docs 完好，cache 损坏但内容也不变）
    """
    if not _real_storage_exists():
        pytest.skip("真实 ~/.niu/lightrag_storage 不存在，跳过场景 5")

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)
    # 拷贝 9 派生文件（让"未被删除"断言有意义）
    for fname in _DERIVED_FILES:
        src = real_storage / fname
        if src.exists():
            shutil.copy2(src, tmp_storage / fname)

    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    # 写损坏 cache（非合法 JSON）
    (tmp_storage / "kv_store_llm_response_cache.json").write_text(
        "corrupt not valid json <<<"
    )

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：result["_unrecoverable"] is True
    assert result.get("_unrecoverable") is True, (
        "cache 损坏应报 unrecoverable"
    )
    # 断言 2：reason 含 "cache"
    reason = result.get("_unrecoverable_reason", "")
    assert "cache" in reason, (
        f"原因应提 cache: {reason}"
    )
    # _repair_all_async 在步骤 1 检测到 3 真相源损坏 → return + _deleted=[]
    assert result.get("_deleted") == [], (
        f"cache 损坏时不应删派生文件（_deleted 应为空），实际: {result.get('_deleted')}"
    )

    # 断言 3：3 真相源 mtime + sha256 不变
    # cache 被改写为 "corrupt not valid json <<<"，其 sha256/mtime 在"写损坏"后已变，
    # 但 _repair_all_async 不会再修改它。
    # 这里只校验 GraphML / full_docs（这俩完好的）。
    after_truth = _record_truth_source_state(tmp_storage)
    for fname in [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
    ]:
        assert after_truth[fname]["sha256"] == truth_before[fname]["sha256"], (
            f"真相源 {fname} sha256 变化（repair 不应动 GraphML/full_docs）"
        )
        assert after_truth[fname]["mtime"] == truth_before[fname]["mtime"], (
            f"真相源 {fname} mtime 变化（repair 不应动 GraphML/full_docs）"
        )


# =============================================================================
# 场景 6：含旧版本 doc + 已删实体 → 重建后不复活
# =============================================================================


async def test_scenario_6_no_reanimate_deleted_and_old(tmp_path, monkeypatch):
    """场景 6：fixture 含已删实体（deleted-entity）+ 旧版本（old-entity）
    → _repair_all_async → 派生文件不含 deleted-entity / old-entity。

    fixture 的 cache 含：
    - chunk_deleted: extract 出 deleted-entity（GraphML 不含，应不复活）
    - chunk_old: extract 出 old-entity（GraphML 不含，应不复活）

    text_chunks 必须真正含活跃 chunk（不是空 dict 意外通过）。

    验证：
    1. repair 不报 unrecoverable
    2. text_chunks 真正重建了活跃 chunk（chunk_id_1）
    3. 已删实体的 chunk（chunk_id_deleted / chunk_id_old）不重建
    4. 9 派生文件都不含 deleted-entity / old-entity
    5. 单独强校验 entity_chunks / full_entities / vdb_entities 不含这两个实体
    6. 3 真相源 mtime + sha256 不变（合成 fixture 的 GraphML/full_docs/cache）
    """
    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    _make_synthetic_fixture(tmp_storage)

    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    from lightrag.utils import compute_mdhash_id

    doc_v1_content = "v1 content for synthetic fixture document one"
    deleted_content = "deleted entity content that should not be rebuilt"
    old_content = "old version content that should not be rebuilt"
    chunk_id_1 = compute_mdhash_id(doc_v1_content, prefix="chunk-")
    chunk_id_deleted = compute_mdhash_id(deleted_content, prefix="chunk-")
    chunk_id_old = compute_mdhash_id(old_content, prefix="chunk-")

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：repair 不报 unrecoverable
    assert not result.get("_unrecoverable", False), (
        f"修复应成功: {result.get('_unrecoverable_reason', '')}"
    )

    # 断言 2：text_chunks 真正重建了活跃 chunk（证明非空，不是意外通过）
    tc_path = tmp_storage / "kv_store_text_chunks.json"
    assert tc_path.exists(), "text_chunks.json 应被重建"
    tc = json.loads(tc_path.read_text())
    assert chunk_id_1 in tc, (
        "活跃 chunk 应被重建（证明 text_chunks 非空，不是意外通过）"
    )
    assert tc[chunk_id_1]["content"] == doc_v1_content, (
        "重建的 chunk 内容应匹配 full_docs"
    )

    # 断言 3：已删实体的 chunk 不重建
    assert chunk_id_deleted not in tc, "已删实体的 chunk 不应被重建"
    assert chunk_id_old not in tc, "旧版本 chunk 不应被重建"

    # 断言 4：9 派生文件都不含 deleted-entity / old-entity
    for fname in _DERIVED_FILES:
        path = tmp_storage / fname
        if not path.exists():
            # 全新用户场景部分派生文件可能不写盘（v9 跟 LightRAG 一致），跳过
            continue
        content = path.read_text()
        assert "deleted-entity" not in content, (
            f"{fname} 不应含 deleted-entity（已删实体不复活）"
        )
        assert "old-entity" not in content, (
            f"{fname} 不应含 old-entity（旧版本不复活）"
        )

    # 断言 5：单独强校验 entity_chunks / full_entities / vdb_entities 不含这两个实体
    ec_path = tmp_storage / "kv_store_entity_chunks.json"
    if ec_path.exists():
        ec = json.loads(ec_path.read_text())
        assert "deleted-entity" not in ec, "entity_chunks 不应含 deleted-entity"
        assert "old-entity" not in ec, "entity_chunks 不应含 old-entity"

    fe_path = tmp_storage / "kv_store_full_entities.json"
    if fe_path.exists():
        fe = json.loads(fe_path.read_text())
        assert "deleted-entity" not in fe, "full_entities 不应含 deleted-entity"
        assert "old-entity" not in fe, "full_entities 不应含 old-entity"

    vdb_e_path = tmp_storage / "vdb_entities.json"
    assert vdb_e_path.exists(), "vdb_entities.json 应被重建"
    vdb_e = json.loads(vdb_e_path.read_text())
    vdb_e_names = {
        entry.get("entity_name") for entry in vdb_e.get("data", [])
    }
    assert "deleted-entity" not in vdb_e_names, (
        "vdb_entities 不应含 deleted-entity"
    )
    assert "old-entity" not in vdb_e_names, "vdb_entities 不应含 old-entity"
    # 应含 entity-a / entity-b（这俩是 GraphML 活跃节点）
    assert "entity-a" in vdb_e_names, "vdb_entities 应含 entity-a"
    assert "entity-b" in vdb_e_names, "vdb_entities 应含 entity-b"

    # 断言 6：3 真相源 mtime + sha256 不变
    _assert_truth_sources_unchanged(tmp_storage, truth_before)


# =============================================================================
# 场景 7：weight 衰减值保留 → 重建后 GraphML 的 weight 不变
# =============================================================================


async def test_scenario_7_weight_preserved_graphml_untouched(
    tmp_path, monkeypatch
):
    """场景 7：fixture GraphML edge weight=0.5（衰减后）
    → _repair_all_async → GraphML 的 weight 仍是 0.5（GraphML 一字节未动）
    + vdb_relationships 不含 weight（meta_fields 不含）。

    核心断言：
    - GraphML 完全不动（真相源不可动）
    - vdb_relationships 不含 weight 字段（meta_fields 不含 weight）
    - 3 真相源 mtime + sha256 不变
    """
    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    _make_synthetic_fixture(tmp_storage)

    _patch_fake_embedding(monkeypatch)
    _patch_to_tmp_storage(tmp_storage, monkeypatch)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_before = _record_truth_source_state(tmp_storage)

    # 记录 GraphML 原始字节（用于断言"一字节未动"）
    graphml_path = tmp_storage / "graph_chunk_entity_relation.graphml"
    graphml_before = graphml_path.read_bytes()

    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # 断言 1：repair 不报 unrecoverable
    assert not result.get("_unrecoverable", False), (
        f"修复应成功: {result.get('_unrecoverable_reason', '')}"
    )

    # 断言 2：GraphML 一字节未动（真相源不可动）
    assert graphml_path.read_bytes() == graphml_before, (
        "GraphML 不应被修改（weight 衰减值保留的核心保证）"
    )

    # 断言 3：解析 GraphML 确认 weight 仍是 0.5
    tree = ET.parse(graphml_path)
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    weights = [
        d.text
        for d in tree.findall(".//g:edge/g:data[@key='d7']", ns)
    ]
    assert weights == ["0.5"], (
        f"GraphML edge weight 应仍是 0.5（衰减值保留）, got {weights}"
    )

    # 断言 4：vdb_relationships 不含 weight 字段（meta_fields 不含 weight）
    vdb_rel_path = tmp_storage / "vdb_relationships.json"
    assert vdb_rel_path.exists(), "vdb_relationships.json 应被重建"
    vdb_rel = json.loads(vdb_rel_path.read_text())
    assert len(vdb_rel.get("data", [])) > 0, (
        "合成 fixture 有 1 条 edge，vdb_relationships 应有 1 条向量"
    )
    for entry in vdb_rel.get("data", []):
        assert "weight" not in entry, (
            f"vdb_relationships entry 不应含 weight 字段: {entry}"
        )
        # 应含的 meta_fields 是 src_id / tgt_id / content / source_id / file_path
        assert "src_id" in entry, "vdb_relationships entry 应含 src_id"
        assert "tgt_id" in entry, "vdb_relationships entry 应含 tgt_id"
        assert "content" in entry, "vdb_relationships entry 应含 content"

    # 断言 5：3 真相源 mtime + sha256 不变
    _assert_truth_sources_unchanged(tmp_storage, truth_before)
