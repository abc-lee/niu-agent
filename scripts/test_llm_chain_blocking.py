"""
追踪 LightRAG LLM 调用链中的阻塞点

关键问题：LightRAG daemon 事件循环 → HTTP → FastAPI → asyncio.to_thread
"""

import sys
import time
import asyncio
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def trace_llm_call_in_lightrag():
    """追踪 LightRAG 中的 LLM 调用"""
    print("\n" + "=" * 60)
    print("追踪 LightRAG LLM 调用")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async
    import httpx

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 检查 LLM 函数的包装
    print(f"  llm_model_func 类型: {type(rag.llm_model_func)}")
    print(f"  llm_model_func 名称: {rag.llm_model_func.__name__ if hasattr(rag.llm_model_func, '__name__') else 'unknown'}")

    async def trace_call():
        start = time.time()

        # 直接调用底层 LLM 函数（绕过包装）
        # 检查是否有 priority_limit_async_func_call 包装
        original_func = rag.llm_model_func

        print(f"  [{time.time() - start:.1f}s] 开始 LLM 调用...")

        # 记录调用开始时间
        call_start = time.time()

        try:
            # 调用 LLM 函数
            result = await original_func("说一个数字")
            call_elapsed = time.time() - call_start
            print(f"  [{call_elapsed:.1f}s] LLM 调用完成")
            print(f"  响应: {result[:50] if result else 'None'}...")
            return call_elapsed
        except Exception as e:
            call_elapsed = time.time() - call_start
            print(f"  [{call_elapsed:.1f}s] LLM 调用失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(trace_call(), timeout=60)


def test_http_timeout_in_lightrag_loop():
    """测试 LightRAG 事件循环中的 HTTP 超时"""
    print("\n" + "=" * 60)
    print("测试 LightRAG 事件循环中的 HTTP 超时")
    print("=" * 60)

    import httpx
    from niu_api.internal.lightrag_manager import call_async

    async def test_http():
        start = time.time()

        # 测试不同超时设置
        for timeout in [5, 10, 30]:
            print(f"  [{time.time() - start:.1f}s] 测试超时 {timeout}s...")
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        "http://localhost:9876/llm/v1/chat/completions",
                        json={
                            "model": "test",
                            "messages": [{"role": "user", "content": "说一个数字"}]
                        }
                    )
                    elapsed = time.time() - start
                    print(f"  [{elapsed:.1f}s] HTTP 完成: {response.status_code}")
                    data = response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    print(f"  响应: {content[:30] if content else 'None'}...")
                    return elapsed
            except Exception as e:
                elapsed = time.time() - start
                print(f"  [{elapsed:.1f}s] HTTP 失败: {e}")

        return -1

    return call_async(test_http(), timeout=60)


def test_lightrag_insert_single_doc():
    """测试 LightRAG insert 单个文档"""
    print("\n" + "=" * 60)
    print("测试 LightRAG Insert 单个文档")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 清理之前的测试文档
    test_content = "测试照片 single_test：单人照片"

    async def insert_single():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 ainsert...")

        try:
            # 使用较短超时
            track_id = await asyncio.wait_for(rag.ainsert(test_content), timeout=45)
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 完成: {track_id}")
            return elapsed
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 超时")

            # 检查任务状态
            loop = asyncio.get_running_loop()
            tasks = asyncio.all_tasks(loop)
            print(f"  活跃任务数: {len(tasks)}")

            # 检查是否有阻塞的任务
            for task in tasks:
                if not task.done():
                    print(f"    未完成: {task.get_name()}")

            return -1
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert 失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(insert_single(), timeout=60)


def test_lightrag_insert_with_custom_kg():
    """测试 LightRAG ainsert_custom_kg（绕过实体提取）"""
    print("\n" + "=" * 60)
    print("测试 LightRAG ainsert_custom_kg")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    # 自定义 KG 数据（绕过 LLM 实体提取）
    custom_kg = {
        "entities": [
            {
                "entity_name": "测试照片",
                "entity_type": "Photo",
                "description": "一张测试照片",
                "source_id": "test-doc-001"
            },
            {
                "entity_name": "测试人物",
                "entity_type": "Person",
                "description": "照片中的人物",
                "source_id": "test-doc-001"
            }
        ],
        "relationships": [
            {
                "src_id": "测试照片",
                "tgt_id": "测试人物",
                "description": "照片包含人物",
                "keywords": "包含,合影",
                "source_id": "test-doc-001"
            }
        ],
        "chunks": [
            {
                "content": "测试照片 test_doc_001：测试人物合影",
                "source_id": "test-doc-001"
            }
        ]
    }

    async def insert_custom():
        start = time.time()
        print(f"  [{time.time() - start:.1f}s] 开始 ainsert_custom_kg...")

        try:
            await asyncio.wait_for(rag.ainsert_custom_kg(custom_kg, "test-doc-001"), timeout=30)
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert_custom_kg 完成")
            return elapsed
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert_custom_kg 超时")
            return -1
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [{elapsed:.1f}s] ainsert_custom_kg 失败: {e}")
            traceback.print_exc()
            return -1

    return call_async(insert_custom(), timeout=45)


def main():
    print("\n" + "=" * 60)
    print("LightRAG LLM 调用链阻塞点追踪")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序
    results["llm_call"] = trace_llm_call_in_lightrag()
    results["http_timeout"] = test_http_timeout_in_lightrag_loop()
    results["custom_kg"] = test_lightrag_insert_with_custom_kg()
    results["single_insert"] = test_lightrag_insert_single_doc()

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