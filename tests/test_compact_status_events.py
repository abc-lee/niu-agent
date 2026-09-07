"""验证三触发点都广播了 compact_status 事件。

不验证压缩内容质量，只验证事件推送逻辑。
mock call_subagent 让它立即返回，避免真实 LLM 调用。

注意：_tidy_context_impl 是 async def，测试用 asyncio.run 调用。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_loop(events):
    """构造一个假 main_loop，call_soon_threadsafe 只记录事件不调真实 _sync_broadcast。

    注意：不能调 fn(*args)，否则会触发真实 _sync_broadcast，
    而 _event_subscribers 在测试中未初始化，事件被丢弃且 events 列表为空。
    """
    loop = MagicMock()
    def call_soon(fn, *args):
        # fn 是 _sync_broadcast，args[0] 是 event dict
        # 只记录 event，不调 fn（避免依赖 _event_subscribers）
        if args:
            events.append(args[0])
    loop.call_soon_threadsafe = call_soon
    return loop


def _patch_compat_deps():
    """patch _tidy_context_impl 调用链上的依赖，让流程尽快走到 call_subagent 或异常。

    _tidy_context_impl 内部顺序约：
    1. get_message_store() → 拿 messages
    2. _read_context_window_tokens() → 拿 token 配置
    3. get_or_create_runner() → 拿 runner
    4. call_subagent(...) → 压缩

    测试要让流程走到 4，必须 mock 1-3 返回合理值。
    """
    return [
        patch("niu_api.compat.get_message_store", new=AsyncMock(return_value=MagicMock(
            get_messages=AsyncMock(return_value=[
                MagicMock(role="user", content="test message", tool_calls=None)
            ])
        ))),
        patch("niu_api.compat._read_context_window_tokens", return_value=200000),
        patch("niu_api.chat.get_or_create_runner", return_value=MagicMock(
            llm_config=MagicMock()
        )),
    ]


def test_compat_tidy_impl_emits_compact_status_force():
    """模式2 force：_tidy_context_impl 应广播 started + done。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches:
        p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", return_value="压缩摘要"):
            from niu_api.compat import _tidy_context_impl
            # _tidy_context_impl 是 async def，用 asyncio.run 调用
            asyncio.run(_tidy_context_impl({"mode": "force"}))
    except (Exception, SystemExit):
        # 流程中后续可能因 mock 不全抛异常，但事件应已广播
        pass
    finally:
        for p in patches:
            p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "started" in statuses, f"force 模式未广播 started，实际: {statuses}"
    assert "done" in statuses, f"force 模式未广播 done，实际: {statuses}"
    assert statuses.index("started") < statuses.index("done")


def test_compat_tidy_impl_emits_compact_status_sleep():
    """模式3 sleep：_tidy_context_impl 应广播 started + done。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches:
        p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", return_value="压缩摘要"):
            from niu_api.compat import _tidy_context_impl
            asyncio.run(_tidy_context_impl({"mode": "sleep"}))
    except (Exception, SystemExit):
        pass
    finally:
        for p in patches:
            p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "started" in statuses, f"sleep 模式未广播 started，实际: {statuses}"
    assert "done" in statuses, f"sleep 模式未广播 done，实际: {statuses}"


def test_compat_tidy_impl_emits_done_on_exception():
    """压缩失败时也必须广播 done，避免前端圆环卡死。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches:
        p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", side_effect=RuntimeError("LLM 失败")):
            from niu_api.compat import _tidy_context_impl
            try:
                asyncio.run(_tidy_context_impl({"mode": "force"}))
            except RuntimeError:
                pass
    except (Exception, SystemExit):
        pass
    finally:
        for p in patches:
            p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "done" in statuses, f"异常路径未广播 done，前端会卡死，实际: {statuses}"

