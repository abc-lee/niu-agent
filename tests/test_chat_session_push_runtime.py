"""chat_session 投递段 runtime mock 测试（规格 §4.1 rows 6/7）。

row 6: chat_error 非 None → push_im_reply 仍被调用（无条件投递），第二参为错误文案
row 7: chat_error None → push_im_reply 被调用且第二参 == 完整 full_reply

环境复用 test_notify_llm_error.py 的 _mock_chat_session_env/_run_chat_session 模式
（假锁/假 store/假 context_manager 全 mock，无真实 LLM / DB / 图谱写入）。

mock 目标（项目实证教训——patch 消费方解析点）：
compat.chat_session 函数体内 `from niu_api.channel.gateway import push_im_reply`
在调用时从 gateway 模块取属性 → patch('niu_api.channel.gateway.push_im_reply') 有效；
patch 消费方命名空间 niu_api.compat.push_im_reply 无效（compat 无模块级绑定）。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from niu_api import compat


def _make_runner(chunks=("hello ", "world"), chat_side_effect=None):
    """构造 chat_session 可用的 MagicMock runner（对齐 test_notify_llm_error._llm_error_runner）。"""
    runner = MagicMock()
    if chat_side_effect is not None:
        runner.chat.side_effect = chat_side_effect
    else:
        runner.chat.return_value = iter(chunks)
    runner.last_return_value = None
    runner.should_push_im.return_value = False
    runner.get_im_channel.return_value = ""
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()
    return runner


def _run_chat_session(runner):
    """mock 环境运行 compat.chat_session，返回 (resp, push_calls)。

    环境照抄 test_notify_llm_error._mock_chat_session_env：
    假锁/假 store/假 context_manager + get_or_create_runner/notify_new_message 全 mock；
    persist_agent_reply 透传 full_reply；notify_llm_error_sync 吞掉（本组测试不触发）；
    push_im_reply 以记录器替换（patch 源头模块属性，兼容函数体内局部 import）。
    """
    from niu_api.compat import chat_session

    fake_lock = MagicMock()
    fake_lock.locked.return_value = False
    fake_lock.acquire = AsyncMock()
    fake_lock.release = MagicMock()
    old_lock = compat._chat_lock
    compat._chat_lock = fake_lock

    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")

    cm = MagicMock()
    cm.get_context_for_chat = AsyncMock(return_value=[])

    ctx = (
        patch("niu_api.config.get_config", return_value=SimpleNamespace(llm=SimpleNamespace(api_key="test"))),
        # compat.py L17 模块级 `from agent.session import get_message_store` 已复制绑定到
        # compat 命名空间——patch 源模块对 chat_session 内调用无效（会写真实 ~/.niu/messages.db）。
        # 必须 patch 消费方命名空间 niu_api.compat.get_message_store。
        patch("niu_api.compat.get_message_store", AsyncMock(return_value=store)),
        patch("agent.context_manager.get_context_manager", AsyncMock(return_value=cm)),
        patch("niu_api.chat.get_or_create_runner", return_value=runner),
        patch("niu_api.chat.notify_new_message", AsyncMock()),
        patch("agent.runner.clear_stop"),
        patch("agent.runner.drain_supplements"),
    )

    async def fake_persist(store_arg, rv, history_len, full_reply, **kw):
        # 透传：让 full_reply 保持投递段入参（完整拼接文本）
        return ("persisted-msg-id", full_reply)

    push_calls = []

    async def fake_push(runner_arg, reply):
        push_calls.append((runner_arg, reply))
        return True

    try:
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], \
                patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: None), \
                patch("niu_api.chat.persist_agent_reply", side_effect=fake_persist), \
                patch("niu_api.channel.gateway.push_im_reply", side_effect=fake_push):
            resp = asyncio.run(
                chat_session(SimpleNamespace(message="hello", source="electron", resources=None))
            )
    finally:
        compat._chat_lock = old_lock
    return resp, push_calls


def test_chat_error_non_none_still_calls_push_im_reply():
    """row 6：chat_error 非 None（LLM 调用抛异常）→ push_im_reply 仍被 await 调用，第二参为错误文案。

    异常文案流式期已进卡（stream_error str chunk → notify_stream），必须终结；
    full_reply = f"Error: {str(e)}"（非 LLM 异常不替换为友好文案）。
    """
    def raise_value_error(*a, **kw):
        raise ValueError("internal bug")

    runner = _make_runner(chat_side_effect=raise_value_error)
    resp, push_calls = _run_chat_session(runner)

    assert len(push_calls) == 1, "chat_error 非 None 也必须调用 push_im_reply（无条件投递语义）"
    pushed_runner, reply = push_calls[0]
    assert pushed_runner is runner  # 第一参 = runner（get_or_create_runner 返回的同一对象）
    assert reply == "Error: internal bug"  # 第二参 = full_reply 错误文案（f"Error: {str(e)}"）
    assert resp.reply == reply  # 返回给前端的与投递的一致


def test_chat_error_none_calls_push_im_reply_with_full_reply():
    """row 7：chat_error None（正常路径）→ push_im_reply 被调用且第二参 == 完整 full_reply。"""
    runner = _make_runner(chunks=("hello ", "world"))
    resp, push_calls = _run_chat_session(runner)

    assert len(push_calls) == 1
    pushed_runner, reply = push_calls[0]
    assert pushed_runner is runner
    assert reply == "hello world"  # 完整 full_reply（chunks 拼接全文，persist 透传）
    assert resp.reply == reply  # 前端收到的回复与 IM 投递内容一致
