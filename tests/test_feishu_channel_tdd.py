"""飞书通道异步架构修复 — TDD 测试

验证 _on_message sync handler + threading 架构的正确性。
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

    def __init__(self, content="hello", chat_id="chat_123", sender_id="user_1"):
        self.content_text = content
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_content_type = "text"
        self.resources = []
        self.raw = {}


class MockFeishuChannel:
    """模拟 lark-oapi SDK 的 FeishuChannel"""

    def __init__(self):
        self.sent_messages: list[tuple[str, dict]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler):
        self._handlers.setdefault(event, []).append(handler)

    async def send(self, chat_id: str, message: dict):
        self.sent_messages.append((chat_id, message))

    def connect_until_ready(self, timeout=30):
        pass

    async def disconnect(self):
        pass


class MockChatSync:
    """模拟 _chat_sync 的阻塞调用"""

    def __init__(self, reply="测试回复", delay=0.1):
        self.reply = reply
        self.delay = delay
        self.call_count = 0

    def __call__(self, content: str) -> str:
        self.call_count += 1
        time.sleep(self.delay)  # 模拟阻塞
        return self.reply


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

    def test_route_in_sync_calls_chat_sync(self):
        """route_in_sync 应该调用 _chat_sync 并返回结果"""
        router = ChannelRouter.__new__(ChannelRouter)
        mock_chat = MockChatSync(reply="你好")
        router._chat_sync = mock_chat

        msg = UnifiedMessage(
            content="在吗",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_1",
        )

        result = router.route_in_sync(msg)
        assert result == "你好"
        assert mock_chat.call_count == 1

    def test_route_in_sync_passes_content(self):
        """route_in_sync 应该传递 message.content 给 _chat_sync"""
        router = ChannelRouter.__new__(ChannelRouter)
        received_content = []

        def fake_chat_sync(content: str) -> str:
            received_content.append(content)
            return "ok"

        router._chat_sync = fake_chat_sync

        msg = UnifiedMessage(
            content="今天天气怎么样",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_1",
        )

        router.route_in_sync(msg)
        assert received_content == ["今天天气怎么样"]


# ============== Task 2: _on_message sync + threading ==============


class TestOnMessageSyncHandler:
    """验证 _on_message 是同步 handler + threading 架构"""

    def _make_adapter(self, chat_sync_reply="测试回复", chat_sync_delay=0.05):
        """创建测试用 FeishuChannelAdapter"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter

        mock_channel = MockFeishuChannel()
        mock_router = MagicMock()
        mock_router.route_in_sync = MockChatSync(
            reply=chat_sync_reply, delay=chat_sync_delay
        )

        adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
        adapter.channel = mock_channel
        adapter.router = mock_router
        adapter._user_p2p_chat_id = None

        return adapter, mock_channel, mock_router

    def test_on_message_is_sync_function(self):
        """_on_message 应该是普通同步函数，不是 async"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        import inspect

        assert not inspect.iscoroutinefunction(
            FeishuChannelAdapter._on_message
        ), "_on_message 不应该是 async 函数"

    def test_on_message_returns_immediately(self, bg_loop):
        """_on_message 应该立即返回，不阻塞"""
        adapter, mock_channel, mock_router = self._make_adapter(
            chat_sync_delay=2.0  # 模拟长时间阻塞
        )
        msg = MockFeishuMsg(content="在吗")

        start = time.time()
        bg_loop.invoke_sync_handler(adapter._on_message, msg)
        elapsed = time.time() - start

        # sync handler 应该在 0.5s 内返回（threading 启动开销）
        assert elapsed < 0.5, f"_on_message 耗时 {elapsed:.2f}s，应该立即返回"

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
        time.sleep(0.2)
        assert mock_router.route_in_sync.call_count == 0

    def test_on_message_processes_in_thread(self, bg_loop):
        """_on_message 应该在独立线程中处理消息"""
        adapter, mock_channel, mock_router = self._make_adapter()

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # 等待线程完成
        time.sleep(0.5)

        # route_in_sync 应该被调用
        assert mock_router.route_in_sync.call_count == 1

    def test_on_message_sends_reply_via_run_coroutine_threadsafe(self, bg_loop):
        """_on_message 应该通过 run_coroutine_threadsafe 发送回复"""
        adapter, mock_channel, mock_router = self._make_adapter(
            chat_sync_reply="这是回复"
        )

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)

        # 等待线程完成 + 协程执行
        time.sleep(0.5)

        # 验证 send 被调用
        assert len(mock_channel.sent_messages) == 1
        chat_id, message = mock_channel.sent_messages[0]
        assert chat_id == "chat_123"
        assert message == {"markdown": "这是回复"}

    def test_on_message_no_reply_sends_nothing(self, bg_loop):
        """如果 _chat_sync 返回空，不应该发送回复"""
        adapter, mock_channel, mock_router = self._make_adapter(
            chat_sync_reply=""  # 空回复
        )

        msg = MockFeishuMsg(content="你好")
        bg_loop.invoke_sync_handler(adapter._on_message, msg)
        time.sleep(0.5)

        assert len(mock_channel.sent_messages) == 0

    def test_on_message_thread_exception_does_not_crash(self, bg_loop):
        """工作线程中的异常不应该导致崩溃"""
        adapter, mock_channel, mock_router = self._make_adapter()
        mock_router.route_in_sync = Mock(side_effect=RuntimeError("模拟异常"))

        msg = MockFeishuMsg(content="你好")
        # 不应该抛出异常
        bg_loop.invoke_sync_handler(adapter._on_message, msg)
        time.sleep(0.5)

        # 不应该发送消息
        assert len(mock_channel.sent_messages) == 0


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
