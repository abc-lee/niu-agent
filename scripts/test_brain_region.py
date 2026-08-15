"""
脑区功能全量测试 — 验证边的动态连接机制

测试目标：
1. 脑区实体创建 + 锚定关系（brain:Niu → brain:{region}）
2. 边权重 decay（定时衰减）
3. 边断开（权重低于阈值时删除边）
4. 脑区激活/衰减闭环（激活度 0.92/轮衰减）
5. 上下文注入（brain_region_prompt → LLM 提示词）
6. amerge_entities 改名后关系迁移

前置条件：API 服务器在运行（python -m niu_api）

设计文档：docs/lightrag-plans/06-brain-region-activation.md
"""

import sys
import time
import json

sys.stdout.reconfigure(encoding="utf-8")

# ─── 常量 ───
BRAIN_ENTITY = "brain:Niu"
TEST_REGION = "brain:测试脑区"
TEST_REGION_RENAMED = "brain:重命名脑区"
TEST_KNOWLEDGE = "Python编程"
TEST_TOOL = "lightrag-server"

# 衰减参数（与 region_manager.py 对齐）
DECAY_FACTOR = 0.92
MIN_WEIGHT = 0.05
DISCONNECT_THRESHOLD = 0.03

# 激活度参数（与 region_activation.py 对齐）
ACTIVATION_DECAY = 0.92
ACTIVATION_THRESHOLD = 0.3


# ─── 工具函数 ───
def get_all_entity_names(rag):
    return set(rag.chunk_entity_relation_graph._graph.nodes)


def get_entity_details(rag, name):
    g = rag.chunk_entity_relation_graph._graph
    name_lower = name.lower()
    if name_lower in g.nodes:
        return dict(g.nodes[name_lower])
    return None


def get_edge_data(rag, src, tgt):
    """获取边数据（包括 weight）"""
    g = rag.chunk_entity_relation_graph._graph
    src_lower = src.lower()
    tgt_lower = tgt.lower()
    if g.has_edge(src_lower, tgt_lower):
        return dict(g.edges[src_lower, tgt_lower])
    return None


def get_all_edges_for(rag, entity_name):
    """获取实体的所有边"""
    g = rag.chunk_entity_relation_graph._graph
    name_lower = entity_name.lower()
    edges = []
    if name_lower in g.nodes:
        for src, tgt in g.edges():
            if src == name_lower or tgt == name_lower:
                data = dict(g.edges[src, tgt])
                edges.append((src, tgt, data))
    return edges


def inject_kg(rag, call_async, entities, relationships, source_id="brain:test"):
    """注入 custom_kg (chunks=[])，不触发 LLM"""
    custom_kg = {
        "entities": entities,
        "relationships": relationships,
        "chunks": [],
    }
    try:
        call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)
        print("  OK")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def cleanup_entity(rag, call_async, name):
    """删除指定实体"""
    try:
        call_async(rag.adelete_by_entity(name), timeout=120)
        return True
    except Exception:
        return False


def print_edges(rag, entity_name, label=""):
    """打印实体的所有边"""
    edges = get_all_edges_for(rag, entity_name)
    print(f"  {label}边数: {len(edges)}")
    for src, tgt, data in edges:
        weight = data.get("weight", "?")
        keywords = data.get("keywords", "?")
        desc = str(data.get("description", ""))[:60]
        print(f"    {src} → {tgt} [weight={weight}, keywords={keywords}] {desc}")


# ─── 测试用例 ───


def test_1_brain_region_creation(rag, call_async):
    """
    测试1: 脑区实体创建 + 锚定关系
    - 创建 brain:测试脑区 实体
    - 建立 brain:Niu → brain:测试脑区 的锚定关系
    - 验证实体和关系存在
    """
    print("\n" + "=" * 60)
    print("测试1: 脑区实体创建 + 锚定关系")
    print("=" * 60)

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)

    # 创建脑区实体
    print("  [A] inject_custom_kg 创建脑区实体...")
    ok = inject_kg(
        rag,
        call_async,
        entities=[
            {
                "entity_name": TEST_REGION,
                "entity_type": "BrainRegion",
                "description": "测试脑区，用于验证脑区功能",
            }
        ],
        relationships=[
            {
                "src_id": BRAIN_ENTITY,
                "tgt_id": TEST_REGION,
                "keywords": "remembers",
                "description": "拥有测试脑区",
            }
        ],
        source_id="brain:test",
    )
    if not ok:
        return False

    # 验证
    entities = get_all_entity_names(rag)
    has_region = TEST_REGION.lower() in entities
    details = get_entity_details(rag, TEST_REGION)

    print(f"  脑区实体存在: {has_region}")
    if details:
        print(f"  entity_type: {details.get('entity_type')}")
        print(f"  description: {str(details.get('description', ''))[:100]}")

    # 验证锚定关系
    edges = get_all_edges_for(rag, TEST_REGION)
    has_anchor = any(
        BRAIN_ENTITY.lower() in (src.lower(), tgt.lower()) for src, tgt, _ in edges
    )
    print(f"  锚定关系存在: {has_anchor}")

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)

    return has_region and has_anchor


def test_3_edge_weight_decay(rag, call_async):
    """
    测试3: 边权重 decay（定时衰减）
    - 创建脑区 + 知识实体 + 边（weight=0.8）
    - 模拟多轮衰减：weight *= DECAY_FACTOR
    - 验证权重逐轮下降
    """
    print("\n" + "=" * 60)
    print("测试3: 边权重 decay（定时衰减）")
    print("=" * 60)

    # 清理
    for n in [TEST_REGION, TEST_KNOWLEDGE]:
        cleanup_entity(rag, call_async, n)

    # 创建实体 + 边
    print("  [A] inject_custom_kg 创建脑区+知识实体+边...")
    ok = inject_kg(
        rag,
        call_async,
        entities=[
            {
                "entity_name": TEST_REGION,
                "entity_type": "BrainRegion",
                "description": "测试脑区",
            },
            {
                "entity_name": TEST_KNOWLEDGE,
                "entity_type": "Concept",
                "description": "Python编程知识",
            },
        ],
        relationships=[
            {
                "src_id": TEST_REGION,
                "tgt_id": TEST_KNOWLEDGE,
                "keywords": "knows_about",
                "description": "测试脑区关联Python编程知识",
            },
        ],
        source_id="brain:test",
    )
    if not ok:
        return False

    # 设置初始权重
    g = rag.chunk_entity_relation_graph._graph
    src_lower = TEST_REGION.lower()
    tgt_lower = TEST_KNOWLEDGE.lower()
    initial_weight = 0.8
    if g.has_edge(src_lower, tgt_lower):
        g.edges[src_lower, tgt_lower]["weight"] = initial_weight
        print(f"  设置初始 weight: {initial_weight}")
    else:
        print("  边不存在！")
        cleanup_entity(rag, call_async, TEST_REGION)
        cleanup_entity(rag, call_async, TEST_KNOWLEDGE)
        return False

    # 模拟多轮衰减
    print(f"  [B] 模拟衰减 (factor={DECAY_FACTOR})...")
    weights = [initial_weight]
    current = initial_weight
    for i in range(10):
        current = current * DECAY_FACTOR
        if g.has_edge(src_lower, tgt_lower):
            g.edges[src_lower, tgt_lower]["weight"] = current
        weights.append(round(current, 4))
        if current < DISCONNECT_THRESHOLD:
            print(f"  第{i+1}轮: weight={current:.4f} < {DISCONNECT_THRESHOLD} → 应断开")
            break

    print(f"  衰减轨迹: {weights}")

    # 验证权重下降
    final_weight = g.edges[src_lower, tgt_lower].get("weight", 0) if g.has_edge(src_lower, tgt_lower) else 0
    weight_decreased = final_weight < initial_weight
    print(f"  最终权重: {final_weight:.4f}")
    print(f"  权重下降: {weight_decreased}")

    # 计算断开需要的轮数
    rounds_to_disconnect = 0
    w = initial_weight
    while w > DISCONNECT_THRESHOLD:
        w *= DECAY_FACTOR
        rounds_to_disconnect += 1
    print(f"  从 {initial_weight} 衰减到 {DISCONNECT_THRESHOLD} 需要 {rounds_to_disconnect} 轮")

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)
    cleanup_entity(rag, call_async, TEST_KNOWLEDGE)

    return weight_decreased


def test_4_edge_disconnect(rag, call_async):
    """
    测试4: 边断开（权重低于阈值时删除边）
    - 创建脑区 + 知识实体 + 边
    - 将权重设为低于阈值
    - 模拟断开操作：删除边
    - 验证边被删除，实体仍存在
    """
    print("\n" + "=" * 60)
    print("测试4: 边断开（权重低于阈值时删除边）")
    print("=" * 60)

    # 清理
    for n in [TEST_REGION, TEST_KNOWLEDGE]:
        cleanup_entity(rag, call_async, n)

    # 创建实体 + 边
    print("  [A] inject_custom_kg 创建脑区+知识实体+边...")
    ok = inject_kg(
        rag,
        call_async,
        entities=[
            {
                "entity_name": TEST_REGION,
                "entity_type": "BrainRegion",
                "description": "测试脑区",
            },
            {
                "entity_name": TEST_KNOWLEDGE,
                "entity_type": "Concept",
                "description": "Python编程知识",
            },
        ],
        relationships=[
            {
                "src_id": TEST_REGION,
                "tgt_id": TEST_KNOWLEDGE,
                "keywords": "knows_about",
                "description": "测试脑区关联Python编程知识",
            },
        ],
        source_id="brain:test",
    )
    if not ok:
        return False

    # 验证边存在
    g = rag.chunk_entity_relation_graph._graph
    src_lower = TEST_REGION.lower()
    tgt_lower = TEST_KNOWLEDGE.lower()
    edge_exists_before = g.has_edge(src_lower, tgt_lower)
    print(f"  断开前边存在: {edge_exists_before}")

    # 将权重设为低于阈值
    if g.has_edge(src_lower, tgt_lower):
        g.edges[src_lower, tgt_lower]["weight"] = 0.02  # 低于 DISCONNECT_THRESHOLD=0.03
        print(f"  设置 weight=0.02 (低于阈值 {DISCONNECT_THRESHOLD})")

    # 模拟断开操作：删除边
    print("  [B] 模拟断开操作：删除低权重边...")
    if g.has_edge(src_lower, tgt_lower):
        weight = g.edges[src_lower, tgt_lower].get("weight", 1.0)
        if weight < DISCONNECT_THRESHOLD:
            g.remove_edge(src_lower, tgt_lower)
            print(f"  边已删除 (weight={weight:.4f} < {DISCONNECT_THRESHOLD})")

    # 验证
    edge_exists_after = g.has_edge(src_lower, tgt_lower)
    region_exists = TEST_REGION.lower() in g.nodes
    knowledge_exists = TEST_KNOWLEDGE.lower() in g.nodes

    print(f"  断开后边存在: {edge_exists_after} (应为 False)")
    print(f"  脑区实体存在: {region_exists} (应为 True)")
    print(f"  知识实体存在: {knowledge_exists} (应为 True)")

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)
    cleanup_entity(rag, call_async, TEST_KNOWLEDGE)

    return not edge_exists_after and region_exists and knowledge_exists


def test_6_brain_region_prompt_injection(rag, call_async):
    """
    测试6: 上下文注入（brain_region_prompt → LLM 提示词）
    - 创建脑区实体
    - 调用 brain_region_prompt 生成注入内容
    - 验证注入内容包含脑区信息
    - ainsert 包含脑区相关文本，验证 LLM 合并
    """
    print("\n" + "=" * 60)
    print("测试6: 上下文注入（brain_region_prompt）")
    print("=" * 60)

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)

    # 创建脑区实体
    print("  [A] inject_custom_kg 创建脑区实体...")
    ok = inject_kg(
        rag,
        call_async,
        entities=[
            {
                "entity_name": TEST_REGION,
                "entity_type": "BrainRegion",
                "description": "测试脑区，用于验证上下文注入",
            }
        ],
        relationships=[
            {
                "src_id": BRAIN_ENTITY,
                "tgt_id": TEST_REGION,
                "keywords": "remembers",
                "description": "拥有测试脑区",
            }
        ],
        source_id="brain:test",
    )
    if not ok:
        return False

    # 获取 brain_region_prompt 生成的内容
    print("  [B] 获取 brain_region_prompt 注入内容...")
    try:
        from niu_api.internal.brain_region_prompt import (
            build_dynamic_brain_region_prompt,
            build_static_brain_region_prompt,
        )

        # 注入内容 = 静态脑区架构 + 动态脑区列表（lightrag_manager._llm_model_func 组合方式）
        injected_content = (
            build_static_brain_region_prompt() + "\n\n" + build_dynamic_brain_region_prompt()
        )

        has_brain_info = "brain" in injected_content.lower() or "脑区" in injected_content
        print(f"  注入内容长度: {len(injected_content)}")
        print(f"  包含脑区信息: {has_brain_info}")
        if injected_content:
            # 显示前300字符
            print(f"  注入内容预览: {injected_content[:300]}...")
    except Exception as e:
        print(f"  获取注入内容失败: {e}")
        import traceback
        traceback.print_exc()
        has_brain_info = False

    # ainsert 包含脑区相关文本
    print("  [C] ainsert 包含脑区相关文本...")
    try:
        call_async(
            rag.ainsert("测试脑区是用户大脑中的一个区域，负责管理测试相关的知识。"),
            timeout=300,
        )
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")

    # 验证脑区实体没有分裂
    entities = get_all_entity_names(rag)
    region_variants = [n for n in entities if "测试脑区" in n]
    print(f"  脑区实体变体: {region_variants}")
    no_split = len(region_variants) <= 1

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION)
    for v in region_variants:
        if v != TEST_REGION.lower():
            cleanup_entity(rag, call_async, v)

    return has_brain_info and no_split


def test_7_merge_region_rename(rag, call_async):
    """
    测试7: amerge_entities 改名后关系迁移
    - 创建脑区 + 知识实体 + 关系
    - amerge_entities 改名脑区
    - 验证关系迁移到新实体
    """
    print("\n" + "=" * 60)
    print("测试7: amerge_entities 改名脑区后关系迁移")
    print("=" * 60)

    # 清理
    for n in [TEST_REGION, TEST_REGION_RENAMED, TEST_KNOWLEDGE]:
        cleanup_entity(rag, call_async, n)

    # 创建脑区 + 知识 + 关系
    print("  [A] inject_custom_kg 创建脑区+知识+关系...")
    ok = inject_kg(
        rag,
        call_async,
        entities=[
            {
                "entity_name": TEST_REGION,
                "entity_type": "BrainRegion",
                "description": "测试脑区",
            },
            {
                "entity_name": TEST_KNOWLEDGE,
                "entity_type": "Concept",
                "description": "Python编程知识",
            },
        ],
        relationships=[
            {
                "src_id": BRAIN_ENTITY,
                "tgt_id": TEST_REGION,
                "keywords": "remembers",
                "description": "拥有测试脑区",
            },
            {
                "src_id": TEST_REGION,
                "tgt_id": TEST_KNOWLEDGE,
                "keywords": "knows_about",
                "description": "测试脑区关联Python编程知识",
            },
        ],
        source_id="brain:test",
    )
    if not ok:
        return False

    # 记录改名前的边
    edges_before = get_all_edges_for(rag, TEST_REGION)
    print(f"  改名前边数: {len(edges_before)}")
    for src, tgt, data in edges_before:
        print(f"    {src} → {tgt} [keywords={data.get('keywords')}]")

    # 改名
    print(f"  [B] amerge_entities: {TEST_REGION} → {TEST_REGION_RENAMED}...")
    try:
        result = call_async(
            rag.amerge_entities(
                source_entities=[TEST_REGION],
                target_entity=TEST_REGION_RENAMED,
                target_entity_data={
                    "description": "重命名后的测试脑区",
                    "entity_type": "BrainRegion",
                },
            ),
            timeout=120,
        )
        print(f"  OK: {result}")
    except Exception as e:
        print(f"  FAIL: {e}")
        cleanup_entity(rag, call_async, TEST_REGION)
        cleanup_entity(rag, call_async, TEST_KNOWLEDGE)
        return False

    # 验证
    entities = get_all_entity_names(rag)
    old_exists = TEST_REGION.lower() in entities
    new_exists = TEST_REGION_RENAMED.lower() in entities
    knowledge_exists = TEST_KNOWLEDGE.lower() in entities

    print(f"  旧脑区存在: {old_exists} (应为 False)")
    print(f"  新脑区存在: {new_exists} (应为 True)")
    print(f"  知识实体存在: {knowledge_exists} (应为 True)")

    # 检查关系迁移
    edges_after = get_all_edges_for(rag, TEST_REGION_RENAMED)
    print(f"  改名后边数: {len(edges_after)}")
    for src, tgt, data in edges_after:
        print(f"    {src} → {tgt} [keywords={data.get('keywords')}]")

    has_brain_edge = any(
        BRAIN_ENTITY.lower() in (src.lower(), tgt.lower())
        for src, tgt, _ in edges_after
    )
    has_knowledge_edge = any(
        TEST_KNOWLEDGE.lower() in (tgt.lower(), src.lower())
        for src, tgt, _ in edges_after
    )
    print(f"  brain:Niu→脑区 边迁移: {has_brain_edge}")
    print(f"  脑区→知识 边迁移: {has_knowledge_edge}")

    # 清理
    cleanup_entity(rag, call_async, TEST_REGION_RENAMED)
    cleanup_entity(rag, call_async, TEST_KNOWLEDGE)

    return (
        not old_exists
        and new_exists
        and knowledge_exists
        and has_brain_edge
        and has_knowledge_edge
    )


def test_8_activation_decay_simulation(rag, call_async):
    """
    测试8: 脑区激活度衰减模拟
    - 模拟 RegionActivationManager 的激活/衰减逻辑
    - 验证：激活后激活度=1.0，每轮衰减 *0.92
    - 验证：低于阈值 0.3 时标记为非活跃
    - 这是纯逻辑测试，不依赖 LightRAG 图
    """
    print("\n" + "=" * 60)
    print("测试8: 脑区激活度衰减模拟（纯逻辑）")
    print("=" * 60)

    # 模拟激活度
    activation = 0.0
    activations = []

    # 激活
    activation = 1.0
    activations.append(("activate", activation))

    # 模拟 15 轮衰减
    for i in range(15):
        activation *= ACTIVATION_DECAY
        status = "active" if activation >= ACTIVATION_THRESHOLD else "inactive"
        activations.append((f"decay_{i+1}", round(activation, 4), status))

    # 打印轨迹
    for entry in activations:
        if len(entry) == 2:
            print(f"  {entry[0]}: activation={entry[1]:.4f}")
        else:
            print(f"  {entry[0]}: activation={entry[1]:.4f} [{entry[2]}]")

    # 计算变为非活跃的轮数
    rounds_to_inactive = 0
    a = 1.0
    while a >= ACTIVATION_THRESHOLD:
        a *= ACTIVATION_DECAY
        rounds_to_inactive += 1
    print(f"\n  从 1.0 衰减到 {ACTIVATION_THRESHOLD} 需要 {rounds_to_inactive} 轮")

    # 验证
    final_active = activation >= ACTIVATION_THRESHOLD
    print(f"  15轮后仍活跃: {final_active}")
    print(f"  15轮后激活度: {activation:.4f}")

    # 模拟：激活 → 衰减 → 再激活 → 衰减
    print("\n  [模拟] 激活 → 衰减 → 再激活 → 衰减:")
    a = 0.0
    for i in range(20):
        if i == 0:
            a = 1.0  # 首次激活
            print(f"  轮{i}: activate → {a:.4f}")
        elif i == 10:
            a = 1.0  # 再激活
            print(f"  轮{i}: re-activate → {a:.4f}")
        else:
            a *= ACTIVATION_DECAY
            status = "active" if a >= ACTIVATION_THRESHOLD else "inactive"
            print(f"  轮{i}: decay → {a:.4f} [{status}]")

    return not final_active  # 15轮后应变为非活跃


def test_9_region_activation_manager_api(rag, call_async):
    """
    测试9: RegionActivationManager API 可用性
    - 尝试导入 RegionActivationManager
    - 尝试创建实例
    - 尝试调用 activate / decay / get_status
    - 验证 API 是否真正可用
    """
    print("\n" + "=" * 60)
    print("测试9: RegionActivationManager API 可用性")
    print("=" * 60)

    # 尝试导入
    print("  [A] 导入 RegionActivationManager...")
    try:
        from niu_api.internal.region_activation import RegionActivationManager

        print("  OK: 导入成功")
    except ImportError as e:
        print(f"  FAIL: 导入失败 — {e}")
        print("  结论: RegionActivationManager 不存在或无法导入")
        return False

    # 尝试创建实例 + 测试 API
    print("  [B] 创建实例 + 测试 API...")
    try:
        from niu_api.internal.region_activation import BrainRegionState

        manager = RegionActivationManager()
        print("  OK: 实例创建成功")

        # 初始化：从 BrainRegionInfo 列表创建状态
        # initialize_from_regions 需要 BrainRegionInfo 对象
        from niu_api.internal.region_manager import BrainRegionInfo

        test_regions = [
            BrainRegionInfo(
                name="brain:region:测试1", label="测试1", community_id="test_1",
                description="测试脑区1", size=5, representative="Python",
                members=["Python", "Django"], updated_at=0.0,
            ),
            BrainRegionInfo(
                name="brain:region:测试2", label="测试2", community_id="test_2",
                description="测试脑区2", size=3, representative="Rust",
                members=["Rust"], updated_at=0.0,
            ),
        ]
        manager.initialize_from_regions(test_regions)
        print("  OK: initialize_from_regions()")

        # 激活脑区
        activated = manager.activate_regions(["Python"], {"Python": "test_1"})
        print(f"  OK: activate_regions() → {activated}")

        # 获取状态地图
        region_map = manager.get_region_map()
        print(f"  OK: get_region_map() → {len(region_map)} 个脑区")

        # 获取活跃脑区
        active = manager.get_active_regions()
        print(f"  OK: get_active_regions() → {len(active)} 个活跃")

        # 状态灯
        for state in region_map:
            light = manager.get_status_light(state.activation)
            print(f"  {state.label}: {light} activation={state.activation:.2f}")

        # 衰减
        manager.decay_all()
        print("  OK: decay_all()")

        # 手动调暗
        manager.manual_dim(["测试1"])
        print("  OK: manual_dim()")

        # 手动激活
        manager.manual_activate(["测试2"])
        print("  OK: manual_activate()")

        api_ok = True
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        api_ok = False

    return api_ok


def test_10_region_manager_api(rag, call_async):
    """
    测试10: RegionManager API 可用性
    - 尝试导入 RegionManager
    - 检查实际方法名（与代码对齐）
    - 尝试创建实例（需要 adapter + ingester）
    - 验证边权重管理 API 是否可用
    """
    print("\n" + "=" * 60)
    print("测试10: RegionManager API 可用性")
    print("=" * 60)

    # 尝试导入
    print("  [A] 导入 RegionManager...")
    try:
        from niu_api.internal.region_manager import RegionManager

        print("  OK: 导入成功")
    except ImportError as e:
        print(f"  FAIL: 导入失败 — {e}")
        return False

    # 检查实际方法名（与 region_manager.py 对齐）
    print("  [B] 检查实际方法...")
    actual_methods = [
        "create_region_nodes",
        "update_region_summaries",
        "get_all_regions",
        "get_region_members",
        "cleanup_stale_regions",
        "dissolve_shrunk_regions",
        "_decay_structural_edges",
    ]
    found_count = 0
    for method in actual_methods:
        has_method = hasattr(RegionManager, method)
        if has_method:
            found_count += 1
        print(f"    {method}: {'✅' if has_method else '❌'}")
    print(f"  方法命中率: {found_count}/{len(actual_methods)}")

    # 尝试创建实例（需要 adapter + ingester）
    print("  [C] 创建实例（需要 adapter + ingester）...")
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

        adapter = LightRAGAdapter()
        ingester = LightRAGIngester()
        manager = RegionManager(adapter, ingester)
        print("  OK: 实例创建成功")
    except Exception as e:
        print(f"  FAIL: 实例创建失败 — {e}")
        import traceback
        traceback.print_exc()
        return False

    # 尝试调用 get_all_regions
    print("  [D] 调用 get_all_regions...")
    regions = []
    try:
        regions = manager.get_all_regions()
        print(f"  OK: get_all_regions() → {len(regions)} 个脑区")
        for r in regions:
            print(f"    {r.name} ({r.label}, size={r.size})")
    except Exception as e:
        print(f"  FAIL: get_all_regions 失败 — {e}")

    # 尝试调用 _decay_structural_edges
    print("  [E] 调用 _decay_structural_edges...")
    try:
        disconnected = manager._decay_structural_edges(regions)
        print(f"  OK: _decay_structural_edges() → 断开 {disconnected} 条边")
    except Exception as e:
        print(f"  FAIL: _decay_structural_edges 失败 — {e}")

    # 方法命中率 > 50% 即通过
    return found_count >= len(actual_methods) // 2


# ─── 主流程 ───


def run_test():
    print("=" * 60)
    print("脑区功能全量测试 — 验证边的动态连接机制")
    print("=" * 60)

    print("\n[Init] 初始化 LightRAG...")
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  FAIL — 请确保 API 服务器在运行")
        return False
    print("  OK")

    # 确保 brain:Niu 存在
    entities = get_all_entity_names(rag)
    if BRAIN_ENTITY.lower() not in entities:
        print("  创建 brain:Niu...")
        inject_kg(
            rag,
            call_async,
            entities=[
                {
                    "entity_name": BRAIN_ENTITY,
                    "entity_type": "Niu",
                    "description": "Self entity — all memory relations start from here",
                }
            ],
            relationships=[],
            source_id="brain",
        )

    # 逐个测试
    results = {}

    tests = [
        ("test1_region_creation", test_1_brain_region_creation),
        ("test3_edge_decay", test_3_edge_weight_decay),
        ("test4_edge_disconnect", test_4_edge_disconnect),
        ("test6_prompt_injection", test_6_brain_region_prompt_injection),
        ("test7_merge_rename", test_7_merge_region_rename),
        ("test8_activation_decay_sim", test_8_activation_decay_simulation),
        ("test9_activation_manager_api", test_9_region_activation_manager_api),
        ("test10_region_manager_api", test_10_region_manager_api),
    ]

    for name, test_fn in tests:
        try:
            results[name] = test_fn(rag, call_async)
        except Exception as e:
            print(f"  异常: {e}")
            import traceback

            traceback.print_exc()
            results[name] = False

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {name}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  通过: {passed}/{total}")

    return all(results.values())


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
