#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the tool_call accumulation fix in litellm_adapter"""
import sys
import os

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import json
import httpx
from datetime import datetime

MINIMAX_API_KEY = "sk-cp--fRDJHXjWX4DvmSh_pWqYcTZABiMamJaRVLgsfk9jQyrKP79webHaEdI3ER6uLmpm-3esnKx9VPDPM3Qekm6NqTDO5TwNxwszlQuYTsLuCMPfo-BIy3C8FE"
API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"

LOG_FILE = f"E:/tools/ai-bot/logs/tool_accumulator_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# 测试 LiteLLM 的工具累积逻辑
log("=" * 60)
log("Testing LiteLLM tool_call accumulation")
log("=" * 60)

import litellm
from litellm import completion

tool_schema = [{
    "type": "function",
    "function": {
        "name": "test_accumulator",
        "description": "Test tool",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description"},
                "file_path": {"type": "string", "description": "File path"}
            },
            "required": ["task", "file_path"]
        }
    }
}]

try:
    log("Calling litellm.completion()...")
    response = completion(
        model="MiniMax-M2.7-highspeed",
        messages=[{"role": "user", "content": "Call test_accumulator with task='入库照片' and file_path='E:/test.jpg'"}],
        tools=tool_schema,
        custom_llm_provider="anthropic",
        api_base="https://api.minimaxi.com/anthropic",
        api_key=MINIMAX_API_KEY,
        max_tokens=1024,
        stream=True,
    )

    # 用修复后的累积逻辑处理
    tool_calls_accumulator = {}
    tool_calls = []

    for chunk in response:
        choices = getattr(chunk, "choices", [None])
        delta = getattr(choices[0], "delta", None) if choices and choices[0] else None
        if not delta:
            continue

        if hasattr(delta, "tool_calls") and delta.tool_calls:
            for tc in delta.tool_calls:
                tc_index = getattr(tc, "index", len(tool_calls_accumulator))

                if tc_index not in tool_calls_accumulator:
                    tool_calls_accumulator[tc_index] = {
                        "id": getattr(tc, "id", None) or f"call_{tc_index}",
                        "name": "",
                        "arguments": ""
                    }

                if hasattr(tc, "id") and tc.id:
                    tool_calls_accumulator[tc_index]["id"] = tc.id

                if hasattr(tc, "function") and tc.function:
                    fn = tc.function
                    # 关键修复: 只有非 None 才更新 name
                    if hasattr(fn, "name") and fn.name:
                        tool_calls_accumulator[tc_index]["name"] = fn.name
                    if hasattr(fn, "arguments") and fn.arguments:
                        tool_calls_accumulator[tc_index]["arguments"] += fn.arguments

                log(f"  Accumulator update: idx={tc_index}, name='{tool_calls_accumulator[tc_index]['name']}', args='{tool_calls_accumulator[tc_index]['arguments'][:50]}'")

    log(f"\nAccumulator state after all chunks:")
    for idx, data in sorted(tool_calls_accumulator.items()):
        log(f"  [{idx}] name='{data['name']}', args='{data['arguments'][:100]}'")

    # 处理 tool_calls
    for idx in sorted(tool_calls_accumulator.keys()):
        tc_data = tool_calls_accumulator[idx]
        tc_name = tc_data["name"]
        tc_args_raw = tc_data["arguments"] or "{}"

        # 跳过完全空的
        if not tc_name and not tc_args_raw:
            log(f"  SKIP: empty tool_call at index {idx}")
            continue

        if not tc_name:
            log(f"  SKIP: no name at index {idx}, args='{tc_args_raw[:50]}'")
            continue

        try:
            tc_args = json.loads(tc_args_raw)
        except json.JSONDecodeError:
            tc_args = {}

        tool_calls.append({"name": tc_name, "args": tc_args, "id": tc_data["id"]})
        log(f"  FINAL tool_call: {tc_name}({json.dumps(tc_args, ensure_ascii=False)[:80]})")

    log(f"\nRESULT: {len(tool_calls)} tool_calls extracted")
    for tc in tool_calls:
        log(f"  - {tc}")

except Exception as e:
    log(f"Failed: {e}")
    import traceback
    traceback.print_exc()

log(f"\nLog: {LOG_FILE}")
