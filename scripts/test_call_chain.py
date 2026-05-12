"""
诊断 LightRAG → HTTP → FastAPI → asyncio.to_thread 调用链

目标：追踪是否存在事件循环死锁
"""

import sys
import time
import asyncio
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_event_loop_status():
    """测试事件循环状态"""
    print("\n" + "=" * 60)
    print("测试事件循环状态")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import _ensure_loop, _loop_thread

    loop = _ensure_loop()
    print(f"  LightRAG 事件循环: {loop}")
    print(f"  事件循环运行中: {loop.is_running()}")
    print(f"  事件循环线程: {_loop_thread.name if _loop_thread else 'None'}")
    print(f"  当前线程: {threading.current_thread().name}")

    # 检查是否有 asyncio.to_thread 使用的线程池
    try:
        executor = loop._default_executor
        if executor:
            print(f"  默认执行器: {executor}")
            print(f"  最大工作线程: {executor._max_workers if hasattr(executor, '_max_workers') else 'unknown'}")
    except Exception as e:
        print(f"  获取执行器失败: {e}")


def test_http_client_in_lightrag_loop():
    """测试在 LightRAG 事件循环中发起 HTTP 请求"""
    print("\n" + "=" * 60)
    print("测试 LightRAG 事件循环中的 HTTP 请求")
    print("=" * 60)

    import httpx
    from niu_api.internal.lightrag_manager import call_async

    async def make_http_request():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 HTTP 请求...")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get("http://localhost:9876/llm/v1/health")
                elapsed = time.time() - start
                print(f"  [{elapsed:.1f}s] HTTP 请求完成: {response.status_code}")
                return elapsed
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] HTTP 请求失败: {e}")
            return -1

    return call_async(make_http_request(), timeout=60)


def test_llm_call_chain():
    """测试完整的 LLM 调用链"""
    print("\n" + "=" * 60)
    print("测试完整 LLM 调用链")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    async def test_chain():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 LLM 调用链测试...")

        # 1. 直接调用 LLM 函数
        print(f"  [{time.time() - start:.1f}s] 调用 rag.llm_model_func...")
        try:
            result = await rag.llm_model_func("说一个数字")
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] LLM 调用完成: {result[:50] if result else 'None'}...")
            return elapsed
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] LLM 调用失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(test_chain(), timeout=60)


def test_concurrent_llm_calls():
    """测试并发 LLM 调用"""
    print("\n" + "=" * 60)
    print("测试并发 LLM 调用")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return

    async def test_concurrent():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 3 个并发 LLM 调用...")

        async def single_call(i):
            call_start = time.time()
            print(f"  [{time.time() - start:.1f}s] 调用 {i} 开始...")
            try:
                result = await rag.llm_model_func(f"说数字 {i}")
                elapsed = time.time() - call_start
                print(f"  [{time.time() - start:.1f}s] 调用 {i} 完成: {elapsed:.1f}s")
                return elapsed
            except Exception as e:
                elapsed = time.time() - call_start
                print(f"  [{time.time() - start:.1f}s] 调用 {i} 失败: {e}")
                return -1

        results = await asyncio.gather(
            single_call(1),
            single_call(2),
            single_call(3),
        )

        total = time.time() - start
        print(f"  [{total:.1f}s] 所有调用完成")
        print(f"  单个时间: {results}")
        return total

    return call_async(test_concurrent(), timeout=120)


def test_embedding_in_lightrag_loop():
    """测试 LightRAG 事件循环中的 embedding 调用"""
    print("\n" + "=" * 60)
    print("测试 LightRAG 事件循环中的 Embedding")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    async def test_embed():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 embedding 调用...")

        try:
            result = await rag.embedding_func(["测试文本"])
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] Embedding 完成: shape={result.shape}")
            return elapsed
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] Embedding 失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(test_embed(), timeout=30)


def test_lightrag_insert_with_tracing():
    """测试 LightRAG insert 并追踪内部调用"""
    print("\n" + "=" * 60)
    print("测试 LightRAG Insert（带追踪）")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 检查 LightRAG 的配置
    print(f"  llm_model_max_async: {rag.llm_model_max_async}")
    print(f"  default_llm_timeout: {rag.default_llm_timeout}")
    print(f"  entity_extract_max_gleaning: {rag.entity_extract_max_gleaning}")

    test_content = "测试照片 test_002：王五合影"

    async def traced_insert():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 ainsert...")

        # 添加超时保护
        try:
            # 使用 asyncio.wait_for 添加超时
            result = await asyncio.wait_for(rag.ainsert(test_content), timeout=60)
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 完成")
            return elapsed
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 超时（60s）")

            # 打印当前任务状态
            tasks = asyncio.all_tasks(asyncio.get_running_loop())
            print(f"  当前活跃任务数: {len(tasks)}")
            for task in tasks:
                print(f"    - {task.get_name()}: {task.done()}")

            return -1
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(traced_insert(), timeout=90)


def main():
    print("\n" + "=" * 60)
    print("LightRAG 调用链诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序：从底层开始
    test_event_loop_status()
    results["http_request"] = test_http_client_in_lightrag_loop()
    results["embedding"] = test_embedding_in_lightrag_loop()
    results["llm_call"] = test_llm_call_chain()
    results["concurrent_llm"] = test_concurrent_llm_calls()
    results["insert"] = test_lightrag_insert_with_tracing()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        if elapsed is None:
            continue
        status = "✓" if isinstance(elapsed, (int, float)) and elapsed >= 0 else "✗"
        if isinstance(elapsed, (int, float)):
            print(f"  {status} {name}: {elapsed:.3f}s")
        else:
            print(f"  {status} {name}: {elapsed}")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
