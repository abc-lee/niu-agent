"""
诊断 LLM 调用延迟来源

目标：找出 56.82s 延迟的真正原因

测试层级：
1. 直接 httpx 调用 API（基准线）
2. 直接 LiteLLM 调用
3. LiteLLMSession.chat()
4. LLM Proxy call_llm_via_litellm()
5. LightRAG llm_model_func
"""

import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_1_httpx_direct():
    """测试 1: 直接 httpx 调用 API（基准线）"""
    print("\n" + "=" * 60)
    print("测试 1: 直接 httpx 调用 API")
    print("=" * 60)

    import httpx

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    api_base = llm.get("apiBase", "")
    api_key = llm.get("apiKey", "")
    model = llm.get("model", "")

    url = f"{api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "说一个数字"}],
        "stream": False,
    }

    print(f"  URL: {url}")
    print(f"  Model: {model}")

    t0 = time.time()
    print(f"  [0.00s] 开始 HTTP 请求...")

    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(url, headers=headers, json=payload)
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


def test_2_litellm_direct():
    """测试 2: 直接 LiteLLM 调用"""
    print("\n" + "=" * 60)
    print("测试 2: 直接 LiteLLM 调用")
    print("=" * 60)

    import os
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_NO_AIOHTTP_TRANSPORT", "True")

    import litellm
    litellm.suppress_debug_info = True

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    model = llm.get("model", "")
    api_key = llm.get("apiKey", "")
    api_base = llm.get("apiBase", "")
    api_type = llm.get("type", "openai")

    print(f"  Model: {model}")
    print(f"  API Base: {api_base}")

    t0 = time.time()
    print(f"  [0.00s] 开始 litellm.completion()...")

    try:
        response = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "说一个数字"}],
            api_key=api_key,
            api_base=api_base,
            custom_llm_provider=api_type,
            stream=False,
            timeout=60,
        )
        t1 = time.time()
        content = response.choices[0].message.content
        print(f"  [{t1-t0:.2f}s] 响应完成")
        print(f"  总耗时: {t1-t0:.2f}s")
        print(f"  内容: {content[:50] if content else 'None'}...")
        return t1 - t0
    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


def test_3_litellm_session():
    """测试 3: LiteLLMSession.chat()"""
    print("\n" + "=" * 60)
    print("测试 3: LiteLLMSession.chat()")
    print("=" * 60)

    from agent.generic.litellm_adapter import LiteLLMSession

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    llm_config = {
        "api_type": llm.get("type", "openai"),
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
    }

    print(f"  Model: {llm_config['model']}")

    t0 = time.time()
    print(f"  [0.00s] 创建 LiteLLMSession...")

    session = LiteLLMSession(cfg=llm_config)
    t1 = time.time()
    print(f"  [{t1-t0:.2f}s] Session 创建完成")

    print(f"  [{t1-t0:.2f}s] 开始 chat()...")

    try:
        gen = session.chat(messages=[{"role": "user", "content": "说一个数字"}])
        t2 = time.time()
        print(f"  [{t2-t0:.2f}s] chat() 返回 generator")

        # 消费 generator
        chunks = []
        mock_response = None
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
        except StopIteration as e:
            mock_response = e.value

        t3 = time.time()
        content = "".join(chunks)
        print(f"  [{t3-t0:.2f}s] 响应完成")
        print(f"  总耗时: {t3-t0:.2f}s")
        print(f"  - Session 创建: {t1-t0:.2f}s")
        print(f"  - chat() 调用: {t2-t1:.2f}s")
        print(f"  - 消费 generator: {t3-t2:.2f}s")
        print(f"  内容: {content[:50] if content else 'None'}...")
        return t3 - t0
    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


def test_4_llm_proxy():
    """测试 4: LLM Proxy call_llm_via_litellm()"""
    print("\n" + "=" * 60)
    print("测试 4: LLM Proxy call_llm_via_litellm()")
    print("=" * 60)

    import asyncio

    async def run_test():
        from niu_api.llm_proxy import call_llm_via_litellm

        t0 = time.time()
        print(f"  [0.00s] 开始 call_llm_via_litellm()...")

        try:
            result = await call_llm_via_litellm(
                messages=[{"role": "user", "content": "说一个数字"}],
            )
            t1 = time.time()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  [{t1-t0:.2f}s] 响应完成")
            print(f"  总耗时: {t1-t0:.2f}s")
            print(f"  内容: {content[:50] if content else 'None'}...")
            return t1 - t0
        except Exception as e:
            t_error = time.time()
            print(f"  [{t_error-t0:.2f}s] 失败: {e}")
            import traceback
            traceback.print_exc()
            return -1

    return asyncio.run(run_test())


def test_5_lightrag_llm_func():
    """测试 5: LightRAG llm_model_func"""
    print("\n" + "=" * 60)
    print("测试 5: LightRAG llm_model_func")
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


def main():
    print("\n" + "=" * 60)
    print("LLM 调用延迟诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序：从最底层开始
    results["httpx_direct"] = test_1_httpx_direct()
    results["litellm_direct"] = test_2_litellm_direct()
    results["litellm_session"] = test_3_litellm_session()
    results["llm_proxy"] = test_4_llm_proxy()
    results["lightrag_llm"] = test_5_lightrag_llm_func()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        status = "✓" if isinstance(elapsed, (int, float)) and elapsed >= 0 else "✗"
        if isinstance(elapsed, (int, float)):
            print(f"  {status} {name}: {elapsed:.2f}s")

    # 分析延迟来源
    print("\n" + "=" * 60)
    print("延迟分析")
    print("=" * 60)

    if results["httpx_direct"] > 0:
        print(f"  基准线 (httpx): {results['httpx_direct']:.2f}s")

    if results["litellm_direct"] > 0 and results["httpx_direct"] > 0:
        overhead = results["litellm_direct"] - results["httpx_direct"]
        print(f"  LiteLLM 开销: {overhead:.2f}s")

    if results["litellm_session"] > 0 and results["litellm_direct"] > 0:
        overhead = results["litellm_session"] - results["litellm_direct"]
        print(f"  LiteLLMSession 开销: {overhead:.2f}s")

    if results["llm_proxy"] > 0 and results["litellm_session"] > 0:
        overhead = results["llm_proxy"] - results["litellm_session"]
        print(f"  LLM Proxy 开销: {overhead:.2f}s")

    if results["lightrag_llm"] > 0 and results["llm_proxy"] > 0:
        overhead = results["lightrag_llm"] - results["llm_proxy"]
        print(f"  LightRAG 开销: {overhead:.2f}s")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
