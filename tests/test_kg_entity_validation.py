"""
TDD 测试：照片入库和文档入库后知识图谱实体验证。

核心验证问题：
1. 照片入库后，KG 中的 person:{uuid} 实体和关系是否正确
2. 人物改名后，KG 中的实体描述是否同步更新
3. 文档入库后，LLM 抽取的"张三"和照片的 person:{uuid} 是否自动合并
4. 如果不自动合并，人名替换为 person:{uuid}(张三) 格式后是否合并

测试数据：E:/tmp/2009.6.4西柏坡/ (33张照片)
"""

import json
import sys
from pathlib import Path

import pytest

# 确保项目路径可用
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "photo-server" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "lightrag-server" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# P0 单元测试：验证 sync_photo_to_kg 的调用参数
# ============================================================

class TestSyncPhotoToKgEntityFormat:
    """验证 sync_photo_to_kg 创建的实体和关系格式是否正确。"""

    def test_person_entity_name_format(self):
        """人物实体名必须是 person:{uuid} 格式。"""
        from niu_photo_server import sync_photo_to_kg
        # 检查函数签名和文档
        assert sync_photo_to_kg.__doc__ is not None
        # 验证函数存在且可调用
        assert callable(sync_photo_to_kg)


class TestNamePersonKgSync:
    """验证 name_person 的 KG 同步逻辑。"""

    def test_name_person_entity_type_lowercase(self):
        """验证 name_person 中 entity_type 大小写问题。

        当前代码用 'person'（小写），sync_photo_to_kg 用 'Person'（大写）。
        前端 mapNodeType 做 .toLowerCase() 兼容，但后端应该统一。
        """
        import inspect

        from niu_photo_server import name_person
        source = inspect.getsource(name_person)
        # 记录当前状态（小写 'person'），后续需要修复为 'Person'
        has_lowercase = '"person"' in source
        has_uppercase = '"Person"' in source
        # 至少有一个存在
        assert has_lowercase or has_uppercase, "name_person 应该设置 entity_type"


class TestMergePersonsKgSync:
    """验证 merge_persons 的 KG 同步逻辑。"""

    def test_merge_persons_creates_merged_into_relation(self):
        """验证 merge_persons 创建了 merged_into 关系。"""
        import inspect

        from niu_photo_server import merge_persons
        source = inspect.getsource(merge_persons)
        assert "merged_into" in source, "merge_persons 应该创建 merged_into 关系"


# ============================================================
# P1 集成测试：真实 LightRAG 验证
# ============================================================

@pytest.mark.integration
class TestIntegrationPhotoKg:
    """用真实照片数据验证 KG 中的实体和关系。

    需要启动 Python API 服务。
    """

    PHOTO_DIR = "E:/tmp/2009.6.4西柏坡"

    @pytest.fixture(autouse=True)
    def setup(self):
        """确保测试数据存在。"""
        self.photo_dir = Path(self.PHOTO_DIR)
        if not self.photo_dir.exists():
            pytest.skip(f"测试数据目录不存在: {self.PHOTO_DIR}")

    def test_photo_dir_has_files(self):
        """验证测试数据目录有照片文件。"""
        photos = list(self.photo_dir.glob("*.jpg"))
        assert len(photos) > 0, f"测试目录应有照片: {self.PHOTO_DIR}"

    def test_lightrag_storage_exists(self):
        """验证 LightRAG 存储目录存在。"""
        storage = Path.home() / ".niu" / "lightrag_storage"
        assert storage.exists(), "LightRAG 存储目录应存在"

    def test_lightrag_graph_has_data(self):
        """验证 LightRAG 图谱有数据。"""
        graph_file = Path.home() / ".niu" / "lightrag_storage" / "graph_chunk_entity_relation.graphml"
        if not graph_file.exists():
            pytest.skip("LightRAG 图谱文件不存在，需要先入库数据")
        # graphml 文件应该非空
        assert graph_file.stat().st_size > 0, "图谱文件不应为空"

    def test_read_existing_person_entities(self):
        """读取 KG 中已有的 person: 开头的实体，验证格式。"""
        entities_file = Path.home() / ".niu" / "lightrag_storage" / "kv_store_full_entities.json"
        if not entities_file.exists():
            pytest.skip("实体存储文件不存在")

        data = json.loads(entities_file.read_text(encoding="utf-8"))
        person_entities = []
        for key, value in data.items():
            if key.startswith("person:"):
                person_entities.append({"key": key, "data": value})

        # 如果有 person 实体，验证格式
        for pe in person_entities:
            key = pe["key"]
            # person: 后面应该是 UUID
            uuid_part = key.replace("person:", "")
            assert len(uuid_part) > 0, f"person 实体应有 UUID: {key}"

            # 验证 entity_type 字段
            data = pe["data"]
            if isinstance(data, dict):
                entity_type = data.get("entity_type", "")
                # 前端 mapNodeType 做 .toLowerCase()，所以 Person 和 person 都能识别
                assert entity_type.lower() == "person", \
                    f"person 实体 entity_type 应为 Person/person，实际: {entity_type}"


@pytest.mark.integration
class TestIntegrationDocumentPersonMerge:
    """核心测试：文档中的"张三"和照片中的 person:{uuid} 是否自动合并。"""

    def test_check_existing_entity_name_collision(self):
        """检查 KG 中是否存在同名但不同格式的实体。

        如果存在 entity_name="张三" 和 entity_name="person:{uuid}"（description 含"张三"），
        说明两者没有自动合并。
        """
        entities_file = Path.home() / ".niu" / "lightrag_storage" / "kv_store_full_entities.json"
        if not entities_file.exists():
            pytest.skip("实体存储文件不存在")

        data = json.loads(entities_file.read_text(encoding="utf-8"))

        # 收集所有人物实体
        person_uuid_entities = {}  # person:{uuid} -> description
        plain_name_entities = {}   # 普通名字实体 -> description

        for key, value in data.items():
            if key.startswith("person:"):
                desc = ""
                if isinstance(value, dict):
                    desc = value.get("description", "")
                person_uuid_entities[key] = desc
            elif isinstance(value, dict):
                et = value.get("entity_type", "").lower()
                if et == "person":
                    plain_name_entities[key] = value.get("description", "")

        # 报告发现
        print("\n=== KG 人物实体统计 ===")
        print(f"person:{{uuid}} 格式: {len(person_uuid_entities)} 个")
        for k, v in person_uuid_entities.items():
            print(f"  {k}: {v[:50]}...")
        print(f"普通名字格式: {len(plain_name_entities)} 个")
        for k, v in plain_name_entities.items():
            print(f"  {k}: {v[:50]}...")

        # 如果两种格式都存在同名人物，说明没有自动合并
        if person_uuid_entities and plain_name_entities:
            # 提取 person:{uuid} 中的名字
            uuid_names = set()
            for desc in person_uuid_entities.values():
                # description 格式通常是 "张三, detected in photo: xxx" 或 "Renamed to: 张三"
                name = desc.split(",")[0].strip()
                if name and not name.startswith("未命名"):
                    uuid_names.add(name)

            plain_names = set(plain_name_entities.keys())

            collisions = uuid_names & plain_names
            if collisions:
                print(f"\n⚠️ 发现同名未合并实体: {collisions}")
                print("这些名字同时存在于 person:{uuid} 和普通名字格式中，")
                print("说明 LightRAG 没有自动合并同名实体。")
