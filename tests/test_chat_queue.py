"""ChatQueue 单元测试 — 消息队列 + 串行处理 + 上下文合并"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
         patch("niu_api.chat.notify_new_message", new_callable=AsyncMock), \
         patch("agent.context_manager.get_context_manager") as mock_cm, \
         patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")), \
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
async def test_enqueue_sync_with_no_loop():
    """enqueue_sync 在所有循环都不可用时应返回失败"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(MagicMock())

    # 模拟 _main_loop=None 且无运行中循环
    with patch("niu_api.chat._main_loop", None), \
         patch.object(asyncio, "get_running_loop", side_effect=RuntimeError("no running loop")):
        result = q.enqueue_sync("测试无循环")
        assert result.queued is False


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


# ── scheduler 回复"仅终结卡片、不投递"（定时任务飞书流式卡片永不终结修复）──

class _FakeIMGateway:
    """记录 send_sync 调用的假 IMGateway（is_connected 可配）"""

    def __init__(self, connected=True):
        self.is_connected = connected
        self.sent = []  # (channel_id, content, pop_reply_to, ask_finalize)

    def send_sync(self, channel_id, content, pop_reply_to=True, ask_finalize=False):
        self.sent.append((channel_id, content, pop_reply_to, ask_finalize))


def _scheduler_patches(mock_store):
    """scheduler 路由测试共用的 _process_single 补丁集"""
    return (
        patch("niu_api.chat_queue.get_message_store", return_value=mock_store),
        patch("niu_api.chat.notify_new_message", new_callable=AsyncMock),
        patch("agent.context_manager.get_context_manager"),
        patch("niu_api.chat.persist_agent_reply", new_callable=AsyncMock, return_value=("msg-id", "回复内容")),
        patch("niu_api.chat_queue.ChatQueue._check_overflow", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_scheduler_reply_finalizes_im_card(mock_runner, mock_store, monkeypatch):
    """scheduler 通道无 channel_id + 有 IM 继承：send_sync 空 content 终结卡片（仅终结不投递，
    不新增独立 IM 消息），reply_future._im_finalized 置位（watcher 据此跳过自推防双投递）"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    mock_runner.get_im_channel.return_value = "oc_im_cid_1"
    gw = _FakeIMGateway()
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)

    p1, p2, p3, p4, p5 = _scheduler_patches(mock_store)
    with p1, p2, p3 as mock_cm, p4, p5:
        _setup_context_manager(mock_cm)
        await q.start()
        try:
            reply, future = await q.enqueue_and_wait_with_future("[定时任务] 吃药", timeout=10.0)
            assert reply == "回复内容"
            # 仅终结不投递：SEND 空 content（adapter _on_send 用 accumulated 定稿），pop_reply_to=False 保留回复目标
            assert gw.sent == [("oc_im_cid_1", "", False, False)]
            # 确定性标志置位（resolve 前写入，watcher 读同源 asyncio.Future）
            assert getattr(future, "_im_finalized", False) is True
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_scheduler_reply_no_im_inherit_noop(mock_runner, mock_store, monkeypatch):
    """scheduler 通道无 IM 继承：维持原 no-op（回复只走 SSE）——不 send_sync、不置标志"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    mock_runner.get_im_channel.return_value = ""  # 无 IM 继承
    gw = _FakeIMGateway()
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)

    p1, p2, p3, p4, p5 = _scheduler_patches(mock_store)
    with p1, p2, p3 as mock_cm, p4, p5:
        _setup_context_manager(mock_cm)
        await q.start()
        try:
            reply, future = await q.enqueue_and_wait_with_future("[定时任务] 无继承", timeout=10.0)
            assert reply == "回复内容"
            assert gw.sent == []  # 无卡片可终结 → no-op
            assert getattr(future, "_im_finalized", False) is False
        finally:
            await q.stop()


@pytest.mark.asyncio
async def test_scheduler_merged_batch_all_futures_flagged(mock_runner, mock_store, monkeypatch):
    """同来源合并场景：两请求同窗口入队合并——遍历合并批次置位，supplement 的 future 也置位
    （只置 first_req 会让 supplement 的 watcher 自推 → 双投递）"""
    from niu_api.chat_queue import ChatQueue

    q = ChatQueue(mock_runner)
    mock_runner.get_im_channel.return_value = "oc_im_cid_1"
    gw = _FakeIMGateway()
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)

    p1, p2, p3, p4, p5 = _scheduler_patches(mock_store)
    with p1, p2, p3 as mock_cm, p4, p5:
        _setup_context_manager(mock_cm)
        q.pause()  # 暂停 worker，确保两请求同窗口入队合并
        await q.start()
        try:
            t1 = asyncio.create_task(q.enqueue_and_wait_with_future("[智能家居] 事件1", timeout=10.0))
            t2 = asyncio.create_task(q.enqueue_and_wait_with_future("[智能家居] 事件2", timeout=10.0))
            await asyncio.sleep(0.2)  # 等待两个请求都入队
            q.resume()
            (r1, f1), (r2, f2) = await asyncio.gather(t1, t2)
            assert r1 == r2 == "回复内容"  # 合并批次共享同一回复
            assert getattr(f1, "_im_finalized", False) is True
            assert getattr(f2, "_im_finalized", False) is True  # supplement 也置位
            assert len(gw.sent) == 1  # 合并批次只终结一次
        finally:
            await q.stop()


# ── ha_watcher 自推条件化（读 reply_future._im_finalized 标志）──

@pytest.fixture
def bg_loop():
    """后台线程真实事件循环——_push_to_chat 用 run_coroutine_threadsafe 桥接（同步线程函数）"""
    import threading
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


class _FakeRouter:
    def __init__(self):
        self.pushed = []  # (content, channel, channel_id)

    def has_channel(self, name):
        return name == "im"

    async def push(self, content, channel, channel_id):
        self.pushed.append((content, channel, channel_id))


def _setup_watcher_mocks(monkeypatch, bg_loop, reply, reply_future):
    """配置 _push_to_chat 的 loop/queue/router  mocks，返回 fake router"""
    from niu_api.internal.ha_watcher.watcher import _HAWatcher

    async def _fake_enqueue(content, source, session_id):
        return (reply, reply_future)

    fake_q = MagicMock()
    fake_q.enqueue_and_wait_with_future = _fake_enqueue
    router = _FakeRouter()
    monkeypatch.setattr("niu_api.chat._main_loop", bg_loop)
    monkeypatch.setattr("niu_api.chat_queue.get_chat_queue", lambda: fake_q)
    monkeypatch.setattr("niu_api.channel.get_channel_router", lambda: router)
    return _HAWatcher.__new__(_HAWatcher), router


@pytest.mark.asyncio
async def test_watcher_self_push_when_not_finalized(monkeypatch, bg_loop):
    """无卡（chat_queue 未终结，future 无 _im_finalized 标志）→ watcher 保持现状自推"""
    reply_future = asyncio.Future()  # 真实 future 对象，无标志
    w, router = _setup_watcher_mocks(monkeypatch, bg_loop, "Agent回复", reply_future)
    await asyncio.to_thread(w._push_to_chat, "灯开了")  # 同步线程函数——to_thread 避免阻塞测试 loop
    assert router.pushed == [("Agent回复", "im", "")]


@pytest.mark.asyncio
async def test_watcher_self_push_skipped_when_im_finalized(monkeypatch, bg_loop):
    """有卡（chat_queue 已 send_sync 终结，_im_finalized=True）→ watcher 不自推（防双投递）"""
    reply_future = asyncio.Future()  # 真实 future 对象挂标志
    reply_future._im_finalized = True
    w, router = _setup_watcher_mocks(monkeypatch, bg_loop, "Agent回复", reply_future)
    await asyncio.to_thread(w._push_to_chat, "灯开了")
    assert router.pushed == []
