"""
诊断 LiteLLM 内部延迟

目标：精确定位 litellm.completion() 内部的阻塞点
"""

import sys
import time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 在导入 litellm 之前设置环境变量
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_NO_AIOHTTP_TRANSPORT", "True")


def test_litellm_with_detailed_timing():
    """详细追踪 LiteLLM 调用的每个阶段"""
    print("\n" + "=" * 60)
    print("详细追踪 LiteLLM 调用")
    print("=" * 60)

    import json
    import litellm
    litellm.suppress_debug_info = True

    # 从配置读取
    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    model = llm.get("model", "")
    api_key = llm.get("apiKey", "")
    api_base = llm.get("apiBase", "")
    api_type = llm.get("type", "openai")

    print(f"  Model: {model}")
    print(f"  API Base: {api_base}")
    print(f"  API Type: {api_type}")
    print(f"  API Key: {api_key[:10]}...")

    messages = [{"role": "user", "content": "说一个数字"}]

    # 阶段 1: 导入时间
    t0 = time.time()
    print(f"\n  [0.00s] 开始准备调用...")

    # 阶段 2: 准备参数
    t1 = time.time()
    request_params = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "api_base": api_base,
        "custom_llm_provider": api_type,
        "stream": True,
        "timeout": 60,
    }
    print(f"  [{t1-t0:.2f}s] 参数准备完成")

    # 阶段 3: 调用 litellm.completion()
    t2 = time.time()
    print(f"  [{t2-t0:.2f}s] 调用 litellm.completion()...")

    try:
        response = litellm.completion(**request_params)
        t3 = time.time()
        print(f"  [{t3-t0:.2f}s] litellm.completion() 返回（耗时 {t3-t2:.2f}s）")

        # 阶段 4: 消费流式响应
        content = ""
        chunk_count = 0
        first_chunk_time = None
        last_chunk_time = None

        for chunk in response:
            chunk_count += 1
            if first_chunk_time is None:
                first_chunk_time = time.time()
                print(f"  [{first_chunk_time-t0:.2f}s] 首个 chunk 到达（TTFT: {first_chunk_time-t3:.2f}s）")

            delta = getattr(chunk, 'choices', [None])[0].delta if hasattr(chunk, 'choices') else None
            if delta and hasattr(delta, 'content') and delta.content:
                content += delta.content

            last_chunk_time = time.time()

        t4 = time.time()
        print(f"  [{t4-t0:.2f}s] 流式响应完成")
        print(f"  总耗时: {t4-t0:.2f}s")
        print(f"  - 参数准备: {t2-t1:.2f}s")
        print(f"  - completion() 调用: {t3-t2:.2f}s")
        print(f"  - 流式消费: {t4-t3:.2f}s")
        print(f"  - 首 chunk 延迟(TTFT): {first_chunk_time-t3:.2f}s" if first_chunk_time else "")
        print(f"  - Chunk 数量: {chunk_count}")
        print(f"  - 内容: {content[:50]}...")

        return t4 - t0

    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


def test_httpx_direct():
    """直接使用 httpx 调用 API（绕过 LiteLLM）"""
    print("\n" + "=" * 60)
    print("直接 HTTP 调用（绕过 LiteLLM）")
    print("=" * 60)

    import json
    import httpx

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    api_base = llm.get("apiBase", "")
    api_key = llm.get("apiKey", "")
    model = llm.get("model", "")

    # 构造 OpenAI 格式请求
    url = f"{api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "说一个数字"}],
        "stream": True,
    }

    print(f"  URL: {url}")
    print(f"  Model: {model}")

    t0 = time.time()
    print(f"  [0.00s] 开始 HTTP 请求...")

    try:
        with httpx.Client(timeout=60) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                t1 = time.time()
                print(f"  [{t1-t0:.2f}s] 连接建立（耗时 {t1-t0:.2f}s）")

                content = ""
                chunk_count = 0
                first_chunk_time = None

                for line in response.iter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        chunk_count += 1
                        if first_chunk_time is None:
                            first_chunk_time = time.time()
                            print(f"  [{first_chunk_time-t0:.2f}s] 首个数据 chunk")

                        try:
                            data = json.loads(line[6:])
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            if "content" in delta:
                                content += delta["content"]
                        except:
                            pass

                t2 = time.time()
                print(f"  [{t2-t0:.2f}s] 流式响应完成")
                print(f"  总耗时: {t2-t0:.2f}s")
                print(f"  - 连接建立: {t1-t0:.2f}s")
                print(f"  - 流式消费: {t2-t1:.2f}s")
                print(f"  - Chunk 数量: {chunk_count}")
                print(f"  - 内容: {content[:50]}...")

                return t2 - t0

    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        return -1


def test_litellm_non_stream():
    """测试非流式 LiteLLM 调用"""
    print("\n" + "=" * 60)
    print("非流式 LiteLLM 调用")
    print("=" * 60)

    import json
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

    messages = [{"role": "user", "content": "说一个数字"}]

    t0 = time.time()
    print(f"  [0.00s] 开始非流式调用...")

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_key=api_key,
            api_base=api_base,
            custom_llm_provider=api_type,
            stream=False,
            timeout=60,
        )

        t1 = time.time()
        print(f"  [{t1-t0:.2f}s] 调用完成")

        content = response.choices[0].message.content
        print(f"  总耗时: {t1-t0:.2f}s")
        print(f"  内容: {content[:50] if content else 'None'}...")

        return t1 - t0

    except Exception as e:
        t_error = time.time()
        print(f"  [{t_error-t0:.2f}s] 失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


def main():
    print("\n" + "=" * 60)
    print("LiteLLM 内部延迟诊断")
    print("=" * 60)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    # 测试顺序：从最底层开始
    results["httpx_direct"] = test_httpx_direct()
    results["litellm_non_stream"] = test_litellm_non_stream()
    results["litellm_stream"] = test_litellm_with_detailed_timing()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        status = "✓" if elapsed >= 0 else "✗"
        print(f"  {status} {name}: {elapsed:.2f}s")

    # 分析延迟来源
    if results["httpx_direct"] > 0 and results["litellm_stream"] > 0:
        overhead = results["litellm_stream"] - results["httpx_direct"]
        print(f"\n  LiteLLM 流式开销: {overhead:.2f}s")

    if results["litellm_non_stream"] > 0 and results["litellm_stream"] > 0:
        stream_overhead = results["litellm_stream"] - results["litellm_non_stream"]
        print(f"  流式模式开销: {stream_overhead:.2f}s")

    print(f"\n结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
