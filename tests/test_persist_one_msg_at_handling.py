"""_persist_one_msg @ 来源处理测试（修正版方案 1）+ persist_agent_reply 去重（方案 2）。

背景（000006/000026 实证）：主 Agent 轮中 persist 事件把含 @子Agent 段的 assistant
content 原样写入 DB → 用户对话流看到主↔子内容；轮末 persist_agent_reply 对 full_reply
整轮拼接懒匹配跨消息提取 → 超长 subagent_msg + 子 Agent 已清理变 orphan。

修正版：
- assistant 有 tool_calls：工具是回复通道——仅 strip content 的 @ 段，不提取（000006）
- assistant 无 tool_calls：提取 @ 为 subagent_msg（轮中落库 → db_monitor 实时路由，
  子 Agent 挂起时即收到，不 orphan）+ strip content（000026）
- 其他 role：原样写入
"""
import asyncio
from unittest import mock

from agent.runner import NiuRunner


class _MiniRunner(NiuRunner):
    """最小化 runner：绕过 __init__ 副作用，只测 _persist_one_msg。"""

    def __init__(self):
        self._extracted_at_msgs = []


def _make_runner():
    runner = _MiniRunner()
    runner._sync_add_message = mock.MagicMock(return_value="msg-1")
    return runner, runner._sync_add_message


def test_persist_one_msg_tool_calls_assistant_strips_only():
    """有 tool_calls 的 assistant：content 的 @ 段仅 strip，不提取 subagent_msg。"""
    runner, sync_add = _make_runner()
    with mock.patch("niu_api.chat.notify_new_message_sync") as notify:
        msg_id = runner._persist_one_msg({
            "role": "assistant",
            "content": "哈哈它叫我老板了。\n@nutritionist 你好，先告诉你用户情况",
            "tool_calls": [{"id": "call-1"}],
        })
    assert msg_id == "msg-1"
    write_kwargs = sync_add.call_args.kwargs
    assert write_kwargs["role"] == "assistant"
    assert "@nutritionist" not in write_kwargs["content"]  # @ 段已剥离，不泄露到用户对话
    subagent_writes = [c for c in sync_add.call_args_list
                       if c.kwargs.get("role") == "subagent_msg"]
    assert subagent_writes == []  # 有 tool_calls 不提取（工具是回复通道）
    assert runner._extracted_at_msgs == []
    # SSE 推送剥离后的 content（用户对话流看不到主↔子内容）
    notify.assert_called_once()
    assert "@nutritionist" not in notify.call_args.args[2]


def test_persist_one_msg_pure_text_extracts_subagent_msg():
    """无 tool_calls 的 assistant：@ 段提取为 subagent_msg（轮中落库）+ content strip。"""
    runner, sync_add = _make_runner()
    with mock.patch("niu_api.chat.notify_new_message_sync") as notify:
        msg_id = runner._persist_one_msg({
            "role": "assistant",
            "content": "哈哈它叫我老板了。\n@nutritionist 你好，先告诉你用户情况",
        })
    assert msg_id == "msg-1"
    calls = sync_add.call_args_list
    roles = [c.kwargs["role"] for c in calls]
    assert roles == ["subagent_msg", "assistant"]  # 先 subagent_msg 后 assistant
    assert "@nutritionist [主Agent]" in calls[0].kwargs["content"]  # format_for_db
    assert "@nutritionist" not in calls[1].kwargs["content"]  # assistant content 已 strip
    assert runner._extracted_at_msgs == [calls[0].kwargs["content"]]  # 去重记录
    notify.assert_called_once()
    assert "@nutritionist" not in notify.call_args.args[2]


def test_persist_one_msg_non_assistant_untouched():
    """tool 等其他 role 原样写入（@ 处理只针对 assistant）。"""
    runner, sync_add = _make_runner()
    with mock.patch("niu_api.chat.notify_new_message_sync"):
        runner._persist_one_msg({"role": "tool", "tool_call_id": "call-1", "content": "@foo 内容"})
    write_kwargs = sync_add.call_args.kwargs
    assert write_kwargs["role"] == "tool"
    assert write_kwargs["content"] == "@foo 内容"
    assert runner._extracted_at_msgs == []


def test_persist_agent_reply_dedups_already_extracted():
    """rv=None + extracted_at_msgs 已含该 subagent_msg → 兜底提取去重跳过（不重复入库）。"""
    from niu_api.chat import persist_agent_reply

    class _FakeStore:
        def __init__(self):
            self.calls = []

        async def add_message(self, **kwargs):
            self.calls.append(kwargs)
            return f"id-{len(self.calls)}"

    store = _FakeStore()
    full_reply = "@nutritionist 你好"
    persisted = [{"role": "assistant", "content": "", "_persisted_id": "pid-1"}]
    with mock.patch("niu_api.chat.notify_new_message", new_callable=mock.AsyncMock) as notify:
        asyncio.run(persist_agent_reply(
            store, None, 0, full_reply,
            persisted_msgs=persisted,
            extracted_at_msgs=["@nutritionist [主Agent] 你好"],
        ))
    subagent_writes = [c for c in store.calls if c["role"] == "subagent_msg"]
    assert subagent_writes == []  # 已由 _persist_one_msg 轮中提取 → 去重跳过
    assert not notify.called


def test_persist_agent_reply_extracts_when_no_dedup_list():
    """rv=None + extracted_at_msgs 未提供（None）→ 兜底提取照常（旧契约不回归）。"""
    from niu_api.chat import persist_agent_reply

    class _FakeStore:
        def __init__(self):
            self.calls = []

        async def add_message(self, **kwargs):
            self.calls.append(kwargs)
            return f"id-{len(self.calls)}"

    store = _FakeStore()
    full_reply = "@nutritionist 你好"
    with mock.patch("niu_api.chat.notify_new_message", new_callable=mock.AsyncMock) as notify:
        asyncio.run(persist_agent_reply(store, None, 0, full_reply, persisted_msgs=[]))
    subagent_writes = [c for c in store.calls if c["role"] == "subagent_msg"]
    assert len(subagent_writes) == 1
    assert "nutritionist" in subagent_writes[0]["content"]
    assert not notify.called
