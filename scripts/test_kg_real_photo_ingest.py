"""
真实照片入库测试 — 验证4步流程 + 实体source_id不是UNKNOWN + 实体有chunk关联

用法:
  python scripts/test_kg_real_photo_ingest.py
"""
import sys, os, json, time
from pathlib import Path
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# MCP server workdirs
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

PRODUCTION_STORAGE = os.path.expanduser("~/.niu/lightrag_storage/")
TEST_PHOTO_PATH = r"REDACTED_WIN_PATH\2026\05\2026-05-10\20090603_092316.jpg"
TEST_ABSTRACT = "任飞合影，2009:06:03"
TEST_PERSON_ID = "9830e57f-d092-4dfd-a5e2-974f08a3309a"
TEST_PERSON_NAME = "任飞"

pass_count = 0
fail_count = 0

def _pass(msg):
    global pass_count; pass_count += 1; print(f"  [PASS] {msg}")
def _fail(msg):
    global fail_count; fail_count += 1; print(f"  [FAIL] {msg}")
def _section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

def read_graphml(storage_dir):
    import networkx as nx
    fp = os.path.join(storage_dir, "graph_chunk_entity_relation.graphml")
    if not os.path.exists(fp):
        return {}, []
    G = nx.read_graphml(fp)
    nodes = {nid: dict(attrs) for nid, attrs in G.nodes(data=True)}
    edges = [{"src": s, "tgt": t, **dict(a)} for s, t, a in G.edges(data=True)]
    return nodes, edges

def read_json_store(storage_dir, filename):
    fp = os.path.join(storage_dir, filename)
    if not os.path.exists(fp):
        return {}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)

# ============== Phase 1: format_photo_ingest_data 验证 ==============

def test_format():
    _section("Phase 1: format_photo_ingest_data 验证")
    from niu_photo_server import format_photo_ingest_data

    detected_persons = [
        {"id": TEST_PERSON_ID, "name": TEST_PERSON_NAME, "auto_label": "未命名人物_1"}
    ]
    result = format_photo_ingest_data(TEST_PHOTO_PATH, TEST_ABSTRACT, detected_persons)
    entities = result.get("entities", [])
    relationships = result.get("relationships", [])

    # 照片实体名必须是短名 photo:{stem}，不是全路径
    photo_entity = next((e for e in entities if e["entity_type"] == "Photo"), None)
    if photo_entity:
        ename = photo_entity["entity_name"]
        expected_stem = Path(TEST_PHOTO_PATH.replace("\\", "/").lower()).stem
        expected_name = f"photo:{expected_stem}"
        if ename == expected_name:
            _pass(f"照片实体名用短名: {ename}")
        else:
            _fail(f"照片实体名错误: 期望 {expected_name}, 实际 {ename}")

        # file_path 仍为完整路径
        fp = photo_entity.get("file_path", "")
        if fp and len(fp) > len(ename):
            _pass(f"file_path 保留完整路径: {fp[:60]}...")
        else:
            _fail(f"file_path 不正确: {fp}")
    else:
        _fail("缺少 Photo 实体")

    # 人物实体用人名
    person_entity = next((e for e in entities if e["entity_type"] == "person"), None)
    if person_entity:
        pname = person_entity["entity_name"]
        if pname == TEST_PERSON_NAME:
            _pass(f"人物实体用人名: {pname}")
        elif pname.startswith("person:"):
            _fail(f"人物实体用了 person:uuid 格式: {pname}")
        else:
            _pass(f"人物实体名: {pname}")
    else:
        _fail("缺少 person 实体")

    # features 关系
    features_rel = next((r for r in relationships if r.get("keywords") == "features"), None)
    if features_rel:
        _pass(f"features 关系存在: {features_rel['src_id']} -> {features_rel['tgt_id']}")
    else:
        _fail("缺少 features 关系")

    print(f"\n  完整输出:")
    for e in entities:
        print(f"    entity: {e['entity_name']} | type={e['entity_type']} | fp={e.get('file_path','')[:50]}")
    for r in relationships:
        print(f"    rel: {r['src_id'][:40]} --[{r['keywords']}]--> {r['tgt_id'][:40]}")

# ============== Phase 2: 真实入库 + 验证 ==============

def test_real_ingest():
    _section("Phase 2: 真实入库 (需要API+LLM)")

    # 初始化 LightRAG + ToolRegistry
    try:
        from niu_api.internal.lightrag_manager import get_lightrag
        rag = get_lightrag()
        if rag is None:
            _fail("LightRAG 未初始化，请先启动 API 服务器")
            return
        _pass("LightRAG 初始化成功")
    except Exception as e:
        _fail(f"LightRAG 初始化失败: {e}")
        return

    try:
        from agent.mcp_loader import load_mcp_tools
        registry = load_mcp_tools()
        _pass(f"ToolRegistry 加载成功: {len(registry._tools)} tools")
    except Exception as e:
        _fail(f"ToolRegistry 加载失败: {e}")
        return

    from niu_photo_server import sync_photo_to_kg

    # 记录入库前状态
    nodes_before, edges_before = read_graphml(PRODUCTION_STORAGE)
    print(f"  入库前: {len(nodes_before)} 节点, {len(edges_before)} 边")

    # 执行入库
    detected_persons = [
        {"id": TEST_PERSON_ID, "name": TEST_PERSON_NAME, "auto_label": "未命名人物_1"}
    ]
    print(f"\n  调用 sync_photo_to_kg ...")
    result = sync_photo_to_kg(TEST_PHOTO_PATH, TEST_ABSTRACT, detected_persons)
    print(f"  返回: {result}")

    if result.get("status") == "success":
        _pass("sync_photo_to_kg 返回 success")
    else:
        _fail(f"sync_photo_to_kg 返回错误: {result}")

    # 等待异步处理
    print(f"  等待 LightRAG 处理 (8秒) ...")
    time.sleep(8)

    # 验证图谱
    nodes_after, edges_after = read_graphml(PRODUCTION_STORAGE)
    print(f"  入库后: {len(nodes_after)} 节点, {len(edges_after)} 边")
    print(f"  新增: {len(nodes_after)-len(nodes_before)} 节点, {len(edges_after)-len(edges_before)} 边")

    # ============== 关键验证: source_id 不是 UNKNOWN ==============
    _section("Phase 3: 验证实体 source_id (核心问题)")

    expected_stem = Path(TEST_PHOTO_PATH.replace("\\", "/").lower()).stem
    expected_photo_name = f"photo:{expected_stem}"

    # 找照片实体
    photo_node = None
    for nid, attrs in nodes_after.items():
        eid = attrs.get("entity_id", "")
        if eid.lower() == expected_photo_name.lower():
            photo_node = (nid, attrs)
            break

    if photo_node:
        nid, attrs = photo_node
        source_id = attrs.get("source_id", "N/A")
        file_path = attrs.get("file_path", "N/A")
        entity_type = attrs.get("entity_type", "N/A")
        description = attrs.get("description", "")[:80]

        print(f"\n  照片实体: {expected_photo_name}")
        print(f"  entity_type: {entity_type}")
        print(f"  description: {description}")
        print(f"  file_path: {file_path[:60]}")
        print(f"  source_id: {source_id[:60]}")

        # 核心检查: source_id 不能是 UNKNOWN / custom_kg / N/A
        if source_id in ("UNKNOWN", "custom_kg", "N/A", ""):
            _fail(f"source_id 是 '{source_id}' — 实体不可检索！这就是用户发现的问题！")
        else:
            _pass(f"source_id 正确: {source_id[:60]}")
    else:
        _fail(f"照片实体不在图谱中: {expected_photo_name}")

    # 找人物实体
    person_node = None
    for nid, attrs in nodes_after.items():
        eid = attrs.get("entity_id", "")
        if eid == TEST_PERSON_NAME:
            person_node = (nid, attrs)
            break

    if person_node:
        nid, attrs = person_node
        source_id = attrs.get("source_id", "N/A")
        print(f"\n  人物实体: {TEST_PERSON_NAME}")
        print(f"  source_id: {source_id[:60]}")

        if source_id in ("UNKNOWN", "custom_kg", "N/A", ""):
            _fail(f"人物 source_id 是 '{source_id}' — 实体不可检索！")
        else:
            _pass(f"人物 source_id 正确: {source_id[:60]}")
    else:
        _fail(f"人物实体不在图谱中: {TEST_PERSON_NAME}")

    # ============== 验证实体有边（不是孤岛） ==============
    _section("Phase 4: 验证实体不是孤岛")

    if photo_node:
        photo_edges = [e for e in edges_after if e["src"] == photo_node[0] or e["tgt"] == photo_node[0]]
        if photo_edges:
            _pass(f"照片实体有 {len(photo_edges)} 条边 (非孤岛)")
            for e in photo_edges:
                kw = e.get("keywords", "")
                tgt = e["tgt"] if e["src"] == photo_node[0] else e["src"]
                print(f"    --[{kw}]--> {tgt[:50]}")
        else:
            _fail("照片实体是孤岛 (无边)")

    if person_node:
        person_edges = [e for e in edges_after if e["src"] == person_node[0] or e["tgt"] == person_node[0]]
        if person_edges:
            _pass(f"人物实体有 {len(person_edges)} 条边 (非孤岛)")
        else:
            _fail("人物实体是孤岛 (无边)")

    # ============== 验证无碎片实体 ==============
    _section("Phase 5: 验证无碎片实体")

    # 检查是否有除了 expected_photo_name 之外的 photo: 前缀碎片
    photo_entities = []
    for nid, attrs in nodes_after.items():
        eid = attrs.get("entity_id", "")
        if eid.lower().startswith("photo:") and expected_stem in eid.lower():
            photo_entities.append(eid)

    if len(photo_entities) == 1:
        _pass(f"只有1个照片实体: {photo_entities[0]}")
    elif len(photo_entities) > 1:
        _fail(f"发现 {len(photo_entities)} 个照片相关实体 (碎片化):")
        for pe in photo_entities:
            print(f"    - {pe}")
    else:
        print(f"  (未找到照片实体，可能合并到已有实体)")

    # ============== 验证 chunk 存在 ==============
    _section("Phase 6: 验证 chunk 存在")

    chunk_store = read_json_store(PRODUCTION_STORAGE, "kv_store_text_chunks.json")
    found_chunk = False
    for cid, cdata in chunk_store.items():
        if not isinstance(cdata, dict):
            continue
        content = cdata.get("content", "")
        fp = cdata.get("file_path", "")
        if expected_stem in content or expected_photo_name in content:
            found_chunk = True
            _pass(f"找到匹配 chunk: content 包含 {expected_stem}")
            print(f"    chunk_id: {cid}")
            print(f"    file_path: {fp[:60]}")
            print(f"    content: {content[:150]}...")
            break

    if not found_chunk:
        _fail(f"未找到包含 {expected_stem} 的 chunk")

    # doc_status
    doc_store = read_json_store(PRODUCTION_STORAGE, "kv_store_doc_status.json")
    normalized_path = TEST_PHOTO_PATH.replace("\\", "/").lower()
    for did, ddata in doc_store.items():
        if isinstance(ddata, dict) and ddata.get("file_path") == normalized_path:
            _pass(f"doc_status 中有对应文档: status={ddata.get('status','?')}")
            break
    else:
        print(f"  (doc_status 中未找到 file_path={normalized_path[:60]}... 的文档)")

def main():
    _section("真实照片入库测试 — 4步流程 + source_id + chunk 验证")
    print(f"  生产图谱: {PRODUCTION_STORAGE}")
    print(f"  测试照片: {TEST_PHOTO_PATH}")
    print(f"  测试人物: {TEST_PERSON_NAME}")

    test_format()
    test_real_ingest()

    _section("汇总")
    total = pass_count + fail_count
    print(f"  总计: {total} 项")
    print(f"  通过: {pass_count}")
    print(f"  失败: {fail_count}")
    if fail_count == 0:
        print(f"\n  *** 全部通过 ***")
    else:
        print(f"\n  *** {fail_count} 项失败 ***")
    return 0 if fail_count == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
