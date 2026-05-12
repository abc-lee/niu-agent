"""
诊断照片处理完整流程的延迟

目标：找出 3-5 分钟延迟的具体来源
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_photo_processing_flow():
    """模拟照片处理的完整流程"""
    print("\n" + "=" * 60)
    print("照片处理流程诊断")
    print("=" * 60)

    from agent.tool_registry import get_registry
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    # 模拟照片数据
    file_path = "/test/photo_20250101_120000.jpg"
    abstract = "测试照片：户外风景，阳光明媚"
    detected_persons = ["张三", "李四"]

    # 构造 chunk_text
    normalized_stem = "photo_20250101_120000"
    entity_names = [normalized_stem] + detected_persons
    chunk_text = (
        f"照片 {normalized_stem}：{abstract}\n"
        f"实体：{', '.join(entity_names)}\n"
        f"人物：{', '.join(detected_persons)}\n"
    )

    print(f"  Chunk text length: {len(chunk_text)} chars")
    print(f"  Content: {chunk_text[:100]}...")

    registry = get_registry()

    # Step 1: lightrag_insert_custom_kg
    print("\n--- Step 1: lightrag_insert_custom_kg ---")
    custom_kg_fn = registry.get("lightrag-server/lightrag_insert_custom_kg")
    if not custom_kg_fn:
        print("  ERROR: lightrag_insert_custom_kg not available")
        return -1

    # 构造实体和关系
    entities = [
        {"entity_name": normalized_stem, "entity_type": "Photo", "description": abstract},
        {"entity_name": "张三", "entity_type": "Person", "description": "照片中的人物"},
        {"entity_name": "李四", "entity_type": "Person", "description": "照片中的人物"},
    ]
    relationships = [
        {"src_id": normalized_stem, "tgt_id": "张三", "keywords": "包含"},
        {"src_id": normalized_stem, "tgt_id": "李四", "keywords": "包含"},
    ]
    chunks = [{"content": chunk_text, "source_id": file_path}]

    t0 = time.time()
    print(f"  [0.00s] 开始 Step 1...")
    try:
        result = custom_kg_fn(
            entities=entities,
            relationships=relationships,
            chunks=chunks,
            source_id=file_path,
        )
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] Step 1 完成: {result}")
    except Exception as e:
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] Step 1 失败: {e}")
        return -1

    step1_time = t1 - t0

    # Step 2: lightrag_insert
    print("\n--- Step 2: lightrag_insert ---")
    insert_fn = registry.get("lightrag-server/lightrag_insert")
    if not insert_fn:
        print("  ERROR: lightrag_insert not available")
        return -1

    t0 = time.time()
    print(f"  [0.00s] 开始 Step 2 (ainsert)...")
    try:
        result = insert_fn(
            content=chunk_text,
            file_path=file_path,
            doc_id=f"doc-{normalized_stem}",
        )
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] Step 2 完成: {result}")
    except Exception as e:
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] Step 2 失败: {e}")
        return -1

    step2_time = t1 - t0

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    print(f"  Step 1 (custom_kg): {step1_time:.2f}s")
    print(f"  Step 2 (ainsert):   {step2_time:.2f}s")
    print(f"  总计:               {step1_time + step2_time:.2f}s")

    return step1_time + step2_time


def test_lightrag_ainsert_directly():
    """直接测试 LightRAG ainsert"""
    print("\n" + "=" * 60)
    print("直接测试 LightRAG ainsert")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 检查配置
    print(f"  llm_model_max_async: {rag.llm_model_max_async}")
    print(f"  default_llm_timeout: {rag.default_llm_timeout}")
    print(f"  entity_extract_max_gleaning: {rag.entity_extract_max_gleaning}")
    print(f"  chunk_token_size: {rag.chunk_token_size}")

    content = "测试照片 photo_test：户外风景，阳光明媚。人物：张三、李四。"

    t0 = time.time()
    print(f"\n  [0.00s] 开始 ainsert...")

    try:
        track_id = call_async(rag.ainsert(content), timeout=600)
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] ainsert 完成: {track_id}")
        return t1 - t0
    except Exception as e:
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] ainsert 失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


def test_llm_call_count():
    """测试一次 ainsert 需要多少次 LLM 调用"""
    print("\n" + "=" * 60)
    print("测试 LLM 调用次数")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async
    import asyncio

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return

    # 检查 chunk 数量
    content = "测试照片 photo_test：户外风景，阳光明媚。人物：张三、李四。"
    print(f"  Content length: {len(content)} chars")
    print(f"  Chunk token size: {rag.chunk_token_size}")

    # 估算 chunk 数量
    # 中文约 1.5 tokens/char，英文约 0.25 tokens/char
    # 假设混合，约 1 token/char
    estimated_chunks = max(1, len(content) // rag.chunk_token_size)
    print(f"  Estimated chunks: {estimated_chunks}")

    # 每次 chunk 需要 (1 + entity_extract_max_gleaning) 次 LLM 调用
    llm_calls_per_chunk = 1 + rag.entity_extract_max_gleaning
    total_llm_calls = estimated_chunks * llm_calls_per_chunk
    print(f"  LLM calls per chunk: {llm_calls_per_chunk}")
    print(f"  Estimated total LLM calls: {total_llm_calls}")

    # 如果每次 LLM 调用需要 15 秒
    avg_llm_time = 15
    estimated_time = total_llm_calls * avg_llm_time
    print(f"\n  如果每次 LLM 调用需要 {avg_llm_time}s:")
    print(f"  预计总时间: {estimated_time}s ({estimated_time/60:.1f} 分钟)")


def main():
    print("\n" + "=" * 60)
    print("照片处理延迟诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序
    test_llm_call_count()
    results["direct_ainsert"] = test_lightrag_ainsert_directly()
    results["full_flow"] = test_photo_processing_flow()

    print("\n" + "=" * 60)
    print("最终结果")
    print("=" * 60)
    for name, elapsed in results.items():
        if elapsed is None or elapsed < 0:
            continue
        print(f"  {name}: {elapsed:.2f}s ({elapsed/60:.1f} 分钟)")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
