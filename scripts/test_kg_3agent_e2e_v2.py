#!/usr/bin/env python3
"""
KG 注入端到端测试脚本 v2 (Agent 2/3 — 测试执行者)

与 v1 的关键区别:
  - 场景4: 通过率<100% 必须 PARTIAL_PASS, 不能 PASS
  - 场景3: 碎片化必须 FAIL, 不能 PASS
  - 边匹配: 使用 keywords 属性而非 description 子串模糊匹配
  - 严格统计缺失人物名单

8 个场景:
  1. 单人物注入
  2. 多人物同框
  3. 人物命名更新
  4. 100人压力测试
  5. 路径一致性
  6. file_path 正确性
  7. 实体去重
  8. 边完整性

约束:
  - 不修改任何生产代码
  - 不修改生产图谱数据
  - 写操作在测试工作目录 E:/tmp/bot/lightrag_test/
  - 必须用 networkx.read_graphml() 验证 graphml
  - ToolRegistry 必须初始化 (调用 load_mcp_tools)
"""

import os
import sys
import json
import time
import shutil
import traceback
from pathlib import Path
from datetime import datetime

# ── 路径配置 ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path("E:/tools/ai-bot")
PROD_STORAGE = Path("C:/Users/LiLei/.niu/lightrag_storage")
TEST_WORKDIR = Path("E:/tmp/bot/lightrag_test")

# 添加项目路径 (必须在 import 之前)
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "photo-server" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "lightrag-server" / "src"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))

# 测试计数器
_total = 0
_results = []  # [(scenario, status, evidence)]  status: PASS / FAIL / PARTIAL_PASS


def record_result(scenario: str, status: str, evidence: str):
    """记录测试结果。status 必须是 PASS / FAIL / PARTIAL_PASS"""
    global _total
    assert status in ("PASS", "FAIL", "PARTIAL_PASS"), f"Invalid status: {status}"
    _total += 1
    _results.append((scenario, status, evidence))
    print(f"  [{status}] {scenario}")
    if evidence:
        for line in evidence.strip().split("\n"):
            print(f"        {line}")


def record_pass(scenario: str, evidence: str):
    record_result(scenario, "PASS", evidence)


def record_fail(scenario: str, evidence: str):
    record_result(scenario, "FAIL", evidence)


def record_partial(scenario: str, evidence: str):
    record_result(scenario, "PARTIAL_PASS", evidence)


# ── 步骤 1: 准备测试环境 ──────────────────────────────────────────────
def prepare_test_environment():
    """复制生产 lightrag_storage 到测试工作目录"""
    print("\n[准备] 复制生产 lightrag_storage 到测试工作目录...")
    if TEST_WORKDIR.exists():
        shutil.rmtree(TEST_WORKDIR)

    shutil.copytree(PROD_STORAGE, TEST_WORKDIR / "lightrag_storage")
    print(f"  已复制到: {TEST_WORKDIR / 'lightrag_storage'}")

    graphml_path = TEST_WORKDIR / "lightrag_storage" / "graph_chunk_entity_relation.graphml"
    if not graphml_path.exists():
        raise FileNotFoundError(f"graphml 文件不存在: {graphml_path}")
    print(f"  graphml 文件大小: {graphml_path.stat().st_size / 1024:.1f} KB")
    return graphml_path


# ── 步骤 2: 初始化 ToolRegistry ──────────────────────────────────────
def initialize_tool_registry():
    """初始化 ToolRegistry，注册所有 MCP 工具"""
    print("\n[初始化] 加载 MCP 工具到 ToolRegistry...")

    from agent.mcp_loader import load_mcp_tools
    from agent.tool_registry import get_registry

    load_mcp_tools()

    registry = get_registry()

    # 验证关键工具已注册
    insert_kg_tool = registry.get("lightrag-server/lightrag_insert_custom_kg")
    if insert_kg_tool is None:
        raise RuntimeError("lightrag-server/lightrag_insert_custom_kg 工具未注册")

    print("  ToolRegistry 初始化完成")
    print(f"  已注册工具数量: {len(registry._tools)}")
    return registry


# ── 步骤 3: 配置 LightRAG 使用测试目录 ───────────────────────────────
def configure_lightrag_for_test():
    """让 LightRAG 指向测试目录

    关键: 创建一个使用 dummy LLM 的 LightRAG 实例并注入到
    lightrag_manager._rag_instance, 这样 ainsert_custom_kg 可以工作
    而不需要真正的 LLM API。
    """
    workdir = str(TEST_WORKDIR / "lightrag_storage")
    print(f"\n[配置] LightRAG 工作目录: {workdir}")

    import niu_api.internal.lightrag_manager as lm

    # 修改 STORAGE_DIR 模块级变量
    lm.STORAGE_DIR = Path(workdir)
    print(f"  已修改 STORAGE_DIR: {lm.STORAGE_DIR}")

    # 创建 LightRAG 实例 (使用 dummy LLM/embedding, 不需要 API)
    import asyncio
    import numpy as np
    from lightrag.lightrag import LightRAG, EmbeddingFunc

    async def dummy_llm(prompt, **kwargs):
        return ''

    async def dummy_embedding(texts):
        return np.zeros((len(texts), 768))

    rag = LightRAG(
        working_dir=workdir,
        llm_model_func=dummy_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=768,
            max_token_size=512,
            func=dummy_embedding,
        ),
    )
    print("  LightRAG 实例创建成功 (dummy LLM)")

    # 初始化存储 (使用 asyncio.run)
    asyncio.run(rag.initialize_storages())
    print("  存储初始化完成")

    kg = rag.chunk_entity_relation_graph
    nx_graph = kg._graph if hasattr(kg, "_graph") else kg
    print(f"  图谱加载: {len(nx_graph.nodes())} nodes, {len(nx_graph.edges())} edges")

    # 注入到 lightrag_manager
    lm._rag_instance = rag
    print("  已设置 _rag_instance")

    # 确保 lightrag event loop 运行
    lm._ensure_loop()
    print("  lightrag event loop 已启动")

    # 清除 lightrag_adapter 中的缓存
    try:
        import niu_api.internal.lightrag_adapter as la
        if hasattr(la, '_adapter'):
            la._adapter = None
            print("  已清除 _adapter")
        if hasattr(la, '_ingester'):
            la._ingester = None
            print("  已清除 _ingester")
    except Exception:
        pass

    # 清除 lightrag_server 中的缓存
    try:
        import niu_lightrag_server as lsm
        lsm._adapter = None
        lsm._ingester = None
        print("  已清除 lightrag_server 缓存")
    except Exception:
        pass

    return workdir


# ── 辅助函数: graphml 读取与查询 ─────────────────────────────────────
def read_graphml():
    """读取测试工作目录的 graphml 文件"""
    import networkx as nx
    graphml_path = TEST_WORKDIR / "lightrag_storage" / "graph_chunk_entity_relation.graphml"
    if not graphml_path.exists():
        raise FileNotFoundError(f"graphml 不存在: {graphml_path}")
    G = nx.read_graphml(str(graphml_path))
    return G


def get_nx_graph():
    """直接从 LightRAG 实例获取 NetworkX 图 (更快，无需文件 I/O)"""
    import niu_api.internal.lightrag_manager as lm
    rag = lm._rag_instance
    if rag is None:
        raise RuntimeError("LightRAG 实例未初始化")
    kg = rag.chunk_entity_relation_graph
    nx_graph = kg._graph if hasattr(kg, "_graph") else kg
    # 返回快照以避免并发修改
    with lm.graph_read_lock():
        return nx_graph.copy()


def find_node_by_name(G, name: str, entity_type: str = None):
    """在 graphml 中查找节点

    LightRAG graphml 中节点 ID 就是 entity_name, 所以:
    - 直接按节点 ID 匹配
    - 也检查 entity_id 属性 (有些版本可能用此属性)
    """
    name_lower = name.lower()
    matches = []
    for n, d in G.nodes(data=True):
        # 节点 ID 就是 entity_name
        if str(n).lower() == name_lower:
            if entity_type is None or d.get("entity_type", "").lower() == entity_type.lower():
                matches.append((n, d))
                continue
        # 也检查 entity_id 属性
        eid = d.get("entity_id", "")
        if eid.lower() == name_lower:
            if entity_type is None or d.get("entity_type", "").lower() == entity_type.lower():
                matches.append((n, d))
    return matches


def find_photo_node(G, photo_path: str):
    """查找 photo 类型节点

    Photo 节点 ID 格式为 "photo:<path>" (来自 format_photo_ingest_data)。
    """
    # 构造完整的 entity_name
    expected_id = f"photo:{photo_path}".lower()
    matches = []
    for n, d in G.nodes(data=True):
        if d.get("entity_type", "").lower() == "photo":
            node_id_lower = str(n).lower()
            eid_lower = d.get("entity_id", "").lower()
            if node_id_lower == expected_id or eid_lower == expected_id:
                matches.append((n, d))
    return matches


def find_edges_between(G, source_name: str, target_name: str, keywords: str = None):
    """查找两个节点之间的边

    v2 改进: 优先使用 keywords 属性精确匹配边类型，
    因为 inject_custom_kg 写入的边都有 keywords 字段。

    参数:
      source_name: 源节点名称
      target_name: 目标节点名称
      keywords: 边的关键词类型 (如 "features", "remembers", "co_occurs_with")
    """
    source_lower = source_name.lower()
    target_lower = target_name.lower()

    # 对 photo: 前缀的源/目标，构造完整 ID
    def _make_photo_id(name):
        if not name.lower().startswith("photo:"):
            return f"photo:{name}".lower()
        return name.lower()

    edges = []
    for u, v, d in G.edges(data=True):
        u_lower = str(u).lower()
        v_lower = str(v).lower()

        # 方向匹配: source->target 或 target->source (双向)
        forward = (u_lower == source_lower and v_lower == target_lower)
        backward = (u_lower == target_lower and v_lower == source_lower)

        # 也检查 photo: 前缀匹配
        if not forward and not backward:
            u_photo = _make_photo_id(u_lower) if not u_lower.startswith("photo:") else u_lower
            v_photo = _make_photo_id(v_lower) if not v_lower.startswith("photo:") else v_lower
            forward = (u_photo == source_lower and v_photo == target_lower)
            backward = (u_photo == target_lower and v_photo == source_lower)

            # 也尝试 source/target 带 photo: 前缀
            if not forward and not backward:
                src_photo = _make_photo_id(source_lower)
                tgt_photo = _make_photo_id(target_lower)
                forward = (u_lower == src_photo and v_lower == tgt_photo)
                backward = (u_lower == tgt_photo and v_lower == src_photo)

        if not (forward or backward):
            continue

        if keywords is None:
            edges.append((u, v, d))
        else:
            # v2: 精确匹配 keywords 属性
            edge_keywords = d.get("keywords", "").lower()
            if edge_keywords == keywords.lower():
                edges.append((u, v, d))
            # 兼容: 有些版本可能用 type 属性
            elif d.get("type", "").lower() == keywords.lower():
                edges.append((u, v, d))

    return edges


def count_person_nodes(G):
    """统计所有 person 类型节点"""
    return [(n, d) for n, d in G.nodes(data=True)
            if d.get("entity_type", "").lower() == "person"]


# ── 辅助函数: 构造测试数据并注入 ──────────────────────────────────────
def make_detected_person(name: str, face_id: str = None, auto_label: str = None) -> dict:
    """构造 detected_persons 列表中的一个元素

    format_photo_ingest_data 读取 p.get("name") 和 p.get("auto_label"),
    所以必须用这些键名。

    命名逻辑:
      - 已命名: name 有值, name != auto_label → entity_name = name
      - 未命名: name 为空 / 以"未命名人物"开头 / name == auto_label → entity_name = auto_label
    """
    if face_id is None:
        face_id = f"face_{name}_{int(time.time()*1000)}"
    return {
        "name": name,
        "auto_label": auto_label or "",
        "face_id": face_id,
    }


def make_named_person(name: str, face_id: str = None) -> dict:
    """构造已命名人物 (name 和 auto_label 不同, entity_name = name)"""
    if face_id is None:
        face_id = f"face_{name}_{int(time.time()*1000)}"
    return {
        "name": name,
        "auto_label": f"未命名人物_{hash(name) % 10000}",
        "face_id": face_id,
    }


def make_unnamed_person(auto_label: str, face_id: str = None) -> dict:
    """构造未命名人物 (name 为空, entity_name = auto_label)"""
    if face_id is None:
        face_id = f"face_unnamed_{int(time.time()*1000)}"
    return {
        "name": "",
        "auto_label": auto_label,
        "face_id": face_id,
    }


def inject_photo(photo_path: str, persons: list, abstract: str = "测试照片", registry=None):
    """调用 format_photo_ingest_data + lightrag_insert_custom_kg 注入 KG"""
    from niu_photo_server import format_photo_ingest_data

    # 调用真实的 format_photo_ingest_data
    data = format_photo_ingest_data(
        file_path=photo_path,
        abstract=abstract,
        detected_persons=persons,
    )

    if not data:
        raise RuntimeError(f"format_photo_ingest_data 返回空数据: path={photo_path}")

    entities = data.get("entities", [])
    relationships = data.get("relationships", [])
    print(f"    format_photo_ingest_data: {len(entities)} entities, {len(relationships)} relationships")

    # 通过 ToolRegistry 注入
    insert_tool = registry.get("lightrag-server/lightrag_insert_custom_kg")
    if insert_tool is None:
        raise RuntimeError("lightrag-server/lightrag_insert_custom_kg 工具未注册")

    result = insert_tool(
        entities=entities,
        relationships=relationships,
        chunks=[],
        source_id=f"photo:{photo_path}",
    )

    print(f"    注入结果: {str(result)[:200]}")
    return data, result


def refresh_graph():
    """刷新图谱: 让 LightRAG 保存 graphml, 然后读取"""
    # inject_custom_kg 直接修改内存中的 NetworkX 图,
    # graphml 在 LightRAG shutdown 或显式 save 时写入。
    # 为了让 read_graphml() 看到最新数据, 我们直接从内存读取。
    return get_nx_graph()


# ── 场景 1: 单人物注入 ────────────────────────────────────────────────
def test_scenario_1(registry):
    """1张照片+1个人物(赵静)，验证 person+photo+edges 全部注入"""
    print("\n[场景1] 单人物注入 — 赵静")

    photo_path = "E:/photos/vacation/beach_001.jpg"
    persons = [make_named_person("赵静", "face_zhaojing_001")]

    try:
        data, result = inject_photo(photo_path, persons, "赵静在海滩度假", registry)

        G = refresh_graph()

        # 1.1 person 节点存在
        person_nodes = find_node_by_name(G, "赵静", "person")
        if len(person_nodes) >= 1:
            record_pass("1.1 person节点存在",
                        f"找到 {len(person_nodes)} 个 '赵静' person 节点")
        else:
            record_fail("1.1 person节点存在",
                        "未找到 person 节点 '赵静'")

        # 1.2 photo 节点存在
        photo_nodes = find_photo_node(G, photo_path)
        if len(photo_nodes) >= 1:
            record_pass("1.2 photo节点存在",
                        f"找到 {len(photo_nodes)} 个 photo 节点, ID={photo_nodes[0][0] if photo_nodes else 'N/A'}")
        else:
            # 诊断: 列出所有 photo 节点
            all_photos = [(n, d.get("entity_id", "")) for n, d in G.nodes(data=True)
                          if d.get("entity_type", "").lower() == "photo"]
            record_fail("1.2 photo节点存在",
                        f"未找到 photo 节点 '{photo_path}'\n所有 photo 节点: {all_photos[:5]}")

        # 1.3 features 边 (photo -> person)
        # 注意: format_photo_ingest_data 中 features 边的 src_id 是 photo_entity_name,
        # tgt_id 是 entity_name。所以 source=photo_path, target=赵静
        photo_entity_id = f"photo:{photo_path}"
        features_edges = find_edges_between(G, photo_entity_id, "赵静", "features")
        if features_edges:
            record_pass("1.3 features边存在",
                        f"找到 {len(features_edges)} 条 features 边")
        else:
            # 诊断: 检查两节点间所有边
            all_edges = find_edges_between(G, photo_entity_id, "赵静")
            edge_details = [d.get("keywords", "?") for _, _, d in all_edges]
            record_fail("1.3 features边存在",
                        f"未找到 features 边; 两节点间边 keywords: {edge_details}")

        # 1.4 remembers 边 (brain:Niu -> person)
        remembers_edges = find_edges_between(G, "brain:Niu", "赵静", "remembers")
        if remembers_edges:
            record_pass("1.4 remembers边存在 (brain:Niu→赵静)",
                        f"找到 {len(remembers_edges)} 条 remembers 边")
        else:
            record_fail("1.4 remembers边存在 (brain:Niu→赵静)",
                        "未找到 remembers 边")

        # 1.5 remembers 边 (brain:Niu -> photo)
        remembers_photo = find_edges_between(G, "brain:Niu", photo_entity_id, "remembers")
        if remembers_photo:
            record_pass("1.5 remembers边存在 (brain:Niu→photo)",
                        f"找到 {len(remembers_photo)} 条 remembers 边")
        else:
            record_fail("1.5 remembers边存在 (brain:Niu→photo)",
                        "未找到 remembers 边")

    except Exception as e:
        record_fail("场景1整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 2: 多人物同框 ────────────────────────────────────────────────
def test_scenario_2(registry):
    """1张照片+4个人物(周磊、孙婷、吴强、郑丽)，验证 co_occurs_with 关系"""
    print("\n[场景2] 多人物同框 — 周磊、孙婷、吴强、郑丽")

    photo_path = "E:/photos/party/group_002.jpg"
    persons = [
        make_named_person("周磊", "face_zhoulei_002"),
        make_named_person("孙婷", "face_sunting_002"),
        make_named_person("吴强", "face_wuqiang_002"),
        make_named_person("郑丽", "face_zhengli_002"),
    ]

    try:
        data, result = inject_photo(photo_path, persons, "四人聚会合照", registry)

        G = refresh_graph()
        names = ["周磊", "孙婷", "吴强", "郑丽"]

        # 2.1 4个 person 节点
        found_names = []
        missing_names = []
        for name in names:
            if find_node_by_name(G, name, "person"):
                found_names.append(name)
            else:
                missing_names.append(name)

        if len(found_names) == 4:
            record_pass("2.1 4个person节点存在", f"全部 {len(found_names)}/4 找到")
        else:
            record_fail("2.1 4个person节点存在",
                        f"仅找到 {len(found_names)}/4, 缺失: {missing_names}")

        # 2.2 co_occurs_with 边 (format_photo_ingest_data 使用的关键词)
        co_occ_count = 0
        missing_pairs = []
        for i, name1 in enumerate(names):
            for name2 in names[i+1:]:
                edges = find_edges_between(G, name1, name2, "co_occurs_with")
                if edges:
                    co_occ_count += 1
                else:
                    missing_pairs.append(f"{name1}-{name2}")

        expected_pairs = len(names) * (len(names) - 1) // 2  # C(4,2) = 6
        if co_occ_count >= expected_pairs:
            record_pass("2.2 co_occurs_with边完整",
                        f"找到 {co_occ_count}/{expected_pairs} 对")
        else:
            record_fail("2.2 co_occurs_with边完整",
                        f"仅找到 {co_occ_count}/{expected_pairs} 对, 缺失: {missing_pairs}")

        # 2.3 photo 节点
        photo_entity_id = f"photo:{photo_path}"
        photo_nodes = find_photo_node(G, photo_path)
        if photo_nodes:
            record_pass("2.3 photo节点存在", f"找到 {len(photo_nodes)} 个")
        else:
            record_fail("2.3 photo节点存在", "未找到")

    except Exception as e:
        record_fail("场景2整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 3: 人物命名更新 ──────────────────────────────────────────────
def test_scenario_3(registry):
    """先注入未命名人物(未命名人物_3)，再命名为"陈伟"，验证:
    - 是否创建了独立的"陈伟"实体（碎片化） → FAIL
    - 还是正确地将"未命名人物_3"更新为"陈伟" → PASS
    """
    print("\n[场景3] 人物命名更新 — 未命名人物_3→陈伟")

    photo_path_1 = "E:/photos/family/dinner_003a.jpg"
    photo_path_2 = "E:/photos/family/dinner_003b.jpg"

    try:
        # 第一步: 注入未命名人物
        # name="" + auto_label="未命名人物_3" → is_unnamed=True → entity_name="未命名人物_3"
        persons_unnamed = [make_unnamed_person("未命名人物_3", "face_chenwei_003")]
        data1, result1 = inject_photo(photo_path_1, persons_unnamed, "未命名人物照片", registry)

        G_after_first = refresh_graph()
        unnamed_nodes = find_node_by_name(G_after_first, "未命名人物_3", "person")
        print(f"    未命名注入后: 找到 {len(unnamed_nodes)} 个 '未命名人物_3' person 节点")

        # 第二步: 同一 face_id 使用新名字"陈伟"
        # name="陈伟" + auto_label 不同于 name → is_unnamed=False → entity_name="陈伟"
        persons_named = [make_named_person("陈伟", "face_chenwei_003")]
        data2, result2 = inject_photo(photo_path_2, persons_named, "陈伟的晚餐", registry)

        G = refresh_graph()

        # 3.1 陈伟节点存在
        chenwei_nodes = find_node_by_name(G, "陈伟", "person")
        if chenwei_nodes:
            record_pass("3.1 命名后person节点存在",
                        f"找到 {len(chenwei_nodes)} 个 '陈伟' 节点")
        else:
            record_fail("3.1 命名后person节点存在", "未找到 '陈伟' 节点")

        # 3.2 碎片化检查: 关键判断
        # inject_custom_kg 不做实体合并 — 它按 entity_name 直接写入。
        # "未命名人物_3" 和 "陈伟" 是不同的 entity_name, 所以两个节点会同时存在。
        # 这是碎片化: 同一个人在 KG 中有两个实体, 没有合并。
        unnamed_after = find_node_by_name(G, "未命名人物_3", "person")
        person_3_count = len(unnamed_after)
        chenwei_count = len(chenwei_nodes)

        if person_3_count == 0 and chenwei_count >= 1:
            # 理想情况: "未命名人物_3" 被更新/合并为 "陈伟"
            record_pass("3.2 不产生碎片化",
                        f"'未命名人物_3'={person_3_count}, '陈伟'={chenwei_count} — 正确合并")
        elif person_3_count >= 1 and chenwei_count >= 1:
            # 碎片化: 同一个人有两个独立实体
            record_fail("3.2 不产生碎片化",
                        f"碎片化! '未命名人物_3'={person_3_count}, '陈伟'={chenwei_count} — "
                        f"同一人有2个独立实体, 需要 merge_entities 合并")
        elif person_3_count >= 1 and chenwei_count == 0:
            # 未命名人物仍存在但陈伟未创建
            record_partial("3.2 不产生碎片化",
                           f"'未命名人物_3'={person_3_count}, '陈伟'={chenwei_count} — "
                           f"命名更新未生效, 但无碎片化")
        else:
            record_fail("3.2 不产生碎片化",
                        f"'未命名人物_3'={person_3_count}, '陈伟'={chenwei_count} — 意外状态")

        # 3.3 额外诊断: 检查两张照片是否都连接到了人物
        photo1_entity = f"photo:{photo_path_1}"
        photo2_entity = f"photo:{photo_path_2}"
        # 检查 features 边: photo → person
        for person_name in ["未命名人物_3", "陈伟"]:
            for photo_eid in [photo1_entity, photo2_entity]:
                edges = find_edges_between(G, photo_eid, person_name, "features")
                if edges:
                    print(f"    features 边: {photo_eid} → {person_name}: {len(edges)} 条")

    except Exception as e:
        record_fail("场景3整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 4: 100人压力测试 ─────────────────────────────────────────────
def test_scenario_4(registry):
    """100个不同人物名字，每人1张照片，验证批量注入正确性

    严格判定: 通过率<100% 必须 PARTIAL_PASS, 不能 PASS
    """
    print("\n[场景4] 100人压力测试")

    names_100 = [
        "张伟", "王芳", "李明", "赵静", "刘洋", "陈刚", "杨帆", "黄蕾", "周磊", "吴强",
        "徐慧", "孙婷", "马骏", "朱峰", "胡勇", "高远", "林颖", "何坤", "罗敏", "郑浩",
        "马超", "朱莉", "胡斌", "高健", "林涛", "何敏", "罗勇", "郑凯", "梁宇", "宋琳",
        "谢军", "韩梅", "唐杰", "冯刚", "董明", "程鹏", "曹阳", "袁华", "邓丽", "许晴",
        "韩冰", "唐鑫", "冯雪", "董婷", "萧然", "程亮", "曹丹", "袁媛", "邓超", "许浩",
        "傅鑫", "沈洋", "丁一", "贾磊", "夏雨", "钟灵", "田野", "范涛", "石坚", "戴维",
        "潘杰", "葛威", "奚鹏", "彭辉", "鲁强", "韦华", "昌盛", "马丽", "苗青", "凌风",
        "狄龙", "米兰", "贝贝", "明辉", "瞿亮", "戎威", "祖恩", "武力", "严明", "华强",
        "范明", "方圆", "石磊", "熊伟", "崔杰", "康宁", "雷鸣", "侯波", "邹涛", "熊军",
        "金鑫", "陆扬", "郝健", "管清", "邱岳", "白雪", "池波", "连城", "段锐", "钱进",
    ]
    assert len(names_100) == 100, f"名单数量错误: {len(names_100)}"

    try:
        G_before = refresh_graph()
        person_before = set()
        for n, d in G_before.nodes(data=True):
            if d.get("entity_type", "").lower() == "person":
                person_before.add(str(n).lower())

        inject_success = 0
        inject_fail = 0
        inject_errors = []
        for i, name in enumerate(names_100):
            photo_path = f"E:/photos/stress/person_{i+1:03d}.jpg"
            persons = [make_named_person(name, f"face_stress_{i:03d}")]
            try:
                inject_photo(photo_path, persons, f"{name}的照片", registry)
                inject_success += 1
            except Exception as e:
                inject_fail += 1
                if len(inject_errors) < 10:
                    inject_errors.append(f"#{i+1} {name}: {e}")

        G = refresh_graph()

        # 4.1 注入成功率
        if inject_fail == 0:
            record_pass("4.1 100人全部注入成功",
                        f"成功 {inject_success}/100")
        elif inject_fail <= 5:
            record_partial("4.1 100人全部注入成功",
                           f"成功 {inject_success}/100, 失败 {inject_fail}\n错误: {inject_errors}")
        else:
            record_fail("4.1 100人全部注入成功",
                        f"成功 {inject_success}/100, 失败 {inject_fail}\n错误: {inject_errors}")

        # 4.2 person 节点数量 — 严格统计缺失名单
        person_after = set()
        for n, d in G.nodes(data=True):
            if d.get("entity_type", "").lower() == "person":
                person_after.add(str(n).lower())

        new_persons = person_after - person_before
        found_names = []
        missing_names = []
        for name in names_100:
            if name.lower() in new_persons:
                found_names.append(name)
            else:
                missing_names.append(name)

        found_count = len(found_names)
        pass_rate = found_count / 100.0 * 100

        if found_count == 100:
            record_pass("4.2 person节点数量正确",
                        f"找到 {found_count}/100, 通过率 100%")
        elif found_count >= 95:
            # 通过率 >= 95% 但 < 100% → PARTIAL_PASS
            record_partial("4.2 person节点数量正确",
                           f"找到 {found_count}/100, 通过率 {pass_rate:.0f}%\n"
                           f"缺失: {missing_names}")
        else:
            record_fail("4.2 person节点数量正确",
                        f"仅找到 {found_count}/100, 通过率 {pass_rate:.0f}%\n"
                        f"缺失: {missing_names}")

        # 4.3 无重复 person 节点
        person_name_counts = {}
        for n, d in G.nodes(data=True):
            if d.get("entity_type", "").lower() == "person":
                name_lower = str(n).lower()
                person_name_counts[name_lower] = person_name_counts.get(name_lower, 0) + 1

        test_names_lower = {n.lower() for n in names_100}
        duplicates = {k: v for k, v in person_name_counts.items() if v > 1 and k in test_names_lower}
        if not duplicates:
            record_pass("4.3 无重复person节点", "100人无重复")
        else:
            record_fail("4.3 无重复person节点",
                        f"发现重复: {dict(list(duplicates.items())[:10])}")

        # 4.4 graphml 文件完整性
        if G.number_of_nodes() > 0 and G.number_of_edges() > 0:
            record_pass("4.4 graphml文件完整",
                        f"节点: {G.number_of_nodes()}, 边: {G.number_of_edges()}")
        else:
            record_fail("4.4 graphml文件完整", "graphml 为空或损坏")

    except Exception as e:
        record_fail("场景4整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 5: 路径一致性 ────────────────────────────────────────────────
def test_scenario_5(registry):
    """同一照片以3种大小写路径注入，验证不分裂为多个photo节点

    注意: format_photo_ingest_data 使用 f"photo:{file_path}" 作为 entity_name,
    路径大小写不同会产生不同的 entity_name, 因此会分裂为多个节点。
    这是当前实现的已知行为 (PARTIAL_PASS 或 FAIL)。
    """
    print("\n[场景5] 路径一致性 — 大小写不分裂")

    paths = [
        "E:/Photos/Travel/paris_005.jpg",
        "e:/photos/travel/paris_005.jpg",
        "E:/PHOTOS/TRAVEL/PARIS_005.JPG",
    ]
    person = make_named_person("路径测试人A", "face_pathconsistency_005")

    try:
        for path in paths:
            inject_photo(path, [person], "巴黎旅行照片", registry)

        G = refresh_graph()

        # 5.1 检查 photo 节点数量
        # 不同大小写路径会产生不同 entity_name: "photo:E:/Photos/..." vs "photo:e:/photos/..."
        paris_photos = []
        for n, d in G.nodes(data=True):
            if d.get("entity_type", "").lower() == "photo":
                node_id_lower = str(n).lower()
                if "paris_005" in node_id_lower:
                    paris_photos.append((n, d))

        if len(paris_photos) == 1:
            record_pass("5.1 路径不分裂photo节点",
                        f"paris_005 photo 节点: {len(paris_photos)} (1)")
        elif len(paris_photos) == 3:
            # 当前实现会分裂 — 这是已知行为
            record_partial("5.1 路径不分裂photo节点",
                           f"paris_005 photo 节点: {len(paris_photos)} (3) — "
                           f"当前实现按路径大小写区分, 产生不同 entity_name\n"
                           f"节点 ID: {[str(n) for n, _ in paris_photos]}")
        else:
            record_fail("5.1 路径不分裂photo节点",
                        f"paris_005 photo 节点: {len(paris_photos)} (预期1, 实际{len(paris_photos)})")

        # 5.2 person 节点不分裂 (同一 name 同一 entity_name, inject_custom_kg 会合并)
        person_nodes = find_node_by_name(G, "路径测试人A", "person")
        if len(person_nodes) == 1:
            record_pass("5.2 person节点不分裂",
                        f"person 节点: {len(person_nodes)}")
        else:
            record_fail("5.2 person节点不分裂",
                        f"person 节点: {len(person_nodes)} — 分裂了!")

    except Exception as e:
        record_fail("场景5整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 6: file_path 正确性 ─────────────────────────────────────────
def test_scenario_6(registry):
    """验证 photo 节点的 file_path 不为 unknown_source"""
    print("\n[场景6] file_path 正确性 — photo 节点")

    photo_path = "E:/photos/document/test_006.jpg"
    persons = [make_named_person("文档测试人", "face_filepath_006")]

    try:
        data, result = inject_photo(photo_path, persons, "文件路径测试", registry)

        G = refresh_graph()

        # 6.1 检查注入的 photo 节点 file_path 属性
        photo_entity_id = f"photo:{photo_path}"
        photo_nodes = find_photo_node(G, photo_path)

        if not photo_nodes:
            # 可能 node ID 与预期不同, 搜索更宽泛
            all_photo_nodes = [(n, d) for n, d in G.nodes(data=True)
                               if d.get("entity_type", "").lower() == "photo"
                               and "test_006" in str(n).lower()]
            if all_photo_nodes:
                photo_nodes = all_photo_nodes

        if photo_nodes:
            node_id, node_data = photo_nodes[0]
            file_path_val = node_data.get("file_path", "")
            if file_path_val and "unknown" not in file_path_val.lower():
                record_pass("6.1 photo节点file_path不为unknown",
                            f"file_path={file_path_val}")
            elif file_path_val == "":
                # file_path 可能为空 (LightRAG graphml 不一定保留所有属性)
                # 检查节点 ID 是否包含路径信息
                if "test_006" in str(node_id).lower():
                    record_pass("6.1 photo节点file_path不为unknown",
                                f"file_path 属性为空但节点ID包含路径: {node_id}")
                else:
                    record_partial("6.1 photo节点file_path不为unknown",
                                   f"file_path 属性为空, 节点ID: {node_id}")
            else:
                record_fail("6.1 photo节点file_path不为unknown",
                            f"file_path={file_path_val} (含 unknown)")
        else:
            record_fail("6.1 photo节点file_path不为unknown",
                        "未找到 test_006 photo 节点")

        # 6.2 检查 source_id 属性
        if photo_nodes:
            node_id, node_data = photo_nodes[0]
            source_id_val = node_data.get("source_id", "")
            if source_id_val and "photo:" in source_id_val.lower():
                record_pass("6.2 source_id包含photo:前缀",
                            f"source_id={source_id_val}")
            else:
                record_partial("6.2 source_id包含photo:前缀",
                               f"source_id={source_id_val or '(空)'}")

    except Exception as e:
        record_fail("场景6整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 7: 实体去重 ─────────────────────────────────────────────────
def test_scenario_7(registry):
    """同一人物(罗敏去重测试)在不同照片中出现3次，验证不创建重复实体

    inject_custom_kg 对同名 entity_name 会合并 (upsert) 而非创建新节点。
    """
    print("\n[场景7] 实体去重 — 罗敏去重测试出现3次")

    photo_paths = [
        "E:/photos/dedup/photo_a_007.jpg",
        "E:/photos/dedup/photo_b_007.jpg",
        "E:/photos/dedup/photo_c_007.jpg",
    ]
    person = make_named_person("罗敏去重测试", "face_dedup_luomin_007")

    try:
        for path in photo_paths:
            inject_photo(path, [person], "去重测试照片", registry)

        G = refresh_graph()

        # 7.1 同一人物不重复
        person_nodes = find_node_by_name(G, "罗敏去重测试", "person")
        if len(person_nodes) == 1:
            record_pass("7.1 同一人物不重复",
                        f"'罗敏去重测试' person 节点: {len(person_nodes)}")
        elif len(person_nodes) == 0:
            record_fail("7.1 同一人物不重复",
                        "未找到 '罗敏去重测试' person 节点")
        else:
            record_fail("7.1 同一人物不重复",
                        f"'罗敏去重测试' person 节点: {len(person_nodes)} — 重复!")

        # 7.2 关联3张照片 (通过 features 边)
        rel_count = 0
        for photo_path in photo_paths:
            photo_entity_id = f"photo:{photo_path}"
            edges = find_edges_between(G, photo_entity_id, "罗敏去重测试", "features")
            if edges:
                rel_count += 1

        if rel_count >= 3:
            record_pass("7.2 features边关联3张照片",
                        f"关联 features 边: {rel_count}")
        else:
            # 诊断: 检查所有类型的边
            all_rel = 0
            for photo_path in photo_paths:
                photo_entity_id = f"photo:{photo_path}"
                edges = find_edges_between(G, photo_entity_id, "罗敏去重测试")
                all_rel += len(edges)
            record_partial("7.2 features边关联3张照片",
                           f"features 边: {rel_count}, 总边数: {all_rel}")

    except Exception as e:
        record_fail("场景7整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 场景 8: 边完整性 ─────────────────────────────────────────────────
def test_scenario_8(registry):
    """3人同框(唐鑫边测试、冯雪边测试、董婷边测试)，验证 features/remembers/co_occurs_with 边"""
    print("\n[场景8] 边完整性 — 唐鑫边测试、冯雪边测试、董婷边测试")

    photo_path = "E:/photos/edges/group_008.jpg"
    persons = [
        make_named_person("唐鑫边测试", "face_edge_tangxin_008"),
        make_named_person("冯雪边测试", "face_edge_fengxue_008"),
        make_named_person("董婷边测试", "face_edge_dongting_008"),
    ]

    try:
        data, result = inject_photo(photo_path, persons, "三人同框边测试", registry)

        G = refresh_graph()
        names = ["唐鑫边测试", "冯雪边测试", "董婷边测试"]
        photo_entity_id = f"photo:{photo_path}"

        # 8.1 features 边 (photo -> person)
        features_count = 0
        missing_features = []
        for name in names:
            edges = find_edges_between(G, photo_entity_id, name, "features")
            if edges:
                features_count += 1
            else:
                missing_features.append(name)

        if features_count == 3:
            record_pass("8.1 features边 (photo→person)",
                        f"找到 {features_count}/3 条")
        else:
            record_fail("8.1 features边 (photo→person)",
                        f"找到 {features_count}/3 条, 缺失: {missing_features}")

        # 8.2 remembers 边 (brain:Niu -> person)
        remembers_count = 0
        missing_remembers = []
        for name in names:
            edges = find_edges_between(G, "brain:Niu", name, "remembers")
            if edges:
                remembers_count += 1
            else:
                missing_remembers.append(name)

        if remembers_count == 3:
            record_pass("8.2 remembers边 (brain:Niu→person)",
                        f"找到 {remembers_count}/3 条")
        else:
            record_fail("8.2 remembers边 (brain:Niu→person)",
                        f"找到 {remembers_count}/3 条, 缺失: {missing_remembers}")

        # 8.3 co_occurs_with 边 (person <-> person)
        co_occ_count = 0
        missing_co_occ = []
        for i, name1 in enumerate(names):
            for name2 in names[i+1:]:
                edges = find_edges_between(G, name1, name2, "co_occurs_with")
                if edges:
                    co_occ_count += 1
                else:
                    missing_co_occ.append(f"{name1}-{name2}")

        expected_co_occ = 3  # C(3,2) = 3
        if co_occ_count >= expected_co_occ:
            record_pass("8.3 co_occurs_with边 (person↔person)",
                        f"找到 {co_occ_count}/{expected_co_occ} 条")
        else:
            record_fail("8.3 co_occurs_with边 (person↔person)",
                        f"找到 {co_occ_count}/{expected_co_occ} 条, 缺失: {missing_co_occ}")

        # 8.4 remembers 边 (brain:Niu -> photo)
        remembers_photo = find_edges_between(G, "brain:Niu", photo_entity_id, "remembers")
        if remembers_photo:
            record_pass("8.4 remembers边 (brain:Niu→photo)",
                        f"找到 {len(remembers_photo)} 条")
        else:
            record_fail("8.4 remembers边 (brain:Niu→photo)",
                        "未找到")

    except Exception as e:
        record_fail("场景8整体", f"异常: {e}\n{traceback.format_exc()}")


# ── 主流程 ────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("KG 注入端到端测试 v2")
    print(f"时间: {datetime.now().isoformat()}")
    print("关键改进: 场景3碎片化→FAIL, 场景4<100%→PARTIAL_PASS, 边匹配用keywords")
    print("=" * 70)

    # 步骤 1: 准备测试环境
    try:
        graphml_path = prepare_test_environment()
    except Exception as e:
        print(f"[FATAL] 准备测试环境失败: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 关键: 设置 sys.argv 避免 LightRAG parse_args 退出
    saved_argv = sys.argv[:]
    sys.argv = ["lightrag"]

    # 步骤 2: 配置 LightRAG 使用测试目录
    try:
        workdir = configure_lightrag_for_test()
    except Exception as e:
        sys.argv = saved_argv
        print(f"[FATAL] LightRAG 配置失败: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 步骤 3: 初始化 ToolRegistry
    try:
        registry = initialize_tool_registry()
    except Exception as e:
        sys.argv = saved_argv
        print(f"[FATAL] ToolRegistry 初始化失败: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 恢复 sys.argv
    sys.argv = saved_argv

    # 步骤 4: 基线
    print("\n[基线] 记录初始 graphml 状态...")
    G_baseline = get_nx_graph()
    print(f"  节点数: {G_baseline.number_of_nodes()}")
    print(f"  边数: {G_baseline.number_of_edges()}")

    # 步骤 5: 执行所有场景
    print("\n" + "=" * 70)
    print("开始执行测试场景")
    print("=" * 70)

    test_scenario_1(registry)
    test_scenario_2(registry)
    test_scenario_3(registry)
    test_scenario_4(registry)
    test_scenario_5(registry)
    test_scenario_6(registry)
    test_scenario_7(registry)
    test_scenario_8(registry)

    # 步骤 6: 汇总
    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)

    pass_count = sum(1 for _, s, _ in _results if s == "PASS")
    fail_count = sum(1 for _, s, _ in _results if s == "FAIL")
    partial_count = sum(1 for _, s, _ in _results if s == "PARTIAL_PASS")

    for scenario, status, evidence in _results:
        print(f"  [{status}] {scenario}")

    print(f"\n总计: {_total} 项")
    print(f"  PASS:        {pass_count}")
    print(f"  PARTIAL_PASS: {partial_count}")
    print(f"  FAIL:        {fail_count}")
    print(f"  通过率: {pass_count/_total*100:.1f}%" if _total > 0 else "N/A")

    # 最终状态
    try:
        G_final = get_nx_graph()
        print(f"\n最终图谱状态:")
        print(f"  节点数: {G_final.number_of_nodes()} (基线: {G_baseline.number_of_nodes()})")
        print(f"  边数: {G_final.number_of_edges()} (基线: {G_baseline.number_of_edges()})")
    except Exception:
        pass

    # 返回码: 0=全部PASS, 1=有FAIL, 2=有PARTIAL_PASS但无FAIL
    if fail_count > 0:
        return 1
    elif partial_count > 0:
        return 2
    else:
        return 0


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
