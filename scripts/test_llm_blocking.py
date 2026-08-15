"""
测试 LLM 调用阻塞问题

目标：定位 30-60 秒延迟的具体位置

测试场景：
1. 直接调用 LLM proxy（不经过 LightRAG）
2. 模拟 LightRAG 实体提取流程
3. 测试 asyncio.to_thread 线程池状态
"""

import sys
import time
import asyncio
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_2_get_brain_regions():
    """测试 get_brain_regions 的执行时间"""
    print("\n" + "=" * 60)
    print("测试 2: get_brain_regions 执行时间")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_brain_regions, get_lightrag

    # 第一次调用（可能触发 LightRAG 初始化）
    start = time.time()
    regions = get_brain_regions()
    elapsed = time.time() - start
    print(f"  第一次调用: {elapsed:.3f}s, 结果: {regions}")

    # 第二次调用（应该更快）
    start = time.time()
    regions = get_brain_regions()
    elapsed = time.time() - start
    print(f"  第二次调用: {elapsed:.3f}s, 结果: {regions}")

    return elapsed


def test_3_get_lightrag():
    """测试 get_lightrag 的执行时间"""
    print("\n" + "=" * 60)
    print("测试 3: get_lightrag 执行时间")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag

    start = time.time()
    rag = get_lightrag()
    elapsed = time.time() - start
    print(f"  get_lightrag: {elapsed:.3f}s, 结果: {rag is not None}")

    return elapsed


async def test_4_llm_proxy_direct():
    """测试直接调用 LLM proxy"""
    print("\n" + "=" * 60)
    print("测试 4: 直接调用 LLM proxy")
    print("=" * 60)

    from niu_api.llm_proxy import call_llm_via_litellm

    messages = [{"role": "user", "content": "你好，请用一句话回复"}]

    start = time.time()
    print(f"  开始调用 LLM...")

    try:
        response = await call_llm_via_litellm(messages=messages)
        elapsed = time.time() - start
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  LLM 调用完成: {elapsed:.2f}s")
        print(f"  响应内容: {content[:100]}...")
        return elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"  LLM 调用失败: {elapsed:.2f}s, 错误: {e}")
        return -1


async def test_5_lightrag_insert():
    """测试 LightRAG insert（会触发 LLM 实体提取）"""
    print("\n" + "=" * 60)
    print("测试 5: LightRAG insert (ainsert)")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 简短的测试内容
    test_content = "照片 test_photo：张三合影，2024年"

    start = time.time()
    print(f"  开始 ainsert...")

    try:
        result = call_async(rag.ainsert(test_content), timeout=120)
        elapsed = time.time() - start
        print(f"  ainsert 完成: {elapsed:.2f}s")
        return elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ainsert 失败: {elapsed:.2f}s, 错误: {e}")
        traceback.print_exc()
        return -1


async def test_6_thread_pool_status():
    """测试线程池状态"""
    print("\n" + "=" * 60)
    print("测试 6: 线程池状态")
    print("=" * 60)

    import concurrent.futures

    # 获取默认线程池
    loop = asyncio.get_running_loop()

    # 检查当前活跃线程数
    print(f"  当前线程数: {threading.active_count()}")
    print(f"  当前线程列表:")
    for thread in threading.enumerate():
        print(f"    - {thread.name} (daemon={thread.daemon})")

    # 测试 asyncio.to_thread
    def blocking_task(duration):
        time.sleep(duration)
        return f"slept {duration}s"

    start = time.time()
    tasks = []
    for i in range(3):
        task = asyncio.to_thread(blocking_task, 0.1)
        tasks.append(task)

    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start
    print(f"  并发 3 个 0.1s 任务: {elapsed:.3f}s (预期 ~0.1s)")
    print(f"  结果: {results}")


async def test_7_full_photo_ingest_flow():
    """测试完整的照片入库流程（模拟）"""
    print("\n" + "=" * 60)
    print("测试 7: 完整照片入库流程（模拟）")
    print("=" * 60)

    from niu_api.internal.lightrag_adapter import LightRAGIngester

    ingester = LightRAGIngester()

    # 模拟照片入库的 chunk_text
    chunk_text = """
照片 20150919_102426：未命名人物_1合影
实体：20150919_102426, 未命名人物_1
人物：未命名人物_1
"""

    start = time.time()
    print(f"  开始 inject_document...")

    try:
        result = ingester.inject_document(content=chunk_text, doc_id="test-photo-001")
        elapsed = time.time() - start
        print(f"  inject_document 完成: {elapsed:.2f}s")
        print(f"  结果: {result}")
        return elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"  inject_document 失败: {elapsed:.2f}s, 错误: {e}")
        traceback.print_exc()
        return -1


async def test_8_embedding_during_llm():
    """测试 LLM 调用期间的 embedding 操作"""
    print("\n" + "=" * 60)
    print("测试 8: LLM 调用期间的 embedding 操作")
    print("=" * 60)

    from niu_api.internal.embedding import get_model, encode
    from niu_api.llm_proxy import call_llm_via_litellm

    # 预加载 embedding 模型
    print("  预加载 embedding 模型...")
    start = time.time()
    model = get_model()
    elapsed = time.time() - start
    print(f"  模型加载: {elapsed:.3f}s")

    # 同时启动 LLM 调用和 embedding
    async def llm_task():
        start = time.time()
        messages = [{"role": "user", "content": "说一个数字"}]
        result = await call_llm_via_litellm(messages=messages)
        return time.time() - start

    def embedding_task():
        start = time.time()
        result = encode("测试文本")
        return time.time() - start

    start = time.time()
    llm_coro = llm_task()
    emb_future = asyncio.to_thread(embedding_task)

    llm_time, emb_time = await asyncio.gather(llm_coro, emb_future)
    total_time = time.time() - start

    print(f"  LLM 调用时间: {llm_time:.2f}s")
    print(f"  Embedding 时间: {emb_time:.3f}s")
    print(f"  总时间: {total_time:.2f}s")


async def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("LLM 调用阻塞问题诊断测试")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 同步测试
    results["get_brain_regions"] = test_2_get_brain_regions()
    results["get_lightrag"] = test_3_get_lightrag()

    # 异步测试
    results["llm_proxy_direct"] = await test_4_llm_proxy_direct()
    results["lightrag_insert"] = await test_5_lightrag_insert()
    await test_6_thread_pool_status()
    results["photo_ingest"] = await test_7_full_photo_ingest_flow()
    await test_8_embedding_during_llm()

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        status = "✓" if elapsed >= 0 else "✗"
        print(f"  {status} {name}: {elapsed:.3f}s")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    asyncio.run(main())
