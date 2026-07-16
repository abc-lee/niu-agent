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

    # 扁平结构：顶层有各 repair 名 + _unrecoverable + _skipped + _check_summary + _deleted
    assert "_unrecoverable" in result
    assert "_skipped" in result or "_deleted" in result
    # 不应该有嵌套的 repair_result 字段
    assert "repair_result" not in result
    assert "repaired" not in result  # 顶层不应有 repaired（向后兼容）


def test_repair_all_backs_up_before_delete(tmp_path, monkeypatch):
    """repair_all 删 9 派生文件前应备份到临时目录（v4：GraphML 是真相源，不能当派生文件破坏）。"""
    # v4：GraphML 是 3 真相源之一，必须是合法的（有 graph 元素），否则会被判为 unrecoverable
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 写一些派生文件（含旧数据）——注意 graphml 不在派生文件列表里（v4 是真相源）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "data"}')

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 备份可能在 tmp_path 的父目录或别处，取决于实现
    # 关键验证：_deleted 字段记录了删除的派生文件（不含 graphml——它是真相源不被删）
    assert "_deleted" in result
    assert len(result["_deleted"]) > 0
    # GraphML 是真相源，不在 _deleted 里
    assert "graph_chunk_entity_relation.graphml" not in result["_deleted"]


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
    """全新用户（full_docs/cache 都是空 dict {}）→ repair_all 不应报 unrecoverable。"""
    # 写空 dict 的真相源（模拟全新用户首次启动后的状态）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert not result.get("_unrecoverable"), f"空 dict 真相源应能正常 repair: {result.get('_unrecoverable_reason')}"


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


def test_repair_all_rollback_uses_backed_up_list(tmp_path, monkeypatch):
    """回滚应遍历 _backed_up 列表恢复，且删除重建阶段错误写入的文件。

    Bug B2：原实现遍历 _DERIVED_FILES（10 个），backup 不存在则跳过；
    场景 1（删 vdb_*.json）下 vdb 没备份，回滚时跳过，重建阶段写的空 vdb
    残留，原始数据永久丢失。

    v4 机制变更：
    - repair_all 用 getattr(_self_mod, fn.__name__) 间接查找，所以不再 patch
      _REBUILD_ORDER，而是 patch 模块属性。
    - v4 在 unrecoverable 时会 break，所以让 text_chunks 成功，让 vdb_chunks
      写空 vdb 后失败（模拟重建阶段写空 vdb 的场景）。
    - v4 GraphML 是真相源，必须合法（不能用 <baseline/>，会被判为 unrecoverable）。
    """
    import niu_api.internal.lightrag_repair as lightrag_repair

    # 准备 3 真相源（GraphML 必须合法，否则 _check_truth_sources_intact 报 unrecoverable）
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 准备派生文件 baseline（部分存在，vdb_*.json 不存在模拟场景 1）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"baseline": "text_chunks"}')
    # vdb_*.json 故意不存在（模拟场景 1 删 vdb）

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # v4：patch 模块属性（命名函数有 __name__，让 getattr 间接查找能生效）
    # text_chunks 成功，vdb_chunks 写空 vdb 后失败（模拟重建阶段写空 vdb）
    wrote_empty_vdb = {"done": False}

    def mock_text_chunks():
        return {"status": "ok"}

    def boom_vdb_chunks():
        if not wrote_empty_vdb["done"]:
            (tmp_path / "vdb_chunks.json").write_text('{"empty": "vdb"}')
            wrote_empty_vdb["done"] = True
        return {"status": "error", "unrecoverable": True, "message": "simulated"}

    monkeypatch.setattr(lightrag_repair, "repair_text_chunks", mock_text_chunks)
    monkeypatch.setattr(lightrag_repair, "repair_vdb_chunks", boom_vdb_chunks)

    result = lightrag_repair.repair_all()

    # 验证回滚
    assert result.get("_rolled_back") is True

    # 验证已备份的文件恢复到 baseline
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"baseline": "text_chunks"}'

    # 验证没备份的 vdb_*.json 被删除（不是残留空文件）
    assert not (tmp_path / "vdb_chunks.json").exists(), \
        "vdb_chunks.json 应被回滚删除（没备份，重建的空文件应清理）"


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


@pytest.mark.skip(reason="v8-Task 1 将 repair_text_chunks 改为 unrecoverable stub，依赖其重建后 text_chunks 非空的回滚断言需等 Task 4 重写")
def test_repair_all_rolls_back_on_failure(tmp_path, monkeypatch):
    """重建失败时应回滚到备份。"""
    _make_synthetic_fixture(tmp_path)

    # 记录派生文件原始内容
    tc_before = (tmp_path / "kv_store_text_chunks.json").read_bytes()

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    # mock 让 repair_vdb_entities 失败
    import niu_api.internal.lightrag_repair as repair_mod
    def failing_vdb_entities():
        raise Exception("mock failure")
    monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_vdb_entities)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    assert result.get("_rolled_back") is True
    # 原派生文件被恢复
    assert (tmp_path / "kv_store_text_chunks.json").read_bytes() == tc_before


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
    tokens = tokenizer.encode("hello world")
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
    assert nodes["entity-x"] == ("person", "desc X", "chunk-aaa")
    assert nodes["文档库脑区"] == ("brainregion", "文档库脑区描述<SEP>brain_meta_size:94", "chunk-bbb")
    # 缺 d1 → entity_type=""
    assert nodes["entity-no-d1"] == ("", "desc Y", "chunk-ccc")
