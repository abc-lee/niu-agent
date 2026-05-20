"""ChatQueue 单元测试 — 消息队列 + 串行处理 + 上下文合并"""
import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def reset_globals():
    """每个测试前后清理全局单例"""
    import niu_api.chat_queue as mod
    mod._queue = None
    yield
    mod._queue = None


@pytest.fixture
def mock_runner():
    runner = MagicMock()
    runner.chat.return_value = iter(["回复内容"])
    runner.last_return_value = {"result": "ok", "messages": []}
    return runner


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.add_message.return_value = "msg-id-1"
    return store


def _setup_context_manager(mock_cm):
    """配置 mock context_manager"""
    mock_cm_instance = AsyncMock()
    mock_cm_instance.get_context_for_chat.return_value = []
    mock_cm.return_value = mock_cm_instance
    return mock_cm_instance


# get_context_manager 是在 _process_single 内部延迟导入的，
# 所以必须 patch 定义处 agent.context_manager.get_context_manager
# _check_overflow 是实例方法，patch 为 niu_api.chat_queue.ChatQueue._check_overflow


@pytest.mark.asyncio
async def test_enqueue_and_process(mock_runner, mock_store):
    """单条消息入队后应被 ChatWorker 串行处理"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        original = q._process_single
        async def tracked(*a, **kw):
            r = await original(*a, **kw)
            processed.set()
            return r
        q._process_single = tracked

        await q.start()
        try:
            result = await q.enqueue("你好")
            assert result.queued is True
            await asyncio.wait_for(processed.wait(), timeout=5.0)
            # runner.chat(session_id, user_input, stream=False, history=...)
            mock_runner.chat.assert_called_once()
            call_args = mock_runner.chat.call_args
            # 第二个位置参数是 user_input
            assert call_args[0][1] == "你好"
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_message_merging(mock_runner, mock_store):
    """多条待处理消息应合并为一条传给 runner.chat()"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        original = q._process_single
        async def tracked(*a, **kw):
            r = await original(*a, **kw)
            processed.set()
            return r
        q._process_single = tracked

        await q.start()
        try:
            await q.enqueue("第一条消息")
            await q.enqueue("补充信息")
            await q.enqueue("再补充")

            await asyncio.wait_for(processed.wait(), timeout=5.0)

            call_args = mock_runner.chat.call_args
            user_input = call_args[0][1]
            assert "第一条消息" in user_input
            assert "补充信息" in user_input
            assert "再补充" in user_input
            assert "[补充1]" in user_input
            assert "[补充2]" in user_input
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_returns_immediately(mock_runner, mock_store):
    """enqueue 应立即返回，不等待处理完成"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        await q.start()
        try:
            start = time.monotonic()
            result = await q.enqueue("测试立即返回")
            elapsed = time.monotonic() - start
            assert result.queued is True
            assert elapsed < 1.0
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_and_wait(mock_runner, mock_store):
    """enqueue_and_wait 应等待处理完成后返回回复"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        await q.start()
        try:
            reply = await q.enqueue_and_wait("测试等待", timeout=10.0)
            assert reply == "回复内容"
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_drain(mock_runner, mock_store):
    """drain 应清空队列并等待当前处理完成"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        await q.start()
        try:
            # 入队但不等待处理
            await q.enqueue("test1")
            await q.enqueue("test2")
            # drain 应清空队列并等待处理完成
            drained = await q.drain(timeout=5.0)
            assert drained
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_is_processing_flag(mock_runner, mock_store):
    """is_processing 应在处理期间为 True，处理完成后为 False"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processing_during = asyncio.Event()
    processing_checked = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock):

        _setup_context_manager(mock_cm)

        original = q._process_single
        async def tracked(*a, **kw):
            processing_during.set()
            # 等待测试检查 is_processing
            await asyncio.wait_for(processing_checked.wait(), timeout=5.0)
            r = await original(*a, **kw)
            return r
        q._process_single = tracked

        await q.start()
        try:
            assert q.is_processing is False
            await q.enqueue("测试处理标志")
            await asyncio.wait_for(processing_during.wait(), timeout=5.0)
            assert q.is_processing is True
            processing_checked.set()
            # 等待处理完成
            await q.drain(timeout=5.0)
            assert q.is_processing is False
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_enqueue_sync_with_no_loop(mock_runner):
    """enqueue_sync 在主循环不可用时应返回失败"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)

    # enqueue_sync 内部 from niu_api.chat import _main_loop
    # 需要patch niu_api.chat._main_loop
    with patch("niu_api.chat._main_loop", None):
        result = q.enqueue_sync("测试无循环")
        assert result.queued is False


@pytest.mark.asyncio
async def test_feishu_push_on_feishu_source(mock_runner, mock_store):
    """飞书来源的消息处理完成后应推送回复到飞书"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.ChatQueue._push_to_feishu", new_callable=AsyncMock) as mock_push:

        _setup_context_manager(mock_cm)

        original = q._process_with_merge
        async def tracked(req):
            r = await original(req)
            processed.set()
            return r
        q._process_with_merge = tracked

        await q.start()
        try:
            await q.enqueue("飞书消息", source="feishu", channel_id="oc_test123")
            await asyncio.wait_for(processed.wait(), timeout=5.0)
            mock_push.assert_called_once_with("回复内容")
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_no_feishu_push_on_frontend_source(mock_runner, mock_store):
    """前端来源的消息处理完成后不应推送回复到飞书"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    processed = asyncio.Event()

    with patch("niu_api.chat_queue.get_message_store", return_value=mock_store), \
         patch("niu_api.chat_queue.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat_queue.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
         patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock), \
         patch("niu_api.chat_queue.ChatQueue._push_to_feishu", new_callable=AsyncMock) as mock_push:

        _setup_context_manager(mock_cm)

        original = q._process_with_merge
        async def tracked(req):
            r = await original(req)
            processed.set()
            return r
        q._process_with_merge = tracked

        await q.start()
        try:
            await q.enqueue("前端消息", source="frontend")
            await asyncio.wait_for(processed.wait(), timeout=5.0)
            mock_push.assert_not_called()
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_drain_cancels_pending_futures(mock_runner, mock_store):
    """drain 应为队列中等待的请求设置 reply_future 结果"""
    from niu_api.chat_queue import ChatQueue, ChatRequest

    q = ChatQueue(mock_runner)

    # 不启动 worker，直接入队
    loop = asyncio.get_running_loop()
    future1 = loop.create_future()
    future2 = loop.create_future()

    req1 = ChatRequest(content="msg1", source="scheduler", reply_future=future1)
    req2 = ChatRequest(content="msg2", source="scheduler", reply_future=future2)

    await q._queue.put(req1)
    await q._queue.put(req2)

    # drain 应清空队列并设置 future 结果
    drained = await q.drain(timeout=2.0)
    assert drained
    assert future1.result() == "[会话已清空]"
    assert future2.result() == "[会话已清空]"
