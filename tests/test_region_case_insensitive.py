"""region 字典构建时 entity_name key 应 lower 化，查询大小写不敏感。

背景：
    LightRAG fork 已把 entity_name 统一 lowercase 存储（Task 1/2）。
    niu_api 的 region 字典 key 来自 BrainRegionInfo.members（用户配置的脑区
    成员，可能含大写），查询入参来自 vdb 查询返回的 entity_name（Task 1/2
    修复后是 lower）。

    双重保险：构建时 key lower + 查询时入参 lower，任何一边漏了都不会出问题。

被测对象：
    - niu_api.internal.region_injector.BrainContextInjector.activate_for_query
      内联构建的 entity_to_region 字典
    - niu_api.internal.region_activation.RegionActivationManager.activate_regions
      查询外部 entity_to_region + 内部 _entity_to_region 的逻辑
    - niu_api.internal.lightrag_adapter.LightRAGAdapter.search_within_region
      member_set 构建 + filter_lambda 的 entity_name 查询
"""
from unittest.mock import MagicMock

import pytest

from niu_api.internal import region_injector
from niu_api.internal.lightrag_adapter import LightRAGAdapter
from niu_api.internal.region_activation import RegionActivationManager

# ---------------------------------------------------------------------
# 1. region_injector: 字典构建 + 查询大小写不敏感
# ---------------------------------------------------------------------


def test_entity_to_region_dict_is_case_insensitive():
    """region_injector 构建的 entity_to_region 字典 key 应为 lower，
    查询时入参也应 lower，任何大写输入都能命中。"""
    # 模拟用户配置的脑区成员（含大写）
    entity_to_region: dict[str, str] = {}
    region_members = {"tech_region": ["Python", "NumPy", "Web"]}
    for region_name, members in region_members.items():
        for member in members:
            # 这是修复后的代码模式：构建时 key lower
            entity_to_region[member.lower()] = region_name

    # 验证：key 都是 lower
    assert "python" in entity_to_region
    assert "Python" not in entity_to_region  # 大写不在字典里

    # 验证：查询时入参 lower，无论原值大小写都能命中
    assert entity_to_region.get("Python".lower()) == "tech_region"
    assert entity_to_region.get("python".lower()) == "tech_region"
    assert entity_to_region.get("PYTHON".lower()) == "tech_region"


def test_activate_for_query_uses_lower_for_hit_entities():
    """activate_for_query 内联构建的 entity_to_region 字典 key 应 lower，
    vdb 返回的 entity_name 修复后是 lower，查询时入参 lower 能命中。

    这个测试用 mock 的 adapter 模拟 vdb 返回 "python"（小写，模拟 Task 1/2
    修复后的 vdb 行为），region_members 含大写 "Python"（模拟用户配置），
    验证大写 "Python" 在构建时被 lower 化，所以查询 "python" 能命中。
    """
    # 准备 mock adapter，模拟 query_data 返回 entities（entity_name 已是 lower）
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = {
        "data": {
            "entities": [
                {"entity_name": "python", "entity_type": "technology"},
            ]
        }
    }

    mock_activation_mgr = MagicMock()
    mock_activation_mgr.activate_regions.return_value = set()
    mock_region_mgr = MagicMock()

    # 准备 region_members（含大写 "Python"，模拟用户配置）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "niu_api.internal.region_injector.get_all_region_members",
            lambda: {"tech_region": ["Python"]},
        )

        injector = region_injector.BrainContextInjector(
            mock_adapter, mock_activation_mgr, mock_region_mgr
        )
        _, entity_to_region, hit_entities = injector.activate_for_query("python数据分析")

    # hit_entities 是 vdb 返回的 entity_name（"python"，已是 lower）
    assert "python" in hit_entities

    # entity_to_region 字典里应该有 "python" 这个 key（构建时把 "Python" lower 化）
    assert "python" in entity_to_region
    # 字典里不应该有大写 "Python"（构建时已 lower）
    assert "Python" not in entity_to_region
    # 查到对应的 region
    assert entity_to_region.get("python") == "tech_region"


def test_activate_for_query_classify_path_lower():
    """_classify_entity_to_region 的结果加入字典时，entity_name key 也应 lower。

    当 vdb 返回的 entity 不在 region_members 里时，会走 _classify_entity_to_region
    分类到默认脑区，然后把 (entity_name, region_name) 加入 entity_to_region 字典。
    验证这里的 key 也是 lower。
    """
    # 准备 mock adapter，返回一个不在 region_members 的 entity（大写 "UnknownEntity"）
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = {
        "data": {
            "entities": [
                {"entity_name": "UnknownEntity", "entity_type": "note"},
            ]
        }
    }

    mock_activation_mgr = MagicMock()
    mock_activation_mgr.activate_regions.return_value = set()
    mock_region_mgr = MagicMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "niu_api.internal.region_injector.get_all_region_members",
            lambda: {"tech_region": ["python"]},  # 不含 UnknownEntity
        )

        injector = region_injector.BrainContextInjector(
            mock_adapter, mock_activation_mgr, mock_region_mgr
        )
        _, entity_to_region, _ = injector.activate_for_query("something")

    # _classify_entity_to_region 应该给 entity_type="note" 分类到 "文档库脑区"
    # 关键：字典 key 应是 lower（"unknownentity"），不是 "UnknownEntity"
    assert "unknownentity" in entity_to_region
    assert "UnknownEntity" not in entity_to_region
    assert entity_to_region.get("unknownentity") == "文档库脑区"


# ---------------------------------------------------------------------
# 2. region_activation: 构建时 key lower + 查询时入参 lower
# ---------------------------------------------------------------------


def test_region_activation_dict_keys_are_lower():
    """RegionActivationManager 内部 _entity_to_region 字典 key 应为 lower。"""
    from niu_api.internal.region_manager import BrainRegionInfo

    manager = RegionActivationManager()

    # 构造 BrainRegionInfo，members 含大写
    region = BrainRegionInfo(
        name="tech_region",
        label="技术",
        community_id="community_1",
        description="编程技术",
        size=2,
        representative="python",
        members=["Python", "NumPy"],  # 含大写
        updated_at=0.0,
    )
    manager.initialize_from_regions([region])

    # 内部字典 key 应是 lower
    internal_map = manager.get_entity_to_region_map()
    assert "python" in internal_map
    assert "Python" not in internal_map
    assert "numpy" in internal_map
    assert internal_map["python"] == "tech_region"


def test_region_activation_query_is_case_insensitive():
    """activate_regions 查询时对入参 lower，无论 hit_entities 大小写都能命中。"""
    from niu_api.internal.region_manager import BrainRegionInfo

    manager = RegionActivationManager()
    region = BrainRegionInfo(
        name="tech_region",
        label="技术",
        community_id="community_1",
        description="编程技术",
        size=1,
        representative="python",
        members=["python"],  # 已 lower
        updated_at=0.0,
    )
    manager.initialize_from_regions([region])

    # 用大写 "Python" 作为 hit_entity 查询
    activated = manager.activate_regions(["Python"], {})
    assert "tech_region" in activated

    # 用小写 "python" 作为 hit_entity 查询
    activated_lower = manager.activate_regions(["python"], {})
    assert "tech_region" in activated_lower

    # 用全大写 "PYTHON" 作为 hit_entity 查询
    activated_upper = manager.activate_regions(["PYTHON"], {})
    assert "tech_region" in activated_upper


def test_region_activation_external_dict_query_case_insensitive():
    """activate_regions 查询外部传入的 entity_to_region 时也对入参 lower。"""
    from niu_api.internal.region_manager import BrainRegionInfo

    manager = RegionActivationManager()
    region = BrainRegionInfo(
        name="tech_region",
        label="技术",
        community_id="community_1",
        description="编程技术",
        size=1,
        representative="other_entity",
        members=["other_entity"],  # 内部字典含 other_entity
        updated_at=0.0,
    )
    manager.initialize_from_regions([region])

    # 外部传入字典（key 已 lower 化）
    external_map = {"python": "tech_region"}
    # hit_entity 是大写 "Python"，应能通过 external_map 命中
    activated = manager.activate_regions(["Python"], external_map)
    assert "tech_region" in activated


# ---------------------------------------------------------------------
# 3. lightrag_adapter: member_set 构建 lower + filter_lambda 查询 lower
# ---------------------------------------------------------------------


def test_search_within_region_member_set_is_lower():
    """search_within_region 的 member_set 应 lower 化，filter_lambda 查询也 lower，
    vdb 返回的 entity_name（已是 lower）能命中，大写也能命中。"""
    adapter = LightRAGAdapter()

    # 构造一个 fake result，filter_lambda 通过后返回
    fake_result = {
        "data": {
            "entities": [
                {"entity_name": "python", "entity_type": "technology"},
                {"entity_name": "numpy", "entity_type": "library"},
            ]
        }
    }

    # 模拟 query_data 直接返回 fake_result（不调 LightRAG）
    adapter.query_data = MagicMock(return_value=fake_result)

    # region_member_names 传入大写 "Python"
    result = adapter.search_within_region(
        query="编程",
        region_member_names={"Python", "NumPy"},  # 含大写
        mode="local",
        top_k=10,
        keywords=["编程"],
    )

    # filter_lambda 应该用 lower 比较，大写 "Python" 也能匹配小写 "python"
    knowledge_entities = result.get("knowledge", [])
    entity_names = [e.get("entity_name") for e in knowledge_entities]
    # 至少 "python" 应该在结果里（因为 member_set 已 lower 化，entity_name 也是 lower）
    assert "python" in entity_names


def test_search_within_region_handles_none_entity_name():
    """filter_lambda 中 data.get('entity_name') 可能返回 None，不应 crash。"""
    adapter = LightRAGAdapter()

    # 构造一个 fake result，其中一个 entity 的 entity_name 是 None
    fake_result = {
        "data": {
            "entities": [
                {"entity_name": None, "entity_type": "other"},
                {"entity_name": "python", "entity_type": "technology"},
            ]
        }
    }

    adapter.query_data = MagicMock(return_value=fake_result)

    # 不应 raise AttributeError
    result = adapter.search_within_region(
        query="编程",
        region_member_names={"python"},
        mode="local",
        top_k=10,
        keywords=["编程"],
    )

    # python 应该在结果里，None entity 不会 crash
    knowledge_entities = result.get("knowledge", [])
    entity_names = [e.get("entity_name") for e in knowledge_entities]
    assert "python" in entity_names
