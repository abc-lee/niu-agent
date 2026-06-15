#!/usr/bin/env python3
"""测试: 脑区成员范围内向量检索

验证流程:
1. 获取脑区的成员实体名列表
2. 在 LightRAG entities_vdb 中，只在这些成员实体范围内做向量检索
3. 返回与 query 语义最匹配的 top 10 成员实体
4. 对比无过滤的全局检索结果

技术方案: 绕过 LightRAG 封装层，直接获取底层 NanoVectorDB 客户端，
调用其 query() 方法并传入 filter_lambda 参数。
"""

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "python", "lib", "python3.11", "site-packages"))


def test_brain_region_filtered_search():
    """主测试函数"""

    # ===== Step 1: 获取 LightRAG 实例 =====
    print("=" * 60)
    print("Step 1: 获取 LightRAG 实例")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("[FAIL] LightRAG 未初始化，请先启动应用")
        return False

    print("[OK] LightRAG 实例获取成功")

    # ===== Step 2: 获取脑区成员列表 =====
    print("\n" + "=" * 60)
    print("Step 2: 获取脑区成员列表")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_all_region_members, get_brain_regions

    all_regions = get_brain_regions()
    print(f"  脑区列表: {all_regions}")

    region_members = get_all_region_members()
    print(f"  脑区成员映射: {len(region_members)} 个脑区")

    # 打印每个脑区的成员数
    for region_name, members in region_members.items():
        print(f"    {region_name}: {len(members)} 个成员")
        if members:
            print(f"      前5个: {members[:5]}")

    # 收集所有脑区成员实体名
    all_member_names = set()
    for members in region_members.values():
        all_member_names.update(members)

    print(f"\n  总成员实体数: {len(all_member_names)}")

    if not all_member_names:
        print("[WARN] 脑区成员为空（包含 边可能不存在）")
        print("  尝试从 activation manager 获取...")
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                for state in mgr.get_region_map():
                    members = mgr.get_members_of_region(state.region_id)
                    all_member_names.update(members)
                print(f"  从 activation manager 获取成员: {len(all_member_names)}")
        except Exception as e:
            print(f"  activation manager 不可用: {e}")

    if not all_member_names:
        print("[FAIL] 没有可用的脑区成员实体，无法测试")
        return False

    # ===== Step 3: 获取底层 NanoVectorDB 客户端 =====
    print("\n" + "=" * 60)
    print("Step 3: 获取底层 NanoVectorDB 客户端")
    print("=" * 60)

    entities_vdb = rag.entities_vdb
    nano_client = call_async(entities_vdb._get_client())
    print(f"  NanoVectorDB 客户端获取成功")

    # 获取存储数据统计
    storage = getattr(nano_client, "_NanoVectorDB__storage")
    data_count = len(storage.get("data", []))
    print(f"  entities_vdb 记录数: {data_count}")

    if data_count == 0:
        print("[FAIL] entities_vdb 中没有数据")
        return False

    # 查看一条记录的结构
    sample = storage["data"][0]
    print(f"  记录字段: {list(sample.keys())}")
    print(f"  entity_name 示例: {sample.get('entity_name')}")

    # 获取 cosine 阈值
    threshold = getattr(entities_vdb, "cosine_better_than_threshold", 0.2)
    print(f"  cosine 阈值: {threshold}")

    # ===== Step 4: 构造 filter_lambda 并执行过滤检索 =====
    print("\n" + "=" * 60)
    print("Step 4: 执行脑区成员范围内向量检索")
    print("=" * 60)

    query_text = "Python 编程开发"
    member_set = all_member_names

    # 获取 query embedding
    embedding = call_async(entities_vdb.embedding_func([query_text]))[0]
    print(f"  Query: '{query_text}'")
    print(f"  Embedding 维度: {embedding.shape}")
    print(f"  过滤范围: {len(member_set)} 个脑区成员实体")

    # 构造 filter_lambda
    filter_fn = lambda data: data.get("entity_name") in member_set

    # 执行过滤检索
    results = nano_client.query(
        query=embedding,
        top_k=10,
        better_than_threshold=threshold,
        filter_lambda=filter_fn,
    )

    print(f"\n  过滤检索结果 ({len(results)} 条):")
    for i, r in enumerate(results):
        entity_name = r.get("entity_name", "?")
        score = r.get("__metrics__", 0)
        entity_type = r.get("entity_type", "?")
        print(f"    {i+1}. {entity_name} (score={score:.4f}, type={entity_type})")

    # ===== Step 5: 对比无过滤的全局检索 =====
    print("\n" + "=" * 60)
    print("Step 5: 对比全局检索结果（无过滤）")
    print("=" * 60)

    global_results = nano_client.query(
        query=embedding,
        top_k=10,
        better_than_threshold=threshold,
    )

    print(f"  全局检索结果 ({len(global_results)} 条):")
    for i, r in enumerate(global_results):
        entity_name = r.get("entity_name", "?")
        score = r.get("__metrics__", 0)
        entity_type = r.get("entity_type", "?")
        in_region = "IN" if entity_name in member_set else "OUT"
        print(f"    {i+1}. {entity_name} (score={score:.4f}, type={entity_type}) [{in_region}]")

    # ===== Step 6: 多 query 测试 =====
    print("\n" + "=" * 60)
    print("Step 6: 多 query 测试")
    print("=" * 60)

    test_queries = [
        "差旅费报销流程",
        "项目开发进度",
        "照片管理",
        "会议记录",
        "知识图谱检索",
    ]

    for q in test_queries:
        emb = call_async(entities_vdb.embedding_func([q]))[0]

        # 过滤检索
        filtered = nano_client.query(
            query=emb, top_k=5, better_than_threshold=threshold,
            filter_lambda=filter_fn,
        )

        # 全局检索
        global_r = nano_client.query(
            query=emb, top_k=5, better_than_threshold=threshold,
        )

        print(f"\n  Query: '{q}'")
        print(f"    过滤检索: {[r.get('entity_name', '?') for r in filtered]}")
        print(f"    全局检索: {[r.get('entity_name', '?') for r in global_r]}")

        # 计算重叠率
        filtered_names = {r.get("entity_name") for r in filtered}
        global_names = {r.get("entity_name") for r in global_r}
        overlap = filtered_names & global_names
        if global_names:
            overlap_pct = len(overlap) / len(global_names) * 100
        else:
            overlap_pct = 0
        print(f"    重叠率: {overlap_pct:.0f}% ({len(overlap)}/{len(global_names)})")

    # ===== Step 7: 验证过滤效果 =====
    print("\n" + "=" * 60)
    print("Step 7: 验证过滤效果")
    print("=" * 60)

    # 回到第一个 query，验证过滤结果是否全在成员范围内
    filtered_names = {r.get("entity_name") for r in results}
    non_member_results = filtered_names - member_set
    if non_member_results:
        print(f"  [FAIL] 过滤泄漏: {non_member_results} 不在成员集中但出现在结果里")
        return False
    else:
        print(f"  [OK] 过滤验证通过: 所有结果都在成员范围内")

    # 检查全局检索中有多少在脑区外
    global_out = {r.get("entity_name") for r in global_results if r.get("entity_name") not in member_set}
    print(f"  全局检索中脑区外实体: {len(global_out)}/{len(global_results)}")
    if global_out:
        print(f"    脑区外实体: {global_out}")

    print("\n" + "=" * 60)
    print("测试完成!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = test_brain_region_filtered_search()
    sys.exit(0 if success else 1)
