# 飞书通道修复实施计划 v4 (TDD)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复飞书通道的 6 个实际 bug，使其能正常收发文字、图片、文档消息

**Architecture:** 飞书通道是独立的外部客户端映射层，通过 HTTP 调用 `/chat/sync`，与前端完全无关

**Tech Stack:** Python, lark-oapi SDK, FastAPI, pytest

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `tests/test_feishu_channel.py` | 测试文件 | 新建 |
| `niu_api/channel/feishu_channel.py` | 飞书通道适配器 | 修改 |
| `niu_api/channel/__init__.py` | ChannelRouter | 修改 |
| `niu_api/chat.py` | /chat/sync 端点 | 修改 |
| `niu_api/compat.py` | ChatRequest 兼容模型 | 修改 |
| `niu_api/internal/scheduler/service.py` | 定时推送 | 修改 |

---

## 测试辅助函数

所有测试共享以下辅助函数，放在 `tests/test_feishu_channel.py` 顶部：

```python
import pytest
from unittest.mock import Mock, MagicMock, patch, call
from niu_api.channel.base import UnifiedMessage
from niu_api.channel import ChannelRouter


def _create_msg(content="你好", chat_id="chat_123", sender_id="user_001",
                chat_type="p2p", resources=None, raw_content_type="text"):
    """创建 mock 飞书消息（模拟 SDK InboundMessage）"""
    msg = Mock()
    msg.content_text = content
    msg.chat_id = chat_id
    msg.sender_id = sender_id
    msg.chat_type = chat_type  # SDK InboundMessage 有此属性
    msg.resources = resources or []
    msg.raw_content_type = raw_content_type
    msg.raw = {}
    return msg


def _create_adapter():
    """创建飞书通道适配器（不真正连接 SDK）

    使用 __new__ + 手动属性设置，避免 __init__ 触发 SDK 连接和文件 I/O。
    """
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
```

---

### Task 1: 修复纯图片/文件消息被丢弃

**Files:**
- Create: `tests/test_feishu_channel.py`
- Modify: `niu_api/channel/feishu_channel.py`

当前 `_on_message` 中 `if not unified.content.strip(): return` 会丢弃纯图片消息（content 为空但 resources 不为空）。

- [ ] **Step 1: 写失败测试**

```python
class TestEmptyMessageCheck:
    """Task 1: 纯图片/文件消息不应被丢弃"""

    def test_image_message_not_skipped(self):
        """纯图片消息（content 为空，resources 不为空）不应被跳过"""
        adapter = _create_adapter()
        msg = _create_msg(content="", resources=[{"type": "image", "name": "photo.jpg"}])

        with patch('threading.Thread') as mock_thread:
            adapter._on_message(msg)
            # 消息未被跳过 → 应启动线程处理
            assert mock_thread.called

    def test_truly_empty_message_skipped(self):
        """content 和 resources 都为空的消息应被跳过"""
        adapter = _create_adapter()
        msg = _create_msg(content="", resources=[])

        with patch('threading.Thread') as mock_thread:
            adapter._on_message(msg)
            # 消息被跳过 → 不应启动线程
            mock_thread.assert_not_called()

    def test_text_message_not_skipped(self):
        """纯文本消息不应被跳过"""
        adapter = _create_adapter()
        msg = _create_msg(content="你好", resources=[])

        with patch('threading.Thread') as mock_thread:
            adapter._on_message(msg)
            # 消息未被跳过 → 应启动线程
            assert mock_thread.called
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestEmptyMessageCheck -v
```

预期：`test_image_message_not_skipped` FAIL（当前代码跳过了纯图片消息）

- [ ] **Step 3: 写最小实现**

修改 `niu_api/channel/feishu_channel.py` `_on_message` 中的空消息检查：

```python
# 旧：if not unified.content.strip():
# 新：
if not unified.content.strip() and not unified.resources:
    logger.debug("[FeishuChannel] Empty message with no resources, skipping")
    return
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestEmptyMessageCheck -v
```

预期：3 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/channel/feishu_channel.py
git commit -m "fix: 纯图片/文件消息不再被丢弃 — 检查 content 和 resources 都为空才跳过"
```

---

### Task 2: 修复群聊消息覆盖 P2P chat_id 和 open_id

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/channel/feishu_channel.py`

当前 `_on_message` 无条件更新 `_user_p2p_chat_id` 和 `_user_p2p_open_id`（通过 `_update_persisted_ids`），群聊消息会覆盖 P2P 值。

- [ ] **Step 1: 写失败测试**

```python
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
        """_is_p2p_message 在 chat_type 不存在时返回 False（宁可不更新也不错覆盖）"""
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

    def test_group_message_does_not_overwrite_p2p_open_id(self):
        """群聊消息不应覆盖已有的 P2P open_id"""
        adapter = _create_adapter()
        adapter._user_p2p_open_id = "user_001"
        msg = _create_msg(chat_id="group_chat_456", sender_id="user_002", chat_type="group")

        with patch('threading.Thread'):
            adapter._on_message(msg)
        assert adapter._user_p2p_open_id == "user_001"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestP2PMessageGuard -v
```

预期：`test_is_p2p_message_with_p2p` FAIL（方法不存在）
预期：`test_group_message_does_not_overwrite_p2p_chat_id` FAIL（当前代码无条件覆盖）

- [ ] **Step 3: 写最小实现**

添加 `_is_p2p_message` 方法：

```python
def _is_p2p_message(self, msg) -> bool:
    """判断是否为 P2P 消息（非群聊）"""
    chat_type = getattr(msg, 'chat_type', None)
    if chat_type:
        return chat_type == "p2p"
    logger.warning("[FeishuChannel] Cannot determine chat_type, skipping P2P update")
    return False
```

修改 `_on_message` 中三处赋值，都加 P2P 判断：

```python
# 旧：
# if not self._user_p2p_chat_id:
#     self._user_p2p_chat_id = msg.chat_id
# 新：
if self._is_p2p_message(msg):
    self._user_p2p_chat_id = msg.chat_id

# 新增 open_id 守卫：
if self._is_p2p_message(msg):
    self._user_p2p_open_id = msg.sender_id

# 旧：
# self._update_persisted_ids(msg.chat_id, msg.sender_id)
# 新：
if self._is_p2p_message(msg):
    self._update_persisted_ids(msg.chat_id, msg.sender_id)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestP2PMessageGuard -v
```

预期：6 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/channel/feishu_channel.py
git commit -m "fix: 群聊消息不再覆盖 P2P chat_id/open_id — 添加 _is_p2p_message 判断"
```

---

### Task 3: 修复 session_id 硬编码 + 添加 get_session_id 方法

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/channel/__init__.py`
- Modify: `niu_api/channel/feishu_channel.py`

当前 `route_in_sync` 和 `_chat_sync` 硬编码 `session_id="feishu"`，所有飞书用户共享一个会话。

- [ ] **Step 1: 写失败测试**

```python
class TestSessionID:
    """Task 3: session_id 应基于消息类型动态构造"""

    def test_get_session_id_p2p(self):
        """P2P 消息的 session_id 应为 feishu:{sender_id}"""
        adapter = _create_adapter()
        msg = _create_msg(sender_id="user_001", chat_id="p2p_chat", chat_type="p2p")
        assert adapter.get_session_id(msg) == "feishu:user_001"

    def test_get_session_id_group(self):
        """群聊消息的 session_id 应为 feishu:group:{chat_id}"""
        adapter = _create_adapter()
        msg = _create_msg(sender_id="user_001", chat_id="group_chat", chat_type="group")
        assert adapter.get_session_id(msg) == "feishu:group:group_chat"

    def test_route_in_sync_passes_session_id(self):
        """route_in_sync 应将 session_id 传递到 _chat_sync"""
        router = ChannelRouter()
        with patch.object(router, '_chat_sync', return_value="reply") as mock:
            msg = UnifiedMessage(content="test", channel="feishu", channel_id="c1", sender_id="s1", message_type="text")
            router.route_in_sync(msg, session_id="feishu:user_001", resources=None)
            mock.assert_called_once_with("test", session_id="feishu:user_001", resources=None)

    def test_chat_sync_passes_session_id_in_payload(self):
        """_chat_sync 应在 HTTP payload 中传递 session_id"""
        router = ChannelRouter()
        with patch('requests.post') as mock_post:
            mock_post.return_value = Mock(status_code=200, json=lambda: {"reply": "ok"})
            router._chat_sync("hello", session_id="feishu:user_001", resources=None)
            call_payload = mock_post.call_args[1]['json']
            assert call_payload['session_id'] == "feishu:user_001"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestSessionID -v
```

预期：4 个测试 FAIL（`get_session_id` 方法不存在，`route_in_sync` 不接受 session_id 参数）

- [ ] **Step 3: 写最小实现**

`niu_api/channel/feishu_channel.py` — 添加 `get_session_id` 方法：

```python
def get_session_id(self, msg) -> str:
    """构造飞书消息的 session_id"""
    if self._is_p2p_message(msg):
        return f"feishu:{msg.sender_id}"
    return f"feishu:group:{msg.chat_id}"
```

`niu_api/channel/__init__.py` — 修改 `route_in_sync` 和 `_chat_sync`：

```python
def route_in_sync(self, message: UnifiedMessage, session_id: str = "feishu", resources: list = None) -> str:
    """同步路由消息 — 供飞书通道调用"""
    return self._chat_sync(message.content, session_id=session_id, resources=resources)

def _chat_sync(self, message: str, session_id: str = "feishu", resources: list = None) -> str:
    """同步调用 /chat/sync 端点"""
    import os
    import requests

    port = os.environ.get("NIU_API_PORT", "9876")
    payload = {"session_id": session_id, "message": message}
    if resources:
        payload["resources"] = resources
    try:
        resp = requests.post(
            f"http://127.0.0.1:{port}/chat/sync",
            json=payload,
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json().get("reply", "")
        else:
            logger.error(f"[ChannelRouter] chat/sync returned {resp.status_code}")
            return ""
    except Exception as e:
        logger.error(f"[ChannelRouter] Failed to call chat/sync: {e}")
        return ""
```

`niu_api/channel/feishu_channel.py` — `_on_message` 中使用 `get_session_id`，传给 `_process_and_reply`：

```python
session_id = self.get_session_id(msg)
```

`_process_and_reply` 签名增加 `session_id` 参数：

```python
def _process_and_reply(self, unified, chat_id, session_id):
    try:
        reply = self.router.route_in_sync(unified, session_id=session_id, resources=unified.resources)
        if reply:
            self.channel.schedule(self.channel.send(chat_id, {"markdown": reply}))
        else:
            self.channel.schedule(self.channel.send(chat_id, {"text": "收到，但无法生成回复"}))
    except Exception as e:
        logger.error(f"[FeishuChannel] Process/reply error: {e}")
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestSessionID -v
```

预期：4 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/channel/feishu_channel.py niu_api/channel/__init__.py
git commit -m "fix: session_id 不再硬编码 — 基于 sender_id/chat_id 动态构造，添加 get_session_id 方法"
```

---

### Task 4: 修复回复发送方式 — 用 `channel.schedule()` 代替 `run_coroutine_threadsafe`

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/channel/feishu_channel.py`

当前 `_process_and_reply` 用 `asyncio.run_coroutine_threadsafe(channel.send(), sdk_loop)` 发送回复，依赖捕获的 loop 引用。应改用 SDK 提供的 `channel.schedule()`。同时删除 `_on_message` 中捕获 loop 的代码。

- [ ] **Step 1: 写失败测试**

```python
class TestReplySendMethod:
    """Task 4: 回复应通过 channel.schedule() 发送，不捕获 asyncio loop"""

    def test_on_message_does_not_capture_loop(self):
        """_on_message 不应调用 asyncio.get_running_loop()"""
        adapter = _create_adapter()
        msg = _create_msg(content="你好", chat_id="chat_123", chat_type="p2p")

        with patch('asyncio.get_running_loop') as mock_get_loop, \
             patch('threading.Thread'):
            adapter._on_message(msg)
            mock_get_loop.assert_not_called()

    def test_process_and_reply_uses_schedule(self):
        """_process_and_reply 应通过 channel.schedule() 发送回复"""
        adapter = _create_adapter()
        unified = UnifiedMessage(content="你好", channel="feishu", channel_id="chat_123", sender_id="user_001", message_type="text")

        async def _fake_send(*args, **kwargs):
            pass

        with patch.object(adapter.channel, 'send', side_effect=_fake_send) as mock_send, \
             patch.object(adapter.channel, 'schedule') as mock_schedule, \
             patch.object(adapter.router, 'route_in_sync', return_value="回复内容"):
            adapter._process_and_reply(unified, "chat_123", "feishu:user_001")
            mock_schedule.assert_called_once()
            # 验证 schedule 收到的是协程
            import asyncio
            call_arg = mock_schedule.call_args[0][0]
            assert asyncio.iscoroutine(call_arg)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestReplySendMethod -v
```

预期：`test_on_message_does_not_capture_loop` FAIL（当前代码调用 `asyncio.get_running_loop()`）

- [ ] **Step 3: 写最小实现**

删除 `_on_message` 中捕获 loop 的代码（第 78-83 行）：

```python
# 删除这段：
# try:
#     sdk_loop = asyncio.get_running_loop()
# except RuntimeError:
#     logger.warning(...)
#     return
```

修改 `_process_and_reply` 中的回复发送：

```python
# 旧：asyncio.run_coroutine_threadsafe(self.channel.send(chat_id, {"markdown": reply}), sdk_loop)
# 新：
self.channel.schedule(self.channel.send(chat_id, {"markdown": reply}))
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestReplySendMethod -v
```

预期：2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/channel/feishu_channel.py
git commit -m "fix: 回复发送改用 channel.schedule() — 不再捕获 asyncio loop"
```

---

### Task 5: 修复空回复无反馈 + 注册 error 事件

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/channel/feishu_channel.py`

Agent 返回空回复时飞书端无反馈。SDK error 事件未注册。

- [ ] **Step 1: 写失败测试**

```python
class TestEmptyReplyAndErrorEvent:
    """Task 5: 空回复有反馈 + error 事件注册"""

    def test_empty_reply_sends_notification(self):
        """Agent 返回空回复时应发提示消息"""
        adapter = _create_adapter()
        unified = UnifiedMessage(content="你好", channel="feishu", channel_id="chat_123", sender_id="user_001", message_type="text")

        with patch.object(adapter.channel, 'schedule') as mock_schedule, \
             patch.object(adapter.router, 'route_in_sync', return_value=""):
            adapter._process_and_reply(unified, "chat_123", "feishu:user_001")
            mock_schedule.assert_called_once()
            # 验证发送的是提示消息（包含 "text" key）
            call_arg = mock_schedule.call_args[0][0]
            assert call_arg is not None

    def test_error_event_registered_in_init(self):
        """__init__ 应调用 channel.on("error", self._on_error)"""
        # 不能用 _create_adapter（跳过了 __init__），需要验证初始化流程
        from niu_api.channel.feishu_channel import FeishuChannelAdapter
        with patch('niu_api.channel.feishu_channel.FeishuChannel') as mock_channel_class, \
             patch('lark_oapi.ws.client') as mock_ws_client:
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

    def test_on_error_logs(self):
        """_on_error 应记录错误日志"""
        adapter = _create_adapter()
        adapter._on_error = lambda err: None  # 方法存在
        adapter._on_error("test error")  # 不抛异常即可
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestEmptyReplyAndErrorEvent -v
```

预期：`test_error_event_registered` FAIL（`_on_error` 方法不存在）

- [ ] **Step 3: 写最小实现**

`__init__` 中注册 error 事件：

```python
self.channel.on("error", self._on_error)
```

添加 `_on_error` handler：

```python
def _on_error(self, err):
    """SDK 内部错误集中处理"""
    logger.error(f"[FeishuChannel] SDK error: {err}")
```

空回复提示已在 Task 3 中包含（`_process_and_reply` 的 else 分支）。

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestEmptyReplyAndErrorEvent -v
```

预期：3 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/channel/feishu_channel.py
git commit -m "fix: 空回复发提示 + 注册 SDK error 事件"
```

---

### Task 6: 传递 resources 到 `/chat/sync`

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/chat.py`
- Modify: `niu_api/compat.py`

`ChatRequest` 没有 `resources` 字段，飞书图片/文件信息在到达 Agent 前丢失。

- [ ] **Step 1: 写失败测试**

```python
class TestResourcesPassThrough:
    """Task 6: resources 应传递到 /chat/sync"""

    def test_chat_request_accepts_resources(self):
        """ChatRequest 应接受 resources 字段"""
        from niu_api.chat import ChatRequest
        req = ChatRequest(message="hello", resources=[{"type": "image", "name": "photo.jpg"}])
        assert req.resources == [{"type": "image", "name": "photo.jpg"}]

    def test_chat_request_resources_default_none(self):
        """ChatRequest resources 默认为 None"""
        from niu_api.chat import ChatRequest
        req = ChatRequest(message="hello")
        assert req.resources is None

    def test_compat_chat_request_accepts_resources(self):
        """compat.py 的 ChatRequest 也应接受 resources"""
        from niu_api.compat import ChatRequest
        req = ChatRequest(message="hello", resources=[{"type": "image", "name": "photo.jpg"}])
        assert req.resources == [{"type": "image", "name": "photo.jpg"}]

    def test_format_resources_with_text(self):
        """有文本时 resources 应转为附件描述附加到 message"""
        from niu_api.chat import _format_resources
        resources = [{"type": "image", "name": "photo.jpg"}, {"type": "file", "name": "doc.pdf"}]
        result = _format_resources("你好", resources)
        assert "你好" in result
        assert "附件：" in result
        assert "photo.jpg" in result
        assert "doc.pdf" in result

    def test_format_resources_image_only(self):
        """纯图片消息（content 为空）应只显示 resources 描述"""
        from niu_api.chat import _format_resources
        resources = [{"type": "image", "name": "photo.jpg"}]
        result = _format_resources("", resources)
        assert "photo.jpg" in result
        assert "附件" not in result

    def test_format_resources_no_resources(self):
        """无 resources 时返回原始 content"""
        from niu_api.chat import _format_resources
        assert _format_resources("你好", None) == "你好"
        assert _format_resources("你好", []) == "你好"

    def test_route_in_sync_passes_resources(self):
        """route_in_sync 应将 resources 传递到 _chat_sync"""
        router = ChannelRouter()
        with patch.object(router, '_chat_sync', return_value="reply") as mock:
            msg = UnifiedMessage(content="test", channel="feishu", channel_id="c1", sender_id="s1", message_type="text")
            resources = [{"type": "image", "name": "photo.jpg"}]
            router.route_in_sync(msg, session_id="feishu:s1", resources=resources)
            mock.assert_called_once_with("test", session_id="feishu:s1", resources=resources)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestResourcesPassThrough -v
```

预期：`test_chat_request_accepts_resources` FAIL（ChatRequest 没有 resources 字段）
预期：`test_format_resources_with_text` FAIL（`_format_resources` 函数不存在）

- [ ] **Step 3: 写最小实现**

`niu_api/chat.py` — 新增 `_format_resources` 模块级函数：

```python
def _format_resources(content: str, resources: list | None) -> str:
    """将 resources 转为文本描述附加到 message"""
    if not resources:
        return content
    resource_desc = "\n".join(
        f"[{r.get('type', 'file')}: {r.get('name', 'unknown')}]"
        for r in resources if r
    )
    if content.strip():
        return f"{content}\n\n附件：\n{resource_desc}"
    return resource_desc
```

`niu_api/chat.py` — ChatRequest 新增字段（不改原有默认值）：

```python
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    system_prompt: Optional[str] = None
    resources: Optional[list] = None
```

`niu_api/compat.py` — ChatRequest 同步新增：

```python
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    resources: Optional[list] = None
```

`niu_api/chat.py` — `/chat/sync` 端点使用 `_format_resources`：

```python
# 在端点中，构造传给 runner 的 message
message = _format_resources(req.message, req.resources)
# 然后用 message 代替 req.message 传给 runner.chat()
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestResourcesPassThrough -v
```

预期：7 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/chat.py niu_api/compat.py niu_api/channel/__init__.py
git commit -m "feat: ChatRequest 增加 resources 字段 + _format_resources — 飞书图片/文件信息可传递到 Agent"
```

---

### Task 7: 修复 Scheduler 推送发送方式

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `niu_api/internal/scheduler/service.py`

Scheduler 用 `asyncio.run_coroutine_threadsafe` 推送飞书消息，应改用 `channel.schedule()`。同时保留 `push()` 的 open_id 回退逻辑。

- [ ] **Step 1: 写失败测试**

```python
class TestSchedulerPush:
    """Task 7: Scheduler 推送应通过 channel.schedule() 发送"""

    def test_push_calls_channel_send(self):
        """push 方法应调用 channel.send()（已有逻辑不变）"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "chat_123"

        async def _fake_send(*args, **kwargs):
            pass

        with patch.object(adapter.channel, 'send', side_effect=_fake_send) as mock_send:
            import asyncio
            asyncio.run(adapter.push("chat_123", "提醒内容"))
            mock_send.assert_called_once_with("chat_123", {"markdown": "提醒内容"})

    def test_scheduler_uses_channel_schedule(self):
        """Scheduler 推送应通过 channel.schedule() 而非 run_coroutine_threadsafe"""
        adapter = _create_adapter()
        adapter._user_p2p_chat_id = "chat_123"

        # 验证 scheduler 推送路径：feishu_adapter.channel.schedule(push_coro)
        with patch.object(adapter.channel, 'schedule') as mock_schedule:
            # 模拟 scheduler 的推送调用方式
            push_coro = adapter.push(adapter._user_p2p_chat_id, "提醒内容")
            adapter.channel.schedule(push_coro)
            mock_schedule.assert_called_once()
            # 验证 schedule 收到的是协程
            import asyncio
            call_arg = mock_schedule.call_args[0][0]
            assert asyncio.iscoroutine(call_arg)
            # 清理未 await 的协程，避免 RuntimeWarning
            push_coro.close()
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_feishu_channel.py::TestSchedulerPush -v
```

- [ ] **Step 3: 写最小实现**

修改 `niu_api/internal/scheduler/service.py` 中飞书推送代码（第 126-136 行）：

```python
# 旧：
# future = asyncio.run_coroutine_threadsafe(
#     channel_router.push(agent_reply, "feishu", feishu_adapter.user_p2p_chat_id),
#     _main_loop
# )
# 新：直接用 channel.schedule() 发送，保留 push 的 open_id 回退逻辑
feishu_adapter.channel.schedule(
    feishu_adapter.push(feishu_adapter.user_p2p_chat_id, agent_reply)
)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_feishu_channel.py::TestSchedulerPush -v
```

- [ ] **Step 5: 提交**

```bash
git add tests/test_feishu_channel.py niu_api/internal/scheduler/service.py
git commit -m "fix: Scheduler 飞书推送改用 channel.schedule() — 不再依赖 _main_loop"
```

---

## 不修改的文件

- `niu_api/channel/base.py` — ChannelAdapter 接口不变
- `niu_api/__main__.py` — 启动逻辑不变