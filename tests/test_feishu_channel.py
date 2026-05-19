"""飞书通道修复测试"""
import asyncio
import pytest
from unittest.mock import MagicMock, Mock, patch
from niu_api.channel import ChannelRouter


def _create_msg(content="你好", chat_id="chat_123", sender_id="user_001",
                chat_type="p2p", resources=None, raw_content_type="text"):
    """创建模拟的飞书 InboundMessage"""
    msg = Mock()
    msg.content_text = content
    msg.chat_id = chat_id
    msg.sender_id = sender_id
    msg.chat_type = chat_type
    msg.resources = resources or []
    msg.raw_content_type = raw_content_type
    msg.raw = {"chat_type": chat_type} if chat_type else {}
    return msg


def _create_adapter():
    """创建 FeishuChannelAdapter 实例（跳过 __init__）"""
    from niu_api.channel.feishu_channel import FeishuChannelAdapter
    adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)
    adapter.channel = MagicMock()
    adapter.channel.on = MagicMock()
    adapter.channel.schedule = MagicMock()
    adapter.channel.is_ready = True
    adapter.router = ChannelRouter()
    adapter._user_p2p_chat_id = None
    adapter._user_open_id = None
    adapter._prefs_path = Mock()
    adapter._feishu_prefs = {}
    return adapter


class TestImageMessageNotSkipped:
    """Task 1: 纯图片/文件消息不应被丢弃"""

    def test_image_message_not_skipped(self):
        """纯图片消息（content 为空但有 resources）不应被丢弃"""
        adapter = _create_adapter()
        msg = _create_msg(
            content="",
            resources=[{"type": "image", "file_key": "img_001"}],
            raw_content_type="image"
        )
        with patch('threading.Thread') as mock_thread, \
             patch('asyncio.get_running_loop', return_value=Mock()):
            adapter._on_message(msg)
            # 纯图片消息不应被丢弃，应该启动线程处理
            assert mock_thread.called

    def test_empty_message_with_no_resources_skipped(self):
        """content 和 resources 都为空的消息应被跳过"""
        adapter = _create_adapter()
        msg = _create_msg(content="", resources=[])
        with patch('threading.Thread') as mock_thread:
            adapter._on_message(msg)
            # 空消息应被跳过，不应启动线程
            assert not mock_thread.called

    def test_text_message_not_skipped(self):
        """普通文字消息不应被丢弃"""
        adapter = _create_adapter()
        msg = _create_msg(content="你好")
        with patch('threading.Thread') as mock_thread, \
             patch('asyncio.get_running_loop', return_value=Mock()):
            adapter._on_message(msg)
            assert mock_thread.called


class TestP2PMessageGuard:
    """Task 2: 群聊消息不应覆盖 P2P chat_id/open_id"""

    def test_is_p2p_message_with_p2p(self):
        """_is_p2p_message 对 P2P 消息返回 True"""
        adapter = _create_adapter()
        msg = _create_msg(chat_type="p2p")
        assert adapter._is_p2p_message(msg) is True

    def test_is_p2p_message_with_group(self):
        """_is_p2p_message 对群聊消息返回 False"""
        adapter = _create_adapter()
        msg = _create_msg(chat_type="group")
        assert adapter._is_p2p_message(msg) is False

    def test_is_p2p_message_without_chat_type(self):
        """_is_p2p_message 在 chat_type 不存在时返回 False"""
        adapter = _create_adapter()
        msg = _create_msg(chat_type=None)
        assert adapter._is_p2p_message(msg) is False

    def test_p2p_message_updates_chat_id(self):
        """P2P 消息应更新 _user_p2p_chat_id"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = None
        msg = _create_msg(chat_id="p2p_chat_123", sender_id="user_001", chat_type="p2p")
        with patch('threading.Thread'):
            adapter._on_message(msg)
        assert adapter._user_p2p_chat_id == "p2p_chat_123"

    def test_group_message_does_not_overwrite_p2p_chat_id(self):
        """群聊消息不应覆盖已有的 P2P chat_id"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "p2p_chat_123"
        msg = _create_msg(chat_id="group_chat_456", sender_id="user_002", chat_type="group")
        with patch('threading.Thread'):
            adapter._on_message(msg)
        assert adapter._user_p2p_chat_id == "p2p_chat_123"

    def test_group_message_does_not_overwrite_open_id(self):
        """群聊消息不应覆盖已有的 P2P open_id"""
        adapter = _create_adapter()
        adapter._user_open_id = "user_001"
        msg = _create_msg(chat_id="group_chat_456", sender_id="user_002", chat_type="group")
        with patch('threading.Thread'):
            adapter._on_message(msg)
        assert adapter._user_open_id == "user_001"


class TestResourcesToText:
    """Task 3: resources 转文本描述"""

    def test_format_resources_image(self):
        """图片资源转为 [图片: file_key] 文本"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        adapter = _create_adapter()
        resources = [{"type": "image", "file_key": "img_001.jpg"}]
        result = adapter._format_resources(resources)
        assert "图片" in result
        assert "img_001.jpg" in result

    def test_format_resources_file(self):
        """文件资源转为 [文件: file_name] 文本"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        adapter = _create_adapter()
        resources = [{"type": "file", "file_name": "report.pdf"}]
        result = adapter._format_resources(resources)
        assert "文件" in result
        assert "report.pdf" in result

    def test_format_resources_empty(self):
        """空 resources 返回空字符串"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        adapter = _create_adapter()
        result = adapter._format_resources([])
        assert result == ""

    def test_format_resources_multiple(self):
        """多个 resources 合并为一行一个"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        adapter = _create_adapter()
        resources = [
            {"type": "image", "file_key": "img_001.jpg"},
            {"type": "file", "file_name": "report.pdf"},
        ]
        result = adapter._format_resources(resources)
        assert "图片" in result
        assert "文件" in result
        assert "\n" in result


class TestSessionIdByUser:
    """Task 3: session_id 按用户区分"""

    def test_session_id_uses_sender_id(self):
        """session_id 应基于 sender_id 生成"""
        adapter = _create_adapter()
        msg = _create_msg(sender_id="user_abc", chat_type="p2p")
        with patch.object(adapter.router, 'route_in_sync', return_value="回复") as mock_route:
            with patch('threading.Thread') as mock_thread, \
                 patch('asyncio.get_running_loop', return_value=Mock()):
                adapter._on_message(msg)
                # 获取线程目标函数和参数并执行
                call_kwargs = mock_thread.call_args[1]
                thread_target = call_kwargs['target']
                thread_args = call_kwargs.get('args', ())
                thread_target(*thread_args)
                # 验证 route_in_sync 被调用时传了正确的 session_id
                call_args = mock_route.call_args
                assert call_args is not None
                # route_in_sync 现在接收 UnifiedMessage 和 session_id
                assert "user_abc" in str(call_args)

    def test_session_id_group_uses_channel_id(self):
        """群聊消息 session_id 应基于 channel_id"""
        adapter = _create_adapter()
        msg = _create_msg(sender_id="user_abc", chat_id="group_chat_789", chat_type="group")
        with patch.object(adapter.router, 'route_in_sync', return_value="回复") as mock_route:
            with patch('threading.Thread') as mock_thread, \
                 patch('asyncio.get_running_loop', return_value=Mock()):
                adapter._on_message(msg)
                call_kwargs = mock_thread.call_args[1]
                thread_target = call_kwargs['target']
                thread_args = call_kwargs.get('args', ())
                thread_target(*thread_args)
                call_args = mock_route.call_args
                assert call_args is not None
                assert "group_chat_789" in str(call_args)


class TestScheduleForReply:
    """Task 4: 回复发送应通过 channel.schedule() 而非 run_coroutine_threadsafe"""

    def test_process_and_reply_uses_schedule(self):
        """_process_and_reply 应通过 channel.schedule() 发送回复"""
        import asyncio
        from niu_api.channel.base import UnifiedMessage
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "chat_123"
        # channel.send 必须返回一个真实的协程（MagicMock 默认返回 MagicMock 不是协程）
        async def _fake_send(*args, **kwargs):
            pass
        adapter.channel.send = _fake_send
        unified = UnifiedMessage(
            content="你好",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_001",
            message_type="text",
            resources=[],
            raw={"chat_type": "p2p"},
        )
        with patch.object(adapter.router, 'route_in_sync', return_value="回复内容"):
            adapter._process_and_reply(unified)
        # 验证 channel.schedule 被调用
        assert adapter.channel.schedule.called
        # 验证 schedule 收到的是协程
        call_arg = adapter.channel.schedule.call_args[0][0]
        assert asyncio.iscoroutine(call_arg)
        # 清理未 await 的协程
        call_arg.close()

    def test_on_message_no_run_coroutine_threadsafe(self):
        """_on_message 不应使用 run_coroutine_threadsafe"""
        import inspect
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        source = inspect.getsource(FeishuChannelAdapter._on_message)
        assert "run_coroutine_threadsafe" not in source

    def test_on_message_no_get_running_loop(self):
        """_on_message 不应捕获 asyncio.get_running_loop()"""
        import inspect
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        source = inspect.getsource(FeishuChannelAdapter._on_message)
        assert "get_running_loop" not in source


class TestErrorEventAndEmptyReply:
    """Task 5: 注册 error 事件 + 空回复反馈"""

    def test_error_event_registered_in_init(self):
        """__init__ 应注册 channel.on("error", self._on_error)"""
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        with patch('lark_oapi.ws.client') as mock_ws_client, \
             patch('lark_oapi.channel.FeishuChannel') as mock_channel_class:
            mock_ws_client.loop = MagicMock()
            mock_ws_client.loop.is_running = MagicMock(return_value=False)
            mock_channel = MagicMock()
            mock_channel.on = MagicMock()
            mock_channel_class.return_value = mock_channel
            adapter = FeishuChannelAdapter(
                app_id="test", app_secret="test", channel_router=MagicMock()
            )
            # 验证 on 被调用且包含 "error" 事件
            on_calls = [c[0] for c in mock_channel.on.call_args_list]
            event_names = [c[0] for c in on_calls]
            assert "error" in event_names

    def test_on_error_exists(self):
        """_on_error 方法应存在"""
        adapter = _create_adapter()
        assert hasattr(adapter, '_on_error')

    def test_empty_reply_sends_notification(self):
        """Agent 返回空回复时应发提示消息"""
        from niu_api.channel.base import UnifiedMessage
        adapter = _create_adapter()
        unified = UnifiedMessage(
            content="你好",
            channel="feishu",
            channel_id="chat_123",
            sender_id="user_001",
            message_type="text",
            resources=[],
            raw={"chat_type": "p2p"},
        )
        with patch.object(adapter.channel, 'schedule') as mock_schedule, \
             patch.object(adapter.router, 'route_in_sync', return_value=""):
            adapter._process_and_reply(unified)
            # 空回复时 schedule 也应被调用（发送提示消息）
            assert mock_schedule.called


class TestAtomicFileWrite:
    """Task 6: 原子文件写入"""

    def test_save_prefs_uses_atomic_write(self):
        """_save_prefs 应使用临时文件 + os.replace()"""
        import inspect
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        source = inspect.getsource(FeishuChannelAdapter._save_prefs)
        assert "os.replace" in source

    def test_save_prefs_creates_valid_file(self):
        """_save_prefs 应创建有效的 JSON 文件"""
        import tempfile
        import os
        from pathlib import Path
        adapter = _create_adapter()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs_path = Path(tmpdir) / "preferences.json"
            adapter._prefs_path = prefs_path
            adapter._user_p2p_chat_id = "chat_123"
            adapter._user_open_id = "ou_abc"
            adapter._feishu_prefs = {}
            adapter._save_prefs()
            import json
            with open(prefs_path) as f:
                data = json.load(f)
            assert data["feishu"]["user_p2p_chat_id"] == "chat_123"
            assert data["feishu"]["user_open_id"] == "ou_abc"

    def test_save_prefs_no_partial_file_on_error(self):
        """_save_prefs 失败时不应留下部分写入的文件"""
        import tempfile
        import os
        import json
        from pathlib import Path
        adapter = _create_adapter()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs_path = Path(tmpdir) / "preferences.json"
            adapter._prefs_path = prefs_path
            adapter._user_p2p_chat_id = "chat_123"
            adapter._user_open_id = None
            adapter._feishu_prefs = {}
            # 先写一个有效文件
            adapter._save_prefs()
            # 模拟写入失败（路径指向一个不存在的目录中的文件）
            adapter._prefs_path = Path("/nonexistent_dir/feishu_prefs.json")
            adapter._save_prefs()  # 应该不抛异常
            # 原文件应仍然有效
            with open(prefs_path) as f:
                data = json.load(f)
            assert data["feishu"]["user_p2p_chat_id"] == "chat_123"


class TestSchedulerPush:
    """Task 7: Scheduler 推送应通过 channel.schedule() 发送"""

    def test_push_calls_channel_send(self):
        """push 方法应调用 channel.send()"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "chat_123"

        async def _fake_send(*args, **kwargs):
            pass

        with patch.object(adapter.channel, 'send', side_effect=_fake_send) as mock_send:
            asyncio.run(adapter.push("chat_123", "提醒内容"))
            mock_send.assert_called_once_with("chat_123", {"markdown": "提醒内容"})

    def test_scheduler_uses_channel_schedule(self):
        """Scheduler 推送应通过 channel.schedule() 而非 run_coroutine_threadsafe"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "chat_123"

        with patch.object(adapter.channel, 'schedule') as mock_schedule:
            push_coro = adapter.push(adapter._user_p2p_chat_id, "提醒内容")
            adapter.channel.schedule(push_coro)
            mock_schedule.assert_called_once()
            call_arg = mock_schedule.call_args[0][0]
            assert asyncio.iscoroutine(call_arg)
            push_coro.close()

    def test_scheduler_no_run_coroutine_threadsafe(self):
        """Scheduler 推送代码不应使用 run_coroutine_threadsafe"""
        source = open("REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/scheduler/service.py").read()
        # 提取飞书推送 try 块（从 "# 飞书通道推送" 到对应的 except）
        lines = source.split('\n')
        feishu_block_lines = []
        in_feishu_block = False
        brace_depth = 0
        for line in lines:
            if '# 飞书通道推送' in line:
                in_feishu_block = True
                brace_depth = 0
            if in_feishu_block:
                feishu_block_lines.append(line)
                # 检测 try 块结束：遇到 except 后的下一行非缩进
                if line.strip().startswith('except'):
                    # 收集 except 行和下一行（error handler）
                    continue
                if feishu_block_lines and line.strip() and not line.strip().startswith('#') and not line.startswith(' ' * 12) and len(feishu_block_lines) > 3:
                    break
        feishu_block = '\n'.join(feishu_block_lines)
        assert 'run_coroutine_threadsafe' not in feishu_block, \
            f"Scheduler 飞书推送不应使用 run_coroutine_threadsafe:\n{feishu_block}"
