"""
端到端测试：一轮 JSON 压缩方案

模拟真实场景：上下文到 85%（~170K tokens）时触发 force 压缩。
消息总量控制在 ~170K tokens（不超过 200K 上下文窗口），
子 Agent 拿到全量内容 + 15% 输出空间，一轮 file_write 输出压缩方案。
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from agent.session import get_message_store
    from agent.subagent import count_tokens_for_text

    # 1. 获取真实 MessageStore
    store = await get_message_store()

    # 先记录当前消息数
    existing_messages = await store.get_messages()
    existing_count = len(existing_messages)
    print(f"[0/6] 当前已有 {existing_count} 条消息")

    # 2. 计算已有消息的 token 数，确定需要添加多少消息
    # 真实场景：force 模式在 85%（~170K tokens）时触发，总量不超过 200K
    target_total_tokens = 170_000  # 85% of 200K
    existing_content = "".join(getattr(m, "content", "") or "" for m in existing_messages)
    existing_tokens = count_tokens_for_text(existing_content)
    print(f"[1/6] 已有消息 {existing_tokens:,} tokens")

    # 每条测试消息约 2000 字符 ≈ 1000 tokens
    chars_per_msg = 2000
    tokens_per_msg = 1000
    remaining_budget = max(0, target_total_tokens - existing_tokens)
    msgs_needed = max(0, int(remaining_budget / tokens_per_msg))

    if msgs_needed == 0:
        print(f"  已有消息已超过 {target_total_tokens:,} tokens，跳过添加")
        await cleanup(store, [])
        return

    print(f"[1/6] 写入 {msgs_needed} 条消息（每条 ~{chars_per_msg} 字符）...")

    added_ids = []
    for i in range(msgs_needed):
        content = f"测试消息 #{i+1}：这是一段用于压缩测试的内容。" + \
                  "人工智能技术在知识管理领域有着广泛的应用前景，" * 20 + \
                  f"当前是第{i+1}条消息，总共{msgs_needed}条。"
        role = "user" if i % 2 == 0 else "assistant"
        msg_id = await store.add_message(role=role, content=content)
        added_ids.append(msg_id)

    # 3. 计算压缩前 token 数
    messages = await store.get_messages()
    total_content = "".join(getattr(m, "content", "") or "" for m in messages)
    tokens_before = count_tokens_for_text(total_content)
    print(f"[2/6] 压缩前：{len(messages)} 条消息，约 {tokens_before:,} tokens")

    # 4. 计算完整 prompt 的 token 数
    full_msg_text = "\n".join(
        f"[id:{getattr(m, 'id', '')}] [idx:{i}] {getattr(m, 'role', '?')}: {getattr(m, 'content', '')}"
        for i, m in enumerate(messages, 1)
    )
    full_tokens = count_tokens_for_text(full_msg_text)
    print(f"[3/6] 完整 prompt：约 {full_tokens:,} tokens")

    if full_tokens > 200_000:
        print(f"  ⚠️  prompt tokens ({full_tokens:,}) 超过 200K 窗口！子 Agent 会溢出")
    else:
        print(f"  ✓  prompt tokens 在 200K 窗口内，子 Agent 不会溢出")

    # 5. 加载 MCP 工具
    print(f"[4/7] 加载 MCP 工具...")
    from agent.mcp_loader import load_mcp_tools
    from agent.tool_registry import get_registry
    load_result = load_mcp_tools()
    registry = get_registry()
    all_schemas = registry.get_schemas()
    session_tools = [s for s in all_schemas if 'session' in s.get('name', '')]
    print(f"  MCP 工具加载: {load_result}, 总工具数: {len(all_schemas)}, session工具: {len(session_tools)}")

    # 6. 调用 tidy_context force 模式
    print(f"[5/7] 调用 tidy_context force 模式（真实 LLM）...")

    from niu_api.chat import get_or_create_runner
    runner = get_or_create_runner()
    if not runner:
        print("  ❌ Runner 初始化失败，无法调用 LLM")
        await cleanup(store, added_ids)
        return

    from niu_api.compat import tidy_context
    try:
        result = await tidy_context(request={"session_id": "default", "mode": "force"})
        print(f"[6/7] tidy_context 结果: {result}")
    except Exception as e:
        print(f"[6/7] tidy_context 异常: {e}")
        import traceback
        traceback.print_exc()
        result = None

    # 7. 计算压缩后 token 数
    messages_after = await store.get_messages()
    total_content_after = "".join(getattr(m, "content", "") or "" for m in messages_after)
    tokens_after = count_tokens_for_text(total_content_after)

    print(f"[7/7] 压缩后：{len(messages_after)} 条消息，约 {tokens_after:,} tokens")

    # 验证
    reduction = tokens_before - tokens_after
    reduction_pct = (reduction / tokens_before * 100) if tokens_before > 0 else 0

    print(f"\n{'='*60}")
    print(f"压缩前: {tokens_before:>12,} tokens ({len(messages)} 条消息)")
    print(f"压缩后: {tokens_after:>12,} tokens ({len(messages_after)} 条消息)")
    print(f"减少:   {reduction:>12,} tokens ({reduction_pct:.1f}%)")
    print(f"方案:   一轮 JSON file_write（全量内容，不截断）")
    print(f"{'='*60}")

    if tokens_after < tokens_before:
        print("✅ 压缩成功：token 数减少")
    else:
        print("❌ 压缩失败：token 数未减少")
        print("   可能原因：context-manager 未生成 compress_plan.json")

    # 检查 compress_plan.json 是否被创建和清理
    plan_path = os.path.expanduser("~/.niu/compress_plan.json")
    if os.path.exists(plan_path):
        print(f"⚠️  compress_plan.json 残留：{plan_path}")
    else:
        print(f"✓  compress_plan.json 已清理")

    await cleanup(store, added_ids)


async def cleanup(store, added_ids):
    """清理测试添加的消息"""
    try:
        messages = await store.get_messages()
        existing_ids = {getattr(m, "id", "") for m in messages}
        to_delete = [mid for mid in added_ids if mid in existing_ids]
        if to_delete:
            await store.delete_messages_by_ids(to_delete)
            print(f"\n清理：删除 {len(to_delete)} 条测试消息")
        else:
            print(f"\n清理：测试消息已被压缩删除，无需额外清理")
    except Exception as e:
        print(f"\n清理失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
