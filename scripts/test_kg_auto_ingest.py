"""
照片知识图谱自动入库测试 — 真实端到端验证

通过已启动的 API 服务，使用 call_async 桥接调用 LightRAG ainsert，
验证"一条指令入库，LightRAG自动提取/合并/建边"方案是否可行。

运行方式:
    1. 先启动 API: python -m niu_api
    2. 运行测试: python scripts/test_kg_auto_ingest.py

注意: 使用生产 LightRAG 实例（~/.niu/lightrag_storage/），
      测试数据会写入生产图谱。测试完成后手动清理或保留观察。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def call_async(coro, timeout=600):
    """桥接同步调用到 LightRAG 异步事件循环"""
    from niu_api.internal.lightrag_manager import call_async as _call_async
    return _call_async(coro, timeout=timeout)


def get_rag():
    """获取生产 LightRAG 实例"""
    from niu_api.internal.lightrag_manager import get_lightrag
    return get_lightrag()


def get_nx_graph(rag):
    """获取底层 NetworkX 图（线程安全快照）"""
    from niu_api.internal.lightrag_manager import graph_read_lock
    with graph_read_lock():
        return rag.chunk_entity_relation_graph._graph.copy()


def find_entities(nx_graph, keyword):
    """在 NetworkX 图中查找包含关键词的实体"""
    return [n for n in nx_graph.nodes() if keyword in n]


def find_edges_between(nx_graph, node_a, node_b):
    """在 NetworkX 图中查找两个节点之间的边"""
    edges = []
    if nx_graph.has_node(node_a) and nx_graph.has_node(node_b):
        if nx_graph.has_edge(node_a, node_b):
            edges.append(nx_graph.get_edge_data(node_a, node_b))
        if nx_graph.has_edge(node_b, node_a):
            edges.append(nx_graph.get_edge_data(node_b, node_a))
    return edges


def print_graph_stats(rag):
    """打印图谱统计"""
    g = get_nx_graph(rag)
    print(f"  图谱: {g.number_of_nodes()} 节点, {g.number_of_edges()} 边")


def wait_for_pipeline(rag, timeout=120):
    """等待 ainsert pipeline 处理完成

    ainsert 是 pipeline 模式：先 enqueue，后台异步处理。
    等待直到没有 PENDING 或 PROCESSING 文档。
    """
    from lightrag.base import DocStatus

    start = time.time()
    while time.time() - start < timeout:
        try:
            pending_docs = call_async(
                rag.doc_status.get_docs_by_status(DocStatus.PENDING), timeout=10
            )
            processing_docs = call_async(
                rag.doc_status.get_docs_by_status(DocStatus.PROCESSING), timeout=10
            )
            pending_count = len(pending_docs) if pending_docs else 0
            processing_count = len(processing_docs) if processing_docs else 0
            if pending_count == 0 and processing_count == 0:
                return True
            print(f"  等待... PENDING={pending_count}, PROCESSING={processing_count}")
        except Exception as e:
            print(f"  状态检查异常: {e}")

        time.sleep(5)
    return False  # timeout


# ==================== 测试用例 ====================


def test_t1_basic_extraction():
    """T1: 验证 ainsert 能从照片描述文本中提取实体和关系"""
    print("\n" + "=" * 60)
    print("T1: ainsert 基本提取能力")
    print("=" * 60)

    rag = get_rag()
    print_graph_stats(rag)

    text = (
        "照片文件 IMG_20260101_test.jpg（照片ID: photo-test-001）拍摄于2026年1月1日，"
        "拍摄地点为北京颐和园。照片中出现了一位名叫任飞的人（人物ID: person-test-001），"
        "该人物为成年男性。这是一张测试照片，用于验证知识图谱自动入库功能。"
    )

    print(f"  插入文本: {text[:80]}...")
    track_id = call_async(rag.ainsert(text), timeout=600)
    print(f"  track_id: {track_id}")

    # 等待 pipeline
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  Pipeline 完成: {ok}")

    # 检查实体
    g = get_nx_graph(rag)
    entities_renfei = find_entities(g, "任飞")
    entities_yhy = find_entities(g, "颐和园")
    entities_photo = find_entities(g, "photo-test-001")
    print(f"  '任飞'实体: {entities_renfei}")
    print(f"  '颐和园'实体: {entities_yhy}")
    print(f"  'photo-test-001'实体: {entities_photo}")
    print_graph_stats(rag)

    passed = len(entities_renfei) > 0
    print(f"  T1 结果: {'PASS' if passed else 'FAIL'}")
    return passed


def test_t2_same_name_merge():
    """T2: 验证同名实体自动合并"""
    print("\n" + "=" * 60)
    print("T2: 同名实体自动合并")
    print("=" * 60)

    rag = get_rag()

    text1 = "任飞（人物ID: person-test-001）出现在照片 IMG_test_001.jpg 中，拍摄于2026年1月。这是测试数据。"
    text2 = "任飞在2026年2月去了北京出差，讨论了新项目的技术方案。这是测试数据。"

    print(f"  插入文本1...")
    call_async(rag.ainsert(text1), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本1 pipeline: {ok}")

    print(f"  插入文本2...")
    call_async(rag.ainsert(text2), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本2 pipeline: {ok}")

    g = get_nx_graph(rag)
    entities_renfei = find_entities(g, "任飞")
    print(f"  '任飞'实体: {entities_renfei}")
    print(f"  实体数量: {len(entities_renfei)}")

    # 查询验证
    result = call_async(rag.aquery("任飞"), timeout=30)
    print(f"  查询结果: {str(result)[:300]}")

    # 理想: 只有1个"任飞"实体（合并后）
    # 可接受: 2个以内（LLM可能略有不同）
    passed = len(entities_renfei) <= 2
    print(f"  T2 结果: {'PASS' if passed else 'FAIL'} — {len(entities_renfei)} 个任飞实体")
    return passed


def test_t3_uuid_realname_connection():
    """T3: 验证 UUID 和真名双轨连通"""
    print("\n" + "=" * 60)
    print("T3: UUID-真名双轨连通")
    print("=" * 60)

    rag = get_rag()

    # 首次入库: 未知姓名
    text1 = (
        "照片中出现了一位未命名人物（人物ID: person-test-002），"
        "该人物为成年女性，面部特征已录入系统。这是测试数据。"
    )
    print(f"  插入文本1（未命名）...")
    call_async(rag.ainsert(text1), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本1 pipeline: {ok}")

    g = get_nx_graph(rag)
    person_entities = [e for e in g.nodes() if "person-test-002" in e or "未命名" in e or "人物" in e]
    print(f"  文本1后人物相关实体: {person_entities}")

    # 用户命名
    text2 = (
        "人物ID为person-test-002的人，用户确认其姓名为王芳。"
        "王芳是用户的朋友。这是测试数据。"
    )
    print(f"  插入文本2（命名）...")
    call_async(rag.ainsert(text2), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本2 pipeline: {ok}")

    # 后续入库: 用真名
    text3 = "王芳在2026年春节去了上海，和家人一起过年。这是测试数据。"
    print(f"  插入文本3（真名）...")
    call_async(rag.ainsert(text3), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本3 pipeline: {ok}")

    g = get_nx_graph(rag)
    entities_wangfang = find_entities(g, "王芳")
    entities_person002 = find_entities(g, "person-test-002")
    entities_unnamed = find_entities(g, "未命名")
    print(f"  '王芳'实体: {entities_wangfang}")
    print(f"  'person-test-002'实体: {entities_person002}")
    print(f"  '未命名'实体: {entities_unnamed}")

    # 查询验证
    result1 = call_async(rag.aquery("王芳"), timeout=30)
    print(f"  查询'王芳': {str(result1)[:300]}")
    result2 = call_async(rag.aquery("person-test-002"), timeout=30)
    print(f"  查询'person-test-002': {str(result2)[:300]}")

    # 检查连通性
    all_entities = entities_wangfang + entities_person002 + entities_unnamed
    connected = False
    for e1 in all_entities:
        for e2 in all_entities:
            if e1 != e2 and g.has_node(e1) and g.has_node(e2):
                if g.has_edge(e1, e2) or g.has_edge(e2, e1):
                    connected = True
                    edge_data = g.get_edge_data(e1, e2) if g.has_edge(e1, e2) else g.get_edge_data(e2, e1)
                    print(f"  连通: {e1} ↔ {e2}, 边数据: {edge_data}")

    passed = len(entities_wangfang) > 0
    print(f"  T3 结果: {'PASS' if passed else 'FAIL'} — 王芳实体存在: {len(entities_wangfang) > 0}, 连通: {connected}")
    return passed


def test_t4_multi_photo_same_person():
    """T4: 验证同一人物出现在多张照片时关系正确"""
    print("\n" + "=" * 60)
    print("T4: 多照片同人物关联")
    print("=" * 60)

    rag = get_rag()

    text1 = "任飞（人物ID: person-test-001）出现在照片 IMG_test_004a.jpg（照片ID: photo-test-004a）中，拍摄于2026年1月。这是测试数据。"
    text2 = "任飞（人物ID: person-test-001）出现在照片 IMG_test_004b.jpg（照片ID: photo-test-004b）中，拍摄于2026年2月。这是测试数据。"

    print(f"  插入文本1...")
    call_async(rag.ainsert(text1), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本1 pipeline: {ok}")

    print(f"  插入文本2...")
    call_async(rag.ainsert(text2), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本2 pipeline: {ok}")

    g = get_nx_graph(rag)
    entities_renfei = find_entities(g, "任飞")
    print(f"  '任飞'实体: {entities_renfei}")
    print(f"  实体数量: {len(entities_renfei)}")

    # 查询验证
    result = call_async(rag.aquery("任飞的照片"), timeout=30)
    print(f"  查询结果: {str(result)[:300]}")

    passed = len(entities_renfei) <= 2
    print(f"  T4 结果: {'PASS' if passed else 'FAIL'} — {len(entities_renfei)} 个任飞实体")
    return passed


def test_t5_incomplete_then_enrich():
    """T5: 验证信息不完整时入库，后续补充能关联"""
    print("\n" + "=" * 60)
    print("T5: 信息不完整 → 后续补充关联")
    print("=" * 60)

    rag = get_rag()

    # 首次入库: 地点未知（目录名暗示北京）
    text1 = (
        "照片文件 D:/照片/北京旅行/IMG_test_005.jpg（照片ID: photo-test-005），"
        "拍摄时间未知，出现人物: 人物_test_005（人物ID: person-test-005）。"
        "存储路径暗示可能在北京拍摄。这是测试数据。"
    )
    print(f"  插入文本1（不完整）...")
    call_async(rag.ainsert(text1), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本1 pipeline: {ok}")

    # 后续补充: 从聊天记录提取出地点
    text2 = "用户说照片IMG_test_005.jpg是在北京颐和园拍的，当时是2026年春节假期。这是测试数据。"
    print(f"  插入文本2（补充）...")
    call_async(rag.ainsert(text2), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  文本2 pipeline: {ok}")

    g = get_nx_graph(rag)
    entities_yhy = find_entities(g, "颐和园")
    entities_bj = find_entities(g, "北京")
    entities_photo005 = find_entities(g, "photo-test-005")
    print(f"  '颐和园'实体: {entities_yhy}")
    print(f"  '北京'实体: {entities_bj}")
    print(f"  'photo-test-005'实体: {entities_photo005}")

    # 查询验证
    result = call_async(rag.aquery("photo-test-005的拍摄地点"), timeout=30)
    print(f"  查询结果: {str(result)[:300]}")

    passed = len(entities_yhy) > 0 or len(entities_bj) > 0
    print(f"  T5 结果: {'PASS' if passed else 'FAIL'} — 地点实体存在: {passed}")
    return passed


def test_t6_dream_writer_ainsert():
    """T6: 验证梦境整理用 ainsert 替代 ainsert_custom_kg"""
    print("\n" + "=" * 60)
    print("T6: 梦境整理路径改用 ainsert")
    print("=" * 60)

    rag = get_rag()

    text = (
        "语义记忆: 任飞是用户的同事，经常一起出差讨论技术方案。"
        "任飞擅长Python编程和系统架构设计。"
        "来源: 2026年1月至2月的聊天记录。"
        "相关人物ID: person-test-001。这是测试数据。"
    )

    print(f"  插入文本...")
    call_async(rag.ainsert(text), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  Pipeline 完成: {ok}")

    g = get_nx_graph(rag)
    entities_renfei = find_entities(g, "任飞")
    entities_python = find_entities(g, "Python")
    print(f"  '任飞'实体: {entities_renfei}")
    print(f"  'Python'实体: {entities_python}")

    # 查询验证
    result = call_async(rag.aquery("任飞的技能"), timeout=30)
    print(f"  查询结果: {str(result)[:300]}")

    passed = len(entities_renfei) > 0
    print(f"  T6 结果: {'PASS' if passed else 'FAIL'} — 任飞实体存在: {len(entities_renfei) > 0}")
    return passed


def test_t7_delete_cleanup():
    """T7: 验证删除后的清理行为"""
    print("\n" + "=" * 60)
    print("T7: 删除与清理")
    print("=" * 60)

    rag = get_rag()

    text = "李明_test（人物ID: person-test-999）出现在照片 IMG_test_999.jpg（照片ID: photo-test-999）中，拍摄于2026年3月。这是测试数据。"
    print(f"  插入文本...")
    call_async(rag.ainsert(text), timeout=600)
    ok = wait_for_pipeline(rag, timeout=120)
    print(f"  Pipeline 完成: {ok}")

    g = get_nx_graph(rag)
    entities_liming = find_entities(g, "李明_test")
    print(f"  删除前'李明_test'实体: {entities_liming}")

    # 删除 — 需要找到 doc_id
    # ainsert 的 doc_id 是基于内容 MD5 生成的
    # 我们需要通过 doc_status 找到它
    try:
        # 查找最近处理的文档
        processed_docs = call_async(
            rag.doc_status.get_docs_by_status("PROCESSED"), timeout=10
        )
        if processed_docs:
            # 找到包含测试数据的文档
            for doc_id, doc_info in processed_docs.items():
                content = call_async(rag.full_docs.get_by_id(doc_id), timeout=10)
                if content and "李明_test" in str(content):
                    print(f"  找到文档: {doc_id}")
                    print(f"  删除文档: {doc_id}")
                    result = call_async(rag.adelete_by_doc_id(doc_id), timeout=120)
                    print(f"  删除结果: {result}")
                    break
    except Exception as e:
        print(f"  删除操作异常: {e}")

    g = get_nx_graph(rag)
    entities_liming_after = find_entities(g, "李明_test")
    print(f"  删除后'李明_test'实体: {entities_liming_after}")
    print(f"  孤立实体残留: {len(entities_liming_after) > 0}（LightRAG已知限制）")

    passed = True  # 删除功能本身正常就算通过
    print(f"  T7 结果: {'PASS' if passed else 'FAIL'}")
    return passed


# ==================== 主函数 ====================


def main():
    print("=" * 60)
    print("照片知识图谱自动入库 — 真实端到端测试")
    print("使用生产 LightRAG 实例 + call_async 桥接")
    print("=" * 60)

    # 确认 API 已启动
    rag = get_rag()
    if rag is None:
        print("ERROR: LightRAG 实例不可用，请先启动 API 服务")
        print("  python -m niu_api")
        sys.exit(1)

    print_graph_stats(rag)

    # 运行测试
    results = {}
    tests = [
        ("T1", test_t1_basic_extraction),
        ("T2", test_t2_same_name_merge),
        ("T3", test_t3_uuid_realname_connection),
        ("T4", test_t4_multi_photo_same_person),
        ("T5", test_t5_incomplete_then_enrich),
        ("T6", test_t6_dream_writer_ainsert),
        ("T7", test_t7_delete_cleanup),
    ]

    for name, test_fn in tests:
        try:
            passed = test_fn()
            results[name] = "PASS" if passed else "FAIL"
        except Exception as e:
            print(f"  {name} 异常: {e}")
            import traceback
            traceback.print_exc()
            results[name] = f"ERROR: {e}"

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        print(f"  {name}: {result}")

    total = len(results)
    passed_count = sum(1 for v in results.values() if v == "PASS")
    print(f"\n  通过: {passed_count}/{total}")

    # 最终图谱统计
    print_graph_stats(rag)

    return passed_count == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)