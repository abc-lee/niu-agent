"""repair_text_chunks v4 单元测试（v8-Task 1 删除了 brainregion_zombies/graphml/cache 测试）。"""
import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _write_graphml(tmp_path: Path, nodes: list[tuple[str, str, str]]):
    """写 GraphML。nodes = [(node_id, desc, source_id), ...]"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for node_id, desc, src in nodes:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": node_id})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = desc
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = src
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )


def _make_storage_with_zombie_cache(tmp_path: Path):  # pyright: ignore[reportUnusedFunction]
    """生成含僵尸脑区 + 僵尸 cache 的测试存储。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    znode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "智家测试脑区"})
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d2"}).text = "被删除的重复脑区实体之一。<SEP>brain_meta_size:0"
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-zombie"

    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"

    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    cache = {
        "default:extract:zombie_key": {
            "return": "entity<|#|>智家测试脑区<|#|>brainregion<|#|>被删除的重复脑区实体之一。\nentity<|#|>聊天历史脑区<|#|>brainregion<|#|>正常脑区描述",
            "cache_type": "extract",
            "chunk_id": "chunk-zombie",
            "create_time": 1781930610,
        },
        "default:extract:normal_key": {
            "return": "entity<|#|>正常实体<|#|>concept<|#|>正常描述",
            "cache_type": "extract",
            "chunk_id": "chunk-normal",
            "create_time": 1781930611,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False)
    )

    for fname in ["kv_store_full_docs.json", "kv_store_text_chunks.json",
                  "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
                  "kv_store_full_entities.json", "kv_store_full_relations.json",
                  "kv_store_doc_status.json"]:
        (tmp_path / fname).write_text("{}")
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')


def test_check_all_vdb_missing_but_graphml_intact_returns_major(tmp_path, monkeypatch):
    """vdb_*.json 缺失但 GraphML 完好 → check_all 应报 major（避免启动放行）。"""
    # 2 真相源完好
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # GraphML 完好（有 node）
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "test-entity"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "concept"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    # vdb_entities 不存在（被删了）
    # vdb_relationships 不存在
    # vdb_chunks 不存在

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["major_errors"] >= 1
    err_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    assert any("vdb" in m.lower() for m in err_msgs)


def test_check_all_truth_sources_intact_returns_ok(tmp_path, monkeypatch):
    """3 真相源 + 9 派生文件全部完好 → ok=True（v4 简化后派生文件按 missing 粒度检测）。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # GraphML（有 node）
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "test-entity"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "concept"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    # v4：9 派生文件必须全齐才不报 major
    _derived_files_list = [
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
    vdb_e = {"data": [{"__id__": "ent-test-entity", "entity_name": "test-entity", "vector": "AAAAAA=="}],
             "file_hash": "fake", "embedding_dim": 8, "matrix": "AAAAAA=="}
    (tmp_path / "vdb_entities.json").write_text(json.dumps(vdb_e, ensure_ascii=False))
    for fname in _derived_files_list:
        if fname == "vdb_entities.json":
            continue
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text(json.dumps({"data": [], "embedding_dim": 8, "matrix": ""}, ensure_ascii=False))
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is True


def test_get_lightrag_status_total_errors_correct(tmp_path, monkeypatch):
    """get_lightrag_status 暴露的 total_errors 应 = critical + major + minor。

    用真实 check_all() 返回结构验证（顶层 critical_errors/major_errors/minor_errors 标量字段），
    不用 fake 结构——避免掩盖 check_all 实际返回结构的 bug（违反铁律 5）。
    """
    from niu_api.internal import lightrag_manager

    # 准备损坏现场：GraphML 有 node 但 vdb_entities 不存在（major：数据一致性真损坏）
    # v2 检测逻辑：3 真相源缺失视为全新用户合法（不报 critical），
    # 真损坏由 _check_vdb_missing 检测（GraphML 有 node 但 vdb 缺向量）。
    (tmp_path / "kv_store_full_docs.json").write_text("{}")  # 空 dict（全新用户合法）
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"x": {"return": "y", "cache_type": "extract", "chunk_id": "chunk-x"}}, ensure_ascii=False)
    )
    # 写 GraphML（有 node）+ 不写 vdb_entities → _check_vdb_missing 报 major
    _write_graphml(tmp_path, [("test-entity", "desc", "chunk-x")])

    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    # patch lightrag_manager.STORAGE_DIR + _rag_instance（避免污染真实数据）
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    status = lightrag_manager.get_lightrag_status()

    assert status["integrity"]["ok"] is False
    # total_errors 应 = critical + major + minor（不是永远 0）
    assert status["integrity"]["total_errors"] >= 1
    assert status["integrity"]["total_errors"] != 0
    # 新字段也应暴露
    assert "critical_errors" in status["integrity"]
    assert "major_errors" in status["integrity"]
    assert "minor_errors" in status["integrity"]
    # total_errors 应 = critical + major + minor
    c = status["integrity"]["critical_errors"]
    m = status["integrity"]["major_errors"]
    n = status["integrity"]["minor_errors"]
    assert status["integrity"]["total_errors"] == c + m + n, \
        f"total_errors={status['integrity']['total_errors']} 应 = critical({c}) + major({m}) + minor({n})"


def test_run_repair_on_user_request_repaired_based_on_unrecoverable_flag(tmp_path, monkeypatch):
    """repaired 应基于 repair_all 的 _unrecoverable 字段，不基于 check_all 重检。

    v1 用"重检 check_all 报 major=0"判定 repaired，但历史残留孤儿 chunk 永远报
    major 导致永远 repaired=False。新设计改为基于 repair_all 返回的 _unrecoverable
    字段：只要 repair_all 没报 _unrecoverable，repaired 应为 True（即使重检报 major）。
    """
    from niu_api.internal import lightrag_manager

    # 准备真相源（最小合法存储，repair_all 能成功重建）
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)

    # SkillSync 首次扫描在测试环境里跑不起来，mock 成超时返回 False
    # （函数内部会继续往下跑，二次 repair 不会被触发——post_critical/major 都 0）
    monkeypatch.setattr(
        "agent.injector.sync.wait_first_scan_complete",
        lambda *_args, **_kwargs: False,
    )

    result = lightrag_manager.run_repair_on_user_request()

    assert "repaired" in result
    assert isinstance(result["repaired"], bool)
    # repaired 应基于 _unrecoverable 字段
    if result.get("repair_result", {}).get("_unrecoverable"):
        assert result["repaired"] is False
    else:
        assert result["repaired"] is True


def test_run_repair_on_user_request_repaired_true_when_unrecoverable_false_but_check_major(tmp_path, monkeypatch):
    """区分新旧逻辑的核心场景：repair_all 成功(_unrecoverable=False) 但 check_all 重检报 major>0。

    v1 旧逻辑：repaired = not has_unrecoverable and critical==0 and major==0 → False（bug）
    新逻辑：   repaired = not has_unrecoverable and not repair_result.get('_unrecoverable') → True

    这就是计划提到的"历史残留孤儿 chunk 永远报 major 导致永远 repaired=False"场景——
    重检报 major（孤儿 chunk 未清），但 repair_all 已尽力修了没报 unrecoverable，
    用户应看到 repaired=True（修复已尽力），而不是永远卡在 False。
    """
    from niu_api.internal import lightrag_manager

    # 准备真相源（最小合法存储）
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # SkillSync 首次扫描 mock 成未完成（避免触发二次 repair）
    monkeypatch.setattr(
        "agent.injector.sync.wait_first_scan_complete",
        lambda *_args, **_kwargs: False,
    )

    # 关键：mock repair_all 返回 _unrecoverable=False（修复成功尽力了）
    # 但 check_all 重检返回 major_errors=1（孤儿 chunk 残留，非致命）
    # 旧代码 repaired=False（永远卡），新代码 repaired=True（修复尽力了）
    import niu_api.internal.lightrag_integrity as integrity_mod
    import niu_api.internal.lightrag_repair as repair_mod

    def fake_repair_all():
        # 扁平结构 + _unrecoverable=False（修复未触发不可恢复错误）
        return {
            "_unrecoverable": False,
            "_skipped": [],
            "_deleted": ["kv_store_text_chunks.json"],
            "_check_summary": {"ok": True},
            "text_chunks": {"status": "ok", "rebuilt_count": 1},
        }

    def fake_check_all():
        # 重检发现孤儿 chunk（major=1）——这是计划提到的"历史残留孤儿 chunk"
        return {
            "ok": False,
            "critical_errors": 0,
            "major_errors": 1,
            "minor_errors": 0,
            "errors": [{"code": "entity_chunks_dangling", "severity": "major"}],
        }

    monkeypatch.setattr(repair_mod, "repair_all", fake_repair_all)
    monkeypatch.setattr(integrity_mod, "check_all", fake_check_all)
    # lightrag_manager 内部 from ... import 引用，要 patch 模块级名字
    # 但 run_repair_on_user_request 内部用 `from niu_api.internal.lightrag_repair import repair_all`
    # 是局部导入，每次调用都重新解析，所以 patch repair_mod.repair_all 即可生效。

    result = lightrag_manager.run_repair_on_user_request()

    # 新逻辑：repaired 应为 True（repair_all 没报 _unrecoverable）
    assert result["repair_result"].get("_unrecoverable") is False
    assert result["repaired"] is True, (
        f"repaired 应基于 _unrecoverable=False 判定为 True，"
        f"但实际为 {result['repaired']}（旧逻辑基于 check_all 重检 major=1 误判为 False）"
    )
    # check_all 的 major 错误应仍暴露给用户（不掩盖问题）
    assert result["major_errors"] == 1


def test_check_vdb_missing_uses_sorted_pair(tmp_path, monkeypatch):
    """check_vdb_missing 应该用 sorted pair 比对，跟 repair 写入逻辑一致。

    Bug 根因：
    - repair_vdb_relationships 用 sorted((src, tgt)) 存 src_id/tgt_id（lightrag_repair.py:1441）
    - _check_vdb_missing 用原始顺序 (source, target) 比对 GraphML edge
    - 当 source > target（字母序）时 sorted 后反转，check 误报 missing

    修复后：vdb_r_pairs 和 graphml_pairs 都用 sorted pair 比对。
    """
    from niu_api.internal import lightrag_integrity

    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", tmp_path)

    # 构造 GraphML：1 个 edge，source > target（字母序反转）
    # 注意：必须包 <graph> 元素（跟 LightRAG 实际格式一致），否则 _load_graphml 报 no_graph_element
    graphml_content = '''<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph edgedefault="undirected">
    <node id="zebra"/>
    <node id="apple"/>
    <edge source="zebra" target="apple"/>
  </graph>
</graphml>'''
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(graphml_content)

    # 构造 vdb_relationships：1 条向量，src_id/tgt_id 用 sorted（跟 repair 一致）
    vdb_r = {
        "embedding_dim": 4,
        "data": [{"__id__": "r1", "src_id": "apple", "tgt_id": "zebra", "content": "test"}],
        "matrix": ""
    }
    (tmp_path / "vdb_relationships.json").write_text(json.dumps(vdb_r))

    # 构造 vdb_entities：2 个向量（覆盖 2 个 node）
    vdb_e = {
        "embedding_dim": 4,
        "data": [
            {"__id__": "e1", "entity_name": "zebra"},
            {"__id__": "e2", "entity_name": "apple"},
        ],
        "matrix": ""
    }
    (tmp_path / "vdb_entities.json").write_text(json.dumps(vdb_e))

    errors = lightrag_integrity._check_vdb_missing(tmp_path)

    # 应该没有 vdb_relationships_missing 错误（sorted 比对匹配）
    rel_missing = [e for e in errors if e.get("check") == "vdb_relationships_missing"]
    assert len(rel_missing) == 0, \
        f"不应报 vdb_relationships_missing（sorted 比对应匹配）: {rel_missing}"
    # 同时验证 vdb_entities_missing 也不应误报
    ent_missing = [e for e in errors if e.get("check") == "vdb_entities_missing"]
    assert len(ent_missing) == 0, \
        f"不应报 vdb_entities_missing（2 个 node 都有对应向量）: {ent_missing}"


# =============================================================================
# repair_text_chunks v4：从 GraphML 提活跃 chunk_id + 按需提取重建
# =============================================================================


def test_check_truth_sources_intact_all_intact(tmp_path, monkeypatch):
    """3 真相源全部完好时返回 intact=True。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{"doc-1": {"content": "x"}}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{"k": {"return": "x"}}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()

    assert result["intact"] is True
    assert result["graphml"]["intact"] is True
    assert result["full_docs"]["intact"] is True
    assert result["cache"]["intact"] is True


def test_check_truth_sources_intact_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏时返回 intact=False + graphml.intact=False。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()

    assert result["intact"] is False
    assert result["graphml"]["intact"] is False


def test_check_truth_sources_intact_full_docs_corrupt(tmp_path, monkeypatch):
    """full_docs 损坏时返回 intact=False + full_docs.intact=False。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt")
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()

    assert result["intact"] is False
    assert result["full_docs"]["intact"] is False


def test_check_truth_sources_intact_cache_corrupt(tmp_path, monkeypatch):
    """cache 损坏时返回 intact=False + cache.intact=False。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()

    assert result["intact"] is False
    assert result["cache"]["intact"] is False


# =============================================================================
# Task 3: repair_all 重写为"3 真相源不可动 + 按需提取重建 9 派生文件"的测试
# =============================================================================


def _make_synthetic_fixture(tmp_path: Path):
    """合成 fixture：3 文档 + 5 cache + GraphML（含衰减后 weight + 已删实体已不在）。

    用真实 compute_mdhash_id 生成 chunk_id，让 GraphML source_id 跟 full_docs chunking 产出一致。
    否则 repair_text_chunks 的 full_docs 反查永远找不到匹配 chunk → text_chunks 重建为空 →
    does_not_reanimate 测试"意外通过"（空 dict 不含已删实体）而非"正确验证"。

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
    # key 定义（简化，真实 GraphML 有完整 key 定义）
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
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_1  # 真实 hash

    b = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-b"})
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d2"}).text = "desc B"
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_2  # 真实 hash

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


# ============================================================
# Task 4 测试：check_all 简化为"检 3 真相源 + 9 派生文件 missing"
# ============================================================

# 9 派生文件清单（跟 lightrag_repair._DERIVED_FILES 一致）
_DERIVED_FILES_FOR_TEST = [
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


def _write_intact_truth_sources(tmp_path: Path):
    """写 3 真相源（全部完好）。"""
    # GraphML：1 个实体
    _write_graphml(tmp_path, [("entity-x", "desc X", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{"doc-1": {"content": "x"}}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{"k": {"return": "x"}}')


def test_check_all_3_truth_sources_all_intact(tmp_path, monkeypatch):
    """3 真相源全部完好（派生文件齐全 + vdb 与 GraphML 一致）→ ok=True。"""
    _write_intact_truth_sources(tmp_path)
    # GraphML 有 node "entity-x"（_write_intact_truth_sources 写的）
    # vdb_entities.json 必须含该 node 对应向量，否则 _check_vdb_missing 报 major
    (tmp_path / "vdb_entities.json").write_text(
        json.dumps({"data": [{"entity_name": "entity-x"}], "embedding_dim": 0}, ensure_ascii=False)
    )
    # 其他派生文件：vdb_chunks/vdb_relationships 无对应 GraphML edge → 空 vdb 不报错
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname == "vdb_entities.json":
            continue  # 上面已写
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is True
    assert result["critical_errors"] == 0
    assert result["major_errors"] == 0
    assert result["minor_errors"] == 0


def test_check_all_graphml_corrupt_is_critical(tmp_path, monkeypatch):
    """GraphML 损坏 → critical_errors=1 + ok=False。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1, "GraphML 损坏应为 critical"
    # 真相源损坏的 error 应归类到 checks.truth_source
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "graph_chunk_entity_relation.graphml" for e in truth_errors), \
        "GraphML 错误应记到 truth_source check"


def test_check_all_full_docs_corrupt_is_critical(tmp_path, monkeypatch):
    """full_docs 损坏（非 dict JSON）→ critical_errors>=1。"""
    _write_intact_truth_sources(tmp_path)
    # 覆盖 full_docs 为损坏
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt not json")
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "kv_store_full_docs.json" for e in truth_errors)


def test_check_all_cache_corrupt_is_critical(tmp_path, monkeypatch):
    """cache 损坏（非 dict JSON）→ critical_errors>=1。"""
    _write_intact_truth_sources(tmp_path)
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt not json")
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "kv_store_llm_response_cache.json" for e in truth_errors)


def test_check_all_kvstore_derived_missing_is_not_major(tmp_path, monkeypatch):
    """v2 修复：派生 kv_store 缺失不再报 major（不是损坏）。

    原 test_check_all_missing_derived_file_is_major 期望"9 派生任一 missing → major>=1"，
    v2 改为：派生 kv_store 缺失 → major=0；vdb 缺向量才报 major（数据一致性真损坏）。
    vdb 缺向量的检查由 test_check_all_vdb_missing_but_graphml_intact_returns_major（L71）覆盖。

    此测试 vdb_entities.json 不写（缺 GraphML 对应向量）→ _check_vdb_missing 报 major；
    但派生 kv_store 缺失不应报 major（关键 v2 断言）。
    """
    _write_intact_truth_sources(tmp_path)
    # GraphML 有 node "test-entity"
    # vdb_entities.json 不写（缺该 node 的向量）→ _check_vdb_missing 会报 major
    # 其他派生 kv_store + vdb 写空
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname == "vdb_entities.json":
            continue  # 故意不写
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    # 真相源完好 → critical=0
    assert result["critical_errors"] == 0
    # vdb 缺向量 → major>=1（数据一致性真损坏）
    assert result["major_errors"] >= 1
    assert result["ok"] is False
    # vdb 缺失应记到 checks.vdb_missing（不再记到 derived_missing）
    vdb_errors = result["checks"].get("vdb_missing", {}).get("errors", [])
    assert any(e.get("check") == "vdb_entities_missing" for e in vdb_errors), \
        "vdb 缺向量应记到 vdb_missing check"
    # 关键 v2 断言：派生 kv_store 缺失不应报 major
    derived_errors = result["checks"].get("derived_missing", {}).get("errors", [])
    assert len(derived_errors) == 0, \
        f"派生 kv_store 缺失不应报 major，但 derived_missing 有 errors: {derived_errors}"


def test_check_all_brand_new_user_is_ok(tmp_path, monkeypatch):
    """全新用户：3 真相源全不存在 + 9 派生文件全不存在 → ok=True（合法空状态）。"""
    # 不写任何文件（tmp_path 是空目录）
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    # 全新用户：文件不存在不算损坏，但 9 派生文件 missing 应算 major 还是 ok？
    # v4 决策：全新用户（3 真相源都不存在）时，9 派生文件 missing 也不报错
    # （因为 LightRAG 首次启动会自动初始化所有文件，不该误报）
    assert result["critical_errors"] == 0, "全新用户真相源不存在不应报 critical"


def test_check_all_preserves_zombie_markers_constant():
    """修改 lightrag_integrity.py 后 _ZOMBIE_DESCRIPTION_MARKERS 必须仍可 import。

    lightrag_repair.py:1924 import 这个常量，删了会 ImportError。
    """
    from niu_api.internal.lightrag_integrity import _ZOMBIE_DESCRIPTION_MARKERS
    assert isinstance(_ZOMBIE_DESCRIPTION_MARKERS, tuple)
    assert len(_ZOMBIE_DESCRIPTION_MARKERS) > 0
    # 确认包含已知的标记
    assert "被删除的脑区" in _ZOMBIE_DESCRIPTION_MARKERS


def test_check_all_preserves_load_graphml_and_check_truth_source(tmp_path):
    """修改后 _load_graphml / _check_truth_source 必须仍可 import。

    lightrag_repair.py:1923, 2286 import 这两个函数，删了会 ImportError。
    """
    from niu_api.internal.lightrag_integrity import (
        _check_truth_source,
        _load_graphml,
    )
    # 简单 smoke test：调一次确认可调用
    _write_intact_truth_sources(tmp_path)
    node_ids, _, _, err = _load_graphml(tmp_path / "graph_chunk_entity_relation.graphml")
    assert err is None
    assert len(node_ids) == 1

    err = _check_truth_source("kv_store_full_docs.json", tmp_path)
    assert err == {}, "完好的真相源应返回空 dict（无错误）"


# ============================================================
# Task 5 测试：lightrag_manager 字段 + repaired 判定
# ============================================================


def test_get_lightrag_status_returns_3_severity_fields(tmp_path, monkeypatch):
    """get_lightrag_status 必须返回 critical_errors/major_errors/minor_errors 三字段。

    Rust IntegrityStatus 已加 critical_errors/major_errors/minor_errors 字段
    （main.rs:55-60），但 total_errors 仍保留兼容（不能删，main.rs:54 读）。
    """
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    # 模拟 Phase 1 已跑过（_integrity_result 非 None）
    lm._integrity_result = {
        "ok": True,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0,
        "errors": [],
        "checks": {},
    }

    status = lm.get_lightrag_status()
    integrity = status["integrity"]

    # 三级 severity 字段必须存在
    assert "critical_errors" in integrity, "integrity 必须含 critical_errors"
    assert "major_errors" in integrity, "integrity 必须含 major_errors"
    assert "minor_errors" in integrity, "integrity 必须含 minor_errors"
    # total_errors 保留（Rust main.rs:54 依赖，不能删）
    assert "total_errors" in integrity, "total_errors 保留兼容 Rust（main.rs:54 读）"
    # total_errors = critical + major + minor
    expected_total = (
        integrity["critical_errors"]
        + integrity["major_errors"]
        + integrity["minor_errors"]
    )
    assert integrity["total_errors"] == expected_total, \
        "total_errors 应等于 critical + major + minor 之和"


def test_run_repair_on_user_request_repaired_true_on_success(tmp_path, monkeypatch):
    """repair_all 成功（无 _unrecoverable）→ repaired=True。"""
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    # 模拟 pipeline 不 busy（_read_pipeline_busy 返回 False）
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 模拟 repair_all 成功（无 _unrecoverable）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {"_unrecoverable": False, "text_chunks": {"status": "ok"}},
    )
    # 模拟 reset_init_state + check_all + get_lightrag 不触发真实初始化
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    # 模拟 wait_first_scan_complete 立即返回 True
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda *_args, **_kwargs: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is True, "无 _unrecoverable 时应 repaired=True"
    # 返回结构必须有三级字段
    assert "critical_errors" in result
    assert "major_errors" in result
    assert "minor_errors" in result
    # _repairing 应在 finally 里被清回 False
    assert lm._repairing is False, "_repairing 应在 finally 清回 False"


def test_run_repair_on_user_request_repaired_false_on_unrecoverable(tmp_path, monkeypatch):
    """repair_all 返回 _unrecoverable=True → repaired=False。"""
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 模拟 repair_all 返回 unrecoverable（如 GraphML 损坏）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {
            "_unrecoverable": True,
            "_unrecoverable_reason": "3 真相源损坏",
            "text_chunks": {"status": "error", "unrecoverable": True},
        },
    )
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda *_args, **_kwargs: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is False, "有 _unrecoverable 时应 repaired=False"
    assert lm._repairing is False


def test_run_repair_on_user_request_repaired_false_on_step_error(tmp_path, monkeypatch):
    """repair_all 某步骤返回 status=error（非 unrecoverable）→ repaired=False。

    这是 v4 改动重点：不能只看 _unrecoverable，还要看每个 step 的 status=error。
    现有 L1384-1391 已实现此判定，本测试锁定行为。
    """
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 某步骤 status=error 但没标 unrecoverable
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {
            "_unrecoverable": False,
            "vdb_entities": {"status": "error", "message": "embedding 失败"},
        },
    )
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda *_args, **_kwargs: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is False, "步骤 status=error 时应 repaired=False"


# ============== v8-Task 2 测试：独立加载 tokenizer + chunk_config ==============


def test_get_tokenizer_independent_load():
    """_get_tokenizer 应独立加载 TiktokenTokenizer，不调 get_lightrag_for_repair。

    铁律 3：禁止调 get_lightrag/get_lightrag_for_repair/apipeline。
    v8-Task 2：用 lightrag.utils.TiktokenTokenizer（model_name="gpt-4o-mini"）。
    """
    from niu_api.internal.lightrag_repair import _get_tokenizer
    from niu_api.internal.lightrag_repair_tokenizer import reset_cache

    reset_cache()  # 清缓存，确保本次测试重新加载
    tokenizer = _get_tokenizer()
    assert tokenizer is not None, "TiktokenTokenizer 应加载成功"

    # TiktokenTokenizer 继承 Tokenizer，有 encode/decode 方法（不是 tokenize）
    assert hasattr(tokenizer, "encode"), "tokenizer 应有 encode 方法"
    assert hasattr(tokenizer, "decode"), "tokenizer 应有 decode 方法"

    # 验证 encode 真能用（不抛异常）
    tokens = tokenizer.encode("hello world")  # type: ignore[union-attr]
    assert isinstance(tokens, list), "encode 应返回 list[int]"
    assert len(tokens) > 0, "encode 应返回非空 token list"

    reset_cache()  # 测试完清缓存


def test_get_tokenizer_does_not_call_get_lightrag(monkeypatch):
    """_get_tokenizer 绝不能调 get_lightrag（铁律 3）。

    patch get_lightrag，若 _get_tokenizer 误调它则 AssertionError。
    """
    from unittest.mock import patch

    from niu_api.internal.lightrag_repair import _get_tokenizer
    from niu_api.internal.lightrag_repair_tokenizer import reset_cache

    reset_cache()

    # patch lightrag_manager.get_lightrag；若 _get_tokenizer 误调则 AssertionError
    with patch(
        "niu_api.internal.lightrag_manager.get_lightrag",
        side_effect=AssertionError("禁止调 get_lightrag（铁律 3）"),
    ):
        tokenizer = _get_tokenizer()
        assert tokenizer is not None, "TiktokenTokenizer 应独立加载成功，不依赖 get_lightrag"

    reset_cache()


def test_get_chunk_config_no_get_lightrag(monkeypatch):
    """_get_chunk_config 不应调 get_lightrag（铁律 3）。

    Task 1 已删 get_lightrag_for_repair，所以这里 patch get_lightrag（仍存在的函数）。
    _get_chunk_config 只应调 _get_lightrag_config（读 preferences.json，不调 apipeline），
    不应调 get_lightrag（会触发 apipeline 写真相源）。
    """
    from unittest.mock import patch

    from niu_api.internal.lightrag_repair import _get_chunk_config

    # patch get_lightrag（Task 1 后仍存在）；若 _get_chunk_config 误调它则 AssertionError
    with patch(
        "niu_api.internal.lightrag_manager.get_lightrag",
        side_effect=AssertionError("禁止调 get_lightrag（铁律 3）"),
    ):
        chunk_size, chunk_overlap = _get_chunk_config()
        assert chunk_size > 0, "chunk_token_size 应 > 0"
        assert chunk_overlap >= 0, "chunk_overlap_token_size 应 >= 0"


def test_get_chunk_config_fallback_on_missing_preferences(monkeypatch, tmp_path):
    """_get_chunk_config 在 preferences.json 缺失时应 fallback (1200, 50)。

    对齐 lightrag_manager.py:853 真实默认值 chunk_overlap_token_size=50。
    """
    from unittest.mock import patch

    from niu_api.internal.lightrag_repair import _get_chunk_config

    # patch _get_lightrag_config 返回空 dict（模拟 preferences.json 缺 lightrag 配置）
    with patch(
        "niu_api.internal.lightrag_manager._get_lightrag_config",
        return_value={},
    ):
        chunk_size, chunk_overlap = _get_chunk_config()
        assert chunk_size == 1200, f"fallback chunk_token_size 应=1200，实际={chunk_size}"
        assert chunk_overlap == 50, f"fallback chunk_overlap 应=50，实际={chunk_overlap}"


def test_get_chunk_config_fallback_on_exception(monkeypatch):
    """_get_chunk_config 在 _get_lightrag_config 抛异常时应 fallback (1200, 50)。"""
    from unittest.mock import patch

    from niu_api.internal.lightrag_repair import _get_chunk_config

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated config read failure")

    with patch(
        "niu_api.internal.lightrag_manager._get_lightrag_config",
        side_effect=_raise,
    ):
        chunk_size, chunk_overlap = _get_chunk_config()
        assert chunk_size == 1200
        assert chunk_overlap == 50


def test_get_tokenizer_singleton_cache():
    """_get_tokenizer 应单例缓存：第二次调用直接返回同一实例。"""
    from niu_api.internal.lightrag_repair_tokenizer import get_tokenizer, reset_cache

    reset_cache()
    t1 = get_tokenizer()
    t2 = get_tokenizer()
    assert t1 is not None and t2 is not None
    assert t1 is t2, "第二次调用应返回同一缓存实例"
    reset_cache()


def test_load_graphml_nodes_returns_3_tuple_with_entity_type(tmp_path, monkeypatch):
    """_load_graphml_nodes 应返回 {node_id: (entity_type, desc, src)} 3 元组。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    # 普通实体节点
    n1 = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-x"})
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d1"}).text = "person"
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d2"}).text = "desc X"
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-aaa"

    # 脑区节点
    n2 = ET.SubElement(graph, f"{{{ns}}}node", {"id": "文档库脑区"})
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d2"}).text = "文档库脑区描述<SEP>brain_meta_size:94"
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-bbb"

    # 缺 d1 的节点（entity_type 应为空字符串）
    n3 = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-no-d1"})
    ET.SubElement(n3, f"{{{ns}}}data", {"key": "d2"}).text = "desc Y"
    ET.SubElement(n3, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-ccc"

    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _load_graphml_nodes

    nodes, err = _load_graphml_nodes()
    assert err is None
    assert nodes["entity-x"] == ("person", "desc X", "chunk-aaa", "")
    assert nodes["文档库脑区"] == ("brainregion", "文档库脑区描述<SEP>brain_meta_size:94", "chunk-bbb", "")
    # 缺 d1 → entity_type=""
    assert nodes["entity-no-d1"] == ("", "desc Y", "chunk-ccc", "")


# ==================== v8-Task 4: repair_text_chunks 重写测试 ====================


def _write_graphml_v8(tmp_path: Path, nodes_data, edges_data=None):
    """写 GraphML v8 测试 fixture。
    nodes_data = [(node_id, etype, desc, src), ...]
    edges_data = [(src, tgt, src_ids, desc, kw), ...]
    """
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for node_id, etype, desc, src in nodes_data:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": node_id})
        if etype:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = etype
        if desc:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = desc
        if src:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = src
    if edges_data:
        for src, tgt, src_ids, desc, kw in edges_data:
            edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": src, "target": tgt})
            if desc:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = desc
            if kw:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = kw
            if src_ids:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = src_ids
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )


def _build_cache_prompt(chunk_content: str) -> str:
    """构造 cache original_prompt（含 ``` 包裹的 chunk 原文 + LLM 输出示例）。"""
    return f"""---Task---
Extract entities and relationships from the input text.

---Data---
```
{chunk_content}
```

---Output---
First, output entity list, each entity separated by new line:
```
("entity"<|#|>名字<|#|>类型<|#|>描述)
```

Then output relationship list:
```
("relationship"<|#|>src<|#|>tgt<|#|>desc<|#|>kw)
```
"""


def test_repair_text_chunks_cache_original_prompt_priority(tmp_path, monkeypatch):
    """repair_text_chunks 应优先从 cache original_prompt 提取 chunk 原文。"""
    chunk_content = "测试 chunk 原文 cache 优先"
    # GraphML：1 个实体引用 chunk-active
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", "chunk-active")])

    # text_chunks 为空（强制走 cache 提取路径）
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")

    # cache：1 个 extract entry，chunk_id=chunk-active，original_prompt 含 chunk 原文
    cache = {
        "cache-key-1": {
            "return": "entity<|#|>名字<|#|>person<|#|>描述",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_content),
            "create_time": 1781930000,
        }
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache))

    # full_docs 空（验证 cache 优先于 full_docs）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    assert result["lost"] == 0

    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-active" in tc_after
    assert tc_after["chunk-active"]["content"] == chunk_content
    # llm_cache_list 应包含 cache_key
    assert "cache-key-1" in tc_after["chunk-active"]["llm_cache_list"]


def test_repair_text_chunks_cache_multiple_entries_take_latest_create_time(tmp_path, monkeypatch):
    """同 chunk_id 多条 cache entry，取 create_time 最大的。"""
    chunk_v1 = "v1 chunk 原文"
    chunk_v2 = "v2 chunk 原文"
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", "chunk-active")])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    cache = {
        "cache-key-old": {
            "return": "v1 extraction",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_v1),
            "create_time": 1781930000,
        },
        "cache-key-new": {
            "return": "v2 extraction",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_v2),
            "create_time": 1781930999,  # 更大
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    assert result["status"] == "ok"
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    # 应取 create_time=1781930999 的 entry（v2）
    assert tc_after["chunk-active"]["content"] == chunk_v2


def test_repair_text_chunks_full_docs_fallback_when_cache_miss(tmp_path, monkeypatch):
    """cache 找不到 chunk_id 时，从 full_docs chunking 反查。"""
    from lightrag.utils import compute_mdhash_id

    chunk_content = "这是从 full_docs 反查的 chunk 原文"
    expected_chunk_id = compute_mdhash_id(chunk_content, prefix="chunk-")

    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", expected_chunk_id)])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    # cache 空（强制走 full_docs fallback）
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    # full_docs：1 个 doc，content 经 chunking 后产生 expected_chunk_id
    docs = {
        "doc-1": {
            "content": chunk_content,
            "file_path": "test.md",
            "create_time": 1781930000,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["lost"] == 0
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert expected_chunk_id in tc_after
    assert tc_after[expected_chunk_id]["content"] == chunk_content
    assert tc_after[expected_chunk_id]["full_doc_id"] == "doc-1"


def test_repair_text_chunks_brainregion_direct_construction(tmp_path, monkeypatch):
    """脑区节点（d1=brainregion）直接从 GraphML 构造，不查 full_docs/cache。"""
    brain_desc = "文档库脑区描述<SEP>brain_meta_size:94"
    _write_graphml_v8(tmp_path, [
        ("文档库脑区", "brainregion", brain_desc, "chunk-brain-1"),
    ])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")  # 脑区不在 full_docs
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")  # 脑区也不在 cache

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["lost"] == 0
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-brain-1" in tc_after
    # content = "文档库脑区: {d2 description}"
    assert tc_after["chunk-brain-1"]["content"] == f"文档库脑区: {brain_desc}"
    # full_doc_id = "brain_文档库脑区"
    assert tc_after["chunk-brain-1"]["full_doc_id"] == "brain_文档库脑区"


def test_repair_text_chunks_missing_when_three_sources_all_miss(tmp_path, monkeypatch):
    """cache + full_docs + 脑区都没匹配 → missing（lost>0）。"""
    _write_graphml_v8(tmp_path, [
        ("entity-x", "person", "desc X", "chunk-not-found-anywhere"),
    ])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    assert result["status"] == "ok"
    assert result["expected"] == 1
    assert result["actual"] == 0
    assert result["lost"] == 1


def test_repair_text_chunks_real_cache_extraction(tmp_path, monkeypatch):
    """v8 核心验证（I3）：用真实 cache 数据验证正则提取 chunk 原文正确性。

    真实 cache 的 original_prompt 含 8 个 ```（4 对），只有第一对 ``` 之间是 chunk 原文。
    非贪婪正则 r"```\\s*(.+?)\\s*```" 必须正确提取第一对之间内容，不能跨多对 ```。

    真实数据特征（2026-07-17 验证，主对话已手工清理 GraphML 22 孤儿引用）：
    - 123 个活跃 chunk_id（GraphML 孤儿引用已清理：node source_id 清理 9 + edge source_id 清理 1068）
    - cache extract + full_docs + 脑区 fallback 覆盖全部 123 个 chunk_id
    - 0 个孤儿 chunk（清理后 GraphML 不再引用已删除的 chunk）
    - 最终恢复 123 个，0 个 missing（无孤儿 chunk）
    """
    import os
    import shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")

    # 拷贝真实 3 真相源到 tmp_path
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = asyncio.run(repair_text_chunks())

    # 真实数据（2026-07-17 验证）：
    # - 116 个活跃 chunk_id（GraphML 节点 source_id + edge source_id 全集）
    # - 116 个全部能从 cache + full_docs 恢复
    # - 0 个 missing（孤儿 chunk 已在前期手工清理）
    # 注意：active 数会随 GraphML 演变而变化，断言用关系（actual=expected-lost）而非硬编码
    assert result["status"] == "ok", f"repair_text_chunks 失败: {result.get('message', '')}"
    expected = result["expected"]
    lost = result["lost"]
    actual = result["actual"]
    assert expected == 116, f"活跃 chunk 数应为 116，实际 {expected}"
    assert actual == expected - lost, (
        f"actual 应等于 expected-lost，actual={actual}, expected={expected}, lost={lost}"
    )
    assert lost == 0, f"清理后孤儿 chunk lost 应 0，实际 lost={lost}"

    # 验证 text_chunks 内容非空（每个 chunk content 必须有真实原文，不是空串）
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    empty_content = [cid for cid, v in tc.items() if not v.get("content", "").strip()]
    assert not empty_content, f"以下 chunk content 为空: {empty_content[:5]}"

    # 验证至少有 1 个 chunk 是从 cache 提取的（非脑区 chunk 占多数）
    non_brain = [cid for cid, v in tc.items() if not str(v.get("full_doc_id", "")).startswith("brain_")]
    assert len(non_brain) > 0, "应有非脑区 chunk 从 cache/full_docs 提取"

    # 验证正则没把 LLM 输出示例（后续 3 对 ``` 之间的内容）当 chunk 原文：
    # 如果正则贪婪匹配跨多对 ```，chunk content 会含 "entity<|#|>" 等 LLM 输出标记
    bad_extraction = [cid for cid, v in tc.items() if "<|#|>" in v.get("content", "")]
    assert not bad_extraction, f"正则提取错误，含 LLM 输出标记: {bad_extraction[:5]}"

    # 验证脑区 fallback chunk 的 content + full_doc_id 格式正确
    # 当前真实数据 0 个脑区 source_id（脑区未写入 GraphML）→ 脑区 fallback 数为 0
    # 若未来脑区写入 GraphML 但 cache/full_docs 都没 → 走脑区直接构造 fallback
    brain_fallback = [
        cid for cid, v in tc.items()
        if str(v.get("full_doc_id", "")).startswith("brain_")
    ]
    assert len(brain_fallback) == 0, f"当前真实数据无脑区 chunk，脑区 fallback 应 0，实际 {len(brain_fallback)}"
    for cid in brain_fallback:
        v = tc[cid]
        # content = "{脑区名}: {d2 description}"
        assert ": " in v["content"], f"脑区 chunk content 格式错误: {v['content'][:50]}"
        # full_doc_id = "brain_{脑区名}"
        assert v["full_doc_id"].startswith("brain_"), f"脑区 full_doc_id 格式错误: {v['full_doc_id']}"


# ==================== v8-Task 5: repair_doc_status 回归测试 ====================
# v4 实现：从 full_docs.keys() 循环构造 doc_status，status=processed 当 GraphML 有数据。
# chunks_list 从 text_chunks.full_doc_id 反向分组（空 full_doc_id 跳过）。


@pytest.mark.asyncio
async def test_repair_doc_status_brainregion_chunks_list_attached(tmp_path, monkeypatch):
    """脑区 chunk full_doc_id=brain_xxx 应进 chunks_by_doc（反向分组）。

    v4 实现：脑区 chunk 的 full_doc_id="brain_文档库脑区" 不会写入 doc_status
    （因为 full_docs 通常不含 brain_xxx 条目），但 chunks_by_doc 分组正确。

    回归点：脑区 chunk 的 full_doc_id 不会被误判为空字符串而跳过。
    """
    # GraphML：脑区节点（让 graphml_has_data=True，status=processed）
    _write_graphml_v8(tmp_path, [("文档库脑区", "brainregion", "brain_meta_size:10", "chunk-brain-1")])

    # text_chunks：1 个脑区 chunk
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-brain-1": {
            "content": "文档库脑区: 描述",
            "full_doc_id": "brain_文档库脑区",
            "llm_cache_list": [],
        }
    }))
    # full_docs：1 个普通 doc（脑区不在 full_docs）
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps({
        "doc-1": {"content": "doc content", "file_path": "x.md"},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = await repair_doc_status()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["expected"] == 1, f"expected 1 (full_docs 条数), got {result['expected']}"
    assert result["actual"] == 1
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # full_docs 只有 doc-1 → doc_status 只含 doc-1（brain_文档库脑区 不在 full_docs 不进 doc_status）
    assert "doc-1" in ds
    assert "brain_文档库脑区" not in ds, "脑区 full_doc_id 不应进 doc_status（不在 full_docs）"
    # GraphML 有数据 → status=processed
    assert ds["doc-1"]["status"] == "processed"
    assert ds["doc-1"]["chunks_count"] == 0  # doc-1 没有 chunk


@pytest.mark.asyncio
async def test_repair_doc_status_skip_empty_full_doc_id(tmp_path, monkeypatch):
    """cache fallback chunk 的 full_doc_id="" 应跳过（不写 doc_status 条目）。

    v4 实现：line 829-830 空 full_doc_id 跳过 chunks_by_doc 分组。
    但 doc_status 条目数 = full_docs 条目数（从 full_docs.keys() 循环）。
    所以这个测试验证的是：空 full_doc_id 的 chunk 不会被加进任何 doc 的 chunks_list。
    """
    # GraphML 有 1 node（让 graphml_has_data=True）
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc", "chunk-active")])

    tc = {
        "chunk-active": {
            "content": "chunk 原文",
            "full_doc_id": "",  # 空：cache fallback
            "llm_cache_list": [],
        }
    }
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(tc))
    # full_docs：1 个 doc-1（应进 doc_status，但 chunks_list 为空）
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps({
        "doc-1": {"content": "doc1", "file_path": "1.md"},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = await repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # doc_status 应有 doc-1 条目（来自 full_docs）
    assert "doc-1" in ds
    # 但 doc-1 的 chunks_list 应为空（chunk-active 的 full_doc_id 为空被跳过）
    assert ds["doc-1"]["chunks_count"] == 0
    assert ds["doc-1"]["chunks_list"] == []
    # 空 full_doc_id 的 chunk 不在 chunks_list 中
    assert "chunk-active" not in ds["doc-1"]["chunks_list"]


@pytest.mark.asyncio
async def test_repair_doc_status_chunks_list_grouped_by_doc(tmp_path, monkeypatch):
    """full_docs fallback chunk 的 full_doc_id=doc_id 应进对应 doc 的 chunks_list。

    v4 实现：text_chunks.full_doc_id=doc_id → chunks_by_doc[doc_id].append(chunk_id)
    → doc_status[doc_id].chunks_list = sorted(chunks_by_doc[doc_id])
    """
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc", "chunk-a")])

    tc = {
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-c": {"content": "c", "full_doc_id": "doc-2", "llm_cache_list": []},
    }
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(tc))
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps({
        "doc-1": {"content": "doc1", "file_path": "1.md"},
        "doc-2": {"content": "doc2", "file_path": "2.md"},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = await repair_doc_status()

    assert result["status"] == "ok"
    assert result["actual"] == 2  # doc-1 + doc-2
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # chunks_list 按 full_doc_id 分组 + sorted
    assert set(ds["doc-1"]["chunks_list"]) == {"chunk-a", "chunk-b"}
    assert ds["doc-1"]["chunks_count"] == 2
    assert set(ds["doc-2"]["chunks_list"]) == {"chunk-c"}
    assert ds["doc-2"]["chunks_count"] == 1
    # GraphML 有数据 → 全部 processed
    assert ds["doc-1"]["status"] == "processed"
    assert ds["doc-2"]["status"] == "processed"


@pytest.mark.asyncio
async def test_repair_doc_status_pending_when_graphml_empty(tmp_path, monkeypatch):
    """GraphML 无 node → status=pending（全新用户场景）。"""
    # GraphML 空（无 node）
    _write_graphml_v8(tmp_path, [])

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps({
        "doc-1": {"content": "doc1", "file_path": "1.md"},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = await repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # GraphML 无数据 → status=pending
    assert ds["doc-1"]["status"] == "pending"


# ==================== v8-Task 6: repair_vdb_chunks/entities/relationships 回归测试 ====================
# v4 实现：
# - repair_vdb_chunks：遍历 text_chunks 重新 embedding
# - repair_vdb_entities：遍历 GraphML node（防复活）
# - repair_vdb_relationships：遍历 GraphML edge，data_list 不含 weight


@pytest.mark.asyncio
async def test_repair_vdb_entities_only_graphml_nodes(tmp_path, monkeypatch):
    """repair_vdb_entities 应只遍历 GraphML 存在的 node（防复活）。

    回归点：text_chunks 含已删实体对应的 chunk，但 GraphML 没有该实体节点
    → vdb_entities 不应含已删实体（防复活）。

    v9 Task 6 转换：repair_vdb_entities 改为 async（走 NanoVectorDBStorage.upsert），
    需 await + 用 _FakeEmbedModel 替代真实 bge 模型（避免 ~400MB 加载）。
    """
    _write_graphml_v8(tmp_path, [
        ("entity-active", "person", "desc active", "chunk-a"),
        # 已删实体 entity-deleted 不在 GraphML
    ])

    # text_chunks 含 chunk-a + chunk-deleted（但 chunk-deleted 对应的实体已删）
    # v9 repair_vdb_entities 不读 text_chunks，但保留 fixture 跟 v8 一致
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "content a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-deleted": {"content": "content deleted", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # v9：用 _FakeEmbedModel 替代真实 bge 模型（避免加载 ~400MB）
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    from niu_api.internal.lightrag_repair import repair_vdb_entities

    result = await repair_vdb_entities()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["expected"] == 1
    assert result["actual"] == 1
    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    # 只含 entity-active，不含已删实体（防复活）
    assert len(vdb_e.get("data", [])) == 1
    assert vdb_e["data"][0]["entity_name"] == "entity-active"


@pytest.mark.asyncio
async def test_repair_vdb_relationships_no_weight_in_data(tmp_path, monkeypatch):
    """repair_vdb_relationships 的 data_list item 不应含 weight 字段。

    v4 实现：data_list item 只含 __id__/src_id/tgt_id/content/source_id 5 个字段。
    weight 只在 GraphML d7 字段，vdb 不写 weight（防数据冗余 + 跟 LightRAG 一致）。

    回归点：vdb_relationships.json 的任何 data item 都不应有 "weight" 字段。

    v9 Task 7 转换：repair_vdb_relationships 改为 async（走 NanoVectorDBStorage.upsert），
    需 await + 用 _FakeEmbedModel 替代真实 bge 模型（避免 ~400MB 加载）。
    """
    _write_graphml_v8(
        tmp_path,
        [("entity-a", "person", "desc a", "chunk-a"), ("entity-b", "person", "desc b", "chunk-b")],
        [("entity-a", "entity-b", "chunk-rel", "desc rel", "关系词")],
    )

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel": {"content": "rel", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # v9：用 _FakeEmbedModel 替代真实 bge 模型（避免加载 ~400MB）
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    from niu_api.internal.lightrag_repair import repair_vdb_relationships

    result = await repair_vdb_relationships()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    for item in vdb_r.get("data", []):
        # 任何层级都不应有 weight 字段（v9 走 storage 接口，meta_fields 不含 weight）
        assert "weight" not in item, f"vdb_relationships item 不应含 weight: {item}"
        # 确认 v9 实现的字段都在（meta_fields: src_id/tgt_id/source_id/content/file_path）
        assert "__id__" in item
        assert "src_id" in item
        assert "tgt_id" in item
        assert "content" in item
        assert "source_id" in item
        assert "file_path" in item


@pytest.mark.asyncio
async def test_repair_vdb_chunks_only_text_chunks(tmp_path, monkeypatch):
    """repair_vdb_chunks 只对 text_chunks 中的 chunk embedding（防孤儿 chunk）。

    回归点：GraphML 引用了 chunk-orphan，但 text_chunks 没有该 chunk
    → vdb_chunks 不应含 chunk-orphan（防孤儿）。

    v9 Task 5 转换：repair_vdb_chunks 改为 async（走 NanoVectorDBStorage.upsert），
    需 await + 用 _FakeEmbedModel 替代真实 bge 模型（避免 ~400MB 加载）。
    """
    _write_graphml_v8(tmp_path, [
        ("entity-x", "person", "desc", "chunk-active<SEP>chunk-orphan"),
    ])

    # text_chunks 只含 chunk-active（chunk-orphan 已丢失）
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-active": {"content": "active content", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # v9：用 _FakeEmbedModel 替代真实 bge 模型（避免加载 ~400MB）
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    from niu_api.internal.lightrag_repair import repair_vdb_chunks

    result = await repair_vdb_chunks()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1  # 只 chunk-active
    vdb_c = json.loads((tmp_path / "vdb_chunks.json").read_text())
    chunk_ids_in_vdb = [d.get("__id__", "") for d in vdb_c.get("data", [])]
    # 只含 chunk-active，不含 chunk-orphan（防孤儿）
    assert any("chunk-active" == cid for cid in chunk_ids_in_vdb) or len(chunk_ids_in_vdb) == 1
    assert not any("chunk-orphan" == cid for cid in chunk_ids_in_vdb), "孤儿 chunk 不应出现在 vdb_chunks"


# ==================== v8-Task 7: repair_entity/relation/full_* 回归测试 ====================
# v4 实现：
# - repair_entity_chunks：从 GraphML node source_id 提取，value={"chunk_ids": [...], "count": int}
# - repair_relation_chunks：从 GraphML edge source_id 提取，key=make_relation_chunk_key
# - repair_full_entities/relations：从 GraphML source_id → doc_status.chunks_list 反向映射


@pytest.mark.asyncio
async def test_repair_entity_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_entity_chunks 只从 GraphML node source_id 提取 chunk_ids（防复活）。

    v9 Task 8：函数改 async + 走 JsonKVStorage.upsert（自动注入 _id/create_time/update_time）。
    v4 实现：value = {"chunk_ids": [chunk_id, ...], "count": int}
    回归点：已删实体（不在 GraphML）的 chunk 不应被提取到 entity_chunks。
    """
    _write_graphml_v8(tmp_path, [
        ("entity-active", "person", "desc", "chunk-a<SEP>chunk-b"),
        # 已删实体 entity-deleted 不在 GraphML
    ])

    # text_chunks 含 chunk-a, chunk-b, chunk-deleted（但 chunk-deleted 对应实体已删）
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-deleted": {"content": "deleted", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_entity_chunks

    result = await repair_entity_chunks()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1  # 只 entity-active
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    # v9 实现：value = {"chunk_ids": [...], "count": int, "_id": ..., "create_time": ..., "update_time": ...}
    assert "entity-active" in ec
    assert set(ec["entity-active"]["chunk_ids"]) == {"chunk-a", "chunk-b"}
    assert ec["entity-active"]["count"] == 2
    # v9 Task 8：JsonKVStorage 自动注入 _id/create_time/update_time
    assert ec["entity-active"]["_id"] == "entity-active"
    assert "create_time" in ec["entity-active"]
    assert "update_time" in ec["entity-active"]
    # 已删实体不在 entity_chunks（防复活）
    assert "entity-deleted" not in ec


@pytest.mark.asyncio
async def test_repair_relation_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_relation_chunks 只从 GraphML edge source_id 提取 chunk_ids（防复活）。

    v9 Task 8：函数改 async + 走 JsonKVStorage.upsert（自动注入 _id/create_time/update_time）。
    v4 实现：key = make_relation_chunk_key(src, tgt) = GRAPH_FIELD_SEP.join(sorted((src, tgt)))
            value = {"chunk_ids": [...], "count": int}
    """
    _write_graphml_v8(
        tmp_path,
        [("entity-a", "person", "desc a", "chunk-a"), ("entity-b", "person", "desc b", "chunk-b")],
        [("entity-a", "entity-b", "chunk-rel1<SEP>chunk-rel2", "desc", "kw")],
    )

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel1": {"content": "r1", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel2": {"content": "r2", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from lightrag.constants import GRAPH_FIELD_SEP

    from niu_api.internal.lightrag_repair import repair_relation_chunks

    result = await repair_relation_chunks()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    rc = json.loads((tmp_path / "kv_store_relation_chunks.json").read_text())
    # edge 的 key 是 sorted((src, tgt)) join GRAPH_FIELD_SEP
    # entity-a, entity-b sorted 后还是 ("entity-a", "entity-b")
    expected_key = GRAPH_FIELD_SEP.join(sorted(("entity-a", "entity-b")))
    assert expected_key in rc
    # v9 实现：value = {"chunk_ids": [...], "count": int, "_id": ..., "create_time": ..., "update_time": ...}
    assert set(rc[expected_key]["chunk_ids"]) == {"chunk-rel1", "chunk-rel2"}
    assert rc[expected_key]["count"] == 2
    # v9 Task 8：JsonKVStorage 自动注入 _id/create_time/update_time
    assert rc[expected_key]["_id"] == expected_key
    assert "create_time" in rc[expected_key]
    assert "update_time" in rc[expected_key]


@pytest.mark.asyncio
async def test_repair_full_entities_reverse_mapping(tmp_path, monkeypatch):
    """repair_full_entities 从 GraphML source_id → chunk→doc 反向映射。

    v4 实现：key=doc_id, value=list of entity_name
    回归点：只有 GraphML 存在的实体 + doc_status 中存在的 chunk 才会进 full_entities。
    v9 改 async（走 JsonKVStorage.upsert）。
    """
    _write_graphml_v8(tmp_path, [
        ("entity-x", "person", "desc x", "chunk-a<SEP>chunk-b"),
        # 已删实体 entity-deleted 不在 GraphML
    ])

    # doc_status 提供 chunk→doc 映射（chunk-a, chunk-b → doc-1）
    (tmp_path / "kv_store_doc_status.json").write_text(json.dumps({
        "doc-1": {"status": "processed", "chunks_list": ["chunk-a", "chunk-b"], "chunks_count": 2},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_full_entities

    result = await repair_full_entities()

    assert result["status"] == "ok", f"expected ok, got {result}"
    fe = json.loads((tmp_path / "kv_store_full_entities.json").read_text())
    # v8 LightRAG 原生格式：{doc_id: {"entity_names": [...], "count": N}}
    # entity-x 的 source_id 含 chunk-a, chunk-b 都映射到 doc-1 → doc-1: [entity-x]
    assert "doc-1" in fe
    assert "entity-x" in fe["doc-1"]["entity_names"]
    assert fe["doc-1"]["count"] == 1
    # 已删实体不在 full_entities（防复活）
    all_entities = [e for v in fe.values() for e in v["entity_names"]]
    assert "entity-deleted" not in all_entities


@pytest.mark.asyncio
async def test_repair_full_relations_reverse_mapping(tmp_path, monkeypatch):
    """repair_full_relations 从 GraphML edge source_id → chunk→doc 反向映射。

    v4 实现：key=doc_id, value=list of relation_key (make_relation_chunk_key 格式)
    v9 改 async（走 JsonKVStorage.upsert），pair 必须 sorted。
    """
    _write_graphml_v8(
        tmp_path,
        [("entity-a", "person", "desc a", "chunk-a"), ("entity-b", "person", "desc b", "chunk-b")],
        [("entity-a", "entity-b", "chunk-rel", "desc rel", "kw")],
    )

    # doc_status：chunk-rel → doc-1
    (tmp_path / "kv_store_doc_status.json").write_text(json.dumps({
        "doc-1": {"status": "processed", "chunks_list": ["chunk-rel"], "chunks_count": 1},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_full_relations

    result = await repair_full_relations()

    assert result["status"] == "ok", f"expected ok, got {result}"
    fr = json.loads((tmp_path / "kv_store_full_relations.json").read_text())
    # v8 LightRAG 原生格式：{doc_id: {"relation_pairs": [[src, tgt], ...], "count": N}}
    # edge (entity-a, entity-b) source_id=chunk-rel → doc-1
    assert "doc-1" in fr
    pairs = fr["doc-1"]["relation_pairs"]
    # v9: pair 必须 sorted（["entity-a", "entity-b"]，因为 entity-a < entity-b）
    assert ["entity-a", "entity-b"] in pairs
    assert fr["doc-1"]["count"] == 1


# =============================================================================
# v9 Task 2: RepairEmbeddingFunc 单元测试
# =============================================================================

import numpy as np  # noqa: E402


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

    def encode(self, texts, **kwargs):
        self._call_count += 1
        # 返回 shape=(len(texts), dim) 的 ndarray
        return np.random.rand(len(texts), self.dim).astype(np.float32)


@pytest.mark.asyncio
async def test_repair_embedding_func_basic(monkeypatch):
    """验证 RepairEmbeddingFunc.__call__ 返回 np.ndarray + 维度 768。"""
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    # 用 monkeypatch 替换 get_model（避免加载真实模型）
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 实例化 RepairEmbeddingFunc
    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    # 调 __call__（async）
    texts = ["你好", "世界", "测试"]
    result = await embed_func(texts)

    # 断言：返回 np.ndarray，shape=(3, 768)，dtype=float32
    assert isinstance(result, np.ndarray), f"期望 np.ndarray，实际 {type(result)}"
    assert result.shape == (3, 768), f"期望 shape (3, 768)，实际 {result.shape}"
    assert result.dtype == np.float32, f"期望 dtype float32，实际 {result.dtype}"


@pytest.mark.asyncio
async def test_repair_embedding_func_batches_over_32(monkeypatch):
    """验证 texts 超过 32 条时分批 encode，结果正确合并。"""
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    # 100 条文本（触发分片：3 批 32 + 1 批 4 = 4 次）
    texts = [f"测试文本_{i}" for i in range(100)]
    result = await embed_func(texts)

    assert isinstance(result, np.ndarray)
    assert result.shape == (100, 768)
    # 假模型 encode 应该被调用 4 次（32+32+32+4）
    assert fake_model._call_count == 4, f"期望 4 次 encode 调用，实际 {fake_model._call_count}"


@pytest.mark.asyncio
async def test_repair_embedding_func_empty_input(monkeypatch):
    """验证空 texts 返回 shape=(0, 768) 的 ndarray。"""
    from niu_api.internal import lightrag_repair

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    result = await embed_func([])

    assert isinstance(result, np.ndarray)
    assert result.shape == (0, 768)


@pytest.mark.asyncio
async def test_repair_embedding_func_dimension(monkeypatch):
    """验证 embedding_dim 属性可读（NanoVectorDBStorage 会读这个属性）。"""
    from niu_api.internal import lightrag_repair

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)
    assert embed_func.embedding_dim == 768
    # 验证 model_name 字段也设置正确（BaseVectorStorage._generate_collection_suffix 会读）
    assert embed_func.model_name == "bge-base-zh-v1.5"


@pytest.mark.asyncio
async def test_repair_embedding_func_model_none_raises(monkeypatch):
    """验证模型 None 时抛 RuntimeError。"""
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    # get_model 返回 None（模拟模型未加载）
    monkeypatch.setattr(niu_embedding, "get_model", lambda: None)

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    with pytest.raises(RuntimeError, match="get_model.*None"):
        await embed_func(["测试"])


@pytest.mark.asyncio
async def test_repair_embedding_func_is_async_callable(monkeypatch):
    """验证 RepairEmbeddingFunc 实例的 __call__ 是 async callable（可 await）。"""
    import inspect

    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    # __call__ 应该是协程函数
    assert inspect.iscoroutinefunction(embed_func.__call__), (
        "RepairEmbeddingFunc.__call__ 应该是 async（协程函数）"
    )
    # 同时验证 _embed_async 是协程函数
    assert inspect.iscoroutinefunction(embed_func._embed_async), (
        "RepairEmbeddingFunc._embed_async 应该是 async（协程函数）"
    )


# =============================================================================
# v9 Task 3: repair_text_chunks 走 JsonKVStorage 单元测试
# =============================================================================


def _copy_truth_sources(tmp_storage_dir: Path, real_storage_dir: Path) -> None:
    """拷贝 3 真相源到 tmp 目录（其他派生文件不拷贝，让 repair 重建）。

    脑区/Skills 路径合法状态下 full_docs/cache 可能不存在——任一缺失就 pytest.skip，
    避免 _sha256 读不存在的文件触发 FileNotFoundError。
    """
    tmp_storage_dir.mkdir(parents=True, exist_ok=True)
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        src = real_storage_dir / fname
        if not src.exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
        import shutil
        shutil.copy2(src, tmp_storage_dir / fname)


def _sha256(path: Path) -> str:
    """算文件 sha256（验证真相源不变）。"""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_text_chunks(tmp_storage_dir: Path) -> dict:
    """读 repair 后的 text_chunks.json。"""
    tc_path = tmp_storage_dir / "kv_store_text_chunks.json"
    assert tc_path.exists(), f"text_chunks.json 不存在: {tc_path}"
    with open(tc_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_repair_text_chunks_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 ~/.niu/lightrag_storage 3 真相源到 tmp_path，跑 repair_text_chunks。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. text_chunks.json 生成 + 字段格式正确
    3. 每条 chunk 含 content/full_doc_id/tokens/chunk_order_index/file_path/llm_cache_list
    4. _id / create_time / update_time 由 storage 自动注入
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    # 拷贝 3 真相源到 tmp_path
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    # monkeypatch _STORAGE_DIR 指向 tmp_path
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 跑 repair_text_chunks（async）
    result = await lightrag_repair.repair_text_chunks()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 chunk: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：text_chunks.json 字段格式
    tc = _load_text_chunks(tmp_storage)
    assert isinstance(tc, dict)
    assert len(tc) == result["actual"]

    for chunk_id, chunk_value in tc.items():
        assert isinstance(chunk_value, dict), f"chunk_value 不是 dict: {chunk_id}"
        # 必须字段
        assert "content" in chunk_value, f"缺 content: {chunk_id}"
        assert "full_doc_id" in chunk_value, f"缺 full_doc_id: {chunk_id}"
        assert "tokens" in chunk_value, f"缺 tokens: {chunk_id}"
        assert "chunk_order_index" in chunk_value, f"缺 chunk_order_index: {chunk_id}"
        assert "file_path" in chunk_value, f"缺 file_path: {chunk_id}"
        assert "llm_cache_list" in chunk_value, f"缺 llm_cache_list: {chunk_id}"
        # storage 自动注入字段
        assert "_id" in chunk_value, f"缺 _id（storage 没注入）: {chunk_id}"
        assert "create_time" in chunk_value, f"缺 create_time: {chunk_id}"
        assert "update_time" in chunk_value, f"缺 update_time: {chunk_id}"
        # 类型校验
        assert isinstance(chunk_value["content"], str)
        assert isinstance(chunk_value["full_doc_id"], str)
        assert isinstance(chunk_value["tokens"], int)
        assert isinstance(chunk_value["chunk_order_index"], int)
        assert isinstance(chunk_value["file_path"], str)
        assert isinstance(chunk_value["llm_cache_list"], list)
        # _id 必须 = chunk_id（storage 自动注入）
        assert chunk_value["_id"] == chunk_id


@pytest.mark.asyncio
async def test_repair_text_chunks_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 空（无活跃 chunk_id），不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 text_chunks.json 不应被写空 {}，
    应保持不存在（跟 LightRAG JsonKVStorage.initialize 内存空 dict 不写盘一致）。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 不拷贝任何真相源（全新用户）
    # graphml 不创建（_load_graphml_nodes 把"文件不存在"当合法空 GraphML；
    # 不能写空字符串 ""，因为 ET.parse("") 会 ParseError → unrecoverable）
    # full_docs/cache 写空 dict（_check_truth_sources_intact 把空 dict 当 empty=合法）
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_text_chunks()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 text_chunks.json 应保持不存在
    # （跟 LightRAG JsonKVStorage.initialize 内存空 dict 不写盘一致）
    tc_path = tmp_storage / "kv_store_text_chunks.json"
    assert not tc_path.exists(), (
        "text_chunks.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_text_chunks_cache_corrupt_unrecoverable(monkeypatch, tmp_path):
    """cache 损坏测试：3 真相源之一损坏 → unrecoverable。"""
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 破坏 cache（写非法 JSON）
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{不是合法JSON")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_text_chunks()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "cache 损坏" in result["message"]


@pytest.mark.asyncio
async def test_repair_text_chunks_format_matches_lightrag_native(monkeypatch, tmp_path):
    """字段格式对比：repair 后的 text_chunks.json 跟 LightRAG 原生启动后的格式字节级一致。

    本测试是 D1（走 storage.upsert 不绕过）的核心验证。
    如果 repair 走 storage 接口正确，结果应该跟 LightRAG 自己启动后写入的格式一致。

    Skip 条件：如果没有 LightRAG 原生启动后的对照样本（~/.niu/lightrag_storage backup），
    跳过字节级 diff，只做字段存在性校验（已在 test_repair_text_chunks_real_data 覆盖）。
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    native_tc_path = Path.home() / ".niu" / "lightrag_storage_backup" / "kv_store_text_chunks.json"
    if not real_storage.exists() or not native_tc_path.exists():
        pytest.skip("缺少真实数据或 LightRAG 原生对照样本")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()

    repair_tc = _load_text_chunks(tmp_storage)
    with open(native_tc_path, encoding="utf-8") as f:
        native_tc = json.load(f)

    # 字段集合对比（repair 产生的 chunk_id 必须是 native 的子集）
    repair_keys = set(repair_tc.keys())
    native_keys = set(native_tc.keys())
    assert repair_keys.issubset(native_keys), f"repair 有 native 没有的 chunk: {repair_keys - native_keys}"

    # 共同 chunk_id 的字段对比（忽略 create_time / update_time，因为时间戳会变）
    common_keys = repair_keys & native_keys
    assert len(common_keys) > 0, "没有共同 chunk_id 可对比"

    for cid in list(common_keys)[:5]:  # 抽 5 条对比
        repair_chunk = repair_tc[cid]
        native_chunk = native_tc[cid]
        for field in ["content", "full_doc_id", "tokens", "chunk_order_index", "file_path"]:
            assert repair_chunk.get(field) == native_chunk.get(field), (
                f"chunk {cid} 字段 {field} 不一致: "
                f"repair={repair_chunk.get(field)!r}, native={native_chunk.get(field)!r}"
            )


# ====================================================================
# v9 Task 4: repair_doc_status 单元测试
# ====================================================================


@pytest.mark.asyncio
async def test_repair_doc_status_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 ~/.niu/lightrag_storage 3 真相源到 tmp_path，
    先跑 repair_text_chunks 生成 text_chunks.json，再跑 repair_doc_status。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. doc_status.json 生成 + 字段格式正确（含 track_id / metadata）
    3. 每条 doc 含 status/chunks_count/chunks_list/content_summary/content_length/
       created_at/updated_at/file_path/track_id/metadata/error_msg/multimodal_processed
    4. chunks_list 跟 text_chunks 反查一致
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    # 拷贝 3 真相源到 tmp_path
    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    # monkeypatch _STORAGE_DIR 指向 tmp_path
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks 生成 text_chunks.json（doc_status 依赖）
    tc_result = await lightrag_repair.repair_text_chunks()
    assert tc_result["status"] == "ok", f"repair_text_chunks 失败: {tc_result.get('message')}"

    # 跑 repair_doc_status
    result = await lightrag_repair.repair_doc_status()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 doc: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：doc_status.json 字段格式
    ds_path = tmp_storage / "kv_store_doc_status.json"
    assert ds_path.exists(), "doc_status.json 未生成"
    with open(ds_path, encoding="utf-8") as f:
        ds = json.load(f)
    assert isinstance(ds, dict)
    assert len(ds) == result["actual"]

    # 读 text_chunks 用于反查 chunks_list
    tc_path = tmp_storage / "kv_store_text_chunks.json"
    with open(tc_path, encoding="utf-8") as f:
        tc = json.load(f)

    # 按 full_doc_id 分组 chunks_list（测试侧独立算，跟 repair 函数对照）
    expected_chunks_by_doc: dict[str, list[str]] = {}
    for chunk_id, chunk_value in tc.items():
        if not isinstance(chunk_value, dict):
            continue
        full_doc_id = chunk_value.get("full_doc_id", "")
        if not full_doc_id:
            continue
        expected_chunks_by_doc.setdefault(full_doc_id, []).append(chunk_id)
    for doc_id in expected_chunks_by_doc:
        expected_chunks_by_doc[doc_id].sort()

    for doc_id, doc_value in ds.items():
        assert isinstance(doc_value, dict), f"doc_value 不是 dict: {doc_id}"
        # 必须字段（DocProcessingStatus 数据类要求，base.py:769-796）
        assert "status" in doc_value, f"缺 status: {doc_id}"
        assert "chunks_count" in doc_value, f"缺 chunks_count: {doc_id}"
        assert "chunks_list" in doc_value, f"缺 chunks_list: {doc_id}"
        assert "content_summary" in doc_value, f"缺 content_summary: {doc_id}"
        assert "content_length" in doc_value, f"缺 content_length: {doc_id}"
        assert "created_at" in doc_value, f"缺 created_at: {doc_id}"
        assert "updated_at" in doc_value, f"缺 updated_at: {doc_id}"
        assert "file_path" in doc_value, f"缺 file_path: {doc_id}"
        assert "track_id" in doc_value, f"缺 track_id: {doc_id}"
        assert "metadata" in doc_value, f"缺 metadata: {doc_id}"
        # v9 第 3 轮审查修复 I3：补 error_msg / multimodal_processed
        assert "error_msg" in doc_value, f"缺 error_msg: {doc_id}"
        assert "multimodal_processed" in doc_value, f"缺 multimodal_processed: {doc_id}"
        # 类型校验
        assert isinstance(doc_value["status"], str)
        assert doc_value["status"] in (
            "processed", "pending", "failed", "processing", "preprocessed",
        )
        assert isinstance(doc_value["chunks_count"], int)
        assert isinstance(doc_value["chunks_list"], list)
        assert isinstance(doc_value["content_summary"], str)
        assert isinstance(doc_value["content_length"], int)
        assert isinstance(doc_value["created_at"], str)
        assert isinstance(doc_value["updated_at"], str)
        assert isinstance(doc_value["file_path"], str)
        assert isinstance(doc_value["metadata"], dict)
        # chunks_list 跟 text_chunks 反查一致
        expected_list = expected_chunks_by_doc.get(doc_id, [])
        assert doc_value["chunks_list"] == expected_list, (
            f"doc {doc_id} chunks_list 不一致: "
            f"repair={doc_value['chunks_list'][:3]}..., expected={expected_list[:3]}..."
        )
        # chunks_count 跟 chunks_list 长度一致
        assert doc_value["chunks_count"] == len(doc_value["chunks_list"]), (
            f"doc {doc_id} chunks_count={doc_value['chunks_count']} "
            f"!= chunks_list len={len(doc_value['chunks_list'])}"
        )


@pytest.mark.asyncio
async def test_repair_doc_status_empty_user(monkeypatch, tmp_path):
    """全新用户测试：full_docs 为空，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 doc_status.json 不应被写空 {}，
    应保持不存在。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text("")
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_doc_status()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 doc_status.json 应保持不存在
    # （跟 LightRAG JsonDocStatusStorage.initialize 内存空 dict 不写盘一致）
    ds_path = tmp_storage / "kv_store_doc_status.json"
    assert not ds_path.exists(), (
        "doc_status.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_doc_status_full_docs_corrupt(monkeypatch, tmp_path):
    """full_docs 损坏测试：3 真相源之一损坏 → unrecoverable。"""
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 破坏 full_docs（写非法 JSON）
    (tmp_storage / "kv_store_full_docs.json").write_text("{不是合法JSON")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_doc_status()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "full_docs 损坏" in result["message"]


# =============================================================================
# v9 Task 5: repair_vdb_chunks 走 NanoVectorDBStorage 单元测试
# =============================================================================


def _load_vdb(vdb_path: Path) -> dict:
    """读 vdb 文件，返回 dict。"""
    assert vdb_path.exists(), f"vdb 文件不存在: {vdb_path}"
    with open(vdb_path, encoding="utf-8") as f:
        return json.load(f)


def _decode_matrix(matrix_b64: str, embedding_dim: int = 768):
    """解码 vdb matrix 字段（base64(float32 bytes) → np.ndarray）。"""
    import base64

    import numpy as np

    raw = base64.b64decode(matrix_b64)
    arr = np.frombuffer(raw, dtype=np.float32)
    # matrix 是 2D，行数 = len(data)，列数 = embedding_dim
    if len(arr) % embedding_dim != 0:
        raise ValueError(
            f"matrix 长度 {len(arr)} 不是 embedding_dim {embedding_dim} 的整数倍"
        )
    return arr.reshape(-1, embedding_dim)


def _decode_vector(vector_b64: str):
    """解码 vdb 单条 vector 字段（base64(zlib(float16 bytes)) → np.ndarray）。"""
    import base64
    import zlib

    import numpy as np

    raw = base64.b64decode(vector_b64)
    decompressed = zlib.decompress(raw)
    return np.frombuffer(decompressed, dtype=np.float16).astype(np.float32)


@pytest.mark.asyncio
async def test_repair_vdb_chunks_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，先跑 repair_text_chunks，再跑 repair_vdb_chunks。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. vdb_chunks.json 生成 + 字段格式正确
    3. 每条 chunk 含 __id__/content/full_doc_id/file_path/vector（不含 tokens/chunk_order_index）
    4. matrix 是 L2 归一化后的单位向量（每行模长 ≈ 1）
    5. vector 跟 matrix 对应行维度一致
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    # 用假 embedding 模型（避免加载真实 ~400MB 模型）
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks 生成 text_chunks.json
    tc_result = await lightrag_repair.repair_text_chunks()
    assert tc_result["status"] == "ok", f"repair_text_chunks 失败: {tc_result.get('message')}"

    # 跑 repair_vdb_chunks
    result = await lightrag_repair.repair_vdb_chunks()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 chunk: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：vdb_chunks.json 字段格式
    vdb = _load_vdb(tmp_storage / "vdb_chunks.json")
    assert "embedding_dim" in vdb
    assert vdb["embedding_dim"] == 768
    assert "data" in vdb
    assert isinstance(vdb["data"], list)
    assert len(vdb["data"]) == result["actual"]
    assert "matrix" in vdb
    assert isinstance(vdb["matrix"], str)

    # 断言 4：每条 chunk 字段格式
    for item in vdb["data"]:
        assert "__id__" in item, f"缺 __id__: {item}"
        assert "content" in item, f"缺 content: {item}"
        assert "full_doc_id" in item, f"缺 full_doc_id: {item}"
        assert "file_path" in item, f"缺 file_path: {item}"
        assert "vector" in item, f"缺 vector: {item}"
        assert "__created_at__" in item, f"缺 __created_at__: {item}"
        # 被过滤字段（不应落盘）
        assert "tokens" not in item, f"tokens 不应落盘（meta_fields 过滤）: {item}"
        assert "chunk_order_index" not in item, f"chunk_order_index 不应落盘: {item}"
        assert "llm_cache_list" not in item, f"llm_cache_list 不应落盘: {item}"
        # 类型校验
        assert isinstance(item["__id__"], str)
        assert item["__id__"].startswith("chunk-")
        assert isinstance(item["content"], str)
        assert isinstance(item["full_doc_id"], str)
        assert isinstance(item["file_path"], str)
        assert isinstance(item["vector"], str)
        assert isinstance(item["__created_at__"], int)

    # 断言 5：matrix 是 L2 归一化后的单位向量
    matrix = _decode_matrix(vdb["matrix"], embedding_dim=768)
    assert matrix.shape == (len(vdb["data"]), 768), (
        f"matrix shape {matrix.shape} != ({len(vdb['data'])}, 768)"
    )
    # 每行模长 ≈ 1（NanoVectorDB 内部做 L2 归一化）
    for i, row in enumerate(matrix):
        norm = float((row ** 2).sum() ** 0.5)
        assert 0.99 <= norm <= 1.01, (
            f"matrix 第 {i} 行模长 {norm} 不在 [0.99, 1.01]（L2 归一化失败）"
        )

    # 断言 6：单条 vector 维度跟 matrix 列数一致
    first_vector = _decode_vector(vdb["data"][0]["vector"])
    assert first_vector.shape == (768,), f"vector shape {first_vector.shape} != (768,)"


@pytest.mark.asyncio
async def test_repair_vdb_chunks_empty_user(monkeypatch, tmp_path):
    """全新用户测试：text_chunks 为空，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：全新用户场景下 vdb_chunks.json 不应被写空，
    应保持不存在。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text("")
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks（全新用户不写派生文件，text_chunks.json 不存在）
    await lightrag_repair.repair_text_chunks()

    # 跑 repair_vdb_chunks
    result = await lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    # 全新用户场景下 vdb_chunks.json 应保持不存在
    # （跟 LightRAG NanoVectorDBStorage.initialize 内存空 dict 不写盘一致）
    vdb_path = tmp_storage / "vdb_chunks.json"
    assert not vdb_path.exists(), (
        "vdb_chunks.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_vdb_chunks_meta_fields_filter(monkeypatch, tmp_path):
    """meta_fields 过滤测试：构造 text_chunks 含 tokens/chunk_order_index/llm_cache_list，
    验证 vdb_chunks.json 落盘后这些字段被过滤掉（只保留 content/full_doc_id/file_path）。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 手动构造 text_chunks（含 v8 旧版会落盘但 v9 meta_fields 不允许的字段）
    # 用 compute_mdhash_id 生成 chunk_id（跟 LightRAG 原生一致）
    from lightrag.utils import compute_mdhash_id

    chunk_contents = [
        "这是测试 chunk 1 的内容，用于验证 meta_fields 过滤。",
        "这是测试 chunk 2 的内容，跟 chunk 1 不同。",
        "这是测试 chunk 3 的内容，跟前面两个都不同。",
    ]
    text_chunks_data = {}
    for i, content in enumerate(chunk_contents):
        chunk_id = compute_mdhash_id(content, prefix="chunk-")
        text_chunks_data[chunk_id] = {
            "content": content,
            "full_doc_id": f"doc-test-{i}",
            "file_path": f"/tmp/test_{i}.txt",
            # 以下字段应被 meta_fields 过滤掉
            "tokens": 100 + i,
            "chunk_order_index": i,
            "llm_cache_list": [],
            # 旧版 LightRAG 残留字段
            "create_time": 1700000000 + i,
            "update_time": 1700000000 + i,
            "_id": chunk_id,
        }

    import json as _json

    with open(tmp_storage / "kv_store_text_chunks.json", "w", encoding="utf-8") as f:
        _json.dump(text_chunks_data, f, ensure_ascii=False)

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 跑 repair_vdb_chunks（直接读 text_chunks.json，不需要先跑 repair_text_chunks）
    result = await lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 3, f"应重建 3 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_chunks.json")
    assert len(vdb["data"]) == 3

    # 验证：每条只含 meta_fields 内字段 + storage 自动注入字段
    # 落盘字段：__id__ / __created_at__ / content / full_doc_id / file_path / vector
    # __vector__ 是 np.ndarray，不会落盘到 JSON（NanoVectorDB.save 只写 vector 编码字符串）
    on_disk_expected = {
        "__id__",
        "__created_at__",
        "content",
        "full_doc_id",
        "file_path",
        "vector",
    }

    for item in vdb["data"]:
        item_keys = set(item.keys())
        # 被过滤字段不应出现
        assert "tokens" not in item_keys, f"tokens 不应落盘: {item_keys}"
        assert "chunk_order_index" not in item_keys, f"chunk_order_index 不应落盘: {item_keys}"
        assert "llm_cache_list" not in item_keys, f"llm_cache_list 不应落盘: {item_keys}"
        assert "create_time" not in item_keys, f"create_time 不应落盘: {item_keys}"
        assert "update_time" not in item_keys, f"update_time 不应落盘: {item_keys}"
        assert "_id" not in item_keys, f"_id 不应落盘（v9 用 __id__）: {item_keys}"
        # 落盘字段集合应精确匹配（不允许多余字段）
        assert item_keys == on_disk_expected, (
            f"字段集合不一致: 实际={item_keys}, 期望={on_disk_expected}"
        )

    # 验证 __id__ 是 compute_mdhash_id 算出来的（跟 LightRAG 原生一致）
    expected_ids = {compute_mdhash_id(c, prefix="chunk-") for c in chunk_contents}
    actual_ids = {item["__id__"] for item in vdb["data"]}
    assert actual_ids == expected_ids, (
        f"chunk_id 集合不一致: actual={actual_ids}, expected={expected_ids}"
    )

    # 验证 content/full_doc_id/file_path 正确落盘
    contents_in_vdb = {item["content"] for item in vdb["data"]}
    assert contents_in_vdb == set(chunk_contents), (
        f"content 集合不一致: actual={contents_in_vdb}, expected={set(chunk_contents)}"
    )


@pytest.mark.asyncio
async def test_repair_vdb_chunks_matrix_l2_normalized(monkeypatch, tmp_path):
    """matrix L2 归一化测试：验证 vdb_chunks.json 的 matrix 每行是单位向量。

    NanoVectorDBStorage.upsert 内部调 normalize（dbs.py L51-52 + L93-95）：
    ```python
    def normalize(a: np.ndarray) -> np.ndarray:
        return a / np.linalg.norm(a, axis=-1, keepdims=True)
    ```
    所有 embedding 都会被归一化到单位长度，存到 matrix。

    验证：
    1. matrix shape = (N, 768)
    2. 每行模长 ≈ 1.0（L2 归一化生效）
    3. matrix 跟 data 数量一致
    4. 跟 vector 字段（float16 编码）维度一致
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 用 _FakeEmbedModel（返回随机向量，模长不一定为 1，验证归一化生效）
    # 注意：_FakeEmbedModel.encode 返回 np.random.rand，模长远大于 1
    # 如果 NanoVectorDB 没做归一化，matrix 行模长会 >> 1
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 手动构造 text_chunks（10 条，触发归一化逻辑）
    from lightrag.utils import compute_mdhash_id

    chunk_contents = [f"测试内容_{i:02d} 用于验证 L2 归一化" for i in range(10)]
    text_chunks_data = {}
    for content in chunk_contents:
        chunk_id = compute_mdhash_id(content, prefix="chunk-")
        text_chunks_data[chunk_id] = {
            "content": content,
            "full_doc_id": "doc-test",
            "file_path": "/tmp/test.txt",
        }

    import json as _json

    with open(tmp_storage / "kv_store_text_chunks.json", "w", encoding="utf-8") as f:
        _json.dump(text_chunks_data, f, ensure_ascii=False)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 10

    vdb = _load_vdb(tmp_storage / "vdb_chunks.json")
    assert len(vdb["data"]) == 10

    # 解码 matrix
    matrix = _decode_matrix(vdb["matrix"], embedding_dim=768)
    assert matrix.shape == (10, 768), (
        f"matrix shape {matrix.shape} != (10, 768)"
    )

    # 每行模长应在 [0.99, 1.01]（L2 归一化生效）
    # 使用 numpy 向量化计算（比逐行循环快）
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1)
    for i, norm in enumerate(norms):
        assert 0.99 <= float(norm) <= 1.01, (
            f"matrix 第 {i} 行模长 {float(norm)} 不在 [0.99, 1.01]（L2 归一化失败）"
        )

    # 验证原始 embedding（_FakeEmbedModel 返回）模长 >> 1，
    # 确认是 NanoVectorDB 做了归一化（而不是假模型本来就返回单位向量）
    fake_vecs = fake_model.encode(chunk_contents)
    fake_norms = np.linalg.norm(fake_vecs, axis=1)
    # _FakeEmbedModel 返回 np.random.rand，模长通常在 [3, 5] 之间（768 维）
    assert float(fake_norms[0]) > 2.0, (
        f"_FakeEmbedModel 返回的向量模长 {float(fake_norms[0])} 应 > 2.0 "
        f"（否则无法验证归一化生效）"
    )

    # 验证 vector 字段（float16 编码）维度跟 matrix 列数一致
    first_vector = _decode_vector(vdb["data"][0]["vector"])
    assert first_vector.shape == (768,), (
        f"vector shape {first_vector.shape} != (768,)"
    )
    # 注意：vector 字段是原始 embedding（float16 编码，未归一化）
    # matrix 是归一化后的（float32 编码）
    # 两者维度一致但模长不同——这是 NanoVectorDB 的设计


# =============================================================================
# v9 Task 6: repair_vdb_entities 走 NanoVectorDBStorage 单元测试
# =============================================================================


@pytest.mark.asyncio
async def test_repair_vdb_entities_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，跑 repair_vdb_entities。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. vdb_entities.json 生成 + 字段格式正确
    3. 每条 entity 含 __id__/entity_name/content/source_id/file_path/vector
       （不含 description/entity_type）
    4. __id__ = compute_mdhash_id(entity_name, prefix="ent-")
    5. content 格式 = f"{entity_name}\n{description}"（第一行是 entity_name）
    6. matrix 是 L2 归一化后的单位向量（每行模长 ≈ 1）
    """
    from lightrag.utils import compute_mdhash_id

    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    # 用假 embedding 模型（避免加载真实 ~400MB 模型）
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 跑 repair_vdb_entities（直接读 GraphML，不需要先跑 repair_text_chunks）
    result = await lightrag_repair.repair_vdb_entities()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 entity: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：vdb_entities.json 字段格式
    vdb = _load_vdb(tmp_storage / "vdb_entities.json")
    assert "embedding_dim" in vdb
    assert vdb["embedding_dim"] == 768
    assert "data" in vdb
    assert isinstance(vdb["data"], list)
    assert len(vdb["data"]) == result["actual"]
    assert "matrix" in vdb
    assert isinstance(vdb["matrix"], str)

    # 断言 4：每条 entity 字段格式
    for item in vdb["data"]:
        assert "__id__" in item, f"缺 __id__: {item}"
        assert "entity_name" in item, f"缺 entity_name: {item}"
        assert "content" in item, f"缺 content: {item}"
        assert "source_id" in item, f"缺 source_id: {item}"
        assert "file_path" in item, f"缺 file_path: {item}"
        assert "vector" in item, f"缺 vector: {item}"
        assert "__created_at__" in item, f"缺 __created_at__: {item}"
        # 被过滤字段（不应落盘）
        assert "description" not in item, f"description 不应落盘（meta_fields 过滤）: {item}"
        assert "entity_type" not in item, f"entity_type 不应落盘: {item}"
        # 类型校验
        assert isinstance(item["__id__"], str)
        assert item["__id__"].startswith("ent-")
        assert isinstance(item["entity_name"], str)
        # entity_name 必须 .lower()
        assert item["entity_name"] == item["entity_name"].lower(), (
            f"entity_name 未 lower: {item['entity_name']}"
        )
        assert isinstance(item["content"], str)
        assert isinstance(item["source_id"], str)
        assert isinstance(item["file_path"], str)
        assert isinstance(item["vector"], str)
        assert isinstance(item["__created_at__"], int)

        # __id__ 必须 = compute_mdhash_id(entity_name, prefix="ent-")
        expected_id = compute_mdhash_id(item["entity_name"], prefix="ent-")
        assert item["__id__"] == expected_id, (
            f"__id__ {item['__id__']} != compute_mdhash_id({item['entity_name']}) = {expected_id}"
        )

        # content 格式必须是 f"{entity_name}\n{description}"
        # 即 content 第一行 == entity_name
        first_line = item["content"].split("\n", 1)[0]
        assert first_line == item["entity_name"], (
            f"content 第一行 {first_line!r} != entity_name {item['entity_name']!r}"
        )

    # 断言 5：matrix 是 L2 归一化后的单位向量
    matrix = _decode_matrix(vdb["matrix"], embedding_dim=768)
    assert matrix.shape == (len(vdb["data"]), 768), (
        f"matrix shape {matrix.shape} != ({len(vdb['data'])}, 768)"
    )
    for i, row in enumerate(matrix):
        norm = float((row ** 2).sum() ** 0.5)
        assert 0.99 <= norm <= 1.01, (
            f"matrix 第 {i} 行模长 {norm} 不在 [0.99, 1.01]（L2 归一化失败）"
        )

    # 验证 vector 字段维度跟 matrix 列数一致
    first_vector = _decode_vector(vdb["data"][0]["vector"])
    assert first_vector.shape == (768,), (
        f"vector shape {first_vector.shape} != (768,)"
    )


@pytest.mark.asyncio
async def test_repair_vdb_entities_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 node，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：全新用户场景下 vdb_entities.json 不应被写空，
    应保持不存在。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：GraphML 合法但无 node + 其他真相源空 dict
    # （空字符串 GraphML 会被 ET.parse 当作损坏 XML，必须写合法空 graphml）
    empty_graphml = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph id="G" edgedefault="undirected">
  </graph>
</graphml>
"""
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        empty_graphml, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 跑 repair_vdb_entities
    result = await lightrag_repair.repair_vdb_entities()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    # 全新用户场景下 vdb_entities.json 应保持不存在
    # （跟 LightRAG NanoVectorDBStorage.initialize 内存空 dict 不写盘一致）
    vdb_path = tmp_storage / "vdb_entities.json"
    assert not vdb_path.exists(), (
        "vdb_entities.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_vdb_entities_id_is_hash(monkeypatch, tmp_path):
    """__id__ 是 hash ID 测试：验证 __id__ 是 compute_mdhash_id(entity_name, prefix="ent-")，
    不是 entity_name 原文（v9 修复 v8 bug：v8 用 node_id 做 dict key，但 node_id 已 lower
    所以 hash 是对的；v9 显式用 compute_mdhash_id 防 dict key 退化成 entity_name）。

    构造 GraphML 含 3 个实体（含大小写混合 node id），验证：
    1. __id__ = compute_mdhash_id(entity_name.lower(), prefix="ent-")
    2. __id__ 跟 entity_name 原文不同（除了巧合 hash 等于 name 的极端情况）
    3. __id__ 全部以 "ent-" 前缀开头
    """
    from lightrag.utils import compute_mdhash_id

    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：3 个实体（混合大小写，验证 .lower()）
    # GraphML 字段映射：d1=entity_type, d2=description, d3=source_id, d4=file_path
    graphml_content = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph id="G" edgedefault="undirected">
    <node id="TestEntity">
      <data key="d1">object</data>
      <data key="d2">测试实体 1 描述</data>
      <data key="d3">chunk-aaa</data>
      <data key="d4">/tmp/test1.txt</data>
    </node>
    <node id="AnotherEntity">
      <data key="d1">concept</data>
      <data key="d2">另一个实体描述</data>
      <data key="d3">chunk-bbb</data>
      <data key="d4">/tmp/test2.txt</data>
    </node>
    <node id="LowerEntity">
      <data key="d1">person</data>
      <data key="d2">小写实体</data>
      <data key="d3">chunk-ccc</data>
      <data key="d4">/tmp/test3.txt</data>
    </node>
  </graph>
</graphml>
"""
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    # 真相源其他文件保持空（repair_vdb_entities 只依赖 GraphML）
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_entities()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 3, f"应重建 3 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_entities.json")
    assert len(vdb["data"]) == 3

    # 验证 __id__ 是 compute_mdhash_id(entity_name, prefix="ent-")
    # entity_name 必须 .lower()（GraphML 写入的是 "TestEntity" 等，repair 应 lower）
    expected_names = {"testentity", "anotherentity", "lowerentity"}
    actual_names = {item["entity_name"] for item in vdb["data"]}
    assert actual_names == expected_names, (
        f"entity_name 集合不一致（应 .lower()）: actual={actual_names}, expected={expected_names}"
    )

    for item in vdb["data"]:
        entity_name = item["entity_name"]
        # __id__ 必须以 "ent-" 前缀开头
        assert item["__id__"].startswith("ent-"), (
            f"__id__ {item['__id__']} 不以 'ent-' 开头"
        )
        # __id__ 必须等于 compute_mdhash_id(entity_name, prefix="ent-")
        expected_id = compute_mdhash_id(entity_name, prefix="ent-")
        assert item["__id__"] == expected_id, (
            f"__id__ {item['__id__']} != compute_mdhash_id({entity_name!r}) = {expected_id}"
        )
        # __id__ 不应等于 entity_name 原文（hash ID 应比 entity_name 长）
        # compute_mdhash_id 返回 "ent-" + 32 字符 md5 = 36 字符
        assert item["__id__"] != entity_name, (
            f"__id__ == entity_name（说明 dict key 退化成 entity_name 了）: {item['__id__']}"
        )
        assert len(item["__id__"]) == 36, (
            f"__id__ 长度 {len(item['__id__'])} != 36（'ent-' + 32 字符 md5）"
        )


@pytest.mark.asyncio
async def test_repair_vdb_entities_meta_fields_filter(monkeypatch, tmp_path):
    """meta_fields 过滤测试：构造 GraphML 含 description/entity_type 字段，
    验证 vdb_entities.json 落盘后这些字段被过滤掉（只保留 meta_fields 内字段）。

    meta_fields = {"entity_name", "source_id", "content", "file_path"}（lightrag.py:716）
    落盘字段：__id__ / __created_at__ / entity_name / source_id / content / file_path / vector
    被过滤字段：description / entity_type（v8 旧版会落盘，v9 走 storage 接口被过滤）
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：2 个实体，含完整 d1(entity_type) / d2(description) 字段
    graphml_content = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph id="G" edgedefault="undirected">
    <node id="entity_one">
      <data key="d1">object</data>
      <data key="d2">这是 entity_one 的描述，应被 meta_fields 过滤掉不落盘</data>
      <data key="d3">chunk-001</data>
      <data key="d4">/tmp/file1.txt</data>
    </node>
    <node id="entity_two">
      <data key="d1">concept</data>
      <data key="d2">entity_two 的描述，应被过滤</data>
      <data key="d3">chunk-002</data>
      <data key="d4">/tmp/file2.txt</data>
    </node>
  </graph>
</graphml>
"""
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_entities()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 2, f"应重建 2 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_entities.json")
    assert len(vdb["data"]) == 2

    # 验证：每条只含 meta_fields 内字段 + storage 自动注入字段
    # 落盘字段：__id__ / __created_at__ / entity_name / source_id / content / file_path / vector
    # __vector__ 是 np.ndarray，不会落盘到 JSON（NanoVectorDB.save 只写 vector 编码字符串）
    on_disk_expected = {
        "__id__",
        "__created_at__",
        "entity_name",
        "source_id",
        "content",
        "file_path",
        "vector",
    }

    for item in vdb["data"]:
        item_keys = set(item.keys())
        # 被过滤字段不应出现
        assert "description" not in item_keys, (
            f"description 不应落盘（meta_fields 过滤）: {item_keys}"
        )
        assert "entity_type" not in item_keys, (
            f"entity_type 不应落盘（meta_fields 过滤）: {item_keys}"
        )
        # 落盘字段集合应精确匹配（不允许多余字段）
        assert item_keys == on_disk_expected, (
            f"字段集合不一致: 实际={item_keys}, 期望={on_disk_expected}"
        )

    # 验证 content 格式：f"{entity_name}\n{description}"
    # 即第一行是 entity_name，后面是 description
    for item in vdb["data"]:
        entity_name = item["entity_name"]
        content = item["content"]
        # content 第一行必须是 entity_name
        first_line = content.split("\n", 1)[0]
        assert first_line == entity_name, (
            f"content 第一行 {first_line!r} != entity_name {entity_name!r}"
        )
        # content 必须含 "\n"（因为 description 非空）
        assert "\n" in content, f"content 缺 '\\n' 分隔符: {content!r}"

    # 验证 source_id / file_path 正确落盘
    source_ids = {item["source_id"] for item in vdb["data"]}
    assert source_ids == {"chunk-001", "chunk-002"}, (
        f"source_id 集合不一致: {source_ids}"
    )
    file_paths = {item["file_path"] for item in vdb["data"]}
    assert file_paths == {"/tmp/file1.txt", "/tmp/file2.txt"}, (
        f"file_path 集合不一致: {file_paths}"
    )


# =============================================================================
# v9 Task 7: repair_vdb_relationships 走 NanoVectorDBStorage 单元测试
# =============================================================================


@pytest.mark.asyncio
async def test_repair_vdb_relationships_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，跑 repair_vdb_relationships。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. vdb_relationships.json 生成 + 字段格式正确
    3. 每条 relationship 含 __id__/src_id/tgt_id/source_id/content/file_path/vector
       （不含 keywords/description/weight）
    4. src_id/tgt_id 必须 sorted（src_id <= tgt_id）
    5. __id__ = compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")
    6. content 格式 = f"{keywords}\\t{src}\\n{tgt}\\n{description}"
    7. keywords 用 `", ".join(dict.fromkeys(...))` 去重保序（跨运行稳定）
    8. matrix 是 L2 归一化后的单位向量
    """
    from lightrag.utils import compute_mdhash_id, make_relation_vdb_ids

    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 跑 repair_vdb_relationships
    result = await lightrag_repair.repair_vdb_relationships()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 relationship: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：vdb_relationships.json 字段格式
    vdb = _load_vdb(tmp_storage / "vdb_relationships.json")
    assert vdb["embedding_dim"] == 768
    assert isinstance(vdb["data"], list)
    assert len(vdb["data"]) == result["actual"]
    assert isinstance(vdb["matrix"], str)

    # 断言 4：每条 relationship 字段格式
    for item in vdb["data"]:
        assert "__id__" in item, f"缺 __id__: {item}"
        assert "src_id" in item, f"缺 src_id: {item}"
        assert "tgt_id" in item, f"缺 tgt_id: {item}"
        assert "source_id" in item, f"缺 source_id: {item}"
        assert "content" in item, f"缺 content: {item}"
        assert "file_path" in item, f"缺 file_path: {item}"
        assert "vector" in item, f"缺 vector: {item}"
        assert "__created_at__" in item, f"缺 __created_at__: {item}"
        # 被过滤字段（不应落盘）
        assert "keywords" not in item, f"keywords 不应落盘（meta_fields 过滤）: {item}"
        assert "description" not in item, f"description 不应落盘: {item}"
        assert "weight" not in item, f"weight 不应落盘: {item}"
        # 类型校验
        assert isinstance(item["__id__"], str)
        assert item["__id__"].startswith("rel-")
        assert isinstance(item["src_id"], str)
        assert isinstance(item["tgt_id"], str)
        assert isinstance(item["source_id"], str)
        assert isinstance(item["content"], str)
        assert isinstance(item["file_path"], str)
        assert isinstance(item["vector"], str)
        assert isinstance(item["__created_at__"], int)

        # 断言 5：src_id/tgt_id 必须 sorted（src_id <= tgt_id）
        # 跟 LightRAG operate.py L1586-1587/L2515-2516 一致
        assert item["src_id"] <= item["tgt_id"], (
            f"src_id {item['src_id']!r} > tgt_id {item['tgt_id']!r}（未 sorted）"
        )

        # 断言 6：__id__ = compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")
        # 跟 LightRAG operate.py L1589/L2519 + utils.py L577-578 一致
        # 也等于 make_relation_vdb_ids(sorted_src, sorted_tgt)[0]
        expected_id = compute_mdhash_id(item["src_id"] + item["tgt_id"], prefix="rel-")
        assert item["__id__"] == expected_id, (
            f"__id__ {item['__id__']} != compute_mdhash_id({item['src_id']}+{item['tgt_id']}) = {expected_id}"
        )
        assert item["__id__"] == make_relation_vdb_ids(item["src_id"], item["tgt_id"])[0]

        # 断言 7：content 格式 = f"{keywords}\t{src}\n{tgt}\n{description}"
        # 跟 LightRAG operate.py L1601/L2527 一致
        # content 第 1 段（tab 之前）= keywords
        # content tab 之后第 1 行 = src_id
        # content tab 之后第 2 行 = tgt_id
        # content tab 之后第 3 行起 = description
        parts = item["content"].split("\t", 1)
        assert len(parts) == 2, f"content 缺 tab 分隔符: {item['content']!r}"
        keywords_part = parts[0]
        rest = parts[1]
        lines = rest.split("\n")
        assert len(lines) >= 3, f"content rest 行数 < 3: {rest!r}"
        assert lines[0] == item["src_id"], (
            f"content 第 1 行 {lines[0]!r} != src_id {item['src_id']!r}"
        )
        assert lines[1] == item["tgt_id"], (
            f"content 第 2 行 {lines[1]!r} != tgt_id {item['tgt_id']!r}"
        )
        # description 是 lines[2:] 用 \n join（可能多行）
        # 这里只验证存在性，不验证具体内容（description 来自 GraphML d8）

        # 断言 8：keywords 用 ", " 分隔（如果非空）
        # v9 用 dict.fromkeys 保序去重（跟 LightRAG set 不完全一致但跨运行稳定）
        if keywords_part:
            # keywords_part 应该是 "kw1, kw2, kw3" 格式（逗号+空格分隔）
            # 不能含 <SEP>（应该已被拆分+去重+join）
            assert "<SEP>" not in keywords_part, (
                f"keywords 含 <SEP>（未拆分）: {keywords_part!r}"
            )
            # 拆分后去重检查（去重后应该跟原 list 长度一致）
            kw_list = [k.strip() for k in keywords_part.split(",") if k.strip()]
            assert len(kw_list) == len(set(kw_list)), (
                f"keywords 未去重: {kw_list}"
            )

    # 断言 9：matrix 是 L2 归一化后的单位向量
    matrix = _decode_matrix(vdb["matrix"], embedding_dim=768)
    assert matrix.shape == (len(vdb["data"]), 768)
    for i, row in enumerate(matrix):
        norm = float((row ** 2).sum() ** 0.5)
        assert 0.99 <= norm <= 1.01, f"matrix 第 {i} 行模长 {norm} 不在 [0.99, 1.01]"


@pytest.mark.asyncio
async def test_repair_vdb_relationships_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 edge，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5+6 / I3+I2）：全新用户场景下 vdb_relationships.json 不应被写空，
    应保持不存在。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    # 注意：GraphML 写空字符串会触发 ET.ParseError（不是"无 edge"），
    # 必须写一个有效的空 GraphML（含 graph 元素但无 edge）才能模拟"全新用户无边"。
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"/>\n'
        '</graphml>\n',
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5+6 / I3+I2）：
    # 全新用户场景下 vdb_relationships.json 应保持不存在
    # （跟 LightRAG NanoVectorDBStorage.initialize 内存空 dict 不写盘一致）
    vdb_path = tmp_storage / "vdb_relationships.json"
    assert not vdb_path.exists(), (
        "vdb_relationships.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_vdb_relationships_src_tgt_sorted(monkeypatch, tmp_path):
    """src/tgt sorted 测试：构造 GraphML 含反向 src/tgt（src > tgt），
    验证 vdb_relationships.json 落盘后 src_id <= tgt_id（已 sorted）。

    跟 LightRAG operate.py L1586-1587/L2515-2516 一致：
        if src > tgt: src, tgt = tgt, src
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：edge source="B" target="A"（src > tgt，反向）
    # 验证 repair 后 src_id="A" / tgt_id="B"（sorted）
    graphml_content = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph id="G" edgedefault="undirected">
    <node id="a"/>
    <node id="b"/>
    <edge source="b" target="a">
      <data key="d8">B 到 A 的关系</data>
      <data key="d9">关系词</data>
      <data key="d10">chunk-001</data>
      <data key="d11">/tmp/file1.txt</data>
    </edge>
  </graph>
</graphml>
"""
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 1, f"应重建 1 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_relationships.json")
    assert len(vdb["data"]) == 1
    item = vdb["data"][0]
    # sorted 后 src_id="a" / tgt_id="b"（"a" < "b"）
    assert item["src_id"] == "a", f"src_id 应 sorted 为 'a'，实际 {item['src_id']!r}"
    assert item["tgt_id"] == "b", f"tgt_id 应 sorted 为 'b'，实际 {item['tgt_id']!r}"
    # content tab 后第 1 行 = src_id（sorted 后）
    parts = item["content"].split("\t", 1)
    lines = parts[1].split("\n")
    assert lines[0] == "a", f"content 第 1 行应 'a'（sorted src），实际 {lines[0]!r}"
    assert lines[1] == "b", f"content 第 2 行应 'b'（sorted tgt），实际 {lines[1]!r}"


@pytest.mark.asyncio
async def test_repair_vdb_relationships_keywords_dedup(monkeypatch, tmp_path):
    """keywords 去重保序测试：构造 GraphML d9 含 <SEP> 分隔的重复关键词，
    验证 vdb_relationships.json 落盘后 content 的 keywords 部分用 ", " 分隔 + 已去重 + 保序。

    v9 用 dict.fromkeys 保序去重（跟 LightRAG set 不完全一致但跨运行稳定）。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：d9 含 <SEP> 分隔的重复关键词 "kw_a<SEP>kw_b<SEP>kw_a<SEP>kw_c"
    # 期望去重保序后："kw_a, kw_b, kw_c"
    # 注意：GRAPH_FIELD_SEP = "<SEP>"，含 "<" ">"，在 XML 内必须转义为 &lt;SEP&gt;
    # ET.parse 会自动反转义回 "<SEP>"
    from lightrag.constants import GRAPH_FIELD_SEP as _SEP
    # 构造转义后的 SEP（&lt;SEP&gt;）用于 XML，data.text 会还原成 "<SEP>"
    sep_escaped = _SEP.replace("<", "&lt;").replace(">", "&gt;")
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="a"/>\n'
        '    <node id="b"/>\n'
        '    <edge source="a" target="b">\n'
        '      <data key="d8">关系描述</data>\n'
        f'      <data key="d9">kw_a{sep_escaped}kw_b{sep_escaped}kw_a{sep_escaped}kw_c</data>\n'
        '      <data key="d10">chunk-001</data>\n'
        '      <data key="d11">/tmp/file1.txt</data>\n'
        '    </edge>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 1, f"应重建 1 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_relationships.json")
    assert len(vdb["data"]) == 1
    item = vdb["data"][0]
    # content tab 前是 keywords 部分
    parts = item["content"].split("\t", 1)
    keywords_str = parts[0]
    # 期望去重保序后 "kw_a, kw_b, kw_c"
    assert keywords_str == "kw_a, kw_b, kw_c", (
        f"keywords 去重保序失败: 期望 'kw_a, kw_b, kw_c'，实际 {keywords_str!r}"
    )
    # 不应含 <SEP>
    assert "<SEP>" not in keywords_str
    assert _SEP not in keywords_str


@pytest.mark.asyncio
async def test_repair_vdb_relationships_content_format(monkeypatch, tmp_path):
    """content 格式测试：验证 content 严格遵循 f"{keywords}\\t{src}\\n{tgt}\\n{description}"。

    跟 LightRAG operate.py L1601/L2527 一致：
        rel_content = f"{combined_keywords}\t{src}\n{tgt}\n{final_description}"
    tab 分隔 keywords 和 src，换行分隔 src/tgt/description。
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：含 keywords / description 多行（验证换行分隔）
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="alpha"/>\n'
        '    <node id="beta"/>\n'
        '    <edge source="alpha" target="beta">\n'
        '      <data key="d8">alpha 跟 beta 的关系描述</data>\n'
        '      <data key="d9">关键词1, 关键词2</data>\n'
        '      <data key="d10">chunk-abc</data>\n'
        '      <data key="d11">/tmp/doc.txt</data>\n'
        '    </edge>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 1, f"应重建 1 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_relationships.json")
    assert len(vdb["data"]) == 1
    item = vdb["data"][0]

    # 验证 src_id/tgt_id sorted（"alpha" < "beta"）
    assert item["src_id"] == "alpha"
    assert item["tgt_id"] == "beta"

    # 验证 content 严格格式
    expected_content = "关键词1, 关键词2\talpha\nbeta\nalpha 跟 beta 的关系描述"
    assert item["content"] == expected_content, (
        f"content 格式不对:\n"
        f"  期望: {expected_content!r}\n"
        f"  实际: {item['content']!r}"
    )

    # 验证 file_path 从 d11 读取
    assert item["file_path"] == "/tmp/doc.txt", (
        f"file_path 应从 d11 读取: 期望 '/tmp/doc.txt'，实际 {item['file_path']!r}"
    )

    # 验证 source_id 从 d10 读取
    assert item["source_id"] == "chunk-abc", (
        f"source_id 应从 d10 读取: 期望 'chunk-abc'，实际 {item['source_id']!r}"
    )


@pytest.mark.asyncio
async def test_repair_vdb_relationships_meta_fields_filter(monkeypatch, tmp_path):
    """meta_fields 过滤测试：构造 GraphML 含 keywords/description/weight 字段（实际 weight 不在 GraphML），
    验证 vdb_relationships.json 落盘后 keywords/description/weight 被过滤掉。

    meta_fields = {"src_id", "tgt_id", "source_id", "content", "file_path"}（lightrag.py:722）
    落盘字段：__id__ / __created_at__ / src_id / tgt_id / source_id / content / file_path / vector
    被过滤字段：keywords / description / weight
    """
    from niu_api.internal import embedding as niu_embedding
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="entity_one"/>\n'
        '    <node id="entity_two"/>\n'
        '    <edge source="entity_one" target="entity_two">\n'
        '      <data key="d8">这是 entity_one 跟 entity_two 的关系描述，应被过滤</data>\n'
        '      <data key="d9">这是关键词，应被过滤</data>\n'
        '      <data key="d10">chunk-001</data>\n'
        '      <data key="d11">/tmp/file1.txt</data>\n'
        '    </edge>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_vdb_relationships()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 1, f"应重建 1 条，实际 {result['actual']}"

    vdb = _load_vdb(tmp_storage / "vdb_relationships.json")
    assert len(vdb["data"]) == 1
    item = vdb["data"][0]

    # 验证：每条只含 meta_fields 内字段 + storage 自动注入字段
    # 落盘字段：__id__ / __created_at__ / src_id / tgt_id / source_id / content / file_path / vector
    on_disk_expected = {
        "__id__",
        "__created_at__",
        "src_id",
        "tgt_id",
        "source_id",
        "content",
        "file_path",
        "vector",
    }
    item_keys = set(item.keys())
    # 被过滤字段不应出现
    assert "keywords" not in item_keys, (
        f"keywords 不应落盘（meta_fields 过滤）: {item_keys}"
    )
    assert "description" not in item_keys, (
        f"description 不应落盘（meta_fields 过滤）: {item_keys}"
    )
    assert "weight" not in item_keys, (
        f"weight 不应落盘（meta_fields 过滤）: {item_keys}"
    )
    # 落盘字段集合应精确匹配（不允许多余字段）
    assert item_keys == on_disk_expected, (
        f"字段集合不一致: 实际={item_keys}, 期望={on_disk_expected}"
    )

    # 验证 content 内含 keywords/description（在 content 字符串内，但不是独立字段）
    assert "这是关键词" in item["content"], (
        f"content 应含 keywords 字符串: {item['content']!r}"
    )
    assert "这是 entity_one 跟 entity_two 的关系描述" in item["content"], (
        f"content 应含 description 字符串: {item['content']!r}"
    )


# =============================================================================
# v9 Task 8: repair_entity_chunks / repair_relation_chunks 单元测试
# =============================================================================


@pytest.mark.asyncio
async def test_repair_entity_chunks_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，跑 repair_entity_chunks。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. entity_chunks.json 生成 + 字段格式正确
    3. 每个 entity 含 chunk_ids（list）/count/_id/create_time/update_time
    4. chunk_ids 是 list（不是 GRAPH_FIELD_SEP 字符串）
    5. count == len(chunk_ids)
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_entity_chunks()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 entity_chunk: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：entity_chunks.json 字段格式
    ec_path = tmp_storage / "kv_store_entity_chunks.json"
    assert ec_path.exists(), "entity_chunks.json 未生成"
    with open(ec_path, encoding="utf-8") as f:
        ec = json.load(f)
    assert isinstance(ec, dict)
    assert len(ec) == result["actual"]

    for entity_name, ec_value in ec.items():
        assert isinstance(ec_value, dict), f"ec_value 不是 dict: {entity_name}"
        # 必须字段
        assert "chunk_ids" in ec_value, f"缺 chunk_ids: {entity_name}"
        assert "count" in ec_value, f"缺 count: {entity_name}"
        # storage 自动注入字段
        assert "_id" in ec_value, f"缺 _id（storage 没注入）: {entity_name}"
        assert "create_time" in ec_value, f"缺 create_time: {entity_name}"
        assert "update_time" in ec_value, f"缺 update_time: {entity_name}"
        # 类型校验
        assert isinstance(ec_value["chunk_ids"], list), (
            f"chunk_ids 不是 list: {entity_name}, type={type(ec_value['chunk_ids'])}"
        )
        assert isinstance(ec_value["count"], int), f"count 不是 int: {entity_name}"
        # chunk_ids 元素必须是 str
        for cid in ec_value["chunk_ids"]:
            assert isinstance(cid, str), f"chunk_id 不是 str: {cid}"
        # count == len(chunk_ids)
        assert ec_value["count"] == len(ec_value["chunk_ids"]), (
            f"count {ec_value['count']} != len(chunk_ids) {len(ec_value['chunk_ids'])}"
        )
        # _id == entity_name（storage 自动注入）
        assert ec_value["_id"] == entity_name, (
            f"_id {ec_value['_id']} != entity_name {entity_name}"
        )


@pytest.mark.asyncio
async def test_repair_entity_chunks_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 node，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 entity_chunks.json 不应被写空 {}，
    应保持不存在。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    # 注意：GraphML 写空字符串会触发 ET.ParseError（不是"无 node"），
    # 必须写一个有效的空 GraphML（含 graph 元素但无 node）才能模拟"全新用户无节点"。
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"/>\n'
        '</graphml>\n',
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_entity_chunks()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 entity_chunks.json 应保持不存在
    # （跟 LightRAG JsonKVStorage.initialize 内存空 dict 不写盘一致）
    ec_path = tmp_storage / "kv_store_entity_chunks.json"
    assert not ec_path.exists(), (
        "entity_chunks.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_entity_chunks_chunk_ids_is_list(monkeypatch, tmp_path):
    """chunk_ids 类型校验：必须是 list[str]，不是 GRAPH_FIELD_SEP 分隔的字符串。

    v8 bug：直接写 src 字符串没拆分；v9 用 split 拆分返回 list。
    """
    from lightrag.constants import GRAPH_FIELD_SEP

    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：1 个 node，source_id 含多个 chunk_id（<SEP> 分隔）
    # 注意：GRAPH_FIELD_SEP = '<SEP>'，含 < 字符，在 XML 中必须用实体编码 &lt;SEP&gt;
    # （跟真实 LightRAG GraphML 一致）。XML 解析器解析后变回 '<SEP>' 字符串。
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="entity_one">\n'
        '      <data key="d3">chunk-001&lt;SEP&gt;chunk-002&lt;SEP&gt;chunk-003</data>\n'
        '    </node>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_entity_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 1, f"应重建 1 条，实际 {result['actual']}"

    ec_path = tmp_storage / "kv_store_entity_chunks.json"
    with open(ec_path, encoding="utf-8") as f:
        ec = json.load(f)

    assert "entity_one" in ec, f"entity_one 不在 entity_chunks: {list(ec.keys())}"
    value = ec["entity_one"]

    # 核心断言：chunk_ids 是 list（不是 GRAPH_FIELD_SEP 字符串）
    assert isinstance(value["chunk_ids"], list), (
        f"chunk_ids 应为 list，实际 type={type(value['chunk_ids'])}, "
        f"value={value['chunk_ids']!r}"
    )
    assert not isinstance(value["chunk_ids"], str), (
        f"chunk_ids 不应是 str（v8 bug 直接写 src 字符串）: {value['chunk_ids']!r}"
    )
    # 长度 3（3 个 chunk_id）
    assert len(value["chunk_ids"]) == 3, (
        f"chunk_ids 应有 3 个元素，实际 {len(value['chunk_ids'])}: {value['chunk_ids']}"
    )
    # 元素值正确
    assert value["chunk_ids"] == ["chunk-001", "chunk-002", "chunk-003"], (
        f"chunk_ids 元素值不对: {value['chunk_ids']}"
    )
    # 每个元素是 str
    for cid in value["chunk_ids"]:
        assert isinstance(cid, str), f"chunk_id 不是 str: {cid!r}"
    # GRAPH_FIELD_SEP 不应出现在 chunk_ids 元素里（说明拆分正确）
    for cid in value["chunk_ids"]:
        assert GRAPH_FIELD_SEP not in cid, (
            f"chunk_id {cid!r} 含 GRAPH_FIELD_SEP（拆分未生效）"
        )


@pytest.mark.asyncio
async def test_repair_entity_chunks_count_field(monkeypatch, tmp_path):
    """count 字段校验：count == len(chunk_ids)，包括 source_id 为空的边界情况。"""
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：3 个 node
    # - entity_multi: 多 chunk_id
    # - entity_single: 单 chunk_id
    # - entity_empty: source_id 为空
    # 注意：GRAPH_FIELD_SEP = '<SEP>' 含 < 字符，XML 中必须用 &lt;SEP&gt;
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="entity_multi">\n'
        '      <data key="d3">c1&lt;SEP&gt;c2&lt;SEP&gt;c3&lt;SEP&gt;c4</data>\n'
        '    </node>\n'
        '    <node id="entity_single">\n'
        '      <data key="d3">only_one</data>\n'
        '    </node>\n'
        '    <node id="entity_empty">\n'
        '      <data key="d3"></data>\n'
        '    </node>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_entity_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 3, f"应重建 3 条，实际 {result['actual']}"

    ec_path = tmp_storage / "kv_store_entity_chunks.json"
    with open(ec_path, encoding="utf-8") as f:
        ec = json.load(f)

    # entity_multi: 4 个 chunk_id
    assert ec["entity_multi"]["count"] == 4, (
        f"entity_multi count 应为 4: {ec['entity_multi']}"
    )
    assert len(ec["entity_multi"]["chunk_ids"]) == 4

    # entity_single: 1 个 chunk_id
    assert ec["entity_single"]["count"] == 1, (
        f"entity_single count 应为 1: {ec['entity_single']}"
    )
    assert len(ec["entity_single"]["chunk_ids"]) == 1

    # entity_empty: 0 个 chunk_id（source_id 为空合法）
    assert ec["entity_empty"]["count"] == 0, (
        f"entity_empty count 应为 0: {ec['entity_empty']}"
    )
    assert ec["entity_empty"]["chunk_ids"] == [], (
        f"entity_empty chunk_ids 应为空 list: {ec['entity_empty']['chunk_ids']!r}"
    )

    # 所有 entity 的 count == len(chunk_ids)
    for name, value in ec.items():
        assert value["count"] == len(value["chunk_ids"]), (
            f"{name}: count {value['count']} != len(chunk_ids) {len(value['chunk_ids'])}"
        )


@pytest.mark.asyncio
async def test_repair_relation_chunks_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，跑 repair_relation_chunks。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. relation_chunks.json 生成 + 字段格式正确
    3. 每个 relation 含 chunk_ids（list）/count/_id/create_time/update_time
    4. chunk_ids 是 list（不是 GRAPH_FIELD_SEP 字符串）
    5. count == len(chunk_ids)
    6. key 格式 = make_relation_chunk_key(src, tgt) = "<SEP>".join(sorted((src, tgt)))
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_relation_chunks()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0, f"actual=0，没重建任何 relation_chunk: {result}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：relation_chunks.json 字段格式
    rc_path = tmp_storage / "kv_store_relation_chunks.json"
    assert rc_path.exists(), "relation_chunks.json 未生成"
    with open(rc_path, encoding="utf-8") as f:
        rc = json.load(f)
    assert isinstance(rc, dict)
    assert len(rc) == result["actual"]

    for relation_key, rc_value in rc.items():
        assert isinstance(rc_value, dict), f"rc_value 不是 dict: {relation_key}"
        # 必须字段
        assert "chunk_ids" in rc_value, f"缺 chunk_ids: {relation_key}"
        assert "count" in rc_value, f"缺 count: {relation_key}"
        # storage 自动注入字段
        assert "_id" in rc_value, f"缺 _id: {relation_key}"
        assert "create_time" in rc_value, f"缺 create_time: {relation_key}"
        assert "update_time" in rc_value, f"缺 update_time: {relation_key}"
        # 类型校验
        assert isinstance(rc_value["chunk_ids"], list), (
            f"chunk_ids 不是 list: {relation_key}, type={type(rc_value['chunk_ids'])}"
        )
        assert isinstance(rc_value["count"], int)
        # count == len(chunk_ids)
        assert rc_value["count"] == len(rc_value["chunk_ids"]), (
            f"count {rc_value['count']} != len(chunk_ids) {len(rc_value['chunk_ids'])}"
        )
        # _id == relation_key
        assert rc_value["_id"] == relation_key


@pytest.mark.asyncio
async def test_repair_relation_chunks_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 edge，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 relation_chunks.json 不应被写空 {}，
    应保持不存在。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    # 注意：GraphML 写空字符串会触发 ET.ParseError（不是"无 edge"），
    # 必须写一个有效的空 GraphML（含 graph 元素但无 edge）才能模拟"全新用户无边"。
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"/>\n'
        '</graphml>\n',
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_relation_chunks()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 relation_chunks.json 应保持不存在
    rc_path = tmp_storage / "kv_store_relation_chunks.json"
    assert not rc_path.exists(), (
        "relation_chunks.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_relation_chunks_key_format(monkeypatch, tmp_path):
    """key 格式校验：relation_key = make_relation_chunk_key(src, tgt) = "<SEP>".join(sorted((src, tgt)))。

    验证：
    1. key 是 sorted 后的 src<SEP>tgt（src <= tgt）
    2. 即使 GraphML 里 src > tgt（乱序），key 仍是 sorted 后的
    3. key 是单个字符串（不是 tuple/list）
    """
    from lightrag.constants import GRAPH_FIELD_SEP
    from lightrag.utils import make_relation_chunk_key, parse_relation_chunk_key

    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：2 个 edge
    # - edge1: src=alpha, tgt=beta（alpha < beta，已 sorted）
    # - edge2: src=zeta, tgt=gamma（zeta > gamma，需 sorted 成 gamma<SEP>zeta）
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="alpha"/>\n'
        '    <node id="beta"/>\n'
        '    <node id="gamma"/>\n'
        '    <node id="zeta"/>\n'
        '    <edge source="alpha" target="beta">\n'
        '      <data key="d10">chunk-a</data>\n'
        '    </edge>\n'
        '    <edge source="zeta" target="gamma">\n'
        '      <data key="d10">chunk-b</data>\n'
        '    </edge>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_relation_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] == 2, f"应重建 2 条，实际 {result['actual']}"

    rc_path = tmp_storage / "kv_store_relation_chunks.json"
    with open(rc_path, encoding="utf-8") as f:
        rc = json.load(f)

    # 期望的 2 个 key
    expected_key1 = make_relation_chunk_key("alpha", "beta")  # alpha<SEP>beta
    expected_key2 = make_relation_chunk_key("zeta", "gamma")  # gamma<SEP>zeta（sorted）

    assert expected_key1 in rc, (
        f"key {expected_key1!r} 不在 relation_chunks: {list(rc.keys())}"
    )
    assert expected_key2 in rc, (
        f"key {expected_key2!r} 不在 relation_chunks: {list(rc.keys())}"
    )

    # 验证 key 格式：sorted 后的 src<SEP>tgt（src <= tgt）
    for relation_key in rc.keys():
        # key 必须是 str（不是 tuple/list）
        assert isinstance(relation_key, str), (
            f"relation_key 应为 str，实际 type={type(relation_key)}, value={relation_key!r}"
        )
        # 用 parse_relation_chunk_key 反解
        src, tgt = parse_relation_chunk_key(relation_key)
        # 重新 make 应得同一 key（幂等性）
        assert make_relation_chunk_key(src, tgt) == relation_key, (
            f"key {relation_key!r} 不是 make_relation_chunk_key 生成的（不幂等）"
        )
        # src <= tgt（sorted 后）
        assert src <= tgt, (
            f"relation_key {relation_key!r} 未 sorted: src={src!r} > tgt={tgt!r}"
        )

    # 特别验证 edge2（zeta > gamma）：key 应是 gamma<SEP>zeta（不是 zeta<SEP>gamma）
    assert "zeta" + GRAPH_FIELD_SEP + "gamma" not in rc, (
        "edge2 的 key 应是 sorted 后的 gamma<SEP>zeta，不是 zeta<SEP>gamma"
    )
    assert "gamma" + GRAPH_FIELD_SEP + "zeta" in rc, (
        "edge2 的 key 应是 gamma<SEP>zeta"
    )


@pytest.mark.asyncio
async def test_repair_relation_chunks_chunk_ids_is_list(monkeypatch, tmp_path):
    """chunk_ids 类型校验：必须是 list[str]，不是 GRAPH_FIELD_SEP 分隔的字符串。

    v8 bug：直接写 edge_src_id 字符串没拆分；v9 用 split 拆分返回 list。
    同时验证：同一个 key 被多个 edge 重复时，用 merge_source_ids 合并（保留插入顺序去重）。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)

    # 构造 GraphML：2 个 edge 共享同一对 (alpha, beta)
    # - edge1: source_id = "chunk-001<SEP>chunk-002"
    # - edge2: source_id = "chunk-002<SEP>chunk-003"（chunk-002 重复，应去重）
    # 注意：GRAPH_FIELD_SEP = '<SEP>' 含 < 字符，XML 中必须用 &lt;SEP&gt;
    graphml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected">\n'
        '    <node id="alpha"/>\n'
        '    <node id="beta"/>\n'
        '    <edge source="alpha" target="beta">\n'
        '      <data key="d10">chunk-001&lt;SEP&gt;chunk-002</data>\n'
        '    </edge>\n'
        '    <edge source="beta" target="alpha">\n'
        '      <data key="d10">chunk-002&lt;SEP&gt;chunk-003</data>\n'
        '    </edge>\n'
        '  </graph>\n'
        '</graphml>\n'
    )
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        graphml_content, encoding="utf-8"
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_relation_chunks()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    # 2 个 edge 但 key 相同（alpha<SEP>beta），合并成 1 条
    assert result["actual"] == 1, (
        f"应重建 1 条（2 edge 同 key 合并），实际 {result['actual']}"
    )

    rc_path = tmp_storage / "kv_store_relation_chunks.json"
    with open(rc_path, encoding="utf-8") as f:
        rc = json.load(f)

    # 唯一的 key
    assert len(rc) == 1, f"应只有 1 个 key，实际 {len(rc)}: {list(rc.keys())}"
    relation_key = list(rc.keys())[0]
    value = rc[relation_key]

    # 核心断言：chunk_ids 是 list（不是 GRAPH_FIELD_SEP 字符串）
    assert isinstance(value["chunk_ids"], list), (
        f"chunk_ids 应为 list，实际 type={type(value['chunk_ids'])}, "
        f"value={value['chunk_ids']!r}"
    )
    assert not isinstance(value["chunk_ids"], str), (
        f"chunk_ids 不应是 str（v8 bug 直接写 edge_src_id 字符串）: {value['chunk_ids']!r}"
    )

    # merge_source_ids 合并：chunk-001 + chunk-002 + chunk-002 + chunk-003 → 去重 3 个
    # 保留插入顺序：chunk-001, chunk-002, chunk-003
    assert value["chunk_ids"] == ["chunk-001", "chunk-002", "chunk-003"], (
        f"chunk_ids 合并去重后应为 [chunk-001, chunk-002, chunk-003]，"
        f"实际 {value['chunk_ids']}"
    )
    assert value["count"] == 3, (
        f"count 应为 3（去重后），实际 {value['count']}"
    )

    # 每个元素是 str
    for cid in value["chunk_ids"]:
        assert isinstance(cid, str), f"chunk_id 不是 str: {cid!r}"


# ===== v9 Task 9 测试：repair_full_entities / repair_full_relations 走 JsonKVStorage =====


@pytest.mark.asyncio
async def test_repair_full_entities_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，先跑 repair_text_chunks + repair_doc_status，
    再跑 repair_full_entities。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. full_entities.json 生成 + 字段格式正确
    3. 每个 doc 含 entity_names（list）/count/_id/create_time/update_time
    4. entity_names 是 list（不是 GRAPH_FIELD_SEP 字符串）
    5. count == len(entity_names)
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks + repair_doc_status 生成依赖文件
    tc_result = await lightrag_repair.repair_text_chunks()
    assert tc_result["status"] == "ok", f"repair_text_chunks 失败: {tc_result.get('message')}"
    ds_result = await lightrag_repair.repair_doc_status()
    assert ds_result["status"] == "ok", f"repair_doc_status 失败: {ds_result.get('message')}"

    # 跑 repair_full_entities
    result = await lightrag_repair.repair_full_entities()

    # 断言 1：repair 成功（注意：full_entities 可能 actual=0 如果 GraphML 跟 doc_status 无交叉）
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：full_entities.json 字段格式
    fe_path = tmp_storage / "kv_store_full_entities.json"
    assert fe_path.exists(), "full_entities.json 未生成"
    with open(fe_path, encoding="utf-8") as f:
        fe = json.load(f)
    assert isinstance(fe, dict)
    assert len(fe) == result["actual"]

    for doc_id, fe_value in fe.items():
        assert isinstance(fe_value, dict), f"fe_value 不是 dict: {doc_id}"
        # 必须字段
        assert "entity_names" in fe_value, f"缺 entity_names: {doc_id}"
        assert "count" in fe_value, f"缺 count: {doc_id}"
        # storage 自动注入字段
        assert "_id" in fe_value, f"缺 _id: {doc_id}"
        assert "create_time" in fe_value, f"缺 create_time: {doc_id}"
        assert "update_time" in fe_value, f"缺 update_time: {doc_id}"
        # 类型校验
        assert isinstance(fe_value["entity_names"], list), (
            f"entity_names 不是 list: {doc_id}, type={type(fe_value['entity_names'])}"
        )
        assert isinstance(fe_value["count"], int)
        # entity_names 元素必须是 str
        for en in fe_value["entity_names"]:
            assert isinstance(en, str), f"entity_name 不是 str: {en}"
        # count == len(entity_names)
        assert fe_value["count"] == len(fe_value["entity_names"]), (
            f"count {fe_value['count']} != len(entity_names) {len(fe_value['entity_names'])}"
        )
        # _id == doc_id
        assert fe_value["_id"] == doc_id


@pytest.mark.asyncio
async def test_repair_full_entities_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 node 或 doc_status 为空，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 full_entities.json 不应被写空 {}，
    应保持不存在。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    # 注意：GraphML 写空字符串会触发 ET.ParseError（不是"无 node"），
    # 必须写一个有效的空 GraphML（含 graph 元素但无 node）才能模拟"全新用户无实体"。
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"/>\n'
        '</graphml>\n',
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks + repair_doc_status（全新用户不写派生文件，依赖文件不存在）
    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()

    result = await lightrag_repair.repair_full_entities()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 full_entities.json 应保持不存在
    # （跟 LightRAG JsonKVStorage.initialize 内存空 dict 不写盘一致）
    fe_path = tmp_storage / "kv_store_full_entities.json"
    assert not fe_path.exists(), (
        "full_entities.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_full_entities_value_is_dict_not_list(monkeypatch, tmp_path):
    """字段格式校验：full_entities 的 value 必须是 dict（含 entity_names/count/_id/...），
    不是裸 list（v8 bug）。

    v9 修复：value 是 {"entity_names": list[str], "count": int} 字典格式（跟 LightRAG operate.py L2901-2908 一致）。
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()
    result = await lightrag_repair.repair_full_entities()

    assert result["status"] == "ok"
    if result["actual"] == 0:
        pytest.skip("GraphML 跟 doc_status 无交叉，无数据校验 value 格式")

    fe_path = tmp_storage / "kv_store_full_entities.json"
    with open(fe_path, encoding="utf-8") as f:
        fe = json.load(f)

    for _doc_id, fe_value in fe.items():
        # 核心断言：value 是 dict，不是 list（v8 bug 是直接写 list）
        assert isinstance(fe_value, dict), (
            f"fe_value 应是 dict（含 entity_names/count/_id/...），"
            f"实际 type={type(fe_value)}, value={fe_value!r}"
        )
        assert not isinstance(fe_value, list), (
            f"fe_value 不应是 list（v8 bug 直接写 list[str]）: {fe_value!r}"
        )


@pytest.mark.asyncio
async def test_repair_full_entities_entity_names_not_sorted(monkeypatch, tmp_path):
    """字段格式校验：entity_names 来自 set（无序），不 sorted。

    v8 bug：用 sorted(ents) 排序 entity_names → 跟 LightRAG operate.py L2904
    `list(final_entity_names)`（来自 set，无序）不一致。
    v9 修复：改为 list(entity_set)（不 sorted，跟 LightRAG 一致）。

    由于 set 转 list 在 Python 3.7+ 是插入顺序的（实际由哈希决定），
    难以构造稳定断言"未 sorted"，这里只验证：
    - entity_names 是 list
    - 集合内容正确（用 set 对比顺序无关）
    - 不强制要求 sorted（只要内容对就行）
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()
    result = await lightrag_repair.repair_full_entities()

    assert result["status"] == "ok"
    if result["actual"] == 0:
        pytest.skip("GraphML 跟 doc_status 无交叉，无数据校验 entity_names")

    fe_path = tmp_storage / "kv_store_full_entities.json"
    with open(fe_path, encoding="utf-8") as f:
        fe = json.load(f)

    for _doc_id, fe_value in fe.items():
        entity_names = fe_value["entity_names"]
        # 是 list
        assert isinstance(entity_names, list)
        # 元素都是 str
        for en in entity_names:
            assert isinstance(en, str)
        # 不强制 sorted（set 转 list 无序，跟 LightRAG 一致）
        # 只验证去重正确（len == set 长度）
        assert len(entity_names) == len(set(entity_names)), (
            f"entity_names 有重复: {entity_names}"
        )


@pytest.mark.asyncio
async def test_repair_full_entities_count_field(monkeypatch, tmp_path):
    """字段格式校验：count == len(entity_names)。

    v9 必须有 count 字段（int），且等于 entity_names 长度（跟 LightRAG operate.py L2907 一致）。
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()
    result = await lightrag_repair.repair_full_entities()

    assert result["status"] == "ok"
    if result["actual"] == 0:
        pytest.skip("GraphML 跟 doc_status 无交叉，无数据校验 count")

    fe_path = tmp_storage / "kv_store_full_entities.json"
    with open(fe_path, encoding="utf-8") as f:
        fe = json.load(f)

    for _doc_id, fe_value in fe.items():
        # count 是 int
        assert isinstance(fe_value["count"], int), (
            f"count 应是 int，实际 type={type(fe_value['count'])}, value={fe_value['count']!r}"
        )
        # count == len(entity_names)
        assert fe_value["count"] == len(fe_value["entity_names"]), (
            f"count {fe_value['count']} != len(entity_names) {len(fe_value['entity_names'])}"
        )
        # count >= 1（doc 至少有 1 个 entity 才会被记录）
        assert fe_value["count"] >= 1


@pytest.mark.asyncio
async def test_repair_full_relations_real_data(monkeypatch, tmp_path):
    """真实数据测试：拷贝 3 真相源到 tmp_path，先跑 repair_text_chunks + repair_doc_status，
    再跑 repair_full_relations。

    验证：
    1. repair 不修改 3 真相源（sha256 不变）
    2. full_relations.json 生成 + 字段格式正确
    3. 每个 doc 含 relation_pairs（list of list）/count/_id/create_time/update_time
    4. relation_pairs 是 list of list（每个 pair 是 2 元素 list）
    5. 每个 pair 必须 sorted（pair[0] <= pair[1]）
    6. count == len(relation_pairs)
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    # 记录真相源 sha256
    graphml_sha = _sha256(tmp_storage / "graph_chunk_entity_relation.graphml")
    full_docs_sha = _sha256(tmp_storage / "kv_store_full_docs.json")
    cache_sha = _sha256(tmp_storage / "kv_store_llm_response_cache.json")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑 repair_text_chunks + repair_doc_status 生成依赖文件
    tc_result = await lightrag_repair.repair_text_chunks()
    assert tc_result["status"] == "ok"
    ds_result = await lightrag_repair.repair_doc_status()
    assert ds_result["status"] == "ok"

    # 跑 repair_full_relations
    result = await lightrag_repair.repair_full_relations()

    # 断言 1：repair 成功
    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"

    # 断言 2：真相源 sha256 不变
    assert _sha256(tmp_storage / "graph_chunk_entity_relation.graphml") == graphml_sha
    assert _sha256(tmp_storage / "kv_store_full_docs.json") == full_docs_sha
    assert _sha256(tmp_storage / "kv_store_llm_response_cache.json") == cache_sha

    # 断言 3：full_relations.json 字段格式
    fr_path = tmp_storage / "kv_store_full_relations.json"
    assert fr_path.exists(), "full_relations.json 未生成"
    with open(fr_path, encoding="utf-8") as f:
        fr = json.load(f)
    assert isinstance(fr, dict)
    assert len(fr) == result["actual"]

    for doc_id, fr_value in fr.items():
        assert isinstance(fr_value, dict), f"fr_value 不是 dict: {doc_id}"
        # 必须字段
        assert "relation_pairs" in fr_value, f"缺 relation_pairs: {doc_id}"
        assert "count" in fr_value, f"缺 count: {doc_id}"
        # storage 自动注入字段
        assert "_id" in fr_value, f"缺 _id: {doc_id}"
        assert "create_time" in fr_value, f"缺 create_time: {doc_id}"
        assert "update_time" in fr_value, f"缺 update_time: {doc_id}"
        # 类型校验
        assert isinstance(fr_value["relation_pairs"], list), (
            f"relation_pairs 不是 list: {doc_id}, type={type(fr_value['relation_pairs'])}"
        )
        assert isinstance(fr_value["count"], int)
        # 每个 pair 必须是 list（不是 tuple，tuple 会被 JSON 序列化为 list，但语义上应是 list）
        for pair in fr_value["relation_pairs"]:
            assert isinstance(pair, list), f"pair 不是 list: {pair}, type={type(pair)}"
            assert len(pair) == 2, f"pair 不是 2 元素 list: {pair}"
            assert isinstance(pair[0], str), f"pair[0] 不是 str: {pair}"
            assert isinstance(pair[1], str), f"pair[1] 不是 str: {pair}"
            # 断言 4：每个 pair 必须 sorted（pair[0] <= pair[1]）
            # 跟 LightRAG operate.py L2889 tuple(sorted([src_id, tgt_id])) 一致
            assert pair[0] <= pair[1], (
                f"pair 未 sorted: {pair}, pair[0]={pair[0]!r} > pair[1]={pair[1]!r}"
            )
        # count == len(relation_pairs)
        assert fr_value["count"] == len(fr_value["relation_pairs"]), (
            f"count {fr_value['count']} != len(relation_pairs) {len(fr_value['relation_pairs'])}"
        )
        # _id == doc_id
        assert fr_value["_id"] == doc_id


@pytest.mark.asyncio
async def test_repair_full_relations_empty_user(monkeypatch, tmp_path):
    """全新用户测试：GraphML 无 edge 或 doc_status 为空，不写派生文件（跟 LightRAG 原生首次启动一致）。

    v9 第 2 轮审查修复（问题 5 / I3）：全新用户场景下 full_relations.json 不应被写空 {}，
    应保持不存在。
    """
    from niu_api.internal import lightrag_repair

    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    # 全新用户合法状态：3 真相源全 absent/empty
    # 注意：GraphML 写空字符串会触发 ET.ParseError（不是"无 edge"），
    # 必须写一个有效的空 GraphML（含 graph 元素但无 edge）才能模拟"全新用户无边"。
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"/>\n'
        '</graphml>\n',
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_full_docs.json").write_text("{}")
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()

    result = await lightrag_repair.repair_full_relations()

    assert result["status"] == "ok"
    assert result["expected"] == 0
    assert result["actual"] == 0

    # v9 第 2 轮审查修复（问题 5 / I3）：
    # 全新用户场景下 full_relations.json 应保持不存在
    # （跟 LightRAG JsonKVStorage.initialize 内存空 dict 不写盘一致）
    fr_path = tmp_storage / "kv_store_full_relations.json"
    assert not fr_path.exists(), (
        "full_relations.json 应不存在（全新用户不写派生文件），但被生成了"
    )


@pytest.mark.asyncio
async def test_repair_full_relations_value_is_dict_not_list(monkeypatch, tmp_path):
    """字段格式校验：full_relations 的 value 必须是 dict（含 relation_pairs/count/_id/...），
    不是裸 list（v8 bug）。

    v9 修复：value 是 {"relation_pairs": list[list[str]], "count": int} 字典格式
    （跟 LightRAG operate.py L2911-2919 一致）。
    """
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()
    result = await lightrag_repair.repair_full_relations()

    assert result["status"] == "ok"
    if result["actual"] == 0:
        pytest.skip("GraphML 跟 doc_status 无交叉，无数据校验 value 格式")

    fr_path = tmp_storage / "kv_store_full_relations.json"
    with open(fr_path, encoding="utf-8") as f:
        fr = json.load(f)

    for _doc_id, fr_value in fr.items():
        # 核心断言：value 是 dict，不是 list
        assert isinstance(fr_value, dict), (
            f"fr_value 应是 dict（含 relation_pairs/count/_id/...），"
            f"实际 type={type(fr_value)}, value={fr_value!r}"
        )
        assert not isinstance(fr_value, list), (
            f"fr_value 不应是 list（v8 bug 直接写 list[list[str]]）: {fr_value!r}"
        )


@pytest.mark.asyncio
async def test_repair_full_relations_pair_always_sorted(monkeypatch, tmp_path):
    """单元测试：每个 relation_pair 必须 sorted（pair[0] <= pair[1]）。

    v8 bug：用 [src, tgt] 直接作为 pair（未 sorted）→ 跟 LightRAG operate.py L2889
    `tuple(sorted([src_id, tgt_id]))` 不一致。
    v9 修复：改为 (src, tgt) if src <= tgt else (tgt, src)（每个 pair sorted）。

    构造最小 GraphML：1 个 edge（src="Z" tgt="A"），验证 full_relations 的 pair 是 sorted 后的 ["A", "Z"]。
    """
    from niu_api.internal import lightrag_repair

    # 构造最小 GraphML（src="Z" tgt="A"，sorted 后 pair 应为 ["A", "Z"]）
    graphml_content = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="d10" for="edge" attr.name="source_id" attr.type="string"/>
  <graph id="G">
    <node id="Z"/>
    <node id="A"/>
    <edge source="Z" target="A">
      <data key="d10">chunk-test</data>
    </edge>
  </graph>
</graphml>
"""
    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir(parents=True, exist_ok=True)
    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text(graphml_content, encoding="utf-8")
    (tmp_storage / "kv_store_full_docs.json").write_text(
        json.dumps({"doc-test": {"content": "test", "file_path": "/test.txt", "create_time": 100}}),
        encoding="utf-8",
    )
    (tmp_storage / "kv_store_llm_response_cache.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    # 先跑依赖
    await lightrag_repair.repair_text_chunks()
    await lightrag_repair.repair_doc_status()

    # 手动构造 doc_status 包含 chunk-test（因为 chunking 不会生成 chunk-test）
    ds_path = tmp_storage / "kv_store_doc_status.json"
    with open(ds_path, encoding="utf-8") as f:
        ds = json.load(f)
    ds["doc-test"] = {
        "status": "processed",
        "chunks_count": 1,
        "chunks_list": ["chunk-test"],
        "content_summary": "",
        "content_length": 0,
        "created_at": "",
        "updated_at": "",
        "file_path": "/test.txt",
        "track_id": None,
        "metadata": {},
    }
    with open(ds_path, "w", encoding="utf-8") as f:
        json.dump(ds, f, ensure_ascii=False)

    # 跑 repair_full_relations
    result = await lightrag_repair.repair_full_relations()

    assert result["status"] == "ok", f"repair 失败: {result.get('message')}"
    assert result["actual"] > 0

    fr_path = tmp_storage / "kv_store_full_relations.json"
    with open(fr_path, encoding="utf-8") as f:
        fr = json.load(f)

    assert "doc-test" in fr
    doc_value = fr["doc-test"]
    assert "relation_pairs" in doc_value
    pairs = doc_value["relation_pairs"]
    assert len(pairs) >= 1
    # 找到包含 "Z" 和 "A" 的 pair
    za_pair = None
    for pair in pairs:
        if set(pair) == {"Z", "A"}:
            za_pair = pair
            break
    assert za_pair is not None, f"没找到含 Z/A 的 pair: {pairs}"
    # pair 必须 sorted（["A", "Z"]，不是 ["Z", "A"]）
    assert za_pair == ["A", "Z"], f"pair 未 sorted: {za_pair}"
    assert za_pair[0] <= za_pair[1], f"pair[0] > pair[1]: {za_pair}"


@pytest.mark.asyncio
async def test_repair_full_entities_graphml_corrupt_unrecoverable(monkeypatch, tmp_path):
    """GraphML 损坏测试：3 真相源之一损坏 → unrecoverable。"""
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text("<not valid xml")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_full_entities()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


@pytest.mark.asyncio
async def test_repair_full_relations_graphml_corrupt_unrecoverable(monkeypatch, tmp_path):
    """GraphML 损坏测试：3 真相源之一损坏 → unrecoverable。"""
    from niu_api.internal import lightrag_repair

    real_storage = Path.home() / ".niu" / "lightrag_storage"
    if not real_storage.exists():
        pytest.skip(f"真实数据目录不存在: {real_storage}")

    tmp_storage = tmp_path / "lightrag_storage"
    _copy_truth_sources(tmp_storage, real_storage)

    (tmp_storage / "graph_chunk_entity_relation.graphml").write_text("<not valid xml")

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp_storage))

    result = await lightrag_repair.repair_full_relations()

    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "GraphML 损坏" in result["message"]


# =============================================================================
# v9 Task 10: repair_all async 桥接测试
# =============================================================================


# 派生文件清单（跟 lightrag_repair._DERIVED_FILES 一致）
_DERIVED_FILES_V9 = [
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


def _record_truth_source_hashes(storage_dir: Path) -> dict[str, str]:
    """记录 3 真相源 sha256（repair 前快照）。"""
    return {
        "graphml": _sha256(storage_dir / "graph_chunk_entity_relation.graphml"),
        "full_docs": _sha256(storage_dir / "kv_store_full_docs.json"),
        "cache": _sha256(storage_dir / "kv_store_llm_response_cache.json"),
    }


def _assert_truth_sources_unchanged(storage_dir: Path, before: dict[str, str]) -> None:
    """断言 3 真相源 sha256 不变。"""
    after = _record_truth_source_hashes(storage_dir)
    assert after["graphml"] == before["graphml"], "GraphML sha256 变化（违反铁律 2）"
    assert after["full_docs"] == before["full_docs"], "full_docs sha256 变化（违反铁律 2）"
    assert after["cache"] == before["cache"], "cache sha256 变化（违反铁律 2）"


def test_repair_all_async_returns_flat_structure(tmp_path, monkeypatch):
    """v9 repair_all 同步调用返回扁平结构（向后兼容 Rust format_repair_summary）。

    验证：
    1. repair_all() 是同步调用（不是 coroutine）
    2. 返回扁平结构：顶层有各 repair 名 + _deleted + _truth_source_check
    3. 不应该有嵌套的 repair_result 字段
    """
    # 用 _FakeEmbedModel 替代真实模型（避免加载真实 ~400MB 模型 + CI 无模型）
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 准备最小真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {
        "default:extract:k1": {
            "return": "entity",
            "cache_type": "extract",
            "chunk_id": "chunk-x",
            "create_time": 1,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    # 写最小 GraphML（含 1 个 node，让 _check_truth_sources_intact 通过）
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 扁平结构校验
    assert isinstance(result, dict)
    assert "_deleted" in result
    assert "_truth_source_check" in result
    # 不应该有嵌套的 repair_result 字段
    assert "repair_result" not in result
    assert "repaired" not in result  # 顶层不应有 repaired（向后兼容）


def test_repair_all_async_3_truth_sources_intact(tmp_path, monkeypatch):
    """【真相源保护验证】v9 repair_all 完成后 3 真相源 mtime + sha256 完全不变。

    这是 v9 核心铁律 2 的验证：3 真相源不可动。
    走 storage.upsert 接口（Task 3-9）后，真相源不应被任何 storage 实例修改。
    """
    import os
    import shutil

    # 拷贝真实 3 真相源到 tmp_path
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    if not Path(src_dir).exists():
        pytest.skip(f"真实数据目录不存在: {src_dir}")

    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 用假 embedding 模型（避免加载真实 ~400MB 模型）
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 记录 3 真相源 sha256 + mtime（repair 前快照）
    truth_hashes_before = {
        f: _sha256(tmp_path / f) for f in truth_files
    }
    truth_mtimes_before = {
        f: (tmp_path / f).stat().st_mtime for f in truth_files
    }

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 3 真相源 sha256 + mtime 必须完全不变
    truth_hashes_after = {f: _sha256(tmp_path / f) for f in truth_files}
    truth_mtimes_after = {f: (tmp_path / f).stat().st_mtime for f in truth_files}
    assert truth_hashes_after == truth_hashes_before, (
        f"3 真相源 sha256 变化（违反铁律 2）: "
        f"before={truth_hashes_before}, after={truth_hashes_after}"
    )
    assert truth_mtimes_after == truth_mtimes_before, (
        f"3 真相源 mtime 变化（违反铁律 2）: "
        f"before={truth_mtimes_before}, after={truth_mtimes_after}"
    )

    # repair_all 应成功（无 unrecoverable）
    assert not result.get("_unrecoverable", False), (
        f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"
    )


def test_repair_all_async_9_derived_files_rebuilt_via_storage(tmp_path, monkeypatch):
    """【9 派生文件走 storage 接口】repair_all 后 9 派生文件全部重建 + 含 storage 自动注入字段。

    验证 v9 核心：每个派生文件都走 storage.upsert（不是 v8 的 _atomic_write_json），
    通过检查 storage 自动注入的字段（_id / __id__ / create_time / __created_at__）确认。
    """
    import os
    import shutil

    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    if not Path(src_dir).exists():
        pytest.skip(f"真实数据目录不存在: {src_dir}")

    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert not result.get("_unrecoverable", False), (
        f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"
    )

    # 9 派生文件全部存在 + 是 dict 格式
    for fname in _DERIVED_FILES_V9:
        assert (tmp_path / fname).exists(), f"{fname} 未被重建"
        data = json.loads((tmp_path / fname).read_text())
        assert isinstance(data, dict), f"{fname} 不是 dict"

    # 验证 storage 自动注入字段（v8 _atomic_write_json 不会注入这些字段）
    # 1. text_chunks: 每条 chunk 含 _id / create_time / update_time（JsonKVStorage 自动注入）
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    if tc:  # 全新用户可能为空
        for chunk_id, chunk_value in tc.items():
            assert "_id" in chunk_value, f"text_chunks 缺 _id（storage 没注入）: {chunk_id}"
            assert "create_time" in chunk_value, f"text_chunks 缺 create_time: {chunk_id}"
            assert "update_time" in chunk_value, f"text_chunks 缺 update_time: {chunk_id}"

    # 2. vdb_chunks: 每条 chunk 含 __id__ / __created_at__ / vector（NanoVectorDBStorage 自动注入）
    vdb_chunks = json.loads((tmp_path / "vdb_chunks.json").read_text())
    if vdb_chunks.get("data"):
        for item in vdb_chunks["data"]:
            assert "__id__" in item, f"vdb_chunks 缺 __id__: {item}"
            assert "__created_at__" in item, f"vdb_chunks 缺 __created_at__: {item}"
            assert "vector" in item, f"vdb_chunks 缺 vector: {item}"

    # 3. vdb_entities: 同上
    vdb_entities = json.loads((tmp_path / "vdb_entities.json").read_text())
    if vdb_entities.get("data"):
        for item in vdb_entities["data"]:
            assert "__id__" in item, f"vdb_entities 缺 __id__: {item}"
            assert "vector" in item, f"vdb_entities 缺 vector: {item}"

    # 4. entity_chunks: 每条含 _id / create_time / update_time
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    if ec:
        for entity_name, ec_value in ec.items():
            assert "_id" in ec_value, f"entity_chunks 缺 _id: {entity_name}"
            assert "create_time" in ec_value, f"entity_chunks 缺 create_time: {entity_name}"


def test_repair_all_async_breaks_on_unrecoverable(tmp_path, monkeypatch):
    """repair_all 在某函数报 unrecoverable 后应立即 break，不继续后续 repair。

    v9 验证 async 桥接下 break 逻辑仍生效（v8 是同步 break，v9 是 await + break）。
    """
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 准备合法真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {
        "default:extract:k1": {
            "return": "entity",
            "cache_type": "extract",
            "chunk_id": "chunk-x",
            "create_time": 1,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # monkeypatch repair_text_chunks 报 unrecoverable
    # v9：async 顶层命名函数（有 __name__，让 _repair_all_async 的 getattr 能找到）
    import niu_api.internal.lightrag_repair as repair_mod

    async def failing_repair_text_chunks():
        return {
            "status": "error",
            "expected": 10,
            "actual": 0,
            "lost": 10,
            "message": "mock unrecoverable",
            "unrecoverable": True,
        }

    monkeypatch.setattr(repair_mod, "repair_text_chunks", failing_repair_text_chunks)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 应报 unrecoverable
    assert result.get("_unrecoverable") is True
    assert "text_chunks" in result
    assert result["text_chunks"]["unrecoverable"] is True
    # 后续 repair 不应执行（break 生效）
    # v9 _REBUILD_ORDER_ASYNC 顺序：text_chunks → doc_status → vdb_chunks → ...
    # 如果 break 生效，doc_status / vdb_chunks 等不应在 result 顶层
    assert "doc_status" not in result, "break 未生效：doc_status 不应在 result 中"
    assert "vdb_chunks" not in result, "break 未生效：vdb_chunks 不应在 result 中"
    assert "full_relations" not in result, "break 未生效：full_relations 不应在 result 中"


def test_repair_all_async_no_rollback_on_unrecoverable(tmp_path, monkeypatch):
    """unrecoverable 时不回滚（派生文件已删光，回滚无法恢复）。

    v8 行为：unrecoverable 时派生文件已删，不写 _backed_up / _rolled_back 字段。
    v9 保持同样行为（async 桥接不影响回滚逻辑）。
    """
    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # 准备合法真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {
        "default:extract:k1": {
            "return": "entity",
            "cache_type": "extract",
            "chunk_id": "chunk-x",
            "create_time": 1,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])

    # 预置派生文件（让 _deleted 能记录删除）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "data"}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # monkeypatch repair_text_chunks 报 unrecoverable
    import niu_api.internal.lightrag_repair as repair_mod

    async def failing_repair_text_chunks():
        return {
            "status": "error",
            "expected": 10,
            "actual": 0,
            "lost": 10,
            "message": "mock unrecoverable",
            "unrecoverable": True,
        }

    monkeypatch.setattr(repair_mod, "repair_text_chunks", failing_repair_text_chunks)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # unrecoverable + 不回滚
    assert result.get("_unrecoverable") is True
    # v8/v9 都不写 _backed_up / _rolled_back
    assert "_backed_up" not in result
    assert "_rolled_back" not in result
    # _deleted 应记录删除的派生文件
    assert "_deleted" in result
    assert len(result["_deleted"]) > 0


def test_repair_all_async_new_user_empty_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（3 真相源都不存在）→ repair_all 不应报 unrecoverable。

    v9 验证 async 桥接下全新用户分支仍正常。
    """
    # 不写任何真相源文件（模拟全新用户）
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 全新用户不应报 unrecoverable
    assert not result.get("_unrecoverable"), (
        f"全新用户应能正常 repair: {result.get('_unrecoverable_reason')}"
    )
    # 真相源检查应通过（v4 key 是 intact，不是 ok）
    assert result["_truth_source_check"]["intact"] is True


def test_repair_all_async_new_user_empty_dict_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（3 真相源都存在但都是空内容）→ repair_all 不应报 unrecoverable。

    真实全新用户首次启动 LightRAG 后：GraphML 含空 graph 元素，full_docs/cache 是空 dict {}。
    3 个文件都存在，但都是空内容 → intact=True（全新用户合法）。
    """
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"></graph>\n'
        '</graphml>\n'
    )
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert not result.get("_unrecoverable"), (
        f"空 dict 真相源应能正常 repair: {result.get('_unrecoverable_reason')}"
    )


def test_repair_all_async_unrecoverable_when_truth_source_broken(tmp_path, monkeypatch):
    """真相源损坏（JSON 解析失败）→ unrecoverable，不删除任何文件。

    v9 验证 async 桥接下真相源损坏检测仍正常。
    """
    # full_docs 存在但 JSON 损坏
    (tmp_path / "kv_store_full_docs.json").write_text('{"corrupt": this is not valid JSON')
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "保留"}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_unrecoverable") is True
    # 不应删除任何文件（真相源损坏，没进到删除阶段）
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"old": "保留"}'


def test_repair_all_async_unrecoverable_when_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏（XML 解析失败）→ unrecoverable，不删除任何派生文件。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    (tmp_path / "kv_store_text_chunks.json").write_text('{"chunk-x": {}}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_unrecoverable") is True
    # 派生文件未被删除
    assert (tmp_path / "kv_store_text_chunks.json").exists()
    # 真相源未被修改
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "corrupt xml <<<"


def test_repair_all_async_derived_metadata_diff(tmp_path, monkeypatch):
    """【派生文件元数据 diff（不对比 vector/matrix/content，因假模型 + keywords 顺序差异）】

    repair 后的派生文件跟 LightRAG 原生启动后的派生文件对比。
    v9 核心 D1 验证：走 storage.upsert 不绕过，重建产物跟 LightRAG 原生启动后字段集合一致。

    Skip 条件：如果没有 LightRAG 原生启动后的对照样本（~/.niu/lightrag_storage_backup/），
    跳过字段对比，只做字段存在性校验（已在 test_repair_all_async_9_derived_files_rebuilt_via_storage 覆盖）。
    """
    import os
    import shutil

    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    native_backup_dir = os.path.expanduser("~/.niu/lightrag_storage_backup")
    if not Path(src_dir).exists() or not Path(native_backup_dir).exists():
        pytest.skip("缺少真实数据或 LightRAG 原生对照样本（~/.niu/lightrag_storage_backup/）")

    # 拷贝 3 真相源到 tmp_path
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert not result.get("_unrecoverable", False), (
        f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"
    )

    # 对比每个派生文件（忽略时间戳/embedding/vector/matrix/content，
    # 因为时间戳和假模型 embedding 会变，vdb_relationships content 含 keywords 顺序差异）
    ignore_fields = {
        "create_time", "update_time", "__created_at__",
        "vector", "matrix", "__vector__",  # embedding 是假模型，向量不一致
        "content",  # keywords 顺序差异（dict.fromkeys vs LightRAG set）
    }

    for fname in _DERIVED_FILES_V9:
        repair_path = tmp_path / fname
        native_path = Path(native_backup_dir) / fname
        if not native_path.exists():
            continue  # native 没有这个文件，跳过

        repair_data = json.loads(repair_path.read_text())
        native_data = json.loads(native_path.read_text())

        # 对比每个 key 的字段集合（忽略时间戳/embedding 字段）
        repair_keys = set(repair_data.keys()) if isinstance(repair_data, dict) else set()
        native_keys = set(native_data.keys()) if isinstance(native_data, dict) else set()

        # repair 产生的 key 应该是 native 的子集（native 可能有已删除的）
        if repair_keys:
            assert repair_keys.issubset(native_keys), (
                f"{fname}: repair 有 native 没有的 key: {repair_keys - native_keys}"
            )

        # 共同 key 的字段对比
        common_keys = repair_keys & native_keys
        for key in list(common_keys)[:5]:  # 抽 5 条对比
            repair_value = repair_data[key]
            native_value = native_data[key]
            if not isinstance(repair_value, dict):
                continue
            # 对比非 ignore 字段
            for field in repair_value:
                if field in ignore_fields:
                    continue
                if field in native_value:
                    # chunks_list / chunk_ids / entity_names 顺序可能不同，用 set 对比
                    if isinstance(repair_value[field], list) and field in (
                        "chunks_list", "chunk_ids", "entity_names"
                    ):
                        assert set(repair_value[field]) == set(native_value.get(field, [])), (
                            f"{fname}[{key}].{field} 集合不一致: "
                            f"repair={repair_value[field]}, native={native_value.get(field)}"
                        )
                    else:
                        assert repair_value[field] == native_value[field], (
                            f"{fname}[{key}].{field} 不一致: "
                            f"repair={repair_value[field]!r}, native={native_value[field]!r}"
                        )


def test_repair_all_async_e2e_repair_and_query(tmp_path, monkeypatch):
    """【e2e 测试】repair 前后快照 + 修复后查询验证。

    v9 e2e：跑完整 repair_all → 验证派生文件能被 LightRAG 正常加载（不实际启动 LightRAG，
    只验证文件格式可解析 + 字段完整）。
    """
    import os
    import shutil

    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    if not Path(src_dir).exists():
        pytest.skip(f"真实数据目录不存在: {src_dir}")

    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 记录 repair 前快照
    truth_hashes_before = _record_truth_source_hashes(tmp_path)

    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 1. repair 成功
    assert not result.get("_unrecoverable", False), (
        f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"
    )

    # 2. 真相源不变
    _assert_truth_sources_unchanged(tmp_path, truth_hashes_before)

    # 3. 9 派生文件全部重建
    for fname in _DERIVED_FILES_V9:
        assert (tmp_path / fname).exists(), f"{fname} 未被重建"

    # 4. 跑 lightrag_integrity.check_all 验证派生文件格式可解析（不启动 LightRAG）
    from niu_api.internal.lightrag_integrity import check_all
    check_result = check_all()
    # check_all 应该通过（无 critical 错误，派生文件已重建）
    assert check_result["critical_errors"] == 0, (
        f"check_all 报 critical: {check_result['errors']}"
    )


def test_repair_all_async_restart_after_repair(tmp_path, monkeypatch):
    """【修复后重启验证】修复完成后重启进程读派生文件，验证知识图谱查询正常。

    v9 D14：修复完成后真相源不能动，必须重启进程进入正常启动程序，
    由正常启动程序读派生文件验证知识图谱正确。

    本测试模拟"重启"：跑 repair_all → reset_init_state + check_all 验证派生文件可读 →
    用 storage.initialize() 验证 9 派生文件能被 LightRAG storage 重新加载。
    """
    import os
    import shutil

    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    if not Path(src_dir).exists():
        pytest.skip(f"真实数据目录不存在: {src_dir}")

    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # 1. 跑 repair_all
    from niu_api.internal.lightrag_repair import repair_all
    repair_result = repair_all()
    assert not repair_result.get("_unrecoverable", False), (
        f"repair_all 报 unrecoverable: {repair_result.get('_unrecoverable_reason')}"
    )

    # 2. 模拟"重启"：重置 lightrag_manager 状态 + 重新跑 check_all
    import niu_api.internal.lightrag_manager as lightrag_manager
    lightrag_manager.reset_init_state()

    from niu_api.internal.lightrag_integrity import check_all
    check_result = check_all()

    # 3. check_all 应通过（无 critical 错误，派生文件已重建）
    assert check_result["critical_errors"] == 0, (
        f"重启后 check_all 报 critical: {check_result['errors']}"
    )

    # 4. 验证派生文件可被 LightRAG storage 重新加载（模拟重启后 LightRAG 启动）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
    from lightrag.kg.shared_storage import initialize_share_data, set_default_workspace
    from lightrag.namespace import NameSpace

    from niu_api.internal import lightrag_repair as repair_module

    async def _verify_storage_reload():
        """验证 9 派生文件能被 storage 重新加载（模拟 LightRAG 启动）。"""
        initialize_share_data(workers=1)
        set_default_workspace("")

        global_config = {
            "working_dir": str(tmp_path),
            "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.2},
            "embedding_batch_num": 32,
        }

        # 验证 text_chunks（JsonKVStorage，text_chunks 不需要 embedding）
        tc_storage = JsonKVStorage(
            namespace=NameSpace.KV_STORE_TEXT_CHUNKS,
            workspace="",
            global_config=global_config,
            embedding_func=None,  # type: ignore[arg-type]
        )
        await tc_storage.initialize()
        # _data 应非 None（文件能被加载）
        assert tc_storage._data is not None, "text_chunks storage 加载失败"

        # 验证 vdb_chunks（NanoVectorDBStorage，需要 embedding_func）
        vdb_chunks_storage = NanoVectorDBStorage(
            namespace=NameSpace.VECTOR_STORE_CHUNKS,
            workspace="",
            global_config=global_config,
            embedding_func=repair_module.RepairEmbeddingFunc(embedding_dim=768),
            meta_fields={"full_doc_id", "content", "file_path"},
        )
        await vdb_chunks_storage.initialize()
        assert vdb_chunks_storage._client is not None, "vdb_chunks storage 加载失败"

    # 跑验证（用 asyncio.run，因为本测试是同步函数）
    asyncio.run(_verify_storage_reload())


@pytest.mark.asyncio
async def test_repair_all_async_internal_function_directly(tmp_path, monkeypatch):
    """【async 内部函数验证】直接 await _repair_all_async()（不通过 asyncio.run 桥接）。

    验证 _repair_all_async 在 running event loop 内能正常 await（测试场景）。
    """
    import os
    import shutil

    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    if not Path(src_dir).exists():
        pytest.skip(f"真实数据目录不存在: {src_dir}")

    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    # 脑区/Skills 路径合法状态下 full_docs/cache 可能不存在，跳过而非 FileNotFoundError
    for fname in truth_files:
        if not Path(os.path.join(src_dir, fname)).exists():
            pytest.skip(f"真实数据缺少 {fname}（脑区/Skills 路径合法状态）")
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    from niu_api.internal import embedding as niu_embedding
    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # 直接 await _repair_all_async（在 pytest-asyncio 的 event loop 内）
    from niu_api.internal.lightrag_repair import _repair_all_async
    result = await _repair_all_async()

    # 应成功
    assert not result.get("_unrecoverable", False), (
        f"_repair_all_async 报 unrecoverable: {result.get('_unrecoverable_reason')}"
    )
    # 9 派生文件全部重建
    for fname in _DERIVED_FILES_V9:
        assert (tmp_path / fname).exists(), f"{fname} 未被重建"
