#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test MiniMax tool calling with minimal prompt (no text examples)"""
import sys
import os

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import json
import litellm
from datetime import datetime

LOG_FILE = f"E:/tools/ai-bot/logs/minimax_clean_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

MINIMAX_API_KEY = "sk-cp--fRDJHXjWX4DvmSh_pWqYcTZABiMamJaRVLgsfk9jQyrKP79webHaEdI3ER6uLmpm-3esnKx9VPDPM3Qekm6NqTDO5TwNxwszlQuYTsLuCMPfo-BIy3C8FE"

# 工具schema：只有简单描述，NO <tool_use> 文本示例
tools = [{
    "type": "function",
    "function": {
        "name": "test_tool",
        "description": "A simple test tool",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Test message"}
            },
            "required": ["message"]
        }
    }
}]

# 测试1：使用LiteLLM + Anthropic provider（我们的架构）
log("=" * 60)
log("Test 1: LiteLLM + custom_llm_provider=anthropic")
log("=" * 60)

try:
    response = litellm.completion(
        model="MiniMax-M2.7-highspeed",
        messages=[{"role": "user", "content": "Please call the test_tool with message='hello world'"}],
        tools=tools,
        custom_llm_provider="anthropic",
        api_base="https://api.minimaxi.com/anthropic",
        api_key=MINIMAX_API_KEY,
        max_tokens=1024,
        stream=True,
    )

    tool_calls = []
    content = ""

    for chunk in response:
        choices = getattr(chunk, "choices", [None])
        delta = getattr(choices[0], "delta", None) if choices and choices[0] else None
        if not delta:
            continue
        if hasattr(delta, "content") and delta.content:
            content += delta.content
        if hasattr(delta, "tool_calls") and delta.tool_calls:
            for tc in delta.tool_calls:
                tool_calls.append(tc)
                fn = getattr(tc, "function", None)
                log(f"  Structured tool_call: name={getattr(fn, 'name', None)}, args={getattr(fn, 'arguments', '')}")

    log(f"\nResult: content='{content[:100]}', structured_tool_calls={len(tool_calls)}")
    if not tool_calls and "<tool_use>" in content:
        log("  -> Tool call found in TEXT content!")

except Exception as e:
    log(f"Error: {e}")
    import traceback
    traceback.print_exc()

# 测试2：直接HTTP调用验证MiniMax Anthropic端点
log("\n" + "=" * 60)
log("Test 2: Direct HTTP - verify Anthropic tool_use content block")
log("=" * 60)

import httpx

# 直接发送 Anthropic 格式请求，验证端点支持
# MiniMax Anthropic endpoint应该支持 tool_use content blocks
payload = {
    "model": "MiniMax-M2.7-highspeed",
    "max_tokens": 1024,
    "messages": [
        {"role": "user", "content": "Please call test_tool with message='direct test'"}
    ],
    "tools": [{
        "name": "test_tool",
        "description": "A simple test tool",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Test message"}
            },
            "required": ["message"]
        }
    }],
    "stream": True
}

headers = {
    "Authorization": f"Bearer {MINIMAX_API_KEY}",
    "Content-Type": "application/json",
    "anthropic-beta": "prompt-caching-2024-07-31"
}

try:
    with httpx.stream("POST", "https://api.minimaxi.com/anthropic/v1/messages", json=payload, headers=headers, timeout=60.0) as resp:
        log(f"Status: {resp.status_code}")
        if resp.status_code != 200:
            error = resp.read().decode()
            log(f"Error body: {error}")
        else:
            tool_use_blocks = 0
            text_blocks = 0
            for line in resp.iter_lines():
                if not line.strip() or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    if "content_block" in chunk:
                        cb = chunk["content_block"]
                        if cb.get("type") == "tool_use":
                            tool_use_blocks += 1
                            log(f"  -> tool_use block: {json.dumps(cb, ensure_ascii=False)[:200]}")
                        elif cb.get("type") == "text":
                            text_blocks += 1
                except:
                    pass
            log(f"Summary: tool_use_blocks={tool_use_blocks}, text_blocks={text_blocks}")
except Exception as e:
    log(f"Error: {e}")

log(f"\nLog: {LOG_FILE}")
