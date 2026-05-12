"""
诊断完整调用链：LightRAG → HTTP → LLM Proxy → LiteLLM

目标：精确定位 30-60 秒延迟的来源
"""

import sys
import time
import asyncio
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_openai_sdk_direct():
    """直接使用 OpenAI SDK 调用 LLM Proxy"""
    print("\n" + "=" * 60)
    print("测试 OpenAI SDK 直接调用 LLM Proxy")
    print("=" * 60)

    import json
    from openai import OpenAI

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    # 使用 LLM Proxy 作为 base_url
    proxy_url = "http://localhost:9876/llm/v1"

    print(f"  Proxy URL: {proxy_url}")

    t0 = time.time()
    print(f"  [0.00s] 创建 OpenAI 客户端...")

    client = OpenAI(base_url=proxy_url, api_key="not-needed")
    t1 = time.time()
    print(f"  [{t1-t0:.2f}s] 客户端创建完成")

    print(f"  [{t1-t0:.2f}s] 发送请求...")

    try:
        response = client.chat.completions.create(
            model="proxy-model",
            messages=[{"role": "user", "content": "说一个数字"}],
            stream=False,
        )

        t2 = time.time()
        content = response.choices[0].message.content
        print(f"  [{t2-t0:.2f}s] 响应完成")
        print(f"  总耗时: {t2-t0:.2f}s")
        print(f"  - 客户端创建: {t1-t0:.2f}s")
        print(f"  - API 调用: {t2-t1:.2f}s")
        print(f"  内容: {content[:50] if content else 'None'}...")

        return t2 - t0

    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        return -1


def test_async_openai_sdk():
    """使用 AsyncOpenAI SDK 调用 LLM Proxy"""
    print("\n" + "=" * 60)
    print("测试 AsyncOpenAI SDK 调用 LLM Proxy")
    print("=" * 60)

    import json
    from openai import AsyncOpenAI

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())

    proxy_url = "http://localhost:9876/llm/v1"
    print(f"  Proxy URL: {proxy_url}")

    async def run_test():
        t0 = time.time()
        print(f"  [0.00s] 创建 AsyncOpenAI 客户端...")

        client = AsyncOpenAI(base_url=proxy_url, api_key="not-needed")
        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] 客户端创建完成")

        print(f"  [{t1-t0:.2f}s] 发送异步请求...")

        try:
            response = await client.chat.completions.create(
                model="proxy-model",
                messages=[{"role": "user", "content": "说一个数字"}],
                stream=False,
            )

            t2 = time.time()
            content = response.choices[0].message.content
            print(f"  [{t2-t0:.2f}s] 响应完成")
            print(f"  总耗时: {t2-t0:.2f}s")
            print(f"  - 客户端创建: {t1-t0:.2f}s")
            print(f"  - API 调用: {t2-t1:.2f}s")
            print(f"  内容: {content[:50] if content else 'None'}...")

            await client.close()

            return t2 - t0

        except Exception as e:
            t_error = time.time()
            print(f"  [{t_error-t0:.2f}s] 失败: {e}")
            return -1

    return asyncio.run(run_test())


def test_lightrag_llm_func():
    """测试 LightRAG 的 LLM 函数"""
    print("\n" + "=" * 60)
    print("测试 LightRAG LLM 函数")
    print("=" * 60)

    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  LightRAG 不可用")
        return -1

    async def run_test():
        t0 = time.time()
        print(f"  [0.00s] 调用 rag.llm_model_func...")

        try:
            result = await rag.llm_model_func("说一个数字")
            t1 = time.time()
            print(f"  [{t1-t0:.2f}s] 响应完成")
            print(f"  总耗时: {t1-t0:.2f}s")
            print(f"  内容: {result[:50] if result else 'None'}...")
            return t1 - t0
        except Exception as e:
            t_error = time.time()
            print(f"  [{t_error-t0:.2f}s] 失败: {e}")
            import traceback
            traceback.print_exc()
            return -1

    return call_async(run_test(), timeout=120)


def test_httpx_to_proxy():
    """使用 httpx 直接调用 LLM Proxy"""
    print("\n" + "=" * 60)
    print("测试 httpx 直接调用 LLM Proxy")
    print("=" * 60)

    import httpx

    proxy_url = "http://localhost:9876/llm/v1/chat/completions"
    print(f"  Proxy URL: {proxy_url}")

    t0 = time.time()
    print(f"  [0.00s] 发送 HTTP 请求...")

    try:
        with httpx.Client(timeout=120) as client:
            response = client.post(
                proxy_url,
                json={
                    "model": "proxy-model",
                    "messages": [{"role": "user", "content": "说一个数字"}],
                }
            )

            t1 = time.time()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  [{t1-t0:.2f}s] 响应完成")
            print(f"  总耗时: {t1-t0:.2f}s")
            print(f"  内容: {content[:50] if content else 'None'}...")

            return t1 - t0

    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        return -1


def test_asyncio_to_thread_overhead():
    """测试 asyncio.to_thread 的开销"""
    print("\n" + "=" * 60)
    print("测试 asyncio.to_thread 开销")
    print("=" * 60)

    import httpx

    def sync_http_call():
        t0 = time.time()
        with httpx.Client(timeout=120) as client:
            response = client.post(
                "http://localhost:9876/llm/v1/chat/completions",
                json={
                    "model": "proxy-model",
                    "messages": [{"role": "user", "content": "说一个数字"}],
                }
            )
            t1 = time.time()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return t1 - t0, content

    async def run_test():
        t0 = time.time()
        print(f"  [0.00s] 调用 asyncio.to_thread...")

        elapsed, content = await asyncio.to_thread(sync_http_call)
        t1 = time.time()

        print(f"  [{t1-t0:.2f}s] to_thread 完成")
        print(f"  内部 HTTP 耗时: {elapsed:.2f}s")
        print(f"  to_thread 开销: {t1-t0-elapsed:.2f}s")
        print(f"  内容: {content[:50] if content else 'None'}...")

        return t1 - t0

    return asyncio.run(run_test())


def main():
    print("\n" + "=" * 60)
    print("完整调用链诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序：从最底层开始
    results["httpx_to_proxy"] = test_httpx_to_proxy()
    results["asyncio_to_thread"] = test_asyncio_to_thread_overhead()
    results["openai_sdk_sync"] = test_openai_sdk_direct()
    results["async_openai_sdk"] = test_async_openai_sdk()
    results["lightrag_llm"] = test_lightrag_llm_func()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        if elapsed is None:
            continue
        status = "✓" if isinstance(elapsed, (int, float)) and elapsed >= 0 else "✗"
        if isinstance(elapsed, (int, float)):
            print(f"  {status} {name}: {elapsed:.2f}s")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
