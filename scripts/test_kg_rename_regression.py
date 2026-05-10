#!/usr/bin/env python3
"""
KG 更名回归测试 — 复现更名后实体分裂问题

测试两个核心回归问题：
1. 人物实体分裂：更名后出现两个实体 — 已命名实体（如"张三"）+ 未命名实体（"未命名人物_1"仍存在）
2. 照片实体分裂：出现两个照片实体 — photo:xxx（冒号，正确）+ photo xxx（空格，错误）

用法:
  python scripts/test_kg_rename_regression.py

前置条件:
  - API 服务器已启动（LightRAG 已初始化）
  - 测试照片文件存在
"""
import sys
import os
import json
import time
from pathlib import Path

# ──────────────────────────────────────────────
# 路径设置
# ──────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# MCP server workdirs — 复用参考脚本的路径发现逻辑
MCP_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "mcp-servers.yaml")
if os.path.exists(MCP_CONFIG_PATH):
    try:
        import yaml
        with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
            mcp_config = yaml.safe_load(f) or {}
        for sn, sc in mcp_config.items():
            if isinstance(sc, dict) and "workdir" in sc:
                wd = os.path.normpath(os.path.join(PROJECT_ROOT, sc["workdir"]))
                if os.path.exists(wd) and wd not in sys.path:
                    sys.path.insert(0, wd)
    except ImportError:
        pass

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────
PRODUCTION_STORAGE = os.path.expanduser("~/.niu/lightrag_storage/")
TEST_PHOTO_PATH = r"REDACTED_WIN_PATH\2026\05\2026-05-10\20090603_092316.jpg"
RENAME_TARGET = "测试更名回归"  # 更名后的名字

pass_count = 0
fail_count = 0


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────
def _pass(msg):
    global pass_count
    pass_count += 1
    print(f"  [PASS] {msg}")


def _fail(msg):
    global fail_count
    fail_count += 1
    print(f"  [FAIL] {msg}")


def _section(title):
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def read_graphml(storage_dir):
    """从 GraphML 文件读取完整图谱（节点+边）。"""
    import networkx as nx

    fp = os.path.join(storage_dir, "graph_chunk_entity_relation.graphml")
    if not os.path.exists(fp):
        return {}, []
    G = nx.read_graphml(fp)
    nodes = {nid: dict(attrs) for nid, attrs in G.nodes(data=True)}
    edges = [{"src": s, "tgt": t, **dict(a)} for s, t, a in G.edges(data=True)]
    return nodes, edges


def find_nodes_by_prefix(nodes, prefix):
    """查找 entity_id 以某前缀开头的节点。"""
    return {
        nid: attrs
        for nid, attrs in nodes.items()
        if attrs.get("entity_id", "").startswith(prefix)
    }


def find_nodes_containing(nodes, keyword):
    """查找 entity_id 包含某关键词的节点。"""
    return {
        nid: attrs
        for nid, attrs in nodes.items()
        if keyword in attrs.get("entity_id", "")
    }


def find_nodes_by_entity_id(nodes, entity_id):
    """查找 entity_id 精确匹配的节点。"""
    for nid, attrs in nodes.items():
        if attrs.get("entity_id", "") == entity_id:
            return nid, attrs
    return None, None


# ──────────────────────────────────────────────
# Phase 1: 入库前快照
# ──────────────────────────────────────────────
def take_baseline():
    """记录入库前的图谱状态作为基线。"""
    _section("Phase 1: 入库前基线")

    nodes, edges = read_graphml(PRODUCTION_STORAGE)
    print(f"  图谱节点数: {len(nodes)}")
    print(f"  图谱边数: {len(edges)}")

    # 按类型统计
    photo_colon = find_nodes_by_prefix(nodes, "photo:")
    photo_space = find_nodes_containing(nodes, "photo ")
    unnamed = find_nodes_containing(nodes, "未命名人物")
    named_target = find_nodes_containing(nodes, RENAME_TARGET)

    print(f"\n  照片实体(冒号 photo:): {len(photo_colon)} 个")
    print(f"  照片实体(空格 photo ): {len(photo_space)} 个")
    print(f"  未命名人物: {len(unnamed)} 个 — {list(unnamed.keys())[:5]}")
    print(f"  目标名 '{RENAME_TARGET}': {len(named_target)} 个")

    return {
        "nodes": nodes,
        "edges": edges,
        "photo_colon_count": len(photo_colon),
        "photo_space_count": len(photo_space),
        "unnamed_entity_ids": set(unnamed.keys()),
        "named_target_count": len(named_target),
    }


# ──────────────────────────────────────────────
# Phase 2: 入库照片
# ──────────────────────────────────────────────
def ingest_test_photo():
    """调用 ingest_photo 入库测试照片，返回人物信息。"""
    _section("Phase 2: 入库照片 (ingest_photo)")

    # 初始化 LightRAG + ToolRegistry
    try:
        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
        if rag is None:
            _fail("LightRAG 未初始化，请先启动 API 服务器")
            return None
        _pass("LightRAG 初始化成功")
    except Exception as e:
        _fail(f"LightRAG 初始化失败: {e}")
        return None

    try:
        from agent.mcp_loader import load_mcp_tools

        registry = load_mcp_tools()
        _pass(f"ToolRegistry 加载成功: {len(registry._tools)} tools")
    except Exception as e:
        _fail(f"ToolRegistry 加载失败: {e}")
        return None

    # 调用 ingest_photo
    from niu_photo_server import ingest_photo

    print(f"\n  调用 ingest_photo(file_path={TEST_PHOTO_PATH}) ...")
    result = ingest_photo(TEST_PHOTO_PATH)

    if result.get("status") == "success":
        _pass("ingest_photo 返回 success")
        photo_id = result.get("photo_id", "")
        detected_persons = result.get("detected_persons", [])
        abstract = result.get("abstract", "")
        stored_path = result.get("file_path", "")
        print(f"  photo_id: {photo_id}")
        print(f"  abstract: {abstract}")
        print(f"  stored_path: {stored_path}")
        print(f"  detected_persons ({len(detected_persons)}):")
        for p in detected_persons:
            print(
                f"    - id={p.get('id', '?')[:12]}... "
                f"name={p.get('name', '?')} "
                f"auto_label={p.get('auto_label', '?')}"
            )
    else:
        _fail(f"ingest_photo 返回错误: {result}")
        return None

    # 等待 LightRAG 异步处理完成
    wait_seconds = 8
    print(f"\n  等待 LightRAG 处理 ({wait_seconds}秒) ...")
    time.sleep(wait_seconds)

    return {
        "photo_id": photo_id,
        "detected_persons": detected_persons,
        "abstract": abstract,
        "stored_path": stored_path,
    }


# ──────────────────────────────────────────────
# Phase 3: 入库后验证
# ──────────────────────────────────────────────
def verify_after_ingest(baseline):
    """验证入库后的图谱状态。返回未命名人物的信息（供更名使用）。"""
    _section("Phase 3: 入库后验证")

    nodes, edges = read_graphml(PRODUCTION_STORAGE)
    print(f"  图谱节点数: {len(nodes)} (基线 {len(baseline['nodes'])})")
    print(f"  图谱边数: {len(edges)} (基线 {len(baseline['edges'])})")
    print(f"  新增节点: {len(nodes) - len(baseline['nodes'])}")
    print(f"  新增边: {len(edges) - len(baseline['edges'])}")

    expected_stem = Path(TEST_PHOTO_PATH.replace("\\", "/").lower()).stem
    expected_photo_name = f"photo:{expected_stem}"

    # 照片实体（冒号格式，正确）
    photo_colon = find_nodes_by_prefix(nodes, "photo:")
    new_photo_colon = len(photo_colon) - baseline["photo_colon_count"]
    print(f"\n  照片实体(冒号): {len(photo_colon)} 个 (新增 {new_photo_colon})")

    # 照片实体（空格格式，错误变体）
    photo_space = find_nodes_containing(nodes, "photo ")
    new_photo_space = len(photo_space) - baseline["photo_space_count"]
    print(f"  照片实体(空格): {len(photo_space)} 个 (新增 {new_photo_space})")

    # 未命名人物
    unnamed = find_nodes_containing(nodes, "未命名人物")
    new_unnamed = set(unnamed.keys()) - baseline["unnamed_entity_ids"]
    print(f"  未命名人物: {len(unnamed)} 个 (新增 {len(new_unnamed)})")
    if new_unnamed:
        for nid in new_unnamed:
            eid = nodes[nid].get("entity_id", "?")
            etype = nodes[nid].get("entity_type", "?")
            print(f"    - node_id={nid[:20]}... entity_id={eid} type={etype}")

    # ── TEST 1: 照片实体(冒号)应该新增至少1个 ──
    if new_photo_colon >= 1:
        _pass(f"照片实体(冒号)新增 {new_photo_colon} 个 (>=1)")
    else:
        _fail(f"照片实体(冒号)未新增 (新增 {new_photo_colon})")

    # ── TEST 2: 无空格变体照片实体 ──
    if new_photo_space == 0:
        _pass("无空格变体照片实体新增")
    else:
        space_entities = [
            nodes[nid].get("entity_id", nid) for nid in photo_space
        ]
        _fail(
            f"发现 {new_photo_space} 个空格变体照片实体: {space_entities}"
        )

    # ── TEST 3: 找到预期照片实体 ──
    photo_nid, photo_attrs = find_nodes_by_entity_id(nodes, expected_photo_name)
    if photo_nid:
        _pass(f"找到预期照片实体: {expected_photo_name}")
    else:
        _fail(f"未找到预期照片实体: {expected_photo_name}")
        # 列出所有 photo: 实体帮助诊断
        for nid, attrs in photo_colon.items():
            print(f"    - entity_id={attrs.get('entity_id', '?')}")

    # ── TEST 4: 未命名人物应该存在 ──
    if new_unnamed:
        _pass(f"未命名人物存在: {len(new_unnamed)} 个新增")
    else:
        _fail("未命名人物不存在，无法测试更名流程")

    # 返回未命名人物信息供后续使用
    unnamed_info = []
    for nid in new_unnamed:
        attrs = nodes[nid]
        unnamed_info.append({
            "node_id": nid,
            "entity_id": attrs.get("entity_id", ""),
            "entity_type": attrs.get("entity_type", ""),
        })

    return {
        "expected_photo_name": expected_photo_name,
        "photo_node_id": photo_nid,
        "unnamed_entities": unnamed_info,
    }


# ──────────────────────────────────────────────
# Phase 4: 更名人物
# ──────────────────────────────────────────────
def rename_test_person(ingest_info, verify_info):
    """调用 name_person 将未命名人物更名。"""
    _section("Phase 4: 更名人物 (name_person)")

    detected_persons = ingest_info["detected_persons"]

    # 从 ingest_photo 返回的 detected_persons 中找未命名人物
    # name_person 需要 person_id（UUID），不是 KG 实体名
    unnamed_person = None
    for p in detected_persons:
        pname = p.get("name", "")
        auto_label = p.get("auto_label", "")
        is_unnamed = (
            not pname
            or pname.startswith("未命名人物")
            or pname == auto_label
        )
        if is_unnamed:
            unnamed_person = p
            break

    if not unnamed_person:
        _fail("ingest_photo 返回的 detected_persons 中无未命名人物")
        return None

    person_id = unnamed_person["id"]
    old_name = unnamed_person.get("auto_label", unnamed_person.get("name", ""))
    print(f"  person_id (UUID): {person_id}")
    print(f"  旧名 (auto_label): {old_name}")
    print(f"  新名: {RENAME_TARGET}")

    # 调用 name_person
    from niu_photo_server import name_person

    result = name_person(person_id=person_id, name=RENAME_TARGET)
    print(f"\n  name_person 返回: {result}")

    if result.get("status") == "success":
        _pass(f"name_person 返回 success: {old_name} -> {RENAME_TARGET}")
    else:
        _fail(f"name_person 返回错误: {result}")

    # 等待 LightRAG 处理
    wait_seconds = 5
    print(f"\n  等待 LightRAG 处理 ({wait_seconds}秒) ...")
    time.sleep(wait_seconds)

    return {
        "person_id": person_id,
        "old_name": old_name,
        "new_name": RENAME_TARGET,
    }


# ──────────────────────────────────────────────
# Phase 5: 更名后验证（核心回归测试）
# ──────────────────────────────────────────────
def verify_after_rename(baseline, rename_info):
    """验证更名后的图谱状态 — 核心回归测试。"""
    _section("Phase 5: 更名后验证 (核心回归测试)")

    nodes, edges = read_graphml(PRODUCTION_STORAGE)
    old_name = rename_info["old_name"]
    new_name = rename_info["new_name"]

    # ── 统计各类型实体 ──
    photo_colon = find_nodes_by_prefix(nodes, "photo:")
    photo_space = find_nodes_containing(nodes, "photo ")
    named_nodes = find_nodes_containing(nodes, new_name)
    old_unnamed_nodes = find_nodes_containing(nodes, old_name)

    print(f"  照片实体(冒号 photo:): {len(photo_colon)} 个")
    print(f"  照片实体(空格 photo ): {len(photo_space)} 个")
    print(f"  已命名人物 '{new_name}': {len(named_nodes)} 个")
    print(f"  旧未命名人物 '{old_name}': {len(old_unnamed_nodes)} 个")

    # 打印所有相关实体详情
    if named_nodes:
        print(f"\n  已命名人物详情:")
        for nid, attrs in named_nodes.items():
            print(
                f"    - node_id={nid[:30]}... "
                f"entity_id={attrs.get('entity_id', '?')} "
                f"type={attrs.get('entity_type', '?')}"
            )
    if old_unnamed_nodes:
        print(f"\n  旧未命名人物详情 (应该不存在):")
        for nid, attrs in old_unnamed_nodes.items():
            print(
                f"    - node_id={nid[:30]}... "
                f"entity_id={attrs.get('entity_id', '?')} "
                f"type={attrs.get('entity_type', '?')}"
            )

    # ── TEST 5: 已命名人物应该存在 ──
    if named_nodes:
        _pass(f"已命名人物 '{new_name}' 存在 ({len(named_nodes)} 个)")
    else:
        _fail(f"已命名人物 '{new_name}' 不存在")

    # ── TEST 6: 旧未命名人物应该被删除 ──
    if not old_unnamed_nodes:
        _pass(f"旧未命名人物 '{old_name}' 已删除")
    else:
        old_entity_ids = [
            attrs.get("entity_id", nid) for nid, attrs in old_unnamed_nodes.items()
        ]
        _fail(
            f"旧未命名人物 '{old_name}' 仍存在 ({len(old_unnamed_nodes)} 个): "
            f"{old_entity_ids}"
        )

    # ── TEST 7: 人物实体没有分裂（同名实体只有1个） ──
    if len(named_nodes) <= 1:
        _pass(f"已命名人物无分裂 (仅 {len(named_nodes)} 个)")
    else:
        named_entity_ids = [
            attrs.get("entity_id", nid) for nid, attrs in named_nodes.items()
        ]
        _fail(
            f"人物实体分裂! 发现 {len(named_nodes)} 个 '{new_name}' 实体: "
            f"{named_entity_ids}"
        )

    # ── TEST 8: 照片实体没有冒号/空格分裂 ──
    # 找出所有照片 stem，检查是否有冒号版和空格版同时存在
    photo_stems_colon = {}  # stem -> node_id
    photo_stems_space = {}  # stem -> node_id
    for nid, attrs in photo_colon.items():
        eid = attrs.get("entity_id", "")
        if eid.startswith("photo:"):
            stem = eid[len("photo:"):]
            photo_stems_colon[stem] = nid
    for nid, attrs in photo_space.items():
        eid = attrs.get("entity_id", "")
        if eid.startswith("photo "):
            stem = eid[len("photo "):]
            photo_stems_space[stem] = nid

    # 找同时有冒号版和空格版的 stem
    duplicated_stems = set(photo_stems_colon.keys()) & set(photo_stems_space.keys())
    # 也考虑新增的（不在基线中的）
    baseline_nodes = baseline["nodes"]
    baseline_photo_colon = find_nodes_by_prefix(baseline_nodes, "photo:")
    baseline_photo_space = find_nodes_containing(baseline_nodes, "photo ")
    baseline_stems_colon = set()
    baseline_stems_space = set()
    for nid, attrs in baseline_photo_colon.items():
        eid = attrs.get("entity_id", "")
        if eid.startswith("photo:"):
            baseline_stems_colon.add(eid[len("photo:"):])
    for nid, attrs in baseline_photo_space.items():
        eid = attrs.get("entity_id", "")
        if eid.startswith("photo "):
            baseline_stems_space.add(eid[len("photo "):])

    # 新增的分裂 = 当前分裂中不在基线中的
    new_duplicated = duplicated_stems - (baseline_stems_colon & baseline_stems_space)

    if not new_duplicated:
        _pass("照片实体无冒号/空格分裂")
    else:
        for stem in new_duplicated:
            colon_nid = photo_stems_colon.get(stem, "?")
            space_nid = photo_stems_space.get(stem, "?")
            _fail(
                f"照片实体分裂! stem='{stem}' 同时存在: "
                f"photo:{stem} (node={colon_nid[:20]}...) 和 "
                f"photo {stem} (node={space_nid[:20]}...)"
            )

    # ── TEST 9: features 关系指向正确的人物（不是旧未命名人物） ──
    expected_stem = Path(TEST_PHOTO_PATH.replace("\\", "/").lower()).stem
    expected_photo_name = f"photo:{expected_stem}"
    photo_nid, _ = find_nodes_by_entity_id(nodes, expected_photo_name)

    if photo_nid:
        # 找与照片实体相关的 features 边
        photo_features = []
        for e in edges:
            if e.get("keywords") == "features" and (
                e["src"] == photo_nid or e["tgt"] == photo_nid
            ):
                # 找出对方（人物端）
                if e["src"] == photo_nid:
                    person_nid = e["tgt"]
                else:
                    person_nid = e["src"]
                person_eid = nodes.get(person_nid, {}).get("entity_id", "?")
                photo_features.append({
                    "person_node_id": person_nid,
                    "person_entity_id": person_eid,
                })

        print(f"\n  features 关系 ({len(photo_features)} 条):")
        for pf in photo_features:
            print(f"    - {expected_photo_name} -> {pf['person_entity_id']}")

        # 检查是否有指向旧未命名人物的边
        old_name_edges = [
            pf for pf in photo_features if pf["person_entity_id"] == old_name
        ]
        if not old_name_edges:
            _pass(f"无指向旧未命名人物 '{old_name}' 的 features 边")
        else:
            _fail(
                f"发现 {len(old_name_edges)} 条指向旧未命名人物 "
                f"'{old_name}' 的 features 边"
            )
    else:
        print(f"  [SKIP] 找不到照片实体 {expected_photo_name}，跳过 features 检查")

    # ── TEST 10: 旧未命名人物的所有边应该已迁移到新命名人物 ──
    # 检查旧实体是否还有任何边
    if old_unnamed_nodes:
        old_nid = next(iter(old_unnamed_nodes.keys()))
        old_entity_edges = [
            e
            for e in edges
            if e["src"] == old_nid or e["tgt"] == old_nid
        ]
        if not old_entity_edges:
            _pass(f"旧实体 '{old_name}' 无残留边")
        else:
            edge_keywords = [e.get("keywords", "?") for e in old_entity_edges]
            _fail(
                f"旧实体 '{old_name}' 仍有 {len(old_entity_edges)} 条残留边: "
                f"keywords={edge_keywords}"
            )
    else:
        # 旧实体已被删除，自然无残留边
        _pass(f"旧实体 '{old_name}' 已删除，无残留边")


# ──────────────────────────────────────────────
# Phase 6: 完整诊断（无论测试通过与否）
# ──────────────────────────────────────────────
def print_diagnostic():
    """打印完整图谱诊断信息，帮助定位问题。"""
    _section("Phase 6: 完整诊断")

    nodes, edges = read_graphml(PRODUCTION_STORAGE)

    expected_stem = Path(TEST_PHOTO_PATH.replace("\\", "/").lower()).stem

    # 列出所有与测试照片相关的实体
    print(f"  测试照片 stem: {expected_stem}")
    print(f"\n  所有照片相关实体:")
    for nid, attrs in nodes.items():
        eid = attrs.get("entity_id", "")
        if expected_stem in eid.lower() or eid.startswith("photo:"):
            etype = attrs.get("entity_type", "?")
            desc = attrs.get("description", "")[:60]
            source_id = attrs.get("source_id", "")[:40]
            print(f"    - entity_id={eid} | type={etype}")
            print(f"      desc={desc}")
            print(f"      source_id={source_id}")

    # 列出所有与测试人物相关的实体
    print(f"\n  所有测试人物相关实体:")
    for nid, attrs in nodes.items():
        eid = attrs.get("entity_id", "")
        if RENAME_TARGET in eid or "未命名人物" in eid:
            etype = attrs.get("entity_type", "?")
            desc = attrs.get("description", "")[:60]
            print(f"    - entity_id={eid} | type={etype}")
            print(f"      desc={desc}")

    # 列出所有与测试照片/人物相关的边
    photo_nid, _ = find_nodes_by_entity_id(nodes, f"photo:{expected_stem}")
    related_nids = set()
    if photo_nid:
        related_nids.add(photo_nid)
    for nid, attrs in nodes.items():
        eid = attrs.get("entity_id", "")
        if RENAME_TARGET in eid or ("未命名人物" in eid and expected_stem in str(attrs.get("description", ""))):
            related_nids.add(nid)

    if related_nids:
        print(f"\n  相关边:")
        for e in edges:
            if e["src"] in related_nids or e["tgt"] in related_nids:
                src_eid = nodes.get(e["src"], {}).get("entity_id", e["src"][:20])
                tgt_eid = nodes.get(e["tgt"], {}).get("entity_id", e["tgt"][:20])
                kw = e.get("keywords", "?")
                print(f"    - {src_eid} --[{kw}]--> {tgt_eid}")


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    _section("KG 更名回归测试 — 复现更名后实体分裂问题")
    print(f"  生产图谱: {PRODUCTION_STORAGE}")
    print(f"  测试照片: {TEST_PHOTO_PATH}")
    print(f"  更名目标: {RENAME_TARGET}")

    # 检查测试照片是否存在
    if not os.path.exists(TEST_PHOTO_PATH):
        _fail(f"测试照片不存在: {TEST_PHOTO_PATH}")
        print("\n  请确保测试照片文件存在后再运行。")
        return 1

    # Phase 1: 基线
    baseline = take_baseline()

    # Phase 2: 入库
    ingest_info = ingest_test_photo()
    if ingest_info is None:
        _fail("入库失败，中止测试")
        _section("汇总")
        print(f"  通过: {pass_count}")
        print(f"  失败: {fail_count}")
        return 1

    # Phase 3: 入库后验证
    verify_info = verify_after_ingest(baseline)

    # Phase 4: 更名
    if not verify_info["unnamed_entities"]:
        _fail("无未命名人物，跳过更名测试")
    else:
        rename_info = rename_test_person(ingest_info, verify_info)
        if rename_info:
            # Phase 5: 更名后验证（核心回归测试）
            verify_after_rename(baseline, rename_info)

    # Phase 6: 完整诊断
    print_diagnostic()

    # 汇总
    _section("汇总")
    total = pass_count + fail_count
    print(f"  总计: {total} 项")
    print(f"  通过: {pass_count}")
    print(f"  失败: {fail_count}")
    if fail_count == 0:
        print(f"\n  *** 全部通过 — 更名回归问题已修复 ***")
    else:
        print(f"\n  *** {fail_count} 项失败 — 更名回归问题仍然存在 ***")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
