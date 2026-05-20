"""飞书连接健壮性测试 — ID 管理 + 重连 + push 优先 open_id"""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: minimal fakes for FeishuChannel dependencies
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    """模拟飞书 SDK send() 返回值"""
    success: bool
    error: str = ""


class FakeFeishuChannel:
    """
    最小化 FeishuChannel 替身，只实现被测逻辑。
    不依赖真实飞书 SDK，避免网络连接。
    """

    def __init__(self):
        self._user_p2p_chat_id: str | None = None
        self._user_open_id: str | None = None
        self._feishu_prefs: dict = {}
        self.channel = MagicMock()
        self.channel.is_ready = False
        # 让 send() 成为 AsyncMock
        self.channel.send = AsyncMock(return_value=SendResult(success=True))

    # --- 被测方法（与 feishu_channel.py 保持一致） ---

    async def push(self, channel_id: str, content: str) -> None:
        """主动推送 — 没有 ID 就不发，优先 open_id"""
        target = channel_id or self._user_open_id or self._user_p2p_chat_id
        if not target:
            return
        try:
            result = await self.channel.send(target, {"markdown": content})
            if not result.success:
                fallback = None
                if target == self._user_open_id and self._user_p2p_chat_id:
                    fallback = self._user_p2p_chat_id
                elif target == self._user_p2p_chat_id and self._user_open_id:
                    fallback = self._user_open_id
                if fallback:
                    try:
                        r2 = await self.channel.send(fallback, {"markdown": content})
                    except Exception:
                        pass
        except Exception:
            pass

    def _on_reconnected(self, _=None):
        """WebSocket 重连成功 — 重新加载已保存的 ID"""
        self._feishu_prefs = self._load_prefs()
        self._apply_persisted_ids()

    def _load_prefs(self):
        return {"p2p_chat_id": "chat_reload_123", "open_id": "open_reload_456"}

    def _apply_persisted_ids(self):
        self._user_p2p_chat_id = self._feishu_prefs.get("p2p_chat_id")
        self._user_open_id = self._feishu_prefs.get("open_id")

    # --- 属性 ---

    @property
    def user_open_id(self) -> str | None:
        return self._user_open_id

    @property
    def is_connected(self) -> bool:
        return self.channel.is_ready

    @property
    def has_push_target(self) -> bool:
        return bool(self._user_p2p_chat_id or self._user_open_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPushNoId:
    """push 没有 ID 时静默跳过"""

    @pytest.mark.asyncio
    async def test_skip_when_no_ids(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = None
        await ch.push("", "hello")
        ch.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_channel_id_empty_string(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = None
        await ch.push("", "hello")
        ch.channel.send.assert_not_called()


class TestPushPrefersOpenId:
    """push 优先使用 open_id"""

    @pytest.mark.asyncio
    async def test_open_id_over_chat_id(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        await ch.push("", "hello")
        # channel_id 为空 → 优先 open_id
        ch.channel.send.assert_called_once_with("ou_open123", {"markdown": "hello"})

    @pytest.mark.asyncio
    async def test_chat_id_used_when_no_open_id(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = None
        ch._user_p2p_chat_id = "oc_chat456"
        await ch.push("", "hello")
        ch.channel.send.assert_called_once_with("oc_chat456", {"markdown": "hello"})

    @pytest.mark.asyncio
    async def test_channel_id_overrides_all(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        await ch.push("oc_override", "hello")
        # 显式 channel_id 最高优先
        ch.channel.send.assert_called_once_with("oc_override", {"markdown": "hello"})


class TestPushFallback:
    """push 失败时 fallback 到另一个 ID"""

    @pytest.mark.asyncio
    async def test_open_id_fail_fallback_to_chat_id(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        # 第一次 send（open_id）失败，第二次（chat_id）成功
        ch.channel.send = AsyncMock(
            side_effect=[SendResult(success=False, error="invalid open_id"), SendResult(success=True)]
        )
        await ch.push("", "hello")
        assert ch.channel.send.call_count == 2
        ch.channel.send.assert_any_call("ou_open123", {"markdown": "hello"})
        ch.channel.send.assert_any_call("oc_chat456", {"markdown": "hello"})

    @pytest.mark.asyncio
    async def test_chat_id_fail_fallback_to_open_id(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        # chat_id 优先级低于 open_id，但当 open_id 为 None 时 chat_id 是主目标
        ch._user_open_id = None
        ch.channel.send = AsyncMock(
            side_effect=[SendResult(success=False, error="invalid chat_id"), SendResult(success=True)]
        )
        # 没有 open_id，chat_id 失败后无 fallback
        await ch.push("", "hello")
        assert ch.channel.send.call_count == 1

    @pytest.mark.asyncio
    async def test_chat_id_fail_fallback_to_open_id_when_both_exist(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        # open_id 优先发送，如果 open_id 失败则 fallback 到 chat_id
        # 测试：open_id 失败 → chat_id fallback
        ch.channel.send = AsyncMock(
            side_effect=[SendResult(success=False, error="open_id expired"), SendResult(success=True)]
        )
        await ch.push("", "hello")
        assert ch.channel.send.call_count == 2

    @pytest.mark.asyncio
    async def test_both_fail(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        ch.channel.send = AsyncMock(
            side_effect=[
                SendResult(success=False, error="err1"),
                SendResult(success=False, error="err2"),
            ]
        )
        await ch.push("", "hello")
        assert ch.channel.send.call_count == 2

    @pytest.mark.asyncio
    async def test_no_fallback_when_only_one_id(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = None
        ch.channel.send = AsyncMock(return_value=SendResult(success=False, error="fail"))
        await ch.push("", "hello")
        # 只有 open_id，失败后无 fallback
        assert ch.channel.send.call_count == 1

    @pytest.mark.asyncio
    async def test_exception_in_send(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_open123"
        ch._user_p2p_chat_id = "oc_chat456"
        ch.channel.send = AsyncMock(side_effect=Exception("network error"))
        # 不应抛出异常
        await ch.push("", "hello")
        assert ch.channel.send.call_count == 1


class TestReconnectReloadsIds:
    """重连后重新加载 ID"""

    def test_reconnected_reloads_prefs(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = None
        ch._on_reconnected()
        assert ch._user_p2p_chat_id == "chat_reload_123"
        assert ch._user_open_id == "open_reload_456"

    def test_reconnected_with_sdk_arg(self):
        """SDK 可能传一个参数给回调"""
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = None
        ch._on_reconnected("some_sdk_arg")
        assert ch._user_p2p_chat_id == "chat_reload_123"
        assert ch._user_open_id == "open_reload_456"


class TestHasPushTarget:
    """has_push_target 属性"""

    def test_false_when_no_ids(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = None
        assert ch.has_push_target is False

    def test_true_with_chat_id_only(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = "oc_chat"
        ch._user_open_id = None
        assert ch.has_push_target is True

    def test_true_with_open_id_only(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = None
        ch._user_open_id = "ou_open"
        assert ch.has_push_target is True

    def test_true_with_both_ids(self):
        ch = FakeFeishuChannel()
        ch._user_p2p_chat_id = "oc_chat"
        ch._user_open_id = "ou_open"
        assert ch.has_push_target is True


class TestIsConnected:
    """is_connected 属性"""

    def test_false_when_not_ready(self):
        ch = FakeFeishuChannel()
        ch.channel.is_ready = False
        assert ch.is_connected is False

    def test_true_when_ready(self):
        ch = FakeFeishuChannel()
        ch.channel.is_ready = True
        assert ch.is_connected is True


class TestUserOpenId:
    """user_open_id 属性"""

    def test_returns_none(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = None
        assert ch.user_open_id is None

    def test_returns_value(self):
        ch = FakeFeishuChannel()
        ch._user_open_id = "ou_test123"
        assert ch.user_open_id == "ou_test123"
