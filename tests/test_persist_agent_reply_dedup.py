"""停止场景重复消息修复测试：persist_agent_reply elif 兜底分支去重。

全 mock：store 用 fake 参数（不 patch get_message_store）、notify_new_message 用 patch，
不调真实 LLM/DB。验证停止时 rv=None 走 elif 兜底分支时，若已持久化 assistant 内容
以 full_reply 为前缀（含相等）则跳过重复写入；非前缀（停止落在 reply→persist 窗口）
兜底写入避免丢内容。
"""
import asyncio
from unittest import mock

from niu_api.chat import persist_agent_reply


class FakeStore:
    """fake store：add_message 记录调用"""
    def __init__(self):
        self.calls = []

    async def add_message(self, **kwargs):
        self.calls.append(kwargs)
        return f"id-{len(self.calls)}"


def _assistant_pm(content):
    return {"role": "assistant", "content": content, "_persisted_id": f"pid-{content[:4]}"}


# 1. rv=None + persisted_msgs 含 assistant（content=full_reply 相等）→ skip（不写不 notify）
def test_rv_none_with_persisted_assistant_skips_fallback():
    store = FakeStore()
    full_reply = "好的老板，我现在同步调用事件管理子Agent"
    persisted = [_assistant_pm(full_reply)]
    with mock.patch("niu_api.chat.notify_new_message") as notify:
        mid, _ = asyncio.run(persist_agent_reply(
            store, None, 0, full_reply, persisted_msgs=persisted))
    assert store.calls == []  # 不写 assistant
    assert not notify.called  # notify 同门控不推
    assert mid is None


# 2. rv=None + persisted_msgs 无 assistant（或 None）→ 兜底写
def test_rv_none_without_persisted_assistant_writes_fallback():
    store = FakeStore()
    full_reply = "部分内容"
    with mock.patch("niu_api.chat.notify_new_message") as notify:
        asyncio.run(persist_agent_reply(
            store, None, 0, full_reply, persisted_msgs=None))
    assert len(store.calls) == 1
    assert store.calls[0]["role"] == "assistant"
    assert store.calls[0]["content"] == full_reply
    assert notify.called


# 3. rv=None + 前面轮次 assistant（content="AB"）+ full_reply="C"（非前缀）→ 写（宁写勿丢）
def test_rv_none_multiturn_prefix_write():
    store = FakeStore()
    persisted = [_assistant_pm("前面轮次的回复内容")]
    full_reply = "本轮停止时的文本"
    with mock.patch("niu_api.chat.notify_new_message"):
        asyncio.run(persist_agent_reply(store, None, 0, full_reply, persisted_msgs=persisted))
    assert len(store.calls) == 1  # 非前缀 → 写


# 4. rv=None + persisted_msgs assistant content="AB" + full_reply="AB" → 前缀相等 skip
def test_rv_none_prefix_equal_skip():
    store = FakeStore()
    persisted = [_assistant_pm("AB")]
    with mock.patch("niu_api.chat.notify_new_message") as notify:
        asyncio.run(persist_agent_reply(store, None, 0, "AB", persisted_msgs=persisted))
    assert store.calls == []
    assert not notify.called


# 5. rv dict 分支指纹去重回归（rv 非 None → 走 rv 分支，elif 不执行）
def test_rv_dict_uses_fingerprint_dedup():
    store = FakeStore()
    rv = {"result": "CURRENT_TASK_DONE", "messages": [
        {"role": "assistant", "content": "回复内容", "tool_calls": []},
    ]}
    with mock.patch("niu_api.chat.notify_new_message"):
        asyncio.run(persist_agent_reply(store, rv, 0, "回复内容", persisted_msgs=[]))
    # rv 分支：fingerprint 空（persisted_msgs=[]）→ 写 assistant
    assert len(store.calls) == 1
    assert store.calls[0]["role"] == "assistant"


# 6. rv=None + full_reply 空 → 不写
def test_rv_none_empty_full_reply_no_write():
    store = FakeStore()
    with mock.patch("niu_api.chat.notify_new_message"):
        asyncio.run(persist_agent_reply(store, None, 0, "", persisted_msgs=[]))
    assert store.calls == []


# 7. rv=None + full_reply 含 @（普通文本在前、@ 单独成行在后）→ strip 对齐后前缀相等 skip；
#    at_msgs 的 subagent_msg 写照常
def test_rv_none_with_at_message_skip():
    store = FakeStore()
    full_reply = "好的老板，我现在同步调用事件管理子Agent\n@file-processor-1a2b 补充信息"
    # persisted_msgs 含原始含 @ 的 assistant（V4 通道未 strip）
    persisted = [_assistant_pm("好的老板，我现在同步调用事件管理子Agent\n@file-processor-1a2b 补充信息")]
    with mock.patch("niu_api.chat.notify_new_message") as notify:
        asyncio.run(persist_agent_reply(store, None, 0, full_reply, persisted_msgs=persisted))
    # subagent_msg 写入照常（函数顶部 extract_at_messages 处理）
    subagent_writes = [c for c in store.calls if c["role"] == "subagent_msg"]
    assert len(subagent_writes) == 1
    assert "file-processor-1a2b" in subagent_writes[0]["content"]
    # assistant 兜底 skip（@ 对齐后前缀相等）
    assistant_writes = [c for c in store.calls if c["role"] == "assistant"]
    assert assistant_writes == []
    assert not notify.called
