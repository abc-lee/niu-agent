"""默认脑区 size 更新函数测试（update_default_region_sizes）

单元测试：python/bin/pytest tests/test_default_region_sizes.py -v

覆盖 T1-1 ~ T1-10：
- size 更新正确性 + priority 配置权威透传（配置写什么用什么——含旧值 core；缺失回退 medium）
- 配置 priority 原样透传（自定义脑区兼容）
- 缺 key 空成员（.get 默认 size=0 无条件更新）
- 三层图守卫（rag None / kg None / kg._graph None）
- 无默认脑区 / assign 已删除 / 成员映射整体空防御
- 描述缺失跳过 / desc 全空不注入 / inject 异常传播 / config 缺 priority key
"""
from contextlib import ExitStack
from unittest import mock

import networkx as nx
import pytest

from niu_api.internal.region_manager import (
    REGION_ENTITY_TYPE,
    REGION_SOURCE_ID,
    _encode_description,
)


def _desc(
    summary="概要",
    region_id="default_文档库",
    size=2,
    representative="rep",
    updated_at=123,
    priority="medium",
) -> str:
    """用真实 _encode_description 构造 fake 图节点描述（全部字段带 brain_meta_ 前缀）。"""
    return _encode_description(
        summary=summary,
        region_id=region_id,
        size=size,
        representative=representative,
        updated_at=updated_at,
        priority=priority,
    )


def _make_adapter(graph_descriptions: dict[str, str]):
    """构造 MagicMock adapter + 真实 networkx fake graph（节点含原始 description）。

    fake graph 必须是真实 networkx.Graph——MagicMock 的 nodes.get 恒 truthy，
    T1-7/T1-8 描述缺失跳过路径不可达（恒红）。
    """
    adapter = mock.MagicMock()
    rag = mock.MagicMock()
    kg = mock.MagicMock()
    g = nx.Graph()
    for name, desc in graph_descriptions.items():
        g.add_node(name.lower(), entity_type="brainregion", description=desc)
    kg._graph = g
    rag.chunk_entity_relation_graph = kg
    adapter._get_rag.return_value = rag
    return adapter


def _setup(brain_regions=None, members=None, config=None):
    """进入测试环境：patch lightrag_manager 模块函数 + LightRAGIngester。

    Returns (stack, ingester_cls); 用 `with stack:` 进入。
    """
    stack = ExitStack()
    if brain_regions is not None:
        stack.enter_context(
            mock.patch(
                "niu_api.internal.lightrag_manager.get_brain_regions",
                return_value=brain_regions,
            )
        )
    if members is not None:
        stack.enter_context(
            mock.patch(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                return_value=members,
            )
        )
    if config is not None:
        stack.enter_context(
            mock.patch(
                "niu_api.internal.region_manager.get_default_regions_config",
                return_value=config,
            )
        )
    ingester_cls = stack.enter_context(
        mock.patch("niu_api.internal.lightrag_adapter.LightRAGIngester")
    )
    return stack, ingester_cls


class TestUpdateDefaultRegionSizes:
    """T1-1 主场景：size 更新正确性 + priority 配置权威透传"""

    def test_t1_1_size_update_and_priority_passthrough(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [
            # 配置新值（与迁移后 ~/.niu/preferences.json 一致）——原样透传
            {"label": "文档库", "description": "文档库描述", "priority": "permanent"},
            {"label": "生活事务", "description": "生活事务描述", "priority": "short"},
        ]
        graph_desc = {
            # 旧描述 priority=medium——输出以配置为准（配置权威，非旧描述透传）
            "文档库脑区": _desc(
                summary="文档库摘要", region_id="default_文档库", size=2,
                representative="rep_doc", priority="medium",
            ),
            # 旧描述 priority=core——同样被配置覆盖（旧值不再触发固定映射）
            "生活事务脑区": _desc(
                summary="生活摘要", region_id="default_生活事务", size=1,
                representative="rep_life", priority="core",
            ),
            # 非默认脑区——不应被更新（is_default_region 过滤）
            "编程开发脑区": _desc(
                summary="非默认", region_id="c1", size=5,
                representative="x", priority="medium",
            ),
        }
        members = {
            "文档库脑区": ["Python", "NumPy", "Pandas"],
            "生活事务脑区": ["体检"],
        }
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区", "生活事务脑区", "编程开发脑区"],
            members=members,
            config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        # updated 计数 = 成功注入的默认脑区数（2——非默认脑区不更新）
        assert result == {"updated": 2}

        ingester_cls.assert_called_once_with()
        inject = ingester_cls.return_value.inject_custom_kg
        inject.assert_called_once()
        kwargs = inject.call_args.kwargs
        # 只更新元数据——绝不建边：relationships/chunks 硬性空
        assert kwargs["relationships"] == []
        assert kwargs["chunks"] == []
        assert kwargs["source_id"] == REGION_SOURCE_ID
        entities = kwargs["entities"]
        assert len(entities) == 2

        doc_entity = next(e for e in entities if e["entity_name"] == "文档库脑区")
        assert doc_entity["entity_type"] == REGION_ENTITY_TYPE
        # 配置 priority=permanent 原样透传（配置权威——非旧描述 medium/非固定映射）
        assert "brain_meta_priority:permanent" in doc_entity["description"]
        # size = 实际成员数（3）
        assert "brain_meta_size:3" in doc_entity["description"]
        # summary / region_id / representative 从旧 description 透传
        assert "文档库摘要" in doc_entity["description"]
        assert "brain_meta_region_id:default_文档库" in doc_entity["description"]
        assert "brain_meta_representative:rep_doc" in doc_entity["description"]
        assert "brain_meta_updated_at:" in doc_entity["description"]

        life_entity = next(e for e in entities if e["entity_name"] == "生活事务脑区")
        # 配置 priority=short 原样透传
        assert "brain_meta_priority:short" in life_entity["description"]
        assert "brain_meta_size:1" in life_entity["description"]

    def test_t1_1b_valid_config_priority_respected(self):
        """配置 priority 原样透传（组织机构脑区配置 short → 注入 short——自定义脑区兼容）"""
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [
            # 配置 short——原样透传（无固定映射改写）
            {"label": "组织机构", "description": "组织机构描述", "priority": "short"},
        ]
        graph_desc = {
            "组织机构脑区": _desc(
                summary="org", region_id="default_组织机构", size=0,
                representative="", priority="permanent",
            ),
        }
        members = {"组织机构脑区": []}
        stack, ingester_cls = _setup(
            brain_regions=["组织机构脑区"], members=members, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        assert result == {"updated": 1}
        kwargs = ingester_cls.return_value.inject_custom_kg.call_args.kwargs
        entity = kwargs["entities"][0]
        assert entity["entity_name"] == "组织机构脑区"
        # 配置 short 原样透传
        assert "brain_meta_priority:short" in entity["description"]

    # T1-1c 配置 priority=旧值 core → 原样透传（无固定映射自愈——判别新旧实现）
    def test_t1_1c_legacy_config_priority_passthrough(self):
        """配置 priority=旧值 core → description 含 brain_meta_priority:core（配置权威原样透传）"""
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [
            # 旧值 core（迁移前 ~/.niu/preferences.json 形态）——配置权威：原样透传不映射
            {"label": "文档库", "description": "文档库描述", "priority": "core"},
        ]
        graph_desc = {
            "文档库脑区": _desc(summary="d", priority="permanent"),
        }
        members = {"文档库脑区": ["a", "b"]}
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区"], members=members, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        assert result == {"updated": 1}
        kwargs = ingester_cls.return_value.inject_custom_kg.call_args.kwargs
        entity = kwargs["entities"][0]
        # 判别断言：旧实现固定映射 → permanent；新实现配置权威 → 原样透传 core
        assert "brain_meta_priority:core" in entity["description"]

    # T1-2 缺 key 空成员：非空 map 缺目标 key → .get 默认 size=0 无条件更新
    def test_t1_2_missing_key_empty_members(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [
            {"label": "文档库", "description": "文档库描述", "priority": "permanent"},
            {"label": "生活事务", "description": "生活事务描述", "priority": "short"},
        ]
        graph_desc = {
            "文档库脑区": _desc(summary="d", priority="permanent"),
            # 缺 key 脑区必须预置节点与描述（否则走 T1-7 描述缺失跳过路径）
            "生活事务脑区": _desc(summary="l", priority="short"),
        }
        members = {"文档库脑区": ["a"]}  # 非空 map 但缺「生活事务脑区」key
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区", "生活事务脑区"], members=members, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        # 两个脑区都更新——缺 key 脑区用 .get(name, []) 默认 size=0
        assert result == {"updated": 2}
        kwargs = ingester_cls.return_value.inject_custom_kg.call_args.kwargs
        entities = {e["entity_name"]: e for e in kwargs["entities"]}
        assert "brain_meta_size:1" in entities["文档库脑区"]["description"]
        assert "brain_meta_size:0" in entities["生活事务脑区"]["description"]

    # T1-3 三层图守卫
    def test_t1_3_guards_rag_kg_graph_none(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        # 守卫 1：rag None
        adapter = mock.MagicMock()
        adapter._get_rag.return_value = None
        assert update_default_region_sizes(adapter) == {"updated": 0}

        # 守卫 2：kg None
        adapter = mock.MagicMock()
        rag = mock.MagicMock()
        rag.chunk_entity_relation_graph = None
        adapter._get_rag.return_value = rag
        assert update_default_region_sizes(adapter) == {"updated": 0}

        # 守卫 3：kg._graph None（图存储未初始化）
        adapter = mock.MagicMock()
        rag = mock.MagicMock()
        kg = mock.MagicMock()
        kg._graph = None
        rag.chunk_entity_relation_graph = kg
        adapter._get_rag.return_value = rag
        assert update_default_region_sizes(adapter) == {"updated": 0}

    # T1-4 无默认脑区
    def test_t1_4_no_default_regions(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        stack, _ = _setup(brain_regions=[], config=[])
        with stack:
            assert update_default_region_sizes(_make_adapter({})) == {"updated": 0}

        # 只有非默认脑区（配置未覆盖）→ 过滤后为空 → 0
        stack, _ = _setup(
            brain_regions=["编程开发脑区"],
            config=[{"label": "文档库", "description": "d", "priority": "permanent"}],
        )
        with stack:
            assert update_default_region_sizes(_make_adapter({})) == {"updated": 0}

    # T1-5 assign 函数已删除（clean cutover）——函数体内 import（绿相 collection 安全）
    def test_t1_5_assign_entities_removed(self):
        with pytest.raises(ImportError):
            from niu_api.internal.region_manager import assign_entities_to_default_regions  # noqa: F401

    # T1-6 成员映射整体空防御
    def test_t1_6_empty_members_map_defense(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [{"label": "文档库", "description": "文档库描述", "priority": "permanent"}]
        graph_desc = {"文档库脑区": _desc(summary="d", priority="permanent")}
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区"], members={}, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        assert result == {"updated": 0}
        ingester_cls.return_value.inject_custom_kg.assert_not_called()

    # T1-7 描述缺失跳过（节点缺失 / description 空）
    @pytest.mark.parametrize("skip_mode", ["missing_node", "empty_description"])
    def test_t1_7_missing_description_skipped(self, skip_mode):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [
            {"label": "文档库", "description": "文档库描述", "priority": "permanent"},
            {"label": "生活事务", "description": "生活事务描述", "priority": "short"},
        ]
        if skip_mode == "missing_node":
            graph_desc = {"文档库脑区": _desc(summary="d", priority="permanent")}
        else:
            graph_desc = {
                "文档库脑区": _desc(summary="d", priority="permanent"),
                "生活事务脑区": "",  # 节点存在但 description 空
            }
        members = {"文档库脑区": ["a"]}  # 显式非空 map（防整体空防御路径）
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区", "生活事务脑区"], members=members, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter(graph_desc))

        # 描述缺失脑区跳过更新——只更新 1 个
        assert result == {"updated": 1}
        kwargs = ingester_cls.return_value.inject_custom_kg.call_args.kwargs
        entities = kwargs["entities"]
        assert len(entities) == 1
        assert entities[0]["entity_name"] == "文档库脑区"
        # 跳过脑区（生活事务脑区）不在注入实体中
        assert all(e["entity_name"] != "生活事务脑区" for e in entities)

    # T1-8 desc 全空（fake graph 全默认脑区节点缺失）→ updated 0 + inject 未调
    def test_t1_8_all_descriptions_missing_no_inject(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [{"label": "文档库", "description": "文档库描述", "priority": "permanent"}]
        members = {"文档库脑区": ["a"]}  # 非空——隔离 T1-6 整体空防御路径
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区"], members=members, config=config,
        )
        with stack:
            result = update_default_region_sizes(_make_adapter({}))

        assert result == {"updated": 0}
        # if update_entities: 守卫——防空描述覆盖
        ingester_cls.return_value.inject_custom_kg.assert_not_called()

    # T1-9 inject 异常不吞——向上传播
    def test_t1_9_inject_exception_propagates(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [{"label": "文档库", "description": "文档库描述", "priority": "permanent"}]
        graph_desc = {"文档库脑区": _desc(summary="d", priority="permanent")}
        members = {"文档库脑区": ["a"]}
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区"], members=members, config=config,
        )
        ingester_cls.return_value.inject_custom_kg.side_effect = RuntimeError("boom")
        with stack:
            with pytest.raises(RuntimeError, match="boom"):
                update_default_region_sizes(_make_adapter(graph_desc))

    # T1-10 config 条目缺 priority key（旧配置形态）→ 不抛 KeyError + 回退 medium 判别
    def test_t1_10_config_missing_priority_key(self):
        from niu_api.internal.region_manager import update_default_region_sizes

        config = [{"label": "文档库", "description": "文档库描述"}]  # 无 priority 键
        graph_desc = {
            # 旧描述 priority=permanent——配置缺 priority → 回退 medium（非旧描述/非固定映射）
            "文档库脑区": _desc(summary="d", priority="permanent"),
        }
        members = {"文档库脑区": ["a", "b"]}
        stack, ingester_cls = _setup(
            brain_regions=["文档库脑区"], members=members, config=config,
        )
        with stack:
            # 不抛 KeyError（.get 契约）
            result = update_default_region_sizes(_make_adapter(graph_desc))

        assert result == {"updated": 1}
        kwargs = ingester_cls.return_value.inject_custom_kg.call_args.kwargs
        entity = kwargs["entities"][0]
        # 判别断言：缺 priority → 回退 DEFAULT_PRIORITY=medium
        # （旧实现固定映射 → permanent → 本断言红；新实现正是原"假绿"路径）
        assert "brain_meta_priority:medium" in entity["description"]
