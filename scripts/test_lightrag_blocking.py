"""
精确诊断 LightRAG ainsert 阻塞点

目标：追踪 ainsert 流程中的具体阻塞位置
"""

import sys
import time
import asyncio
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def trace_lightrag_insert():
    """追踪 LightRAG ainsert 的执行流程"""
    print("\n" + "=" * 60)
    print("追踪 LightRAG ainsert 执行流程")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async, _ensure_loop
    from loguru import logger
    import logging

    # 启用详细日志
    logger.add(sys.stderr, level="DEBUG")

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return

    # 检查事件循环状态
    loop = _ensure_loop()
    print(f"  事件循环状态: {loop.is_running()}")
    print(f"  事件循环线程: {threading.current_thread().name}")

    # 简短测试内容
    test_content = "测试照片 test_001：李四合影"

    print(f"\n  开始 ainsert (超时 30s)...")

    # 使用较短超时来观察阻塞点
    start = time.time()

    try:
        # 手动追踪内部调用
        print(f"  [{time.time() - start:.1f}s] 调用 rag.ainsert...")

        # 包装 coroutine 来追踪
        async def traced_ainsert():
            print(f"  [{time.time() - start:.1f}s] 进入 ainsert coroutine")
            try:
                result = await rag.ainsert(test_content)
                print(f"  [{time.time() - start:.1f}s] ainsert coroutine 完成")
                return result
            except Exception as e:
                print(f"  [{time.time() - start:.1f}s] ainsert coroutine 异常: {e}")
                raise

        result = call_async(traced_ainsert(), timeout=30)
        elapsed = time.time() - start
        print(f"  ainsert 完成: {elapsed:.2f}s")
        return elapsed

    except TimeoutError:
        elapsed = time.time() - start
        print(f"  ainsert 超时: {elapsed:.2f}s")

        # 检查线程状态
        print(f"\n  线程状态:")
        for thread in threading.enumerate():
            print(f"    - {thread.name} (daemon={thread.daemon}, alive={thread.is_alive()})")

        return -1

    except Exception as e:
        elapsed = time.time() - start
        print(f"  ainsert 失败: {elapsed:.2f}s, 错误: {e}")
        traceback.print_exc()
        return -1


def test_embedding_thread_safety():
    """测试 embedding 模型的线程安全性"""
    print("\n" + "=" * 60)
    print("测试 Embedding 线程安全性")
    print("=" * 60)

    from niu_api.internal.embedding import get_model, encode
    import concurrent.futures

    model = get_model()
    print(f"  模型已加载")

    # 测试并发 embedding
    def do_encode(text):
        start = time.time()
        result = encode(text)
        return time.time() - start

    texts = [f"测试文本 {i}" for i in range(5)]

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_encode, t) for t in texts]
        results = [f.result() for f in futures]

    elapsed = time.time() - start
    print(f"  5 个并发 embedding: {elapsed:.3f}s (预期 ~0.1s)")
    print(f"  单个时间: {results}")

    # 测试在 asyncio.to_thread 中调用
    async def test_async_embed():
        start = time.time()
        tasks = [asyncio.to_thread(encode, t) for t in texts]
        results = await asyncio.gather(*tasks)
        return time.time() - start

    elapsed = asyncio.run(test_async_embed())
    print(f"  5 个 asyncio.to_thread embedding: {elapsed:.3f}s")


def test_lightrag_llm_call():
    """直接测试 LightRAG 的 LLM 调用"""
    print("\n" + "=" * 60)
    print("测试 LightRAG LLM 调用")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return

    # 直接调用 LightRAG 的 LLM 函数
    async def test_llm():
        start = time.time()
        print(f"  开始 LLM 调用...")

        # LightRAG 的 llm_model_func
        try:
            result = await rag.llm_model_func(
                "请用一句话回复：你好",
                system_prompt="你是一个友好的助手"
            )
            elapsed = time.time() - start
            print(f"  LLM 调用完成: {elapsed:.2f}s")
            print(f"  响应: {result[:100] if result else 'None'}...")
            return elapsed
        except Exception as e:
            elapsed = time.time() - start
            print(f"  LLM 调用失败: {elapsed:.2f}s, 错误: {e}")
            return -1

    return call_async(test_llm(), timeout=60)


def test_lightrag_embedding_call():
    """直接测试 LightRAG 的 embedding 调用"""
    print("\n" + "=" * 60)
    print("测试 LightRAG Embedding 调用")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return

    # 直接调用 LightRAG 的 embedding 函数
    async def test_embed():
        start = time.time()
        print(f"  开始 embedding 调用...")

        try:
            result = await rag.embedding_func(["测试文本"])
            elapsed = time.time() - start
            print(f"  Embedding 调用完成: {elapsed:.3f}s")
            print(f"  结果形状: {result.shape if hasattr(result, 'shape') else len(result)}")
            return elapsed
        except Exception as e:
            elapsed = time.time() - start
            print(f"  Embedding 调用失败: {elapsed:.3f}s, 错误: {e}")
            return -1

    return call_async(test_embed(), timeout=30)


def main():
    print("\n" + "=" * 60)
    print("LightRAG 阻塞点精确诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序：从最底层开始
    results["embedding_thread_safety"] = test_embedding_thread_safety()
    results["lightrag_embedding"] = test_lightrag_embedding_call()
    results["lightrag_llm"] = test_lightrag_llm_call()
    results["lightrag_insert"] = trace_lightrag_insert()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        if elapsed is None:
            continue
        status = "✓" if elapsed >= 0 else "✗"
        if isinstance(elapsed, (int, float)):
            print(f"  {status} {name}: {elapsed:.3f}s")
        else:
            print(f"  {status} {name}: {elapsed}")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
