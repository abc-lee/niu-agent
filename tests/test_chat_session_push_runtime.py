"""chat_session 投递段 runtime 替身测试（P3 D-6 / 规格 §4.1 rows 6/7）。

row 6: 命令产出 error → push_im_reply 仍被 await 调用（无条件投递语义），第二参为共享
       持久化产出的中性文案（P3 §4.6：三端点错误语义统一到 `persist_run_outcome`，
       compat 不再自己拼 `Error: {e}`，错误细节也不进投递文本）
row 7: 正常回合 → push_im_reply 被调用且第二参 == 完整 full_reply
row 8: 投递携带 on_complete 锁内快照（D-6：should_push_im / im_channel_id）

环境：`FakeRuntime`（经 `agent.main_runtime.get_main_runtime` 注入，D-8）+
`main_loop_thread()`（`on_complete` 的 `run_coroutine_threadsafe` 桥接目标）+
假 store / 假 persist / 假 push —— 无真实 LLM / DB / 图谱写入。

mock 目标（项目实证教训——patch 消费方解析点）：
compat.chat_session 函数体内 `from niu_api.channel.gateway import push_im_reply`
在调用时从 gateway 模块取属性 → patch('niu_api.channel.gateway.push_im_reply') 有效；
patch 消费方命名空间 niu_api.compat.push_im_reply 无效（compat 无模块级绑定）。
同理 compat 模块级 `from agent.session import get_message_store` 已复制绑定 →
必须 patch 消费方命名空间 niu_api.compat.get_message_store。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from p2b_fakes import FakeRuntime, main_loop_thread

from niu_api import compat
from niu_api.main_agent_persist import NEUTRAL_REPLY

_DONE_RV = {"result": "CURRENT_TASK_DONE", "data": None, "messages": []}


def _make_runner(chunks=("hello ", "world"), chat_side_effect=None,
                 return_value=None, should_push_im=False, im_channel_id=""):
    """构造 chat_session 可用的 MagicMock runner（对齐 test_notify_llm_error._llm_error_runner）。

    `return_value` 非 None → 正常回合（`advanced and rv is None` 不再判取消）；
    `_persisted_msgs` / `_extracted_at_msgs` 显式 None，避免 Mock 属性把 getattr 默认值变成 Mock。
    """
    runner = MagicMock()
    if chat_side_effect is not None:
        runner.chat.side_effect = chat_side_effect
    else:
        runner.chat.return_value = iter(chunks)
    runner.last_return_value = return_value
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    runner.should_push_im.return_value = should_push_im
    runner.get_im_channel.return_value = im_channel_id
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()
    return runner


def _run_chat_session(runner):
    """mock 环境运行 compat.chat_session，返回 (resp, push_calls, runtime)。

    push_calls 每项为 (runner, 投递文本, snapshot)——snapshot 是 holder（D-6）。
    """
    from niu_api.compat import chat_session

    # 假锁：busy 判据（runtime.is_round_active 会 OR `_chat_lock.locked()`）必须从干净基线起算
    lock_before = compat._chat_lock
    compat._chat_lock = asyncio.Lock()

    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")

    runtime = FakeRuntime(runner=runner)

    ctx = (
        patch("niu_api.config.get_config",
              return_value=SimpleNamespace(llm=SimpleNamespace(api_key="test"))),
        patch("niu_api.compat.get_message_store", AsyncMock(return_value=store)),
        # D-8：端点取 runtime 的唯一入口——注入替身（I-2：端点不取锁、不 runner.chat）
        patch("agent.main_runtime.get_main_runtime", lambda: runtime),
        patch("niu_api.chat.get_or_create_runner", return_value=runner),
        patch("niu_api.chat.notify_new_message", AsyncMock()),
    )

    async def fake_persist(store_arg, rv, history_len, full_reply, **kw):
        # 透传：让 full_reply 保持投递段入参（完整拼接文本）
        return ("persisted-msg-id", full_reply)

    push_calls = []

    async def fake_push(runner_arg, reply, snapshot=None):
        push_calls.append((runner_arg, reply, snapshot))
        return True

    try:
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], \
                patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: None), \
                patch("niu_api.chat.persist_agent_reply", side_effect=fake_persist), \
                patch("niu_api.channel.gateway.push_im_reply", side_effect=fake_push), \
                main_loop_thread():
            resp = asyncio.run(
                chat_session(SimpleNamespace(message="hello", source="electron", resources=None))
            )
    finally:
        runtime.stop()
        compat._chat_lock = lock_before
    return resp, push_calls, runtime


def test_chat_error_non_none_still_calls_push_im_reply():
    """row 6：命令产出 error（runner.chat 抛异常）→ push_im_reply 仍被 await 调用。

    异常文案流式期已进卡（LLM 错误必须终结卡片），P3 §4.6 后投递/返回的是共享持久化
    的中性文案：原始错误细节既不进 DB 也不进投递文本。
    """
    def raise_value_error(*a, **kw):
        raise ValueError("internal bug")

    runner = _make_runner(chat_side_effect=raise_value_error)
    resp, push_calls, runtime = _run_chat_session(runner)

    assert len(push_calls) == 1, "chat_error 非 None 也必须调用 push_im_reply（无条件投递语义）"
    pushed_runner, reply, _snapshot = push_calls[0]
    assert pushed_runner is runner  # 第一参 = runner（get_or_create_runner 返回的同一对象）
    assert reply == NEUTRAL_REPLY, f"P3 统一语义：投递中性文案，实际: {reply!r}"
    assert "internal bug" not in reply, "原始错误细节不得进投递文本"
    assert resp.reply == reply  # 返回给前端的与投递的一致
    assert resp.message_id == "persisted-msg-id", "降级回复行仍必须落库（message_id 来自 holder）"
    # 命令确实跑过并产出 error（端点不再自己捕获 runner 异常）
    assert len(runtime.outcomes) == 1
    assert runtime.outcomes[0].error is not None


def test_chat_error_none_calls_push_im_reply_with_full_reply():
    """row 7：chat_error None（正常路径）→ push_im_reply 被调用且第二参 == 完整 full_reply。"""
    runner = _make_runner(chunks=("hello ", "world"), return_value=_DONE_RV)
    resp, push_calls, runtime = _run_chat_session(runner)

    assert len(push_calls) == 1
    pushed_runner, reply, _snapshot = push_calls[0]
    assert pushed_runner is runner
    assert reply == "hello world"  # 完整 full_reply（chunks 拼接全文，persist 透传）
    assert resp.reply == reply  # 前端收到的回复与 IM 投递内容一致
    assert resp.message_id == "persisted-msg-id"
    # I-1：runner.chat 只由 runtime 线程调用一次；端点侧零 runner 变更（规则 5）
    runner.chat.assert_called_once()
    runner.set_im_channel.assert_not_called()
    runner.set_im_force.assert_not_called()


def test_push_receives_on_complete_im_snapshot():
    """row 8（D-6）：push_im_reply 收到 on_complete 锁内快照，而非 wait 之后现读 runner。"""
    runner = _make_runner(
        chunks=("hi",), return_value=_DONE_RV,
        should_push_im=True, im_channel_id="cid-im-1",
    )
    resp, push_calls, _runtime = _run_chat_session(runner)

    assert len(push_calls) == 1
    _pushed_runner, _reply, snapshot = push_calls[0]
    assert snapshot is not None, "chat_session 必须把 holder 快照传给 push_im_reply（D-6）"
    assert snapshot["should_push_im"] is True
    assert snapshot["im_channel_id"] == "cid-im-1"
    assert resp.reply == "hi"
