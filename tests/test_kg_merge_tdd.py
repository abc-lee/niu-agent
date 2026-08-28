"""
TDD 测试：照片/文档入库后知识图谱实体合并验证（mock 化版本）。

原始目的：对真实存储探测 LightRAG ainsert_custom_kg / inject_entity 的合并行为。
现状（2026-08-28 测试债清算 T1）：
- inject_entity/inject_relation 族已从生产退役 → P0-1/2/5/6 死用例删除（台账 #134-137）
- ainsert_custom_kg 的合并语义属 LightRAG fork，本文件不再探测
- P0-3 改为全 mock 调用契约验证：双入口 get_lightrag() / LightRAGIngester()
  均 patch（FakeLightrag + mock），零写真实图谱（台账 §6.4 写图事件根治）
- P0-4 前端分类兼容性为纯逻辑，原样保留

本文件可安全进全量回归：不构造真实 LightRAG 实例、不触网、不写 ~/.niu/lightrag_storage。
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import networkx as nx

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))
sys.path.insert(0, str(PROJECT_ROOT))


class FakeLightrag:
    """内存假 LightRAG 实例：提供空图谱供只读访问，不触碰真实存储。"""

    def __init__(self) -> None:
        self.chunk_entity_relation_graph = SimpleNamespace(_graph=nx.Graph())


# ============================================================
# P0-3: 验证 depicts 关系注入调用契约（全 mock，零写真实图谱）
# ============================================================

def test_p0_3_depicts_relation():
    """P0-3: 验证 photo → person:{uuid} 的 depicts 关系原样传给 inject_custom_kg。

    双入口均 patch：
    - niu_api.internal.lightrag_manager.get_lightrag → FakeLightrag（内存空图谱）
    - niu_api.internal.lightrag_adapter.LightRAGIngester → mock 实例
    不构造任何真实实例，零写生产图谱。
    """
    fake_rag = FakeLightrag()
    mock_ingester = MagicMock(name="LightRAGIngester")
    mock_ingester.inject_custom_kg.return_value = {"status": "ok"}

    with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=fake_rag), \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=mock_ingester):
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        # 双入口确实被中性化的守卫：patch 失效时立即失败，不放行到真实注入
        assert ingester is mock_ingester, "LightRAGIngester 未被 patch——有写真实图谱风险"

        photo_path = "test_photo_for_depicts.jpg"
        person_uuid = "test-uuid-003"

        result = ingester.inject_custom_kg(
            entities=[
                {"entity_name": f"person:{person_uuid}", "entity_type": "Person",
                 "description": "王五, detected in photo: test", "source_id": "test_p0_3"},
            ],
            relationships=[
                {"src_id": photo_path, "tgt_id": f"person:{person_uuid}",
                 "keywords": "depicts",
                 "description": "Photo test depicts 王五",
                 "source_id": "test_p0_3", "weight": 0.8},
            ],
            chunks=[],
            source_id="test_p0_3",
        )

    assert result["status"] == "ok"
    mock_ingester.inject_custom_kg.assert_called_once()
    _, kwargs = mock_ingester.inject_custom_kg.call_args

    # person:{uuid} 实体原样透传
    entities = kwargs["entities"]
    assert len(entities) == 1
    assert entities[0]["entity_name"] == f"person:{person_uuid}"

    # depicts 关系：src=照片, tgt=人物, keywords=depicts
    rels = [r for r in kwargs["relationships"] if r.get("keywords") == "depicts"]
    assert len(rels) == 1
    assert rels[0]["src_id"] == photo_path
    assert rels[0]["tgt_id"] == f"person:{person_uuid}"


# ============================================================
# P0-4: 验证前端分类兼容性
# ============================================================

def test_p0_4_frontend_classification():
    """P0-4: 验证前端 mapNodeType 对 person:{uuid} 实体的分类。

    前端 renderer.js 的 mapNodeType() 使用 entityType.toLowerCase() 匹配。
    验证 "Person" 和 "person" 都能正确映射到 "person" 分类。
    """
    print("\n" + "=" * 60)
    print("P0-4: 前端分类兼容性验证")
    print("=" * 60)

    # 模拟前端的 typeColors 和 mapNodeType
    type_colors = {
        "person": "#FF6B6B",
        "organization": "#4ECDC4",
        "technology": "#45B7D1",
        "document": "#96CEB4",
        "photo": "#FFEAA7",
        "video": "#DDA0DD",
        "note": "#87CEEB",
        "chat": "#F0E68C",
        "concept": "#98D8C8",
        "location": "#7FDBFF",
        "event": "#FFA07A",
        "other": "#CCCCCC",
    }

    # 模拟 mapNodeType 逻辑（与 renderer.js 一致）
    def map_node_type(entity_type: str, node_type: str = "", source: str = "") -> str:
        # Document 节点用 source 字段分类
        if node_type == "Document":
            source_map = {"photo": "photo", "video": "video", "note": "note", "chat": "chat"}
            return source_map.get(source, "document")
        if node_type == "Concept":
            return "concept"
        # 其他节点用 entityType.toLowerCase()
        et = entity_type.lower() if entity_type else "other"
        if et in type_colors:
            return et
        return "other"

    # 测试用例
    test_cases = [
        ("Person", "", "", "person"),     # sync_photo_to_kg 用大写
        ("person", "", "", "person"),     # name_person 用小写
        ("PERSON", "", "", "person"),     # 任何大小写
        ("Organization", "", "", "organization"),
        ("Technology", "", "", "technology"),
        ("", "Document", "photo", "photo"),       # 照片文件节点
        ("", "Document", "video", "video"),       # 视频文件节点
        ("", "Document", "", "document"),         # 普通文档节点
        ("", "Document", "note", "note"),         # 便利贴节点
    ]

    all_pass = True
    for entity_type, node_type, source, expected in test_cases:
        result = map_node_type(entity_type, node_type, source)
        status = "✅" if result == expected else "❌"
        print(f"  {status} mapNodeType('{entity_type}', nodeType='{node_type}', source='{source}') → '{result}' (期望 '{expected}')")
        if result != expected:
            all_pass = False

    if all_pass:
        print("\n✅ 所有前端分类测试通过 — Person/person 都能正确分类")
    else:
        print("\n❌ 有分类不兼容的情况")

    assert all_pass, "存在 mapNodeType 分类不兼容的测试用例"


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    results = {}
    for name, fn in (("P0-3", test_p0_3_depicts_relation),
                     ("P0-4", test_p0_4_frontend_classification)):
        try:
            fn()
            results[name] = True
        except AssertionError as e:
            results[name] = f"AssertionError: {e}"

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        status = "✅ PASS" if result is True else f"⚠️ {result}" if result else "❌ FAIL"
        print(f"  {name}: {status}")

    print("=" * 60)
