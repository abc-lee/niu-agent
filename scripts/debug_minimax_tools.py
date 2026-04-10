#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify LiteLLM actual request/response for MiniMax tool calls"""
import sys
import os

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import json
import httpx
from datetime import datetime

os.environ["LITELLM_LOG"] = "DEBUG"
os.environ["LITELLM_VERBOSE"] = "TRUE"

LOG_FILE = f"E:/tools/ai-bot/logs/litellm_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ========== Test 1: Direct HTTP call (no LiteLLM) ==========
log("=" * 60)
log("Test 1: Direct HTTP - Verify MiniMax Anthropic endpoint supports tools")
log("=" * 60)

MINIMAX_API_KEY = "sk-cp--fRDJHXjWX4DvmSh_pWqYcTZABiMamJaRVLgsfk9jQyrKP79webHaEdI3ER6uLmpm-3esnKx9VPDPM3Qekm6NqTDO5TwNxwszlQuYTsLuCMPfo-BIy3C8FE"
API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"

tool_schema = {
    "type": "function",
    "function": {
        "name": "test_tool",
        "description": "A test tool",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Test message"}
            },
            "required": ["message"]
        }
    }
}

payload = {
    "model": "MiniMax-M2.7-highspeed",
    "max_tokens": 1024,
    "messages": [
        {"role": "user", "content": "Please call test_tool with message='hello'"}
    ],
    "tools": [tool_schema],
    "stream": True
}

headers = {
    "Authorization": f"Bearer {MINIMAX_API_KEY}",
    "Content-Type": "application/json",
    "anthropic-beta": "prompt-caching-2024-07-31"
}

log(f"Request URL: {API_BASE}")
log(f"Request body: {json.dumps(payload, ensure_ascii=False)}")

try:
    with httpx.stream("POST", API_BASE, json=payload, headers=headers, timeout=60.0) as resp:
        log(f"Status: {resp.status_code}")
        log(f"Headers: {dict(resp.headers)}")

        tool_calls_found = []
        content_chunks = []

        for line in resp.iter_lines():
            if not line.strip():
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                chunk = json.loads(line)
                log(f"RAW: {json.dumps(chunk, ensure_ascii=False)[:400]}")

                if "content_block" in chunk:
                    cb = chunk["content_block"]
                    if cb.get("type") == "tool_use":
                        tool_calls_found.append(cb)
                        log(f">>> TOOL_USE: {json.dumps(cb, ensure_ascii=False)[:300]}")
                    elif cb.get("type") == "text":
                        content_chunks.append(cb.get("text", ""))
                        log(f">>> TEXT: {cb.get('text', '')[:100]}")

                if chunk.get("type") == "content_block_delta":
                    delta = chunk.get("delta", {})
                    dt = delta.get("type", "")
                    if dt == "input_json_delta":
                        log(f">>> INPUT_JSON: {delta.get('partial_json', '')[:100]}")
                    elif dt == "text_delta":
                        log(f">>> TEXT_DELTA: {delta.get('text', '')[:100]}")

            except json.JSONDecodeError:
                log(f"Non-JSON: {line[:100]}")

        log(f"\nSUMMARY: content={''.join(content_chunks)[:200]}, tool_calls={len(tool_calls_found)}")
        for tc in tool_calls_found:
            log(f"  TC: {json.dumps(tc, ensure_ascii=False)[:300]}")

except Exception as e:
    log(f"Request failed: {e}")
    import traceback
    traceback.print_exc()

# ========== Test 2: LiteLLM call (verify SDK conversion) ==========
log("\n" + "=" * 60)
log("Test 2: LiteLLM call - Verify SDK conversion")
log("=" * 60)

import litellm
from litellm import completion

litellm_success_callback = []
litellm_failure_callback = []

def on_success(kwargs):
    log(f"LiteLLM success callback")
    resp = kwargs.get("response", None)
    if resp:
        try:
            log(f"  usage: {getattr(resp, 'usage', None)}")
            log(f"  model: {getattr(resp, 'model', None)}")
        except Exception as e:
            log(f"  error reading resp: {e}")
    litellm_success_callback.append(kwargs)

def on_failure(kwargs):
    log(f"LiteLLM failure callback: {kwargs}")
    litellm_failure_callback.append(kwargs)

class DummyCallback:
    def __init__(self):
        pass
    def async_on_success(self, *a, **k):
        on_success(k)
    def async_on_failure(self, *a, **k):
        on_failure(k)
    def on_success(self, *a, **k):
        on_success(k)
    def on_failure(self, *a, **k):
        on_failure(k)

litellm.callbacks = [DummyCallback()]

try:
    log("Calling litellm.completion()...")
    response = completion(
        model="MiniMax-M2.7-highspeed",
        messages=[{"role": "user", "content": "Please call test_tool with message='hello'"}],
        tools=[tool_schema],
        custom_llm_provider="anthropic",
        api_base="https://api.minimaxi.com/anthropic",
        api_key=MINIMAX_API_KEY,
        max_tokens=1024,
        stream=True,
    )

    tool_calls_found = []
    full_content = ""

    for chunk in response:
        chunk_str = str(chunk)[:200]
        log(f"LiteLLM chunk: {chunk_str}")

        choices = getattr(chunk, "choices", [None])
        delta = getattr(choices[0], "delta", None) if choices and choices[0] else None
        if delta:
            if hasattr(delta, "content") and delta.content:
                full_content += delta.content
            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    tool_calls_found.append(tc)
                    log(f">>> LiteLLM tool_call: name={getattr(getattr(tc, 'function', None), 'name', None)}, args={getattr(getattr(tc, 'function', None), 'arguments', None)}")

    log(f"\nLiteLLM SUMMARY: content={full_content[:200]}, tool_calls={len(tool_calls_found)}")
    for tc in tool_calls_found:
        log(f"  {str(tc)[:300]}")

except Exception as e:
    log(f"LiteLLM call failed: {e}")
    import traceback
    traceback.print_exc()

log(f"\nLog file: {LOG_FILE}")
