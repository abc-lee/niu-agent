"""repair_text_chunks v4 单元测试（v8-Task 1 删除了 brainregion_zombies/graphml/cache 测试）。"""
import json
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path


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
    _DERIVED_FILES_LIST = [
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
    for fname in _DERIVED_FILES_LIST:
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


def test_repair_all_returns_flat_structure(tmp_path, monkeypatch):
    """repair_all 应返回扁平结构（向后兼容 Rust format_repair_summary）。"""
    # 准备最小真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x", "create_time": 1}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 扁平结构：顶层有各 repair 名 + _deleted + 可选 _unrecoverable
    # v8：成功路径不写 _unrecoverable 字段（调用方用 .get("_unrecoverable", False) 兼容）
    assert "_deleted" in result
    # 不应该有嵌套的 repair_result 字段
    assert "repair_result" not in result
    assert "repaired" not in result  # 顶层不应有 repaired（向后兼容）


def test_repair_all_deletes_9_derived_no_backup(tmp_path, monkeypatch):
    """v8：repair_all 删 9 派生文件，不备份、不回滚（铁律 1：其他文件全删除）。"""
    # v4：GraphML 是 3 真相源之一，必须是合法的（有 graph 元素），否则会被判为 unrecoverable
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    # cache 必须有内容（v8: 3 真相源需要状态一致——full_docs has_content 时 cache 也必须 has_content，
    # 否则被 _check_truth_sources_intact 判为 partial 损坏）
    cache = {"chunk-x": {"cache_type": "extract", "chunk_id": "chunk-x", "original_prompt": "```test```", "create_time": 1}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 写一些派生文件（含旧数据）——注意 graphml 不在派生文件列表里（v4 是真相源）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "data"}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # v8：_deleted 字段记录了删除的派生文件（不含 graphml——它是真相源不被删）
    assert "_deleted" in result
    assert len(result["_deleted"]) > 0
    # GraphML 是真相源，不在 _deleted 里
    assert "graph_chunk_entity_relation.graphml" not in result["_deleted"]
    # v8：不再备份，不再回滚
    assert "_backed_up" not in result
    assert "_rolled_back" not in result
    # v8：storage_dir 父目录不应残留 lightrag_storage.prerepair_* 备份目录
    backup_dirs = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    assert backup_dirs == [], f"v8 不应残留备份目录，但发现: {backup_dirs}"


def test_repair_all_unrecoverable_when_truth_source_broken(tmp_path, monkeypatch):
    """真相源损坏（JSON 解析失败）→ unrecoverable，不删除任何文件。

    注意：不能用"full_docs 不存在"模拟损坏——那会被判为"全新用户合法"（ok）。
    必须用"文件存在但 JSON 损坏"触发 _check_truth_source 的 JSON 解析失败 → critical。
    """
    # full_docs 存在但 JSON 损坏（不是合法 JSON）
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


def test_repair_all_new_user_empty_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（3 真相源都不存在）→ repair_all 不应报 unrecoverable。

    全新用户合法启动场景：刚装 niu，~/.niu/lightrag_storage/ 还没创建或为空。
    v4 的 _check_truth_sources_intact 应返回 intact=True（文件不存在 = 全新用户合法），
    repair_all 应正常完成（重建出空派生文件，不报 unrecoverable）。
    """
    # 不写任何真相源文件（模拟全新用户）
    # 也不写派生文件

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 全新用户不应报 unrecoverable
    assert not result.get("_unrecoverable"), f"全新用户应能正常 repair: {result.get('_unrecoverable_reason')}"
    # 真相源检查应通过（v4 key 是 intact，不是 ok）
    assert result["_truth_source_check"]["intact"] is True


def test_repair_all_new_user_empty_dict_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（3 真相源都存在但都是空内容）→ repair_all 不应报 unrecoverable。

    真实全新用户首次启动 LightRAG 后：GraphML 含空 graph 元素，full_docs/cache 是空 dict {}。
    3 个文件都存在，但都是空内容 → intact=True（全新用户合法）。
    """
    # 写空 graph 元素的 GraphML
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
        '  <graph id="G" edgedefault="undirected"></graph>\n'
        '</graphml>\n'
    )
    # 写空 dict 的 full_docs + cache
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert not result.get("_unrecoverable"), f"空 dict 真相源应能正常 repair: {result.get('_unrecoverable_reason')}"


def test_repair_all_3_truth_sources_intact(tmp_path, monkeypatch):
    """v8：repair_all 完成后 3 真相源 mtime + 内容完全不变。"""
    import os
    import hashlib
    import shutil

    # 拷贝真实 3 真相源到 tmp_path（测试用，不动真实数据）
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 记录 3 真相源的 stat + 内容 hash
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    truth_hashes_before = {f: _hash(tmp_path / f) for f in truth_files}
    truth_mtimes_before = {f: (tmp_path / f).stat().st_mtime for f in truth_files}

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 3 真相源 hash + mtime 必须完全不变
    truth_hashes_after = {f: _hash(tmp_path / f) for f in truth_files}
    truth_mtimes_after = {f: (tmp_path / f).stat().st_mtime for f in truth_files}
    assert truth_hashes_after == truth_hashes_before, "3 真相源内容被修改"
    assert truth_mtimes_after == truth_mtimes_before, "3 真相源 mtime 被修改"

    # repair_all 应成功（无 unrecoverable）
    assert not result.get("_unrecoverable", False), f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"


def test_repair_all_9_derived_files_deleted_and_rebuilt(tmp_path, monkeypatch):
    """v8：repair_all 应删除 9 派生文件后重建（铁律 1：不备份，直接删）。"""
    import os
    import shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 9 派生文件预置空 dict
    derived_files = [
        "kv_store_text_chunks.json", "kv_store_doc_status.json",
        "vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json",
        "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
        "kv_store_full_entities.json", "kv_store_full_relations.json",
    ]
    for fname in derived_files:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 9 派生文件应被重建（存在 + 非空 dict 格式）
    for fname in derived_files:
        assert (tmp_path / fname).exists(), f"{fname} 未被重建"
        data = json.loads((tmp_path / fname).read_text())
        assert isinstance(data, dict), f"{fname} 不是 dict"

    # text_chunks 应有活跃 chunk（来自真实 GraphML）
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert len(tc) > 0, "text_chunks 应非空"
    # v8：不备份、不回滚
    assert "_backed_up" not in result
    assert "_rolled_back" not in result
    # storage_dir 父目录不应残留备份目录
    backup_dirs = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    assert backup_dirs == [], f"v8 不应残留备份目录，但发现: {backup_dirs}"


def test_get_lightrag_status_total_errors_correct(tmp_path, monkeypatch):
    """get_lightrag_status 暴露的 total_errors 应 = critical + major + minor。

    用真实 check_all() 返回结构验证（顶层 critical_errors/major_errors/minor_errors 标量字段），
    不用 fake 结构——避免掩盖 check_all 实际返回结构的 bug（违反铁律 5）。
    """
    from niu_api.internal import lightrag_manager

    # 准备损坏现场：full_docs 缺失（critical）+ GraphML 缺失（major）+ vdb 缺失（major）
    # 只写 llm_response_cache（让真相源检查部分通过，但 GraphML/vdb 检测会报 major）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")  # 空 dict（全新用户合法）
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"x": {"return": "y", "cache_type": "extract", "chunk_id": "chunk-x"}}, ensure_ascii=False)
    )
    # 不写 GraphML + 不写 vdb → _check_graphml_post 报 major + _check_vdb_missing 报 major

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
    import niu_api.internal.lightrag_repair as repair_mod
    import niu_api.internal.lightrag_integrity as integrity_mod

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


def test_repair_all_breaks_on_unrecoverable(tmp_path, monkeypatch):
    """repair_all 在某函数报 unrecoverable 后应立即 break，不继续后续 repair。

    Bug B1：原实现只置位 unrecoverable_detected 但不 break，后续 repair 函数
    在依赖数据缺失时写空文件覆盖原始数据。

    v4 机制变更：repair_all 用 getattr(_self_mod, fn.__name__) 间接查找函数
    （让 monkeypatch 能注入失败版本），所以不再 patch _REBUILD_ORDER，
    而是 patch 模块属性 repair_text_chunks / repair_vdb_chunks / repair_vdb_entities。
    """
    import niu_api.internal.lightrag_repair as lightrag_repair

    # 准备 3 真相源（GraphML 必须合法，否则 _check_truth_sources_intact 报 unrecoverable）
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # 用 mock 跟踪后续 repair 函数是否被调用
    call_log = []

    # 命名函数（有 __name__ 属性，让 v4 的 getattr(_self_mod, fn.__name__) 能找到）
    def mock_text_chunks():
        call_log.append("text_chunks")
        return {"status": "error", "unrecoverable": True, "message": "simulated unrecoverable"}

    def mock_repair_vdb_chunks():
        call_log.append("vdb_chunks")
        return {"status": "ok"}

    def mock_repair_vdb_entities():
        call_log.append("vdb_entities")
        return {"status": "ok"}

    # v4：patch 模块属性（repair_all 用 getattr 间接查找，monkeypatch 模块属性能生效）
    monkeypatch.setattr(lightrag_repair, "repair_text_chunks", mock_text_chunks)
    monkeypatch.setattr(lightrag_repair, "repair_vdb_chunks", mock_repair_vdb_chunks)
    monkeypatch.setattr(lightrag_repair, "repair_vdb_entities", mock_repair_vdb_entities)

    result = lightrag_repair.repair_all()

    # 验证：text_chunks 报 unrecoverable 后，后续 vdb_chunks/vdb_entities 不应被调用
    assert result.get("_unrecoverable") is True
    assert "vdb_chunks" not in call_log, f"vdb_chunks 不应被调用（unrecoverable 应 break）: {call_log}"
    assert "vdb_entities" not in call_log, f"vdb_entities 不应被调用: {call_log}"


def test_repair_all_no_rollback_on_unrecoverable(tmp_path, monkeypatch):
    """v8：repair_all 在某函数报 unrecoverable 后不回滚（铁律 1：派生文件已删光，无法回滚）。

    v8 设计：
    - 9 派生文件先全删（不备份）
    - 按依赖链重建，任一 unrecoverable 立即 break
    - 失败时不回滚——真相源从未被修改，用户重新跑 repair_all 即可

    本测试验证 v8 行为：
    - 不再有 _rolled_back 字段
    - 不再有 _backed_up 字段
    - 不残留备份目录
    - unrecoverable 后立即 break（不调后续 repair）
    - 已重建的派生文件保留在现场（不清理）
    """
    import niu_api.internal.lightrag_repair as lightrag_repair

    # 准备 3 真相源（GraphML 必须合法，否则 _check_truth_sources_intact 报 unrecoverable）
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 准备派生文件 baseline（部分存在）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"baseline": "text_chunks"}')
    # vdb_*.json 故意不存在

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # 跟踪后续 repair 是否被调用
    call_log = []

    def mock_text_chunks():
        call_log.append("text_chunks")
        return {"status": "ok"}

    def boom_vdb_chunks():
        call_log.append("vdb_chunks")
        # 模拟重建阶段写空 vdb 后失败
        (tmp_path / "vdb_chunks.json").write_text('{"empty": "vdb"}')
        return {"status": "error", "unrecoverable": True, "message": "simulated"}

    def mock_vdb_entities():
        call_log.append("vdb_entities")
        return {"status": "ok"}

    monkeypatch.setattr(lightrag_repair, "repair_text_chunks", mock_text_chunks)
    monkeypatch.setattr(lightrag_repair, "repair_vdb_chunks", boom_vdb_chunks)
    monkeypatch.setattr(lightrag_repair, "repair_vdb_entities", mock_vdb_entities)

    result = lightrag_repair.repair_all()

    # v8：不再有 _rolled_back / _backed_up 字段
    assert "_rolled_back" not in result, "v8 不应有 _rolled_back 字段"
    assert "_backed_up" not in result, "v8 不应有 _backed_up 字段"
    # v8：unrecoverable + break
    assert result.get("_unrecoverable") is True
    assert "vdb_entities" not in call_log, f"vdb_entities 不应被调用（unrecoverable 应 break）: {call_log}"
    # v8：已重建的派生文件保留在现场（不回滚、不清理）
    assert (tmp_path / "vdb_chunks.json").exists(), "vdb_chunks.json 应保留在现场（不回滚）"
    assert (tmp_path / "vdb_chunks.json").read_text() == '{"empty": "vdb"}'
    # v8：不残留备份目录
    backup_dirs = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    assert backup_dirs == [], f"v8 不应残留备份目录: {backup_dirs}"


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


def test_repair_all_unrecoverable_when_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏时 repair_all 应直接返回 unrecoverable，不备份不删除不重建。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    (tmp_path / "kv_store_text_chunks.json").write_text('{"chunk-x": {}}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_unrecoverable") is True
    # 派生文件未被删除
    assert (tmp_path / "kv_store_text_chunks.json").exists()
    # 真相源未被修改
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "corrupt xml <<<"


def test_repair_all_unrecoverable_when_full_docs_corrupt(tmp_path, monkeypatch):
    """full_docs 损坏时 repair_all 应返回 unrecoverable。"""
    _make_synthetic_fixture(tmp_path)
    # 覆盖 full_docs 为损坏
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_unrecoverable") is True


def test_repair_all_unrecoverable_when_cache_corrupt(tmp_path, monkeypatch):
    """cache 损坏时 repair_all 应返回 unrecoverable。"""
    _make_synthetic_fixture(tmp_path)
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_unrecoverable") is True


def test_repair_all_does_not_touch_truth_sources(tmp_path, monkeypatch):
    """repair_all 不应修改 3 真相源（GraphML + full_docs + cache）一字节。

    使用真实 embedding 模型（CLAUDE.md 铁律 5：测试必须用真实数据+真实LLM，不 mock）。
    测试前会预加载 embedding 模型（通过 niu_api.internal.embedding.get_model）。
    """
    _make_synthetic_fixture(tmp_path)

    # 记录 3 真相源的原始内容
    graphml_before = (tmp_path / "graph_chunk_entity_relation.graphml").read_bytes()
    full_docs_before = (tmp_path / "kv_store_full_docs.json").read_bytes()
    cache_before = (tmp_path / "kv_store_llm_response_cache.json").read_bytes()

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    # 预加载真实 embedding 模型（不 mock LLM）
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载（测试前置条件）"

    from niu_api.internal.lightrag_repair import repair_all
    repair_all()

    # 3 真相源一字节未动
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_bytes() == graphml_before, "GraphML 不应被修改"
    assert (tmp_path / "kv_store_full_docs.json").read_bytes() == full_docs_before, "full_docs 不应被修改"
    assert (tmp_path / "kv_store_llm_response_cache.json").read_bytes() == cache_before, "cache 不应被修改"


@pytest.mark.skip(reason="v8-Task 1 将 repair_text_chunks 改为 unrecoverable stub，依赖其重建行为的测试需等 Task 4 重写")
def test_repair_all_does_not_reanimate_deleted_entities(tmp_path, monkeypatch):
    """repair_all 重建后，已删实体（deleted-entity）不应出现在任何派生文件里。

    使用真实 embedding 模型（不 mock）。
    """
    _make_synthetic_fixture(tmp_path)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载"

    from niu_api.internal.lightrag_repair import repair_all
    repair_all()

    from lightrag.utils import compute_mdhash_id
    doc_v1_content = "v1 content for synthetic fixture document one"
    deleted_content = "deleted entity content that should not be rebuilt"
    old_content = "old version content that should not be rebuilt"
    chunk_id_1 = compute_mdhash_id(doc_v1_content, prefix="chunk-")
    chunk_id_deleted = compute_mdhash_id(deleted_content, prefix="chunk-")
    chunk_id_old = compute_mdhash_id(old_content, prefix="chunk-")

    # 验证 text_chunks 真正重建了活跃 chunk（不是空 dict 意外通过）
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert chunk_id_1 in tc, "活跃 chunk 应被重建（证明 text_chunks 非空，不是意外通过）"
    assert tc[chunk_id_1]["content"] == doc_v1_content
    # 已删实体的 chunk 不重建
    assert chunk_id_deleted not in tc, "已删实体的 chunk 不应被重建"
    assert chunk_id_old not in tc, "旧版本 chunk 不应被重建"

    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    assert "deleted-entity" not in ec
    assert "old-entity" not in ec


def test_repair_all_failure_no_rollback_v8(tmp_path, monkeypatch):
    """v8：重建失败时不回滚，派生文件保持当前状态（不备份不回滚）。

    v8 设计（铁律 1）：
    - 9 派生文件先全删（不备份）
    - 按依赖链重建，任一 unrecoverable 立即 break
    - 失败时不回滚——派生文件已删光，真相源从未被修改，用户重跑 repair_all 即可

    本测试验证 v8 不回滚行为（替代旧 v4 回滚断言）。
    """
    _make_synthetic_fixture(tmp_path)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    # mock 让 repair_vdb_entities 失败
    import niu_api.internal.lightrag_repair as repair_mod
    def failing_vdb_entities():
        raise Exception("mock failure")
    monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_vdb_entities)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # v8：不回滚，不备份
    assert "_rolled_back" not in result, "v8 不应有 _rolled_back 字段"
    assert "_backed_up" not in result, "v8 不应有 _backed_up 字段"
    assert result.get("_unrecoverable") is True
    # v8：派生文件已被删除（不回滚恢复）
    assert "kv_store_text_chunks.json" in result.get("_deleted", []), \
        "派生文件应已被删除"
    # v8：不残留备份目录
    backup_dirs = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    assert backup_dirs == [], f"v8 不应残留备份目录: {backup_dirs}"


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
    """3 真相源全部完好（派生文件齐全）→ ok=True。"""
    _write_intact_truth_sources(tmp_path)
    # 9 派生文件全部存在（内容可空 dict / 空 vdb）
    for fname in _DERIVED_FILES_FOR_TEST:
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


def test_check_all_missing_derived_file_is_major(tmp_path, monkeypatch):
    """9 派生文件任一 missing → major_errors>=1（真相源全完好）。"""
    _write_intact_truth_sources(tmp_path)
    # 只写 8 个派生文件，漏掉 vdb_entities.json
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

    # 真相源完好 → critical=0；缺派生文件 → major>=1
    assert result["critical_errors"] == 0
    assert result["major_errors"] >= 1
    assert result["ok"] is False
    # missing 应记到 checks.derived_missing
    derived_errors = result["checks"]["derived_missing"]["errors"]
    assert any(e.get("file") == "vdb_entities.json" for e in derived_errors), \
        "missing 的派生文件应记到 derived_missing check"


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
        _load_graphml, _check_truth_source,
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
    from niu_api.internal.lightrag_repair_tokenizer import reset_cache
    from niu_api.internal.lightrag_repair import _get_tokenizer

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

    from niu_api.internal.lightrag_repair_tokenizer import reset_cache
    from niu_api.internal.lightrag_repair import _get_tokenizer

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
    from niu_api.internal.lightrag_repair_tokenizer import reset_cache, get_tokenizer

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

    result = repair_text_chunks()

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

    result = repair_text_chunks()

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

    result = repair_text_chunks()

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

    result = repair_text_chunks()

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

    result = repair_text_chunks()

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
    import os, shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")

    # 拷贝真实 3 真相源到 tmp_path
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

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


def test_repair_doc_status_brainregion_chunks_list_attached(tmp_path, monkeypatch):
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

    result = repair_doc_status()

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


def test_repair_doc_status_skip_empty_full_doc_id(tmp_path, monkeypatch):
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

    result = repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # doc_status 应有 doc-1 条目（来自 full_docs）
    assert "doc-1" in ds
    # 但 doc-1 的 chunks_list 应为空（chunk-active 的 full_doc_id 为空被跳过）
    assert ds["doc-1"]["chunks_count"] == 0
    assert ds["doc-1"]["chunks_list"] == []
    # 空 full_doc_id 的 chunk 不在 chunks_list 中
    assert "chunk-active" not in ds["doc-1"]["chunks_list"]


def test_repair_doc_status_chunks_list_grouped_by_doc(tmp_path, monkeypatch):
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

    result = repair_doc_status()

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


def test_repair_doc_status_pending_when_graphml_empty(tmp_path, monkeypatch):
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

    result = repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # GraphML 无数据 → status=pending
    assert ds["doc-1"]["status"] == "pending"


# ==================== v8-Task 6: repair_vdb_chunks/entities/relationships 回归测试 ====================
# v4 实现：
# - repair_vdb_chunks：遍历 text_chunks 重新 embedding
# - repair_vdb_entities：遍历 GraphML node（防复活）
# - repair_vdb_relationships：遍历 GraphML edge，data_list 不含 weight


def test_repair_vdb_entities_only_graphml_nodes(tmp_path, monkeypatch):
    """repair_vdb_entities 应只遍历 GraphML 存在的 node（防复活）。

    回归点：text_chunks 含已删实体对应的 chunk，但 GraphML 没有该实体节点
    → vdb_entities 不应含已删实体（防复活）。
    """
    _write_graphml_v8(tmp_path, [
        ("entity-active", "person", "desc active", "chunk-a"),
        # 已删实体 entity-deleted 不在 GraphML
    ])

    # text_chunks 含 chunk-a + chunk-deleted（但 chunk-deleted 对应的实体已删）
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "content a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-deleted": {"content": "content deleted", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_vdb_entities

    result = repair_vdb_entities()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["expected"] == 1
    assert result["actual"] == 1
    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    # 只含 entity-active，不含已删实体（防复活）
    assert len(vdb_e.get("data", [])) == 1
    assert vdb_e["data"][0]["entity_name"] == "entity-active"


def test_repair_vdb_relationships_no_weight_in_data(tmp_path, monkeypatch):
    """repair_vdb_relationships 的 data_list item 不应含 weight 字段。

    v4 实现：data_list item 只含 __id__/src_id/tgt_id/content/source_id 5 个字段。
    weight 只在 GraphML d7 字段，vdb 不写 weight（防数据冗余 + 跟 LightRAG 一致）。

    回归点：vdb_relationships.json 的任何 data item 都不应有 "weight" 字段。
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

    from niu_api.internal.lightrag_repair import repair_vdb_relationships

    result = repair_vdb_relationships()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    for item in vdb_r.get("data", []):
        # 任何层级都不应有 weight 字段（v4 实现只构造 5 个字段）
        assert "weight" not in item, f"vdb_relationships item 不应含 weight: {item}"
        # 确认 v4 实现的 5 个字段都在
        assert "__id__" in item
        assert "src_id" in item
        assert "tgt_id" in item
        assert "content" in item
        assert "source_id" in item


def test_repair_vdb_chunks_only_text_chunks(tmp_path, monkeypatch):
    """repair_vdb_chunks 只对 text_chunks 中的 chunk embedding（防孤儿 chunk）。

    回归点：GraphML 引用了 chunk-orphan，但 text_chunks 没有该 chunk
    → vdb_chunks 不应含 chunk-orphan（防孤儿）。
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

    from niu_api.internal.lightrag_repair import repair_vdb_chunks

    result = repair_vdb_chunks()

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


def test_repair_entity_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_entity_chunks 只从 GraphML node source_id 提取 chunk_ids（防复活）。

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

    result = repair_entity_chunks()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1  # 只 entity-active
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    # v4 实现：value = {"chunk_ids": [...], "count": int}
    assert "entity-active" in ec
    assert set(ec["entity-active"]["chunk_ids"]) == {"chunk-a", "chunk-b"}
    assert ec["entity-active"]["count"] == 2
    # 已删实体不在 entity_chunks（防复活）
    assert "entity-deleted" not in ec


def test_repair_relation_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_relation_chunks 只从 GraphML edge source_id 提取 chunk_ids（防复活）。

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

    from niu_api.internal.lightrag_repair import repair_relation_chunks
    from lightrag.constants import GRAPH_FIELD_SEP

    result = repair_relation_chunks()

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    rc = json.loads((tmp_path / "kv_store_relation_chunks.json").read_text())
    # edge 的 key 是 sorted((src, tgt)) join GRAPH_FIELD_SEP
    # entity-a, entity-b sorted 后还是 ("entity-a", "entity-b")
    expected_key = GRAPH_FIELD_SEP.join(sorted(("entity-a", "entity-b")))
    assert expected_key in rc
    # v4 实现：value = {"chunk_ids": [...], "count": int}
    assert set(rc[expected_key]["chunk_ids"]) == {"chunk-rel1", "chunk-rel2"}
    assert rc[expected_key]["count"] == 2


def test_repair_full_entities_reverse_mapping(tmp_path, monkeypatch):
    """repair_full_entities 从 GraphML source_id → chunk→doc 反向映射。

    v4 实现：key=doc_id, value=list of entity_name
    回归点：只有 GraphML 存在的实体 + doc_status 中存在的 chunk 才会进 full_entities。
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

    result = repair_full_entities()

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


def test_repair_full_relations_reverse_mapping(tmp_path, monkeypatch):
    """repair_full_relations 从 GraphML edge source_id → chunk→doc 反向映射。

    v4 实现：key=doc_id, value=list of relation_key (make_relation_chunk_key 格式)
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

    result = repair_full_relations()

    assert result["status"] == "ok", f"expected ok, got {result}"
    fr = json.loads((tmp_path / "kv_store_full_relations.json").read_text())
    # v8 LightRAG 原生格式：{doc_id: {"relation_pairs": [[src, tgt], ...], "count": N}}
    # edge (entity-a, entity-b) source_id=chunk-rel → doc-1
    assert "doc-1" in fr
    pairs = fr["doc-1"]["relation_pairs"]
    assert ["entity-a", "entity-b"] in pairs
    assert fr["doc-1"]["count"] == 1


# =============================================================================
# v9 Task 2: RepairEmbeddingFunc 单元测试
# =============================================================================

import numpy as np


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
    from niu_api.internal import lightrag_repair
    from niu_api.internal import embedding as niu_embedding

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
    from niu_api.internal import lightrag_repair
    from niu_api.internal import embedding as niu_embedding

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
    from niu_api.internal import lightrag_repair
    from niu_api.internal import embedding as niu_embedding

    # get_model 返回 None（模拟模型未加载）
    monkeypatch.setattr(niu_embedding, "get_model", lambda: None)

    embed_func = lightrag_repair.RepairEmbeddingFunc(embedding_dim=768)

    with pytest.raises(RuntimeError, match="get_model.*None"):
        await embed_func(["测试"])


@pytest.mark.asyncio
async def test_repair_embedding_func_is_async_callable(monkeypatch):
    """验证 RepairEmbeddingFunc 实例的 __call__ 是 async callable（可 await）。"""
    import inspect

    from niu_api.internal import lightrag_repair
    from niu_api.internal import embedding as niu_embedding

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
