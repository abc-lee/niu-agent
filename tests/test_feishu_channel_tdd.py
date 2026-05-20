"""飞书通道异步架构修复 — TDD 测试

验证 _on_message 直接入队 ChatQueue 架构的正确性。
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch, Mock

import pytest

from niu_api.channel import ChannelRouter, UnifiedMessage


# ============== Helpers ==============


class MockFeishuMsg:
    """模拟 lark-oapi SDK 的 InboundMessage"""

    def __init__(self, content="hello", chat_id="chat_123", sender_id="user_1", chat_type="p2p"):
        self.content_text = content
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.chat_type = chat_type
        self.raw_content_type = "text"
        self.resources = []
        self.raw = {}


class MockFeishuChannel:
    """模拟 lark-oapi SDK 的 FeishuChannel"""

    def __init__(self):
        self.sent_messages: list[tuple[str, dict]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handlers: dict[str, list] = {}
        self.is_ready = True

    def on(self, event: str, handler):
        self._handlers.setdefault(event, []).append(handler)

    async def send(self, chat_id: str, message: dict):
        self.sent_messages.append((chat_id, message))

    def schedule(self, coro):
        """模拟 channel.schedule() — 在 bg loop 中执行协程"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            # 没有 bg loop，直接记录（简单场景）
            try:
                asyncio.get_event_loop().run_until_complete(coro)
            except RuntimeError:
                pass

    def connect_until_ready(self, timeout=30):
        pass

    async def disconnect(self):
        pass


class MockChatSync:
    """模拟 route_in_sync 的调用 — 返回 EnqueueResult"""

    def __init__(self, reply="测试回复", delay=0.1):
        self.reply = reply
        self.delay = delay
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        from niu_api.chat_queue import EnqueueResult
        self.call_count += 1
        time.sleep(self.delay)  # 模拟阻塞
        # 返回 EnqueueResult，queued=True 表示入队成功
        return EnqueueResult(queued=bool(self.reply), request_id=str(self.call_count))


class BgLoopFixture:
    """模拟 SDK 后台事件循环"""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        loop = asyncio.new_event_loop()
        self.loop = loop

        def _runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self._thread = threading.Thread(target=_runner, name="mock-sdk-bg", daemon=True)
        self._thread.start()
        while not loop.is_running():
            time.sleep(0.01)

    def stop(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def invoke_sync_handler(self, handler, *args, timeout=5):
        """模拟 SDK _invoke：在 bg loop 中调用 sync handler"""
        assert self.loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._invoke_coro(handler, *args), self.loop
        )
        return future.result(timeout=timeout)

    async def _invoke_coro(self, handler, *args):
        """模拟 SDK _invoke 的行为：先同步调用，再检查 awaitable"""
        result = handler(*args)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result


@pytest.fixture
def bg_loop():
    fixture = BgLoopFixture()
    fixture.start()
    yield fixture
    fixture.stop()


# ============== Task 1: route_in_sync ==============


class TestRouteInSync:
    """验证 ChannelRouter.route_in_sync 同步路由方法"""

    def test_route_in_sync_exists(self):
        """route_in_sync 方法应该存在"""
        router = ChannelRouter.__new__(ChannelRouter)
        assert hasattr(router, "route_in_sync"), "ChannelRouter 缺少 route_in_sync 方法"

    @patch("niu_api.chat_queue.get_chat_queue")
    def test_route_in_sync_calls_enqueue_sync(self, mock_get_q):
        """route_in_sync 应该调用 ChatQueue.enqueue_sync"""
        from niu_api.chat_queue import EnqueueResult
        mock_q = MagicMock()
        mock_q.enqueue_sync.return_value = EnqueueResult(queued=True, request_id="1")
        mock_get_q.return_value = mock_q

        router = ChannelRouter()
        msg = UnifiedMessage(
            content="在吗",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_1",
            message_type="text",
        )

        result = router.route_in_sync(msg)
        assert isinstance(result, EnqueueResult)
        assert result.queued is True
        mock_q.enqueue_sync.assert_called_once()

    @patch("niu_api.chat_queue.get_chat_queue")
    def test_route_in_sync_passes_content(self, mock_get_q):
        """route_in_sync 应该传递 message.content 给 enqueue_sync"""
        from niu_api.chat_queue import EnqueueResult
        mock_q = MagicMock()
        mock_q.enqueue_sync.return_value = EnqueueResult(queued=True, request_id="1")
        mock_get_q.return_value = mock_q

        router = ChannelRouter()
        msg = UnifiedMessage(
            content="今天天气怎么样",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_1",
            message_type="text",
        )

        router.route_in_sync(msg)
        call_kwargs = mock_q.enqueue_sync.call_args.kwargs
        assert call_kwargs["content"] == "今天天气怎么样"


# ============== Task 2: _on_message sync + threading ==============


class TestOnMessageSyncHandler:
    """验证 _on_message 是同步 handler + threading 架构"""

    def _make_adapter(self, enqueue_success=True, chat_sync_delay=0.05, bg_loop=None):
        """创建测试用 FeishuChannelAdapter"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        from niu_api.chat_queue import EnqueueResult

        mock_channel = MockFeishuChannel()
        if bg_loop is not None:
            mock_channel._loop = bg_loop.loop
        mock_router = MagicMock()
        mock_router.route_in_sync = MockChatSync(
            reply="测试回复" if enqueue_success else "",
            delay=chat_sync_delay,
        )

        adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
        adapter.channel = mock_channel
        adapter.router = mock_router
        adapter._user_p2p_chat_id = None
        adapter._user_open_id = None
        adapter._feishu_prefs = {}
        adapter._prefs_lock = threading.Lock()

        return adapter, mock_channel, mock_router

    def test_on_message_is_sync_function(self):
        """_on_message 应该是普通同步函数，不是 async"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        import inspect

        assert not inspect.iscoroutinefunction(
            FeishuChannelAdapter._on_message
        ), "_on_message 不应该是 async 函数"

    def test_on_message_calls_route_in_sync_directly(self, bg_loop):
        """_on_message 应直接调用 route_in_sync 入队（不再启动线程）"""
        adapter, mock_channel, mock_router = self._make_adapter()
        msg = MockFeishuMsg(content="在吗")

        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # route_in_sync 应该被直接调用（不经过线程）
        assert mock_router.route_in_sync.call_count == 1

    def test_on_message_captures_p2p_chat_id(self, bg_loop):
        """_on_message 应该记录 P2P chat_id"""
        adapter, _, _ = self._make_adapter()
        adapter._user_p2p_chat_id = None

        msg = MockFeishuMsg(chat_id="chat_p2p_456")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        assert adapter._user_p2p_chat_id == "chat_p2p_456"

    def test_on_message_skips_empty_content(self, bg_loop):
        """_on_message 应该跳过空消息"""
        adapter, mock_channel, mock_router = self._make_adapter()

        msg = MockFeishuMsg(content="   ")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # 空消息不应该触发 route_in_sync
        assert mock_router.route_in_sync.call_count == 0

    def test_on_message_processes_directly(self, bg_loop):
        """_on_message 应直接处理消息（不再启动线程）"""
        adapter, mock_channel, mock_router = self._make_adapter()

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # route_in_sync 应该已经被调用（同步，无需等待线程）
        assert mock_router.route_in_sync.call_count == 1

    def test_on_message_enqueue_success(self, bg_loop):
        """route_in_sync 入队成功后，ChatQueue Worker 负责推送回复"""
        adapter, mock_channel, mock_router = self._make_adapter(enqueue_success=True)

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # route_in_sync 应该被调用（入队成功）
        assert mock_router.route_in_sync.call_count == 1

    def test_on_message_enqueue_failure_sends_notification(self, bg_loop):
        """如果入队失败，应该发送失败通知"""
        adapter, mock_channel, mock_router = self._make_adapter(enqueue_success=False)
        # 设置 mock channel 的 _loop 以支持 schedule()
        mock_channel._loop = bg_loop.loop

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)
        time.sleep(0.5)

        # 入队失败时应该发送失败通知
        assert len(mock_channel.sent_messages) == 1

    def test_on_message_exception_does_not_crash(self, bg_loop):
        """_on_message 内部异常不应导致崩溃"""
        adapter, mock_channel, mock_router = self._make_adapter()
        mock_router.route_in_sync = Mock(side_effect=RuntimeError("模拟异常"))

        msg = MockFeishuMsg(content="你好")
        # 不应该抛出异常
        bg_loop.invoke_sync_handler(adapter._on_message, msg)


# ============== Task 3: ws_client.loop monkey-patch ==============


class TestWsClientLoopPatch:
    """验证 __init__ 中修补 ws_client.loop 的逻辑"""

    def test_init_patches_running_loop(self):
        """当 ws_client.loop 正在运行时，__init__ 应该替换为新 loop"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter

        mock_router = MagicMock()

        with patch("lark_oapi.ws.client") as mock_ws_client:
            # 模拟 uvicorn 场景：loop 正在运行
            running_loop = MagicMock()
            running_loop.is_running.return_value = True
            mock_ws_client.loop = running_loop

            with patch("lark_oapi.channel.FeishuChannel") as MockChannel:
                MockChannel.return_value = MagicMock()

                adapter = FeishuChannelAdapter(
                    app_id="test_id", app_secret="test_secret", channel_router=mock_router
                )

                # loop 应该被替换为新的未运行的 loop
                assert mock_ws_client.loop is not running_loop
                assert not mock_ws_client.loop.is_running()
                assert isinstance(mock_ws_client.loop, asyncio.AbstractEventLoop)

    def test_init_keeps_non_running_loop(self):
        """当 ws_client.loop 未运行时，__init__ 不应该替换"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter

        mock_router = MagicMock()

        with patch("lark_oapi.ws.client") as mock_ws_client:
            # 模拟正常场景：loop 未运行
            idle_loop = MagicMock()
            idle_loop.is_running.return_value = False
            mock_ws_client.loop = idle_loop

            with patch("lark_oapi.channel.FeishuChannel") as MockChannel:
                MockChannel.return_value = MagicMock()

                adapter = FeishuChannelAdapter(
                    app_id="test_id", app_secret="test_secret", channel_router=mock_router
                )

                # loop 不应该被替换
                assert mock_ws_client.loop is idle_loop


# ============== Task 4: _on_reconnecting / _on_reconnected signature ==============


class TestReconnectHandlerSignature:
    """验证 _on_reconnecting / _on_reconnected 接受可选参数"""

    def _make_adapter(self):
        """创建测试用 FeishuChannelAdapter"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter

        adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
        adapter.channel = MockFeishuChannel()
        adapter.router = MagicMock()
        adapter._user_p2p_chat_id = None
        adapter._user_open_id = None
        adapter._feishu_prefs = {}
        return adapter

    def test_on_reconnecting_accepts_no_args(self):
        """_on_reconnecting 应该可以无参数调用"""
        adapter = self._make_adapter()
        # 不应该抛出 TypeError
        adapter._on_reconnecting()

    def test_on_reconnecting_accepts_one_arg(self):
        """_on_reconnecting 应该可以接受一个参数（SDK 传的）"""
        adapter = self._make_adapter()
        # 不应该抛出 TypeError: missing 1 required positional argument
        adapter._on_reconnecting(None)
        adapter._on_reconnecting("some_arg")

    def test_on_reconnected_accepts_no_args(self):
        """_on_reconnected 应该可以无参数调用"""
        adapter = self._make_adapter()
        adapter._on_reconnected()

    def test_on_reconnected_accepts_one_arg(self):
        """_on_reconnected 应该可以接受一个参数（SDK 传的）"""
        adapter = self._make_adapter()
        adapter._on_reconnected(None)
        adapter._on_reconnected("some_arg")


# ============== Task 5: chat_id / open_id 持久化 ==============


class TestChatIdPersistence:
    """验证 chat_id 和 open_id 持久化到 preferences.json"""

    def _make_adapter_with_prefs(self, feishu_prefs=None):
        """创建测试用 FeishuChannelAdapter，注入 _load_prefs / _save_prefs"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter

        adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
        adapter.channel = MockFeishuChannel()
        adapter.router = MagicMock()
        adapter._user_p2p_chat_id = None
        adapter._user_open_id = None
        adapter._prefs_path = None  # 测试中不写真实文件
        # 模拟加载的偏好
        adapter._feishu_prefs = feishu_prefs or {}
        return adapter

    def test_init_loads_persisted_chat_id(self):
        """__init__ 应该从 preferences.json 加载持久化的 chat_id"""
        adapter = self._make_adapter_with_prefs(
            feishu_prefs={"user_p2p_chat_id": "oc_persisted_123"}
        )
        # 模拟 _load_prefs 在 __init__ 中的效果
        adapter._apply_persisted_ids()
        assert adapter._user_p2p_chat_id == "oc_persisted_123"

    def test_init_loads_persisted_open_id(self):
        """__init__ 应该从 preferences.json 加载持久化的 open_id"""
        adapter = self._make_adapter_with_prefs(
            feishu_prefs={"user_open_id": "ou_persisted_456"}
        )
        adapter._apply_persisted_ids()
        assert adapter._user_open_id == "ou_persisted_456"

    def test_on_message_updates_persisted_ids(self):
        """收到消息时应该更新并持久化 chat_id 和 open_id"""
        adapter = self._make_adapter_with_prefs()
        adapter._feishu_prefs = {}
        adapter._save_prefs = MagicMock()  # mock 保存方法

        msg = MockFeishuMsg(
            content="你好", chat_id="oc_new_chat_789", sender_id="ou_new_user_012"
        )

        # 模拟 _on_message 中的持久化逻辑
        adapter._update_persisted_ids(msg.chat_id, msg.sender_id)

        assert adapter._user_p2p_chat_id == "oc_new_chat_789"
        assert adapter._user_open_id == "ou_new_user_012"
        adapter._save_prefs.assert_called_once()

    def test_on_message_does_not_save_if_unchanged(self):
        """如果 chat_id 和 open_id 没变，不应该重复保存"""
        adapter = self._make_adapter_with_prefs(
            feishu_prefs={
                "user_p2p_chat_id": "oc_same_chat",
                "user_open_id": "ou_same_user",
            }
        )
        adapter._user_p2p_chat_id = "oc_same_chat"
        adapter._user_open_id = "ou_same_user"
        adapter._save_prefs = MagicMock()

        adapter._update_persisted_ids("oc_same_chat", "ou_same_user")

        adapter._save_prefs.assert_not_called()

    def test_push_uses_persisted_chat_id_without_message(self):
        """启动后无需先发消息，push 就能使用持久化的 chat_id"""
        adapter = self._make_adapter_with_prefs(
            feishu_prefs={"user_p2p_chat_id": "oc_persisted_123"}
        )
        adapter._apply_persisted_ids()

        # push 应该能直接用持久化的 chat_id
        assert adapter.user_p2p_chat_id == "oc_persisted_123"

    def test_apply_persisted_ids_no_overwrite_existing(self):
        """如果内存中已有 chat_id，_apply_persisted_ids 不应该覆盖"""
        adapter = self._make_adapter_with_prefs(
            feishu_prefs={"user_p2p_chat_id": "oc_old"}
        )
        adapter._user_p2p_chat_id = "oc_current"

        adapter._apply_persisted_ids()

        # 内存中的值优先
        assert adapter._user_p2p_chat_id == "oc_current"
