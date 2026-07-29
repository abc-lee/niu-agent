"""E2E 测试：保底边实体真实参与社区重算

按 real-testing-only.md 铁律，本测试不 mock LightRAG/RegionManager，
走真实初始化路径。用 monkeypatch.setattr 把 STORAGE_DIR 指向临时目录，
避免污染 ~/.niu/lightrag_storage/。

注意：与现有 e2e 测试（test_lightrag_repair_e2e_skillsync.py:31 等）保持一致，
**不用 importlib.reload**——reload 会让 lightrag_adapter 等其他模块持有旧 get_lightrag 引用，
导致 e2e 断言崩溃。

**前置条件**：需要 bge-base-zh-v1.5 模型（约 390MB）。模型不存在时自动 skip。
"""
import shutil
from pathlib import Path

import pytest

# 前置条件检查：bge-base-zh-v1.5 模型必须存在，否则 skip 整个模块
_MODELS_DIR = Path(__file__).parent.parent / "models" / "bge-base-zh-v1.5"
if not _MODELS_DIR.exists():
    pytest.skip(
        f"bge-base-zh-v1.5 模型未下载（{_MODELS_DIR} 不存在），跳过 e2e 测试。"
        f"参考 R9 风险点：模型需先下载到 models/bge-base-zh-v1.5/。",
        allow_module_level=True,
    )


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """临时 LightRAG 存储目录，跑完自动清理。

    与现有 e2e 测试模式保持一致：用 monkeypatch.setattr 直接覆盖
    lightrag_manager.STORAGE_DIR、_rag_instance 等模块级变量。

    **关键**：同时 monkeypatch.setenv("HOME", str(tmp_path))——避免
    _clear_sync_state_if_storage_empty（lightrag_manager.py:744-780）用
    Path.home() 不受 STORAGE_DIR patch 影响，删除真实 ~/.niu/skill_sync_state.json
    和 last_region_sync.json。
    """
    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    # 关键：先 patch HOME 到临时目录，避免 _clear_sync_state_if_storage_empty 删真实文件
    # 参考 test_lightrag_repair_e2e_skillsync.py:49 模式
    monkeypatch.setenv("HOME", str(tmp_path))

    # 覆盖 lightrag_manager 的模块级状态（与 test_lightrag_repair_e2e_skillsync.py:31 模式一致）
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", storage_dir)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._init_failed_at", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._repairing", False)

    # 初始化 shared_storage 单进程模式（如未初始化）
    # LightRAG fork 路径：lightrag.kg.shared_storage（不是 lightrag.utils）
    try:
        from lightrag.kg.shared_storage import initialize_share_data
        initialize_share_data()
    except ImportError:
        pass  # LightRAG 未安装则跳过
    except RuntimeError:
        pass  # 已初始化则跳过（initialize_share_data 重复调用会抛 RuntimeError）

    yield storage_dir

    # 测试结束清理：
    # 1. 停止 LightRAG 后台事件循环（避免 daemon thread 引用已删除的临时目录）
    try:
        from niu_api.internal import lightrag_manager as lm
        # lightrag_manager 只有 shutdown_lightrag_loop（没有 _stop_loop）
        lm.shutdown_lightrag_loop(timeout=2.0)
    except Exception:
        pass  # 测试结束清理失败不阻塞

    # 2. 删除临时目录（monkeypatch 会自动恢复模块级变量，无需手动 reload）
    if storage_dir.exists():
        shutil.rmtree(storage_dir, ignore_errors=True)


@pytest.mark.usefixtures("tmp_storage")
def test_floor_edge_entity_participates_in_real_detect_communities():
    """E2E：构造真实 LightRAG 图 + 真实 detect_communities 调用，
    验证保底边实体出现在 partition 成员里"""
    # 注意：在 fixture monkeypatch.setattr 之后才 import，确保拿到的是 patched 状态
    from niu_api.internal import lightrag_manager as lm
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    from niu_api.internal.lightrag_manager import find_entities_with_single_floor_edge
    from niu_api.internal.region_detector import CommunityDetector
    from niu_api.internal.region_manager import BELONGS_TO_RELATION, FLOOR_WEIGHT, INITIAL_WEIGHT

    rag = lm.get_lightrag()
    assert rag is not None, "LightRAG 初始化失败"

    # 构造测试图：1 个脑区 + 1 个保底边实体 + 3 个游离实体
    # 跟 find_entities_with_single_floor_edge L488 一致：用 hasattr 守卫访问 _graph
    graph_obj = rag.chunk_entity_relation_graph
    graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj

    # 添加脑区节点（name 以"脑区"结尾，跟 get_all_region_members 判断方式一致）
    graph.add_node("测试脑区", entity_type="brainregion",
                   description="brain_meta_priority:medium<SEP>测试")

    # 添加保底边实体（归属边 weight=FLOOR_WEIGHT）
    graph.add_node("FloorEdgeEntity", entity_type="concept", description="测试保底")
    graph.add_edge("测试脑区", "FloorEdgeEntity",
                   keywords=BELONGS_TO_RELATION, weight=FLOOR_WEIGHT,
                   description=BELONGS_TO_RELATION)

    # 添加 3 个游离实体 + 它们之间的知识边
    for name in ["FreeA", "FreeB", "FreeC"]:
        graph.add_node(name, entity_type="concept", description=f"游离{name}")

    # 保底边实体跟 FreeA 之间有知识边（不影响归属边计数）
    graph.add_edge("FloorEdgeEntity", "FreeA", keywords="相关", weight=INITIAL_WEIGHT, description="相关")
    graph.add_edge("FreeA", "FreeB", keywords="相关", weight=INITIAL_WEIGHT, description="相关")
    graph.add_edge("FreeB", "FreeC", keywords="相关", weight=INITIAL_WEIGHT, description="相关")

    try:
        # 调用 find_entities_with_single_floor_edge
        floor_entities = find_entities_with_single_floor_edge()
        assert "flooredgeentity" in floor_entities, \
            f"FloorEdgeEntity 应在保底边实体集合里，实际 {floor_entities}"

        # 调用真实 detect_communities
        adapter = LightRAGAdapter()
        detector = CommunityDetector(adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        all_partition_members = []
        for p in result.partitions:
            all_partition_members.extend(p.entity_names)

        # 关键断言：FloorEdgeEntity 必须参与算法
        assert "FloorEdgeEntity" in all_partition_members, \
            "保底边实体应参与社区重算（OR 关系覆盖排除条件）"
    finally:
        # 清理测试数据：删除本次添加的节点（monkeypatch 会自动恢复 _rag_instance=None，
        # 下次 get_lightrag 会重新初始化，不会有残留状态）
        for node in ["测试脑区", "FloorEdgeEntity", "FreeA", "FreeB", "FreeC"]:
            if graph.has_node(node):
                graph.remove_node(node)
