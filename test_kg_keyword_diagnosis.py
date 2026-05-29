"""
LightRAG 关键词诊断脚本

追踪当不传 keywords 给 LightRAG 时，为什么查询返回空结果。

测试流程：
1. 直接调用 LightRAG 的关键词提取函数，看 LLM 返回了什么
2. 用 LLM 提取的关键词手动调用 kg_query，看结果
3. 用 [query] 作为 ll_keywords 手动调用 kg_query，看结果
4. 对比两种关键词在向量检索中的行为差异

运行条件：系统必须处于运行状态（localhost:9876 可用，LightRAG 已初始化）

使用方法：
    python test_kg_keyword_diagnosis.py
"""

import asyncio
import json
import sys
import time
import traceback

# 设置 Python 路径
sys.path.insert(0, "REDACTED_USER_PATH/tools/ai-bot/niu_api")
sys.path.insert(0, "REDACTED_USER_PATH/tools/ai-bot/mcp-servers/lightrag-server/src")

# =========================================
# 诊断 1: 获取 LightRAG 实例并检查状态
# =========================================

print("=" * 60)
print("诊断 1: 获取 LightRAG 实例并检查状态")
print("=" * 60)

from niu_api.internal.lightrag_manager import get_lightrag, call_async

rag = get_lightrag()
if rag is None:
    print("ERROR: LightRAG 实例未初始化！系统必须处于运行状态才能执行此脚本。")
    sys.exit(1)

print(f"LightRAG 实例已获取: {rag}")
print(f"Working dir: {rag.working_dir}")

# 检查知识图谱状态
graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
if graph_obj is None:
    print("ERROR: 知识图谱对象不存在")
    sys.exit(1)

nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
print(f"节点数: {nx_graph.number_of_nodes()}")
print(f"边数: {nx_graph.number_of_edges()}")

# 显示一些实体名示例（前20个）
sample_entities = list(nx_graph.nodes())[:20]
print(f"实体名示例: {sample_entities}")

# 显示一些实体的 entity_type
entity_types = {}
for name, data in nx_graph.nodes(data=True):
    et = data.get("entity_type", "Unknown")
    entity_types.setdefault(et, []).append(name)
print(f"实体类型统计:")
for et, names in sorted(entity_types.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
    print(f"  {et}: {len(names)} 个实体")

# =========================================
# 诊断 2: 测试关键词提取（无 keywords 时 LightRAG 内部调用的函数）
# =========================================

print("\n" + "=" * 60)
print("诊断 2: 测试关键词提取")
print("=" * 60)

# 测试查询（使用中文，因为系统配置了 Chinese 语言）
test_queries = [
    "知识管理助手的功能有哪些",
    "LightRAG的知识图谱如何工作",
    "照片管理和人脸识别",
]

for query in test_queries:
    print(f"\n--- 测试查询: '{query}' ---")

    try:
        from lightrag import QueryParam
        from lightrag.operate import extract_keywords_only, get_keywords_from_query

        # 方式A: 使用 extract_keywords_only 直接提取关键词
        param = QueryParam(mode="local")

        # 需要从 rag 实例获取 global_config
        from dataclasses import asdict
        global_config = asdict(rag)

        print("  调用 extract_keywords_only()...")
        t0 = time.time()
        hl_keywords, ll_keywords = call_async(
            extract_keywords_only(query, param, global_config, hashing_kv=rag.llm_response_cache),
            timeout=120,
        )
        elapsed = time.time() - t0

        print(f"  耗时: {elapsed:.2f}s")
        print(f"  high_level_keywords: {hl_keywords}")
        print(f"  low_level_keywords:  {ll_keywords}")

        # 组合成字符串（LightRAG 内部的做法）
        ll_keywords_str = ", ".join(ll_keywords) if ll_keywords else ""
        hl_keywords_str = ", ".join(hl_keywords) if hl_keywords else ""
        print(f"  ll_keywords_str: '{ll_keywords_str}'")
        print(f"  hl_keywords_str: '{hl_keywords_str}'")

        # 对比：原始查询作为 ll_keywords
        query_as_ll_str = query
        print(f"  query_as_ll_str:  '{query_as_ll_str}'")

    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()

# =========================================
# 诊断 3: 用两种关键词分别做向量检索
# =========================================

print("\n" + "=" * 60)
print("诊断 3: 对比两种关键词在向量检索中的行为")
print("=" * 60)

# 直接测试 entities_vdb 的向量搜索
# 用 LLM 提取的关键词字符串 vs 用原始查询字符串

embedding_func = rag.embedding_func
if embedding_func is None:
    print("ERROR: embedding_func 不存在")
    sys.exit(1)

entities_vdb = rag.entities_vdb
relationships_vdb = rag.relationships_vdb

for query in test_queries:
    print(f"\n--- 查询: '{query}' ---")

    try:
        # Step 1: 先提取关键词
        param = QueryParam(mode="local")
        global_config = asdict(rag)
        hl_kw, ll_kw = call_async(
            extract_keywords_only(query, param, global_config, hashing_kv=rag.llm_response_cache),
            timeout=120,
        )
        ll_keywords_str = ", ".join(ll_kw) if ll_kw else ""
        print(f"  LLM提取的关键词: ll='{ll_keywords_str}', hl='{', '.join(hl_kw) if hl_kw else ''}'")

        # Step 2: 嵌入两种文本，对比向量搜索结果
        texts_to_embed = [ll_keywords_str, query]  # [LLM关键词, 原始查询]
        if not ll_keywords_str:
            print("  WARNING: ll_keywords 为空，跳过嵌入对比")
            continue

        print(f"  嵌入文本对比:")
        print(f"    Text A (LLM关键词): '{ll_keywords_str}'")
        print(f"    Text B (原始查询):  '{query}'")

        embeddings = call_async(embedding_func(texts_to_embed), timeout=60)

        # 用嵌入做向量搜索
        print(f"  向量搜索 (entities_vdb, top_k=20):")

        # 方式A: 用 LLM 关键词的嵌入
        ll_embedding = embeddings[0]
        results_A = call_async(
            entities_vdb.query(ll_keywords_str, top_k=20, query_embedding=ll_embedding),
            timeout=60,
        )
        print(f"    方式A (LLM关键词嵌入) 返回 {len(results_A)} 个实体")
        if results_A:
            for r in results_A[:5]:
                print(f"      - {r.get('entity_name', '?')} (distance={r.get('distance', '?')})")

        # 方式B: 用原始查询的嵌入
        query_embedding = embeddings[1]
        results_B = call_async(
            entities_vdb.query(query, top_k=20, query_embedding=query_embedding),
            timeout=60,
        )
        print(f"    方式B (原始查询嵌入) 返回 {len(results_B)} 个实体")
        if results_B:
            for r in results_B[:5]:
                print(f"      - {r.get('entity_name', '?')} (distance={r.get('distance', '?')})")

        # 方式C: 用 LLM 关键词的嵌入搜索 relationships_vdb（global/hybrid/mix 模式）
        if hl_kw:
            hl_keywords_str = ", ".join(hl_kw)
            hl_embedding_result = call_async(embedding_func([hl_keywords_str]), timeout=60)
            hl_embedding = hl_embedding_result[0]
            results_C = call_async(
                relationships_vdb.query(hl_keywords_str, top_k=20, query_embedding=hl_embedding),
                timeout=60,
            )
            print(f"    方式C (HL关键词嵌入->relationships_vdb) 返回 {len(results_C)} 个关系")
            if results_C:
                for r in results_C[:3]:
                    print(f"      - {r.get('src_id', '?')} -> {r.get('tgt_id', '?')} (distance={r.get('distance', '?')}")

    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()

# =========================================
# 诊断 4: 完整 kg_query 对比测试
# =========================================

print("\n" + "=" * 60)
print("诊断 4: 完整 kg_query 对比测试")
print("=" * 60)

for query in test_queries:
    print(f"\n--- 查询: '{query}' ---")

    try:
        # 方式A: 不传 keywords（让 LightRAG 自己提取）
        param_A = QueryParam(mode="local", only_need_context=True)
        t0 = time.time()
        result_A_str = call_async(rag.aquery(query, param=param_A), timeout=120)
        elapsed_A = time.time() - t0
        print(f"  方式A (无keywords, LLM提取) 耗时: {elapsed_A:.2f}s")
        if result_A_str:
            is_error = any(marker in result_A_str.lower() for marker in ("not able to provide", "[no-context]"))
            if is_error:
                print(f"  方式A 结果: [空结果/错误] - '{result_A_str[:100]}...'")
            else:
                print(f"  方式A 结果长度: {len(result_A_str)} 字符")
                print(f"  方式A 结果前200字: {result_A_str[:200]}")
        else:
            print(f"  方式A 结果: None")

        # 方式B: 传 keywords=[query]（跳过 LLM 提取）
        param_B = QueryParam(mode="local", only_need_context=True)
        param_B.ll_keywords = [query]
        param_B.hl_keywords = [query]
        t0 = time.time()
        result_B_str = call_async(rag.aquery(query, param=param_B), timeout=120)
        elapsed_B = time.time() - t0
        print(f"  方式B (keywords=[query]) 耗时: {elapsed_B:.2f}s")
        if result_B_str:
            is_error = any(marker in result_B_str.lower() for marker in ("not able to provide", "[no-context]"))
            if is_error:
                print(f"  方式B 结果: [空结果/错误] - '{result_B_str[:100]}...'")
            else:
                print(f"  方式B 结果长度: {len(result_B_str)} 字符")
                print(f"  方式B 结果前200字: {result_B_str[:200]}")
        else:
            print(f"  方式B 结果: None")

        # 方式C: query_data 对比（结构化结果）
        print(f"\n  --- query_data 对比 ---")

        # 不传 keywords
        param_C1 = QueryParam(mode="local")
        result_C1 = call_async(rag.aquery_data(query, param=param_C1), timeout=120)
        if result_C1:
            status = result_C1.get("status", "?")
            data = result_C1.get("data", {})
            entities = data.get("entities", [])
            relationships = data.get("relationships", [])
            chunks = data.get("chunks", [])
            print(f"  方式C1 (无keywords) status={status}, entities={len(entities)}, relationships={len(relationships)}, chunks={len(chunks)}")
            # 显示提取的关键词
            metadata = result_C1.get("metadata", {})
            kw_meta = metadata.get("keywords", {})
            print(f"    keywords: hl={kw_meta.get('high_level', [])}, ll={kw_meta.get('low_level', [])}")
        else:
            print(f"  方式C1 (无keywords) 结果: None")

        # 传 keywords
        param_C2 = QueryParam(mode="local")
        param_C2.ll_keywords = [query]
        result_C2 = call_async(rag.aquery_data(query, param=param_C2), timeout=120)
        if result_C2:
            status = result_C2.get("status", "?")
            data = result_C2.get("data", {})
            entities = data.get("entities", [])
            relationships = data.get("relationships", [])
            chunks = data.get("chunks", [])
            print(f"  方式C2 (keywords=[query]) status={status}, entities={len(entities)}, relationships={len(relationships)}, chunks={len(chunks)}")
        else:
            print(f"  方式C2 (keywords=[query]) 结果: None")

    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()

# =========================================
# 诊断 5: 检查向量库内容
# =========================================

print("\n" + "=" * 60)
print("诊断 5: 检查向量库内容")
print("=" * 60)

# 检查 entities_vdb 中有多少实体向量
try:
    # 直接查看向量库的存储文件
    import os
    storage_dir = rag.working_dir
    print(f"Storage dir: {storage_dir}")
    print(f"Storage dir contents:")

    for item in sorted(os.listdir(storage_dir)):
        item_path = os.path.join(storage_dir, item)
        if os.path.isfile(item_path):
            size = os.path.getsize(item_path)
            print(f"  {item}: {size} bytes")
        elif os.path.isdir(item_path):
            sub_items = os.listdir(item_path)
            total_size = sum(os.path.getsize(os.path.join(item_path, s)) for s in sub_items if os.path.isfile(os.path.join(item_path, s)))
            print(f"  {item}/: {len(sub_items)} files, {total_size} bytes total")

    # 尝试直接读取 entities_vdb 的 JSON 存储来看内容
    entities_json = os.path.join(storage_dir, "kv_store_full_entities.json")
    if os.path.exists(entities_json):
        with open(entities_json, "r") as f:
            try:
                entities_data = json.load(f)
            except json.JSONDecodeError:
                # 可能是 JSONL 格式
                entities_data = []
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entities_data.append(json.loads(line))
                        except:
                            pass
        print(f"\n  kv_store_full_entities.json 包含 {len(entities_data)} 条记录")
        # 显示几条记录的内容
        for i, (key, val) in enumerate(list(entities_data.items())[:5] if isinstance(entities_data, dict) else entities_data[:5]):
            if isinstance(entities_data, dict):
                entity_name = key
                entity_data = val
            else:
                entity_name = val.get("entity_name", "?")
                entity_data = val
            print(f"    [{i}] name='{entity_name}', type='{entity_data.get('entity_type', '?')}'")
            desc = entity_data.get("description", "")
            print(f"        description前100字: '{desc[:100]}'")

except Exception as e:
    print(f"  ERROR: {e}")
    traceback.print_exc()

# =========================================
# 诊断 6: 对比嵌入向量相似度
# =========================================

print("\n" + "=" * 60)
print("诊断 6: 对比嵌入向量相似度")
print("=" * 60)

for query in test_queries[:1]:  # 只用第一个查询做详细对比
    print(f"\n--- 查询: '{query}' ---")

    try:
        # 提取关键词
        param = QueryParam(mode="local")
        global_config = asdict(rag)
        hl_kw, ll_kw = call_async(
            extract_keywords_only(query, param, global_config, hashing_kv=rag.llm_response_cache),
            timeout=120,
        )
        ll_keywords_str = ", ".join(ll_kw) if ll_kw else ""

        if not ll_kw:
            print("  WARNING: LLM 未提取到关键词，跳过嵌入对比")
            continue

        print(f"  LLM提取关键词: {ll_kw}")
        print(f"  ll_keywords_str: '{ll_keywords_str}'")

        # 嵌入三种文本：
        # 1. LLM关键词组合字符串（LightRAG 内部做法）
        # 2. 原始查询（我们传 keywords=[query] 时的做法）
        # 3. 每个关键词单独嵌入
        texts = [ll_keywords_str, query] + ll_kw
        print(f"  嵌入文本列表: {texts}")

        all_embeddings = call_async(embedding_func(texts), timeout=60)

        # 计算各嵌入与 LLM关键词嵌入 的余弦相似度
        import numpy as np

        ll_emb = all_embeddings[0]
        query_emb = all_embeddings[1]
        kw_embs = all_embeddings[2:]

        def cosine_sim(a, b):
            return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

        print(f"\n  余弦相似度对比:")
        print(f"    ll_keywords_str vs query: {cosine_sim(ll_emb, query_emb):.4f}")
        for i, kw in enumerate(ll_kw):
            sim = cosine_sim(ll_emb, kw_embs[i])
            print(f"    ll_keywords_str vs kw[{i}] '{kw}': {sim:.4f}")

        # 用每个单独的关键词嵌入搜索
        print(f"\n  单关键词向量搜索对比:")
        for i, kw in enumerate(ll_kw):
            kw_emb = kw_embs[i]
            results = call_async(
                entities_vdb.query(kw, top_k=10, query_embedding=kw_emb),
                timeout=60,
            )
            print(f"    关键词 '{kw}' 搜索结果: {len(results)} 个实体")
            for r in results[:3]:
                print(f"      - {r.get('entity_name', '?')} (distance={r.get('distance', '?')})")

    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()

# =========================================
# 总结
# =========================================

print("\n" + "=" * 60)
print("诊断总结")
print("=" * 60)
print("""
关键发现预期：
1. LLM 提取的关键词是简短的抽象词/短语（如"知识管理"、"功能"）
2. 这些关键词组合成字符串后，嵌入向量与原始查询的嵌入向量差异大
3. entities_vdb 中的向量是用 "实体名\\n描述" 格式存储的
4. 用原始查询嵌入搜索时，因为查询包含了完整的上下文信息，
   与实体描述的语义匹配度高，所以能找到结果
5. 用 LLM 提取的关键词组合嵌入搜索时，因为关键词过于简短/抽象，
   与实体描述的语义匹配度低，所以返回空结果

可能的根因：
- LightRAG 的关键词提取 prompt 配置了 language=Chinese
- LLM 返回的中文关键词可能过于简短（如单字词或短词）
- 向量模型对短关键词的嵌入与对完整查询的嵌入差异显著
- entities_vdb 的 cosine_better_than_threshold 可能较高，
  导致关键词嵌入的匹配被过滤掉

修复建议：
- 在 lightrag_adapter.py 的 query() 方法中，当不传 keywords 时，
  可以考虑同时用 LLM 提取的关键词和原始查询做双重搜索
- 或者在 kg_query 的 fallback 逻辑中（ll_keywords==[] 时强制用 [query]）
  也扩展到 ll_keywords 非空但搜索结果为空的情况
""")