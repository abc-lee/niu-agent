"""
测试 LiteLLM 直接调用 vs 通过 LLM Proxy

目标：确定延迟来源
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_direct_litellm():
    """直接使用 LiteLLM 调用 API"""
    print("\n" + "=" * 60)
    print("测试 1: 直接使用 LiteLLM")
    print("=" * 60)

    import litellm
    litellm.suppress_debug_info = True

    # 从配置读取
    import json
    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    model = llm.get("model", "")
    api_key = llm.get("apiKey", "")
    api_base = llm.get("apiBase", "")

    print(f"  Model: {model}")
    print(f"  API Base: {api_base}")

    messages = [{"role": "user", "content": "说一个数字"}]

    start = time.time()
    print(f"  开始调用 litellm.completion...")

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_key=api_key,
            api_base=api_base,
            stream=True,
            timeout=60,
        )

        # 消费流式响应
        content = ""
        chunk_count = 0
        first_chunk_time = None

        for chunk in response:
            chunk_count += 1
            if first_chunk_time is None:
                first_chunk_time = time.time()
                print(f"  首个 chunk: {first_chunk_time - start:.2f}s")

            delta = getattr(chunk, 'choices', [None])[0].delta if hasattr(chunk, 'choices') else None
            if delta and hasattr(delta, 'content') and delta.content:
                content += delta.content

        elapsed = time.time() - start
        print(f"  完成: {elapsed:.2f}s, {chunk_count} chunks")
        print(f"  内容: {content[:50]}...")
        return elapsed

    except Exception as e:
        elapsed = time.time() - start
        print(f"  失败: {elapsed:.2f}s, 错误: {e}")
        return -1


def test_litellm_session():
    """使用 LiteLLMSession 调用"""
    print("\n" + "=" * 60)
    print("测试 2: 使用 LiteLLMSession")
    print("=" * 60)

    from agent.generic.litellm_adapter import LiteLLMSession

    import json
    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    session = LiteLLMSession(cfg={
        "api_type": llm.get("type", "openai"),
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
    })

    messages = [{"role": "user", "content": "说一个数字"}]

    start = time.time()
    print(f"  开始调用 session.chat...")

    try:
        gen = session.chat(messages=messages)

        # 消费 generator
        content = ""
        chunk_count = 0
        first_chunk_time = None
        mock_response = None

        try:
            while True:
                chunk = next(gen)
                if first_chunk_time is None:
                    first_chunk_time = time.time()
                    print(f"  首个 chunk: {first_chunk_time - start:.2f}s")

                if isinstance(chunk, str):
                    content += chunk
                    chunk_count += 1
        except StopIteration as e:
            mock_response = e.value

        elapsed = time.time() - start
        print(f"  完成: {elapsed:.2f}s, {chunk_count} chunks")
        print(f"  内容: {content[:50] if content else 'None'}...")
        return elapsed

    except Exception as e:
        elapsed = time.time() - start
        print(f"  失败: {elapsed:.2f}s, 错误: {e}")
        import traceback
        traceback.print_exc()
        return -1


def test_llm_proxy():
    """通过 LLM Proxy 调用"""
    print("\n" + "=" * 60)
    print("测试 3: 通过 LLM Proxy")
    print("=" * 60)

    import httpx

    start = time.time()
    print(f"  开始 HTTP 请求...")

    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                "http://localhost:9876/llm/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "说一个数字"}]
                }
            )

        elapsed = time.time() - start
        print(f"  完成: {elapsed:.2f}s, status={response.status_code}")

        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  内容: {content[:50] if content else 'None'}...")
        return elapsed

    except Exception as e:
        elapsed = time.time() - start
        print(f"  失败: {elapsed:.2f}s, 错误: {e}")
        return -1


def main():
    print("\n" + "=" * 60)
    print("LiteLLM 延迟分析")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序
    results["direct_litellm"] = test_direct_litellm()
    results["litellm_session"] = test_litellm_session()
    results["llm_proxy"] = test_llm_proxy()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        status = "✓" if elapsed >= 0 else "✗"
        print(f"  {status} {name}: {elapsed:.2f}s")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
