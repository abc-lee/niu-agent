"""
Verify tool description format impact on MiniMax tool calling.

Test: event-manager style reminder task
- A prompt: uses <tool_use>{"name": "...", "arguments": {...}}</tool_use> text example
- B prompt: uses plain params format content="...", scheduled_at="..."
"""

import requests
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_KEY = "sk-cp--fRDJHXjWX4DvmSh_pWqYcTZABiMamJaRVLgsfk9jQyrKP79webHaEdI3ER6uLmpm-3esnKx9VPDPM3Qekm6NqTDO5TwNxwszlQuYTsLuCMPfo-BIy3C8FE"
BASE_URL = "https://api.minimaxi.com/anthropic/v1/messages"
MODEL = "MiniMax-M2.7-highspeed"


def make_request(prompt: str, tools: list) -> dict:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    payload = {
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools,
    }

    resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=60)
    data = resp.json()
    return {
        "status": resp.status_code,
        "content": data.get("content", []),
        "stop_reason": data.get("stop_reason"),
    }


def extract_tool_calls(content_blocks: list):
    structured = []
    text_tool_calls = []

    for block in content_blocks:
        if block.get("type") == "tool_use":
            structured.append({
                "name": block.get("name"),
                "input": block.get("input", {}),
            })
        elif block.get("type") == "text":
            text = block.get("text", "")
            if "<tool_call>" in text or "<tool_use>" in text:
                text_tool_calls.append(text[:200])

    return structured, text_tool_calls


def main():
    tools = [
        {
            "name": "chat-with-event-manager",
            "description": "Call event manager to handle schedule reminders",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "User request description"
                    }
                },
                "required": ["task"]
            }
        }
    ]

    # A: uses <tool_use> text example
    prompt_a = """You are an event management assistant.

When user says "remind me tomorrow at 9am for a meeting", output:
<tool_use>{"name": "chat-with-event-manager", "arguments": {"task": "Set reminder: tomorrow 9am meeting"}}</tool_use>

Now user says: remind me tomorrow at 3pm about a sales meeting
"""

    # B: uses plain params format
    prompt_b = """You are an event management assistant.

When user says "remind me tomorrow at 9am for a meeting", call the tool:
chat-with-event-manager, params: task="Set reminder: tomorrow 9am meeting"

Now user says: remind me tomorrow at 3pm about a sales meeting
"""

    print("=" * 60)
    print("TEST A: <tool_use> text example in prompt")
    print("=" * 60)
    result_a = make_request(prompt_a, tools)
    structured_a, text_a = extract_tool_calls(result_a["content"])
    print(f"Status: {result_a['status']}")
    print(f"Stop reason: {result_a['stop_reason']}")
    print(f"Structured tool_calls: {len(structured_a)} -> {structured_a}")
    print(f"Text <tool_*> tags: {len(text_a)} -> {text_a}")
    print()

    print("=" * 60)
    print("TEST B: plain params format in prompt")
    print("=" * 60)
    result_b = make_request(prompt_b, tools)
    structured_b, text_b = extract_tool_calls(result_b["content"])
    print(f"Status: {result_b['status']}")
    print(f"Stop reason: {result_b['stop_reason']}")
    print(f"Structured tool_calls: {len(structured_b)} -> {structured_b}")
    print(f"Text <tool_*> tags: {len(text_b)} -> {text_b}")
    print()

    print("=" * 60)
    print("RESULT:")
    print("=" * 60)
    a_ok = len(structured_a) > 0 and len(text_a) == 0
    b_ok = len(structured_b) > 0 and len(text_b) == 0

    print(f"A (tool_use text): {'OK - structured' if a_ok else 'FAIL - went to text'}")
    print(f"B (plain params): {'OK - structured' if b_ok else 'FAIL - went to text'}")

    if not a_ok and b_ok:
        print("\n[CONCLUSION] Plain params format works, tool_use text misleads model")
    elif a_ok and b_ok:
        print("\n[CONCLUSION] Both formats work, problem is elsewhere")
    elif a_ok and not b_ok:
        print("\n[CONCLUSION] tool_use text works, plain params does NOT (unexpected)")
    else:
        print("\n[CONCLUSION] Both went to text - need further analysis")


if __name__ == "__main__":
    main()
