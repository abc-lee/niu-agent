"""
KG 工具集成测试

测试环境：真实 LightRAG 实例（~/.niu/lightrag_storage/）
测试原则：不允许 mock，必须用真实数据

运行方式：
    # 先启动主程序
    ./niu.exe

    # 在另一个终端运行测试
    pytest tests/test_kg_tools_integration.py -v
"""

import pytest
from agent.tool_registry import get_registry


@pytest.fixture(scope="module")
def registry():
    """获取工具注册中心"""
    return get_registry()


@pytest.fixture(scope="module")
def test_entity():
    """测试用实体名（测试结束后清理）"""
    name = "测试实体_KG工具测试"
    yield name
    # Cleanup
    try:
        registry = get_registry()
        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
        delete_fn(entity_name=name)
    except Exception:
        pass


@pytest.fixture(scope="module")
def test_relation_entities():
    """测试用关系实体（测试结束后清理）"""
    src = "测试源实体_KG工具测试"
    tgt = "测试目标实体_KG工具测试"
    yield src, tgt
    # Cleanup
    try:
        registry = get_registry()
        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
        delete_fn(entity_name=src)
        delete_fn(entity_name=tgt)
    except Exception:
        pass


class TestCreateEntity:
    """测试 lightrag_create_entity"""

    def test_create_new_entity(self, registry, test_entity):
        """创建新实体应该成功"""
        fn = registry.get("lightrag-server/lightrag_create_entity")
        result = fn(
            entity_name=test_entity,
            entity_type="Concept",
            description="这是一个测试实体",
        )
        assert result["status"] == "ok"
        assert "创建成功" in result["message"]

    def test_create_existing_entity_fails(self, registry, test_entity):
        """创建已存在的实体应该返回 skipped"""
        fn = registry.get("lightrag-server/lightrag_create_entity")
        result = fn(
            entity_name=test_entity,
            entity_type="Concept",
            description="再次创建",
        )
        assert result["status"] == "ok"
        assert result.get("skipped") is True


class TestGetEntityInfo:
    """测试 lightrag_get_entity_info"""

    def test_get_existing_entity(self, registry, test_entity):
        """查询存在的实体应该返回详情"""
        fn = registry.get("lightrag-server/lightrag_get_entity_info")
        result = fn(entity_name=test_entity)
        assert result["status"] == "ok"
        data = result.get("data", {})
        assert data.get("entity_name") == test_entity

    def test_get_nonexistent_entity(self, registry):
        """查询不存在的实体应该返回空或错误"""
        fn = registry.get("lightrag-server/lightrag_get_entity_info")
        result = fn(entity_name="不存在的实体_xyz123")
        assert result["status"] in ["ok", "error"]


class TestEditEntity:
    """测试 lightrag_edit_entity"""

    def test_edit_description(self, registry, test_entity):
        """修改实体描述应该成功"""
        fn = registry.get("lightrag-server/lightrag_edit_entity")
        result = fn(
            entity_name=test_entity,
            description="修改后的描述",
        )
        assert result["status"] == "ok"
        assert "编辑成功" in result["message"]

    def test_edit_nonexistent_entity_fails(self, registry):
        """修改不存在的实体应该失败"""
        fn = registry.get("lightrag-server/lightrag_edit_entity")
        result = fn(
            entity_name="不存在的实体_xyz123",
            description="新描述",
        )
        assert result["status"] == "error"


class TestCreateRelation:
    """测试 lightrag_create_relation"""

    def test_create_relation(self, registry, test_relation_entities):
        """创建关系应该成功"""
        src, tgt = test_relation_entities

        # 先创建两个实体
        create_fn = registry.get("lightrag-server/lightrag_create_entity")
        create_fn(entity_name=src, entity_type="Concept", description="源实体")
        create_fn(entity_name=tgt, entity_type="Concept", description="目标实体")

        # 创建关系
        fn = registry.get("lightrag-server/lightrag_create_relation")
        result = fn(
            source_entity=src,
            target_entity=tgt,
            keywords="test_relation",
            description="测试关系",
        )
        assert result["status"] == "ok"

    def test_create_relation_missing_entity_fails(self, registry):
        """创建关系时实体不存在应该失败"""
        fn = registry.get("lightrag-server/lightrag_create_relation")
        result = fn(
            source_entity="不存在的实体_a",
            target_entity="不存在的实体_b",
            keywords="test",
        )
        assert result["status"] == "error"


class TestGetRelationInfo:
    """测试 lightrag_get_relation_info"""

    def test_get_existing_relation(self, registry, test_relation_entities):
        """查询存在的关系应该返回详情"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_get_relation_info")
        result = fn(source_entity=src, target_entity=tgt)
        assert result["status"] == "ok"


class TestEditRelation:
    """测试 lightrag_edit_relation"""

    def test_edit_relation_description(self, registry, test_relation_entities):
        """修改关系描述应该成功"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_edit_relation")
        result = fn(
            source_entity=src,
            target_entity=tgt,
            new_description="修改后的关系描述",
        )
        assert result["status"] == "ok"


class TestDeleteRelation:
    """测试 lightrag_delete_relation"""

    def test_delete_relation(self, registry, test_relation_entities):
        """删除关系应该成功"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_delete_relation")
        result = fn(source_entity=src, target_entity=tgt)
        assert result["status"] == "ok"

        # 验证关系已删除
        info_fn = registry.get("lightrag-server/lightrag_get_relation_info")
        info = info_fn(source_entity=src, target_entity=tgt)
        # 关系删除后，graph_data 应该为空或 None
        assert info.get("data", {}).get("graph_data") is None


class TestDedupFeedback:
    """测试 dedup 反馈信息"""

    def test_insert_entity_dedup_has_actionable_options(self, registry, test_entity):
        """重复插入实体应该返回可操作选项"""
        fn = registry.get("lightrag-server/lightrag_insert_entity")
        result = fn(
            name=test_entity,
            entity_type="Concept",
            description="重复插入",
        )
        assert result.get("skipped") is True
        # 检查是否包含可操作选项
        message = result.get("message", "")
        assert "可选操作" in message or "lightrag_edit_entity" in message or "lightrag_insert" in message
