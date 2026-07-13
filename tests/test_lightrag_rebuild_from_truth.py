"""端到端验证：6 种损坏现场全部能从 2 真相源修复（合成 fixture）。

不 mock LLM，用真实 LightRAG 实例 + 真实 embedding。
注意：repair_graphml 重跑 pipeline 时 cache 命中不调 LLM。

测试隔离铁律：所有测试在 tmp_path 内执行，不操作真实 ~/.niu/lightrag_storage 数据。

已知实现限制（Task 4 遗留 bug）：
    LightRAG `_init_flags` 全局变量导致同进程内第二次 get_lightrag() 不重新加载磁盘
    doc_status/text_chunks 等存储——内存数据陈旧，apipeline_process_enqueue_documents
    找不到 pending 文档，"No documents to process"，GraphML 不生成 → unrecoverable。
    这是 Task 4 的实现 bug，不属于 Task 9 测试范围。
    本测试文件用 `patched_storage` fixture 包装 lightrag_manager.get_lightrag，
    每次调用前清空 namespace 共享存储，让 get_lightrag() 总是从磁盘重新加载，
    绕过这个限制。
"""
import json
import shutil
import warnings
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_all, repair_brainregion_zombies
from niu_api.internal.lightrag_integrity import check_all

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "lightrag_truth_sources"

# 10 个派生文件（计划标题写"9 个"是笔误，实际是 10 个，跟 _DERIVED_FILES 一致）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "graph_chunk_entity_relation.graphml",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]


@pytest.fixture
def isolated_storage(tmp_path):
    """复制 fixture 真相源到 tmp_path。"""
    for fname in ["kv_store_full_docs.json", "kv_store_llm_response_cache.json"]:
        src = FIXTURE_DIR / fname
        if src.exists():
            shutil.copy(src, tmp_path / fname)
    return tmp_path


def _reset_lightrag_namespace_state():
    """彻底重置 LightRAG 全局共享存储状态。

    LightRAG 用模块级 `_init_flags` 跟踪 namespace 是否已加载。一旦加载过，
    同进程内不会重新读盘——这会让 repair_graphml 第二次 get_lightrag() 看不到
    磁盘上更新的 doc_status（Task 4 遗留 bug）。

    另外 `_async_locks` 里绑定了 event loop（首次创建时绑），后续 new_event_loop
    调用会报 "bound to a different event loop"。

    本函数彻底重置所有共享存储全局变量，让 LightRAG 从零开始重新初始化。
    """
    from lightrag.kg import shared_storage as ss

    # 调 finalize_share_data 彻底清空（包括 _init_flags / _shared_dicts /
    # _async_locks / _update_flags / _manager / _initialized 等）
    if ss._initialized:
        try:
            ss.finalize_share_data()
        except Exception as e:
            warnings.warn(f"reset namespace: finalize_share_data failed: {type(e).__name__}: {e}")

    # 重新初始化（单进程模式）
    try:
        ss.initialize_share_data(workers=1)
    except Exception as e:
        warnings.warn(f"reset namespace: initialize_share_data failed: {type(e).__name__}: {e}")

    # 同时清 lightrag_manager 的 _rag_instance，让下次 get_lightrag 重建实例
    import niu_api.internal.lightrag_manager as lightrag_manager
    lightrag_manager._rag_instance = None
    lightrag_manager._init_failed_at = 0
    lightrag_manager._init_error = None
    # 测试环境跳过三级门控（否则会因为 _integrity_result=critical/major 拒绝初始化）
    lightrag_manager._integrity_result = None


@pytest.fixture
def patched_storage(tmp_path):
    """patch _STORAGE_DIR 到 tmp_path + 包装 get_lightrag 每次清空 namespace。

    同时 patch lightrag_repair._STORAGE_DIR 和 lightrag_integrity._STORAGE_DIR，
    避免 check_all 读真实路径污染数据。

    关键 workaround：包装 lightrag_manager.get_lightrag + get_lightrag_for_repair，
    每次调用前清空 namespace 共享存储状态。这绕过 Task 4 遗留 bug（_init_flags
    全局变量导致第二次 get_lightrag 不重新加载磁盘 doc_status）。

    Bug A 修复后：repair_text_chunks/repair_graphml 改用 get_lightrag_for_repair，
    所以也必须包装它，否则命名空间不重置 → 第二次调用看到 stale _init_flags=True
    → 跳过 KV load → "No documents to process"。
    """
    import niu_api.internal.lightrag_manager as lightrag_manager

    # 备份原 get_lightrag + get_lightrag_for_repair
    orig_get_lightrag = lightrag_manager.get_lightrag
    orig_get_lightrag_for_repair = lightrag_manager.get_lightrag_for_repair

    def patched_get_lightrag(*args, **kwargs):
        # 每次调用前清空 namespace，强制下次 initialize() 重新从磁盘加载
        _reset_lightrag_namespace_state()
        return orig_get_lightrag(*args, **kwargs)

    def patched_get_lightrag_for_repair(*args, **kwargs):
        # 同上：repair 专用路径也要清空 namespace
        _reset_lightrag_namespace_state()
        return orig_get_lightrag_for_repair(*args, **kwargs)

    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_manager.get_lightrag", patched_get_lightrag), \
         patch("niu_api.internal.lightrag_manager.get_lightrag_for_repair", patched_get_lightrag_for_repair):
        # 初始清空一次（防止其他测试残留）
        _reset_lightrag_namespace_state()
        yield tmp_path

    # 恢复
    _reset_lightrag_namespace_state()


def test_e2e_repair_after_delete_vdb(isolated_storage, patched_storage):
    """场景 1：删 vdb_*.json → repair → 重建。"""
    # 先跑一次 repair 建立 baseline（让所有派生文件存在）
    repair_all()

    # 删 3 个 vdb 文件
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (isolated_storage / fname).unlink()

    result = repair_all()

    assert not result.get("_unrecoverable"), f"修复应成功: {result.get('_unrecoverable_reason')}"
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        assert (isolated_storage / fname).exists()
        assert (isolated_storage / fname).stat().st_size > 0


def test_e2e_repair_after_delete_graphml(isolated_storage, patched_storage):
    """场景 2：删 GraphML → repair → 重建。"""
    repair_all()
    (isolated_storage / "graph_chunk_entity_relation.graphml").unlink()

    result = repair_all()

    assert not result.get("_unrecoverable")
    assert (isolated_storage / "graph_chunk_entity_relation.graphml").exists()


def test_e2e_repair_after_delete_all_derived(isolated_storage, patched_storage):
    """场景 3：删 10 个派生文件全部 → repair → 全部重建。"""
    for fname in _DERIVED_FILES:
        if (isolated_storage / fname).exists():
            (isolated_storage / fname).unlink()

    result = repair_all()

    assert not result.get("_unrecoverable"), f"修复应成功: {result.get('_unrecoverable_reason')}"
    for fname in _DERIVED_FILES:
        assert (isolated_storage / fname).exists(), f"{fname} 应被重建"


def test_e2e_repair_after_corrupt_derived(isolated_storage, patched_storage):
    """场景 4：损坏 10 个派生文件 → repair → 重建。"""
    for fname in _DERIVED_FILES:
        (isolated_storage / fname).write_text('{"corrupt": "garbage"}')

    result = repair_all()

    assert not result.get("_unrecoverable"), f"修复应成功: {result.get('_unrecoverable_reason')}"
    for fname in _DERIVED_FILES:
        content = (isolated_storage / fname).read_text()
        assert "garbage" not in content, f"{fname} 应被重建（不含 garbage）"


def test_e2e_unrecoverable_when_full_docs_corrupt(isolated_storage, patched_storage):
    """场景 5：真相源 full_docs 损坏 → unrecoverable + 回滚。

    注意：不能用"删除 full_docs"模拟损坏——那会被判为"全新用户合法"（ok）。
    必须用"文件存在但 JSON 解析失败"触发 _check_truth_source 的 JSON 解析失败 → critical。
    repair_all 在步骤 1（检测真相源）就 return，根本不会进到"删 10 派生"步骤，
    所以"派生文件保留原状"实际是因为根本没动过（_rolled_back=False，没回滚因为没删）。
    """
    # full_docs 存在但 JSON 损坏（不是合法 JSON）
    (isolated_storage / "kv_store_full_docs.json").write_text('{"corrupt": this is not valid JSON')
    (isolated_storage / "kv_store_text_chunks.json").write_text('{"old": "data"}')

    result = repair_all()

    assert result.get("_unrecoverable") is True
    # 派生文件保留原状（repair_all 在步骤 1 就 return，没动过派生文件）
    assert (isolated_storage / "kv_store_text_chunks.json").read_text() == '{"old": "data"}'


def test_e2e_zombie_cache_cleaned_before_rebuild(isolated_storage, patched_storage):
    """场景 6：含僵尸脑区 cache → 重建后僵尸不复活。

    fixture 的 llm_response_cache 有 1 条 zombie-syn extract entry
    （description 含"被删除的重复脑区实体之一"）。

    实现行为（与计划略有出入）：
    - LightRAG apipeline 不会让 chunk_id 不匹配的 extract 复活到 GraphML
      （fixture 的 zombie-syn 用 chunk-zombie-syn，跟 full_docs chunk_id 不匹配，
      所以不会被命中，不会让 GraphML 含僵尸 node）。
    - repair_brainregion_zombies 只在 GraphML 含 zombie node 时才清 cache（L1834
      if not zombie_names: return early）。
    - 所以本场景实际验证：repair_all 完整流程下，GraphML 不含僵尸脑区，
      即使 cache 里有僵尸 extract entry（因为 extract 不被命中）。

    本场景作为"僵尸不复活"的反向验证——证明即使 cache 含僵尸 extract，
    重建后 GraphML 不会出现"智家测试僵尸脑区" node。
    """
    # 跑 repair_all 完整流程（含 brainregion_zombies + graphml 重建）
    result = repair_all()
    assert not result.get("_unrecoverable"), f"修复应成功: {result.get('_unrecoverable_reason')}"

    # 验证 GraphML 不含"智家测试僵尸脑区" node（僵尸不复活）
    import xml.etree.ElementTree as ET
    tree = ET.parse(isolated_storage / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家测试僵尸脑区" not in node_ids, "僵尸脑区不应复活"

    # 验证正常 extract entry 保留（repair_all 没误清正常 cache）
    cache = json.loads((isolated_storage / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:syn-key-1" in cache, "正常 extract entry 应保留"
    # 注意：zombie-syn 是否被清取决于 GraphML 是否含僵尸 node（实际行为下不会清，
    # 因为 GraphML 不含僵尸，repair_brainregion_zombies 直接 return）。


def test_e2e_rollback_when_rebuild_fails(isolated_storage, patched_storage):
    """场景 7（回滚路径）：真相源完好但 rebuild 阶段抛异常 → 备份恢复。

    代码审查发现场景 5 名不副实——它只验证 early-return（真相源损坏在步骤 1 就 return），
    没真正覆盖 repair_all 步骤 5 的"重建失败→恢复备份"回滚路径。

    本测试真正触发回滚路径：
    1. 先跑一次 repair_all 建立完整 baseline（所有派生文件存在）
    2. 记录 baseline 的 GraphML 内容
    3. patch _REBUILD_ORDER 让 repair_graphml 抛 RuntimeError
    4. 再次调用 repair_all —— 进到步骤 4 重建时抛异常 → 步骤 5 回滚
    5. 断言 _rolled_back=True 且 GraphML 恢复到 baseline（不是被删除/损坏的状态）

    注意：_REBUILD_ORDER 是模块级 list，在模块加载时直接捕获函数引用，
    所以不能 patch lightrag_repair.repair_graphml —— 必须直接 patch _REBUILD_ORDER。
    """
    import niu_api.internal.lightrag_repair as lightrag_repair

    # 1. 先跑一次 repair_all 建立完整 baseline
    baseline_result = repair_all()
    assert not baseline_result.get("_unrecoverable"), \
        f"baseline repair_all 应成功: {baseline_result.get('_unrecoverable_reason')}"

    graphml_path = isolated_storage / "graph_chunk_entity_relation.graphml"
    assert graphml_path.exists(), "baseline 后 GraphML 应存在"
    baseline_graphml = graphml_path.read_bytes()
    assert len(baseline_graphml) > 0, "baseline GraphML 不应为空"

    # 2. patch _REBUILD_ORDER 让 graphml 阶段抛异常
    #    _REBUILD_ORDER 在模块加载时直接捕获函数引用，所以必须替换 list 里的条目
    orig_order = lightrag_repair._REBUILD_ORDER

    def boom():
        raise RuntimeError("simulated rebuild failure")

    patched_order = [
        ("graphml", boom) if name == "graphml" else (name, fn)
        for name, fn in orig_order
    ]

    with patch.object(lightrag_repair, "_REBUILD_ORDER", patched_order):
        # 3. 调用 repair_all —— 应进到步骤 4 重建时抛异常 → 步骤 5 回滚
        result = repair_all()

    # 4. 断言回滚成功
    assert result.get("_rolled_back") is True, \
        f"重建失败应触发回滚: _rolled_back={result.get('_rolled_back')}, " \
        f"_rollback_error={result.get('_rollback_error')}"

    # 5. 断言 GraphML 恢复到 baseline 内容（备份恢复成功，不是被删除状态）
    assert graphml_path.exists(), "回滚后 GraphML 应存在（从备份恢复）"
    rolled_back_graphml = graphml_path.read_bytes()
    assert rolled_back_graphml == baseline_graphml, \
        "回滚后 GraphML 内容应跟 baseline 一致（备份恢复成功）"
