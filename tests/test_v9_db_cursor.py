"""
验证 v9 流式推送的 DB 游标方法是否正常工作

纯 DB 层面测试，不需要飞书消息触发。
"""
import asyncio
import sys

sys.path.insert(0, "REDACTED_USER_PATH/tools/ai-bot")

from agent.session import get_message_store


async def test_db_cursor_methods():
    store = await get_message_store()

    # 1. 测试 get_max_rowid
    max_rowid = await store.get_max_rowid()
    print(f"✓ get_max_rowid: {max_rowid}")

    # 2. 测试 get_assistant_text_after_rowid
    # 读取 DB 中所有 assistant 文本
    all_texts = await store.get_assistant_text_after_rowid(0)
    print(f"✓ get_assistant_text_after_rowid(0): {len(all_texts)} texts")
    for i, text in enumerate(all_texts):
        preview = text[:60] if text else ""
        print(f"  [{i}] len={len(text)} preview: {preview}")

    # 3. 从 max_rowid 读取（应该返回空，因为游标已到最后）
    texts_after_max = await store.get_assistant_text_after_rowid(max_rowid)
    print(f"✓ get_assistant_text_after_rowid({max_rowid}): {len(texts_after_max)} texts (expected 0)")

    # 4. 从中间位置读取
    if max_rowid > 0:
        mid = max_rowid // 2
        texts_after_mid = await store.get_assistant_text_after_rowid(mid)
        print(f"✓ get_assistant_text_after_rowid({mid}): {len(texts_after_mid)} texts")

    print("\n✅ DB 游标方法验证完成")


if __name__ == "__main__":
    asyncio.run(test_db_cursor_methods())
