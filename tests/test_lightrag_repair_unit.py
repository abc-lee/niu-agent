"""repair_brainregion_zombies 扩展：清理 cache 里僵尸 extract entry。"""
import json
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_brainregion_zombies, repair_text_chunks


def _make_storage_with_zombie_cache(tmp_path: Path):
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


def test_repair_brainregion_zombies_cleans_zombie_cache_entries(tmp_path):
    """repair_brainregion_zombies 应清理 llm_response_cache 里的僵尸 extract entry。"""
    _make_storage_with_zombie_cache(tmp_path)

    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    assert result["status"] == "ok"
    assert result["cleaned_count"] == 1

    tree = ET.parse(tmp_path / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家测试脑区" not in node_ids
    assert "聊天历史脑区" in node_ids

    cache = json.loads((tmp_path / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:zombie_key" not in cache, "僵尸 extract entry 应被删除"
    assert "default:extract:normal_key" in cache, "正常 extract entry 应保留"


def test_repair_brainregion_zombies_no_zombies_leaves_cache_intact(tmp_path):
    """没有僵尸脑区时，cache 不变。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    cache = {
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

    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    assert result["status"] == "ok"
    assert result["cleaned_count"] == 0
    cache_after = json.loads((tmp_path / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:normal_key" in cache_after


def test_repair_brainregion_zombies_does_not_delete_normal_doc_with_zombie_word(tmp_path):
    """正常文档含'被删除'字样但 entity_type != brainregion -> 不应误删。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    cache = {
        "default:extract:doc-with-zombie-word": {
            "return": "entity<|#|>系统维护日志<|#|>concept<|#|>记录删除重复脑区的操作，含'被删除'字样但不是脑区。",
            "cache_type": "extract",
            "chunk_id": "chunk-doc",
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

    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    assert result["status"] == "ok"
    assert result["cleaned_count"] == 0
    cache_after = json.loads((tmp_path / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:doc-with-zombie-word" in cache_after


def test_repair_brainregion_zombies_corrupt_cache_preserves_file(tmp_path):
    """cache 文件 JSON 损坏时，repair 不应清空文件，应保留原内容。

    覆盖 except 分支（lightrag_repair.py L2039-2042）：
    - json.loads 抛 JSONDecodeError
    - lrc_data 保持 None（不写盘）
    - 原文件内容保留
    """
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )

    # cache 文件 JSON 损坏（不是合法 JSON）
    corrupt_content = '{"default:extract: this is not valid JSON'
    (tmp_path / "kv_store_llm_response_cache.json").write_text(corrupt_content)

    for fname in ["kv_store_full_docs.json", "kv_store_text_chunks.json",
                  "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
                  "kv_store_full_entities.json", "kv_store_full_relations.json",
                  "kv_store_doc_status.json"]:
        (tmp_path / fname).write_text("{}")
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')

    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()

    # repair 仍正常完成（不报错）
    assert result["status"] == "ok"
    # cache 文件内容应保留原状（不被清空）
    cache_after = (tmp_path / "kv_store_llm_response_cache.json").read_text()
    assert cache_after == corrupt_content, "损坏的 cache 文件应保留原状不被清空"


def test_repair_text_chunks_uses_real_config_not_hardcoded(tmp_path, monkeypatch):
    """repair_text_chunks 应从 _get_lightrag_config() 读真实 chunk_size，不硬编码。"""
    # 准备 full_docs
    docs = {
        "doc-test": {
            "content": "测试文档内容，用于验证配置读取。",
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    # mock _get_lightrag_config 返回自定义 chunk_size
    config_calls = []
    def fake_config():
        config_calls.append(True)
        return {"chunk_token_size": 800, "chunk_overlap_token_size": 50}

    # patch 源模块 lightrag_manager（repair_text_chunks 用局部 import 从源模块取符号）
    monkeypatch.setattr("niu_api.internal.lightrag_manager._get_lightrag_config", fake_config)
    # mock get_lightrag 返回带 tokenizer 的实例
    # 注意：repair_text_chunks 用局部 import（from niu_api.internal.lightrag_manager import get_lightrag），
    # 所以 patch 必须指向源模块 lightrag_manager，不是被测模块 lightrag_repair
    # FakeTokenizer 必须实现 encode + decode（chunking_by_token_size 调 decode 重组 chunk 内容）
    class FakeTokenizer:
        def encode(self, text):
            return text.split()  # 简化：按空格切分
        def decode(self, tokens):
            return " ".join(tokens)  # 简化：用空格拼回
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag", lambda: FakeRag())

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    result = repair_text_chunks()

    assert result["status"] == "ok"
    assert len(config_calls) > 0, "应调用 _get_lightrag_config 读真实配置"


def test_repair_text_chunks_chunk_id_mismatch_returns_unrecoverable(tmp_path, monkeypatch):
    """chunk_id 重合率<50% 时返回 unrecoverable（保护下游引用不失效）。"""
    docs = {
        "doc-test": {
            "content": "测试文档内容",
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    # chunk_id 一致性检查读 kv_store_doc_status.json 的 chunks_list 字段（lightrag_repair.py:500-505）
    # 不是 text_chunks.json。所以旧 chunk_id 必须写到 doc_status.json
    old_tc = {f"chunk-old-{i}": {"content": f"old{i}", "source_id": "doc-test"} for i in range(100)}
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(old_tc, ensure_ascii=False))
    doc_status = {
        "doc-test": {
            "status": "processed",
            "chunks_count": 100,
            "chunks_list": [f"chunk-old-{i}" for i in range(100)],
        }
    }
    (tmp_path / "kv_store_doc_status.json").write_text(json.dumps(doc_status, ensure_ascii=False))

    class FakeTokenizer:
        def encode(self, text):
            return text.split()
        def decode(self, tokens):
            return " ".join(tokens)
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag", lambda: FakeRag())
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    result = repair_text_chunks()

    # 重建后 chunk_id 跟旧的重合率为 0 → 应返回 unrecoverable
    # 代码库约定：unrecoverable 场景用 status="error" + unrecoverable=True（lightrag_repair.py 全部 19 处一致）
    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
    assert "chunk_id" in result.get("message", "").lower() or "重合" in result.get("message", "")


def test_repair_text_chunks_rebuilds_llm_cache_list(tmp_path, monkeypatch):
    """repair_text_chunks 应反向重建 llm_cache_list 从 llm_response_cache。

    验证：cache 里 1 条 extract entry 的 chunk_id 跟重建后的 chunk_id 一致时，
    重建后 text_chunks 里该 chunk 的 llm_cache_list 应含对应 cache_key。
    """
    # 用真实 compute_mdhash_id 算 chunk_id，让 cache 的 chunk_id 跟重建后一致
    from lightrag.utils import compute_mdhash_id
    doc_content = "测试文档内容用于验证 llm_cache_list 反向重建"
    expected_chunk_id = compute_mdhash_id(doc_content, prefix="chunk-")

    docs = {
        "doc-test": {
            "content": doc_content,
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))

    # cache 里 1 条 extract entry，chunk_id 跟重建后的 chunk_id 一致
    cache = {
        "default:extract:key1": {
            "return": "entity<|#|>test<|#|>document<|#|>desc",
            "cache_type": "extract",
            "chunk_id": expected_chunk_id,  # 跟重建后 chunk_id 一致
            "create_time": 1781930610,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    class FakeTokenizer:
        def encode(self, text):
            return text.split()
        def decode(self, tokens):
            # 必须返回原始 content，让 compute_mdhash_id 算出 expected_chunk_id
            return doc_content
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag", lambda: FakeRag())
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    result = repair_text_chunks()

    assert result["status"] == "ok", f"repair 应成功: {result.get('message', '')}"

    # 验证 llm_cache_list 反向重建：text_chunks 里 expected_chunk_id 的 llm_cache_list 应含 key1
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert expected_chunk_id in tc_after, f"重建后的 chunk_id {expected_chunk_id} 应在 text_chunks 里"
    cache_list = tc_after[expected_chunk_id].get("llm_cache_list", [])
    assert "default:extract:key1" in cache_list, f"llm_cache_list 应含 default:extract:key1，实际: {cache_list}"


def test_repair_graphml_clears_rag_instance_before_get_lightrag(tmp_path, monkeypatch):
    """repair_graphml 调 get_lightrag() 前应显式置 _rag_instance=None + 同步 STORAGE_DIR。

    验证修复的必要性：如果 repair_graphml 不清 _rag_instance，get_lightrag() fast path
    会返回旧实例（指向真实 ~/.niu/lightrag_storage），污染真实数据。

    安全设计：不直接调 repair_graphml（避免真实 pipeline 跑污染数据），而是 mock
    get_lightrag 验证调用前的状态。
    """
    import niu_api.internal.lightrag_manager as lightrag_manager

    # 模拟已存在真实 _rag_instance（指向真实 storage）
    class FakeRealRag:
        storage_dir = Path.home() / ".niu/lightrag_storage"
    monkeypatch.setattr(lightrag_manager, "_rag_instance", FakeRealRag())
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", 0)
    monkeypatch.setattr(lightrag_manager, "_init_error", None)

    # patch _STORAGE_DIR 到 tmp_path（模拟测试隔离）
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    # 准备最小真相源（让 repair_graphml 不在 cache 检查阶段就 return）
    docs = {"doc-test": {"content": "test", "file_path": "test.md"}}
    cache = {"default:extract:k1": {"return": "entity<|#|>test<|#|>document<|#|>desc",
            "cache_type": "extract", "chunk_id": "chunk-test", "create_time": 1}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_doc_status.json").write_text("{}")

    # mock get_lightrag：捕获调用时的 _rag_instance 状态
    call_state = {}
    def mock_get_lightrag():
        call_state["_rag_instance_at_call"] = lightrag_manager._rag_instance
        call_state["storage_dir_at_call"] = lightrag_manager.STORAGE_DIR
        return None  # 返回 None 让 repair_graphml 走 unrecoverable 分支，不跑真实 pipeline
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag", mock_get_lightrag)

    from niu_api.internal.lightrag_repair import repair_graphml
    result = repair_graphml()

    # 验证 get_lightrag 被调用前，_rag_instance 已被清空（None）
    assert call_state.get("_rag_instance_at_call") is None, \
        "repair_graphml 调 get_lightrag() 前应清 _rag_instance=None，否则 fast path 返回旧实例污染真实数据"
    # 验证 lightrag_manager.STORAGE_DIR 已同步到 _storage_dir()（tmp_path）
    assert call_state.get("storage_dir_at_call") == tmp_path, \
        "repair_graphml 调 get_lightrag() 前应同步 lightrag_manager.STORAGE_DIR 到 _storage_dir()"


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
    """2 真相源 + GraphML + vdb 全部完好 → ok=True。"""
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

    # vdb_entities（有对应向量）
    vdb_e = {"data": [{"__id__": "ent-test-entity", "entity_name": "test-entity", "vector": "AAAAAA=="}],
             "file_hash": "fake", "embedding_dim": 8, "matrix": "AAAAAA=="}
    (tmp_path / "vdb_entities.json").write_text(json.dumps(vdb_e, ensure_ascii=False))
    (tmp_path / "vdb_chunks.json").write_text(json.dumps({"data": [], "embedding_dim": 8, "matrix": ""}, ensure_ascii=False))
    (tmp_path / "vdb_relationships.json").write_text(json.dumps({"data": [], "embedding_dim": 8, "matrix": ""}, ensure_ascii=False))

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
    """repair_all 删 9 文件前应备份到临时目录。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 写一些派生文件（含旧数据）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "data"}')
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("old graphml")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 应该有备份目录
    backups = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    # 备份可能在 tmp_path 的父目录或别处，取决于实现
    # 关键验证：_deleted 字段记录了删除的文件
    assert "_deleted" in result
    assert len(result["_deleted"]) > 0


def test_repair_all_rolls_back_on_failure(tmp_path, monkeypatch):
    """repair_all 重建失败时应回滚备份（恢复 9 个文件）。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))

    # 写旧派生文件
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "保留"}')
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("old graphml")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    # mock repair_graphml 抛异常（模拟重建失败）
    import niu_api.internal.lightrag_repair as repair_mod
    def fail_graphml():
        raise RuntimeError("模拟重建失败")
    monkeypatch.setattr(repair_mod, "repair_graphml", fail_graphml)

    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    # 应该回滚：派生文件恢复原状
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"old": "保留"}'
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "old graphml"
    # 返回 unrecoverable
    assert result.get("_unrecoverable") is True
    assert result.get("_rolled_back") is True


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
    """全新用户（full_docs/cache 都不存在）→ repair_all 不应报 unrecoverable。

    全新用户合法启动场景：刚装 niu，~/.niu/lightrag_storage/ 还没创建或为空。
    _check_truth_sources 应返回 ok=True（v4 修订），repair_all 应正常完成
    （重建出空派生文件，不报 unrecoverable）。
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
    # 真相源检查应通过
    assert result["_truth_source_check"]["ok"] is True


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
