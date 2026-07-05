"""验证 LLM 在给定 @前缀守则的系统提示词下，是否会输出 @niu-agent/@end 前缀。

用法：python/bin/python tests/verify_llm_at_prefix.py

验证目的：新方案根本假设是 LLM 能遵守"@前缀表达意图"的守则。
如果 LLM 完全不输出 @ 前缀，新方案不可行，需要重新设计。
"""
import json
import os
import sys

# 让脚本能 import 顶层 agent 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.runner import create_client
from agent.generic.agent_loop import exhaust


SYSTEM_PROMPT = """你是一个异步子 Agent。每轮输出必须遵循以下格式：

1. 调用工具继续工作：正常 tool_calls
2. 询问主 Agent（不退出，等主 Agent 回答后继续）：content 必须以 `@niu-agent ` 开头，如 `@niu-agent 我应该选择哪个选项？`
3. 结束会话（任务完成或无法继续）：content 必须以 `@end ` 开头，如 `@end 任务已完成，结果：...`

**重要**：禁止输出不带 @ 前缀的纯 content（会被程序拒绝并要求重新输出）。
遇到需要用户决策的问题时，必须用 `@niu-agent` 询问，禁止直接把问题写在 content 里。
"""

USER_TASK = """请打开 16personalities.com 网站开始 MBTI 测试。
遇到第一个问题时不要自己选，必须询问我（用 @niu-agent 前缀）。"""


def _load_llm_config():
    """从 config/user-config.json 读取 LLM 配置（顶层 llm 字段）。"""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "user-config.json",
    )
    with open(config_path) as f:
        data = json.load(f)
    llm = data["llm"]
    # create_client 期望的字段名
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "provider": llm.get("provider", ""),
        "reasoning_effort": llm.get("reasoning_effort") or None,
        "litellm_kwargs": llm.get("litellm_kwargs", {}),
    }


def main():
    llm_config = _load_llm_config()
    print(f"[INFO] Using model: {llm_config['model']} @ {llm_config['apibase']}")

    client = create_client(llm_config)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TASK},
    ]

    # 第一轮：给一个 browser_navigate 工具，让它先调工具
    tools = [{
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "打开网址",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    }]

    print("\n=== 第一轮（有工具可用）===")
    gen = client.chat(messages=messages, tools=tools)
    response = exhaust(gen)
    print(f"tool_calls: {response.tool_calls}")
    print(f"content: {response.content!r}")

    if response.tool_calls:
        # 模拟工具返回，进入第二轮
        messages.append({
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in response.tool_calls
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": response.tool_calls[0].id,
            "content": "已打开 16personalities.com，进入测试页，第 1 题：你经常结交新朋友。请选择 A/B/C/D。",
        })

        print("\n=== 第二轮（遇到选择题，应输出 @niu-agent）===")
        gen = client.chat(messages=messages, tools=tools)
        response = exhaust(gen)
        print(f"tool_calls: {response.tool_calls}")
        print(f"content: {response.content!r}")

        content = (response.content or "").strip()
        if content.startswith("@niu-agent"):
            print("\n[PASS] 验证通过：LLM 输出了 @niu-agent 前缀")
        elif content.startswith("@end"):
            print("\n[WARN] LLM 输出了 @end（误判任务完成）")
        else:
            print("\n[FAIL] 验证失败：LLM 没有输出 @ 前缀")
            print(f"   content: {content!r}")
            sys.exit(1)
    else:
        # 第一轮就没调工具，直接看 content
        content = (response.content or "").strip()
        if content.startswith("@"):
            print(f"\n[PASS] LLM 输出了 @ 前缀: {content[:80]}")
        else:
            print(f"\n[FAIL] LLM 没调工具也没输出 @ 前缀: {content!r}")
            sys.exit(1)


if __name__ == "__main__":
    main()
