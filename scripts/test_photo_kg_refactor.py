"""照片 KG 结构化注入重构验证测试"""

import sys
from pathlib import Path

# Add photo-server src to sys.path for import
_photo_src = str(Path(__file__).resolve().parent.parent / "mcp-servers" / "photo-server" / "src")
if _photo_src not in sys.path:
    sys.path.insert(0, _photo_src)


def test_format_photo_ingest_data_named_person():
    """已命名人物：entity_name 用人名，不用 person:{uuid}"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影，2026:05:09",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    # 人物实体用人名，不用 person:uuid
    person_entities = [e for e in result["entities"] if e["entity_type"] == "person"]
    assert len(person_entities) == 1
    assert person_entities[0]["entity_name"] == "任飞"
    assert "person:" not in person_entities[0]["entity_name"]
    assert "uuid" not in person_entities[0]["entity_name"]

    # 照片实体存在
    photo_entities = [e for e in result["entities"] if e["entity_type"] == "Photo"]
    assert len(photo_entities) == 1
    assert photo_entities[0]["entity_name"] == "photo:E:/tmp/photo/2026/test.jpg"
    assert photo_entities[0]["file_path"] == "E:/tmp/photo/2026/test.jpg"

    # 关系边存在
    assert len(result["relationships"]) > 0
    # features 边：照片 → 人物
    features_rels = [r for r in result["relationships"] if r["keywords"] == "features"]
    assert len(features_rels) == 1
    assert features_rels[0]["src_id"] == "photo:E:/tmp/photo/2026/test.jpg"
    assert features_rels[0]["tgt_id"] == "任飞"


def test_format_photo_ingest_data_unnamed_person():
    """未命名人物：entity_name 用 auto_label"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test2.jpg",
        abstract="未命名人物_1合影",
        detected_persons=[
            {"id": "uuid-5678", "name": "", "auto_label": "未命名人物_1"},
        ],
    )

    person_entities = [e for e in result["entities"] if e["entity_type"] == "person"]
    assert len(person_entities) == 1
    assert person_entities[0]["entity_name"] == "未命名人物_1"
    assert "person:" not in person_entities[0]["entity_name"]


def test_format_photo_ingest_data_co_occurrence():
    """多人同框：生成 co_occurs_with 双向关系"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/group.jpg",
        abstract="合影",
        detected_persons=[
            {"id": "uuid-a", "name": "任飞", "auto_label": "未命名人物_1"},
            {"id": "uuid-b", "name": "李明", "auto_label": "未命名人物_2"},
        ],
    )

    co_occurs = [r for r in result["relationships"] if r["keywords"] == "co_occurs_with"]
    assert len(co_occurs) >= 1
    names_in_co = set()
    for r in co_occurs:
        names_in_co.add(r["src_id"])
        names_in_co.add(r["tgt_id"])
    assert "任飞" in names_in_co
    assert "李明" in names_in_co


def test_format_photo_ingest_data_brain_niu_anchors():
    """brain:Niu → 人物/照片 remembers 边存在"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    remembers = [r for r in result["relationships"] if r["keywords"] == "remembers"]
    targets = {r["tgt_id"] for r in remembers}
    assert "任飞" in targets
    assert "photo:E:/tmp/photo/2026/test.jpg" in targets


def test_sync_photo_to_kg_uses_inject_custom_kg(monkeypatch):
    """sync_photo_to_kg 应调用 lightrag_insert_custom_kg，不调用 lightrag_insert"""
    from niu_photo_server import sync_photo_to_kg

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    result = sync_photo_to_kg(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    assert result["status"] == "success"
    assert "lightrag-server/lightrag_insert_custom_kg" in called_tools
    assert "lightrag-server/lightrag_insert" not in called_tools

    kwargs = called_tools["lightrag-server/lightrag_insert_custom_kg"]
    assert kwargs["chunks"] == []
    assert kwargs["source_id"] == "photo:E:/tmp/photo/2026/test.jpg"
    entity_names = [e["entity_name"] for e in kwargs["entities"]]
    assert "任飞" in entity_names
    assert "photo:E:/tmp/photo/2026/test.jpg" in entity_names
    assert not any("person:" in n for n in entity_names)


def test_sync_photo_to_kg_file_path_set(monkeypatch):
    """照片实体的 file_path 必须显式设置，不能是 unknown_source"""
    from niu_photo_server import sync_photo_to_kg

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    sync_photo_to_kg(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="test",
        detected_persons=[],
    )

    kwargs = called_tools["lightrag-server/lightrag_insert_custom_kg"]
    photo_entities = [e for e in kwargs["entities"] if e["entity_type"] == "Photo"]
    assert len(photo_entities) == 1
    assert photo_entities[0]["file_path"] == "E:/tmp/photo/2026/test.jpg"
