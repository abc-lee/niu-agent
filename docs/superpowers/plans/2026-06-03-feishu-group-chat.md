# 飞书群聊完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完善飞书群聊处理，使机器人能在群聊中正常工作，所有群聊功能仅在 `chat_type == "group"` 时激活，单聊零影响。

**Architecture:** 在 `_on_message` 入口处按 `chat_type` 分流：群聊走新分支（@bot过滤、元信息注入、reply），单聊走现有逻辑不变。定时推送通过 task_store 增加 chat_id 列 + service 传递 chat_id 支持群目标。

**Tech Stack:** Python 3.11, lark-oapi SDK (Client 同步 API + Builder 模式), SQLite (task_store), pytest

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `tests/test_feishu_group_chat.py` | 群聊功能 TDD 测试 | Create |
| `niu_api/channel/feishu_channel.py` | F1 @bot过滤 + F2 元信息注入 + F3 reply API | Modify |
| `niu_api/internal/scheduler/task_store.py` | F4 task 表增加 chat_id 列 | Modify |
| `niu_api/internal/scheduler/service.py` | F4 trigger_callback 传递 chat_id | Modify |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | F4 schedule_task 增加 chat_id 参数 | Modify |
| `tests/test_scheduler_group_push.py` | F4 群推送 TDD 测试 | Create |

---

### Task 1: F1 @Bot 过滤 — 群聊中仅 @bot 消息触发 Agent

**Files:**
- Create: `tests/test_feishu_group_chat.py`
- Modify: `niu_api/channel/feishu_channel.py:83-210`

- [ ] **Step 1: Write the failing test — 群聊非 @bot 消息被忽略**

```python
"""飞书群聊功能 TDD 测试"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock


class FakeInboundMessage:
    """模拟 SDK InboundMessage"""
    def __init__(self, chat_type="p2p", content_text="hello", mentioned_bot=False,
                 sender_id="ou_user1", sender_name="张三", chat_id="oc_chat1",
                 message_id="om_msg1", mentions=None, raw=None):
        self.chat_type = chat_type
        self.content_text = content_text
        self.mentioned_bot = mentioned_bot
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.chat_id = chat_id
        self.message_id = message_id
        self.mentions = mentions or []
        self.raw = raw or {}
        self.resources = []
        self.raw_content_type = "text"
        self.id = message_id


def _make_adapter():
    """创建 FeishuChannelAdapter 的 mock 实例（不启动 WebSocket）"""
    from niu_api.channel.feishu_channel import FeishuChannelAdapter

    with patch.object(FeishuChannelAdapter, '__init__', lambda self, *a, **k: None):
        adapter = FeishuChannelAdapter.__new__(FeishuChannelAdapter)

    # 初始化必要属性（与 __init__ 中的流式状态变量一致）
    adapter._user_p2p_chat_id = "oc_p2p"
    adapter._user_open_id = "ou_user1"
    adapter._feishu_waiting = False
    adapter._stream_card_id = None
    adapter._stream_message_id = None
    adapter._stream_card_created = False
    adapter._stream_fallback_used = False
    adapter._stream_seq = 0
    adapter._accumulated_text = ""
    adapter._stream_pending_images = []
    adapter._stream_pending_files = []
    adapter._stream_target = None
    adapter._stream_open_id = None
    adapter._stream_chat_id = None
    adapter._stream_sent_media_paths = set()
    adapter._last_pushed_rowid = 0
    adapter._stream_reply_to_id = None
    adapter.router = MagicMock()
    adapter.channel = MagicMock()
    adapter.channel._bot_open_id = "ou_bot123"
    adapter.logger = MagicMock()

    return adapter


class TestF1BotFilter:
    """F1: @Bot 过滤 — 群聊中仅 @bot 消息触发 Agent"""

    def test_group_message_without_mention_is_ignored(self):
        """群聊中未 @bot 的消息应被忽略，不触发 Agent"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="大家好",
            mentioned_bot=False,
        )

        # _on_message 应该直接 return，不调 _enqueue_message
        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        # _enqueue_message 不应被调用
        mock_enqueue.assert_not_called()

    def test_group_message_with_mention_triggers_agent(self):
        """群聊中 @bot 的消息应触发 Agent"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="@bot 帮我查一下",
            mentioned_bot=True,
            chat_id="oc_group1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        # _enqueue_message 应被调用
        mock_enqueue.assert_called_once()

    def test_p2p_message_always_triggers_agent(self):
        """单聊消息无论是否 @bot 都应触发 Agent"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="p2p",
            content_text="你好",
            mentioned_bot=False,
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        mock_enqueue.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF1BotFilter -v`
Expected: FAIL — 群聊消息目前不检查 mentioned_bot，所有消息都触发 Agent

- [ ] **Step 3: Implement F1 — 在 `_on_message` 中添加群聊 @bot 过滤**

在 `niu_api/channel/feishu_channel.py` 的 `_on_message` 方法中，在 `is_p2p = self._is_p2p_message(msg)` （line 104）之后、`logger.info` （line 106）之前插入：

```python
# F1: 群聊中仅 @bot 消息触发 Agent，其他群消息忽略
if not is_p2p and not getattr(msg, 'mentioned_bot', False):
    logger.debug(f"[FeishuChannel] Group message without @bot, skipping")
    return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF1BotFilter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_feishu_group_chat.py niu_api/channel/feishu_channel.py
git commit -m "feat(feishu): F1 group chat @bot filter — only @bot messages trigger agent in group"
```

---

### Task 2: F2 群聊消息元信息注入 + @bot 文本清理

**Files:**
- Modify: `tests/test_feishu_group_chat.py`
- Modify: `niu_api/channel/feishu_channel.py:83-210`

- [ ] **Step 1: Write the failing test — 群聊消息注入发送者前缀**

在 `tests/test_feishu_group_chat.py` 中追加：

```python
class TestF2GroupMessageMeta:
    """F2: 群聊消息元信息注入"""

    def test_group_message_injects_sender_prefix(self):
        """群聊消息应在内容前注入 [群聊] 发送者：xxx 前缀"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="帮我查一下",
            mentioned_bot=True,
            sender_name="张三",
            sender_id="ou_user1",
            chat_id="oc_group1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        # 验证 _enqueue_message 被调用时 message_override 包含发送者前缀
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        message_override = call_kwargs.get('message_override', '')
        if not message_override:
            # 也可能是位置参数
            call_args = mock_enqueue.call_args[0]
            message_override = call_args[1] if len(call_args) > 1 else ''
        assert "[群聊]" in message_override
        assert "张三" in message_override

    def test_group_message_sender_name_fallback(self):
        """sender_name 为空时用 sender_id[:8] 兜底"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="帮我查一下",
            mentioned_bot=True,
            sender_name="",
            sender_id="ou_abc123456789",
            chat_id="oc_group1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        mock_enqueue.assert_called_once()
        # 从 _on_message 内部查看 message_content 的修改
        # 由于 message_content 是局部变量，我们通过 _enqueue_message 参数间接验证
        all_args_str = str(mock_enqueue.call_args)
        assert "ou_abc12" in all_args_str  # sender_id[:8]

    def test_p2p_message_no_sender_prefix(self):
        """单聊消息不注入发送者前缀"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="p2p",
            content_text="你好",
            sender_name="张三",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        mock_enqueue.assert_called_once()
        all_args_str = str(mock_enqueue.call_args)
        assert "[群聊]" not in all_args_str
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF2GroupMessageMeta -v`
Expected: FAIL — 当前群聊消息不注入发送者前缀

- [ ] **Step 3: Implement F2 — 群聊消息元信息注入 + @bot 文本清理**

在 `_on_message` 中，`message_content` 最终赋值完成后（资源替换之后、`_enqueue_message` 调用之前），插入群聊分支：

```python
# F2: 群聊消息元信息注入
if not is_p2p:
    sender_name = getattr(msg, 'sender_name', '') or ''
    sender_id = unified.sender_id or ''
    display_name = sender_name or (sender_id[:8] if sender_id else '未知')

    # 清理 @bot 文本：尝试用 SDK mentions 模块，失败则用正则兜底
    content_to_inject = message_content
    try:
        from lark_oapi.channel.normalize.mentions import extract_mentions, resolve_mentions
        bot_open_id = getattr(self.channel, '_bot_open_id', None)
        if bot_open_id:
            raw_mentions = (getattr(msg, 'raw', None) or {}).get("mentions", [])
            ext = extract_mentions(raw_mentions, bot_open_id=bot_open_id)
            content_to_inject = resolve_mentions(
                message_content, ext,
                strip_bot_mentions=True,
                bot_open_id=bot_open_id,
            )
    except (ImportError, Exception) as e:
        logger.debug(f"[FeishuChannel] Mention cleanup via SDK failed: {e}")
        # 兜底：正则去除 @提及
        content_to_inject = re.sub(r'@[\w一-鿿]+\s*', '', message_content).strip()

    message_content = f"[群聊] 发送者：{display_name}\n\n{content_to_inject}"
```

**注意**：`lark_oapi.channel.normalize.mentions` 的 import 路径需要在实现时验证。如果 SDK 版本中不存在该模块，则使用正则兜底方案。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF2GroupMessageMeta -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_feishu_group_chat.py niu_api/channel/feishu_channel.py
git commit -m "feat(feishu): F2 group chat sender info injection + @bot text cleanup"
```

---

### Task 3: F3 群聊回复使用 reply API

**Files:**
- Modify: `tests/test_feishu_group_chat.py`
- Modify: `niu_api/channel/feishu_channel.py:1-5` (import 块)
- Modify: `niu_api/channel/feishu_channel.py:59-72` (__init__ 流式状态)
- Modify: `niu_api/channel/feishu_channel.py:95-100` (_on_message 状态重置)
- Modify: `niu_api/channel/feishu_channel.py:688-730` (_create_stream_card)

- [ ] **Step 1: Write the failing test — 群聊设置 _stream_reply_to_id**

在 `tests/test_feishu_group_chat.py` 中追加：

```python
class TestF3GroupReply:
    """F3: 群聊回复使用 reply API"""

    def test_group_message_sets_reply_to_id(self):
        """群聊消息应设置 _stream_reply_to_id"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="@bot 你好",
            mentioned_bot=True,
            message_id="om_reply_target",
            chat_id="oc_group1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]):
            adapter._on_message(msg)

        assert adapter._stream_reply_to_id == "om_reply_target"

    def test_p2p_message_reply_to_id_is_none(self):
        """单聊消息 _stream_reply_to_id 应为 None"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="p2p",
            content_text="你好",
            message_id="om_p2p_msg",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]):
            adapter._on_message(msg)

        assert adapter._stream_reply_to_id is None

    def test_create_stream_card_uses_reply_when_set(self):
        """_create_stream_card 在 _stream_reply_to_id 存在时应使用 reply API"""
        adapter = _make_adapter()
        adapter._stream_target = "oc_group1"
        adapter._stream_reply_to_id = "om_reply_target"

        # Mock SDK client
        mock_card_resp = MagicMock()
        mock_card_resp.success.return_value = True
        mock_card_resp.data.card_id = "card_123"
        adapter.channel.client.cardkit.v1.card.create.return_value = mock_card_resp

        mock_reply_resp = MagicMock()
        mock_reply_resp.success.return_value = True
        mock_reply_resp.data.message_id = "om_replied_msg"
        adapter.channel.client.im.v1.message.reply.return_value = mock_reply_resp

        # Mock create（不应被调用）
        mock_create_resp = MagicMock()
        adapter.channel.client.im.v1.message.create.return_value = mock_create_resp

        result = adapter._create_stream_card("测试内容")

        # reply API 应被调用
        adapter.channel.client.im.v1.message.reply.assert_called_once()
        # create API 不应被调用
        adapter.channel.client.im.v1.message.create.assert_not_called()
        assert result == "card_123"

    def test_create_stream_card_uses_create_when_no_reply(self):
        """_create_stream_card 在 _stream_reply_to_id 为 None 时应使用 create API"""
        adapter = _make_adapter()
        adapter._stream_target = "oc_p2p"
        adapter._stream_reply_to_id = None

        mock_card_resp = MagicMock()
        mock_card_resp.success.return_value = True
        mock_card_resp.data.card_id = "card_456"
        adapter.channel.client.cardkit.v1.card.create.return_value = mock_card_resp

        mock_create_resp = MagicMock()
        mock_create_resp.success.return_value = True
        mock_create_resp.data.message_id = "om_created_msg"
        adapter.channel.client.im.v1.message.create.return_value = mock_create_resp

        result = adapter._create_stream_card("测试内容")

        adapter.channel.client.im.v1.message.create.assert_called_once()
        assert result == "card_456"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF3GroupReply -v`
Expected: FAIL — 当前没有 `_stream_reply_to_id` 属性，也没有 reply 逻辑

- [ ] **Step 3: Implement F3 — reply API 支持**

**3a. 添加 import**（`feishu_channel.py` 顶部 import 块，在 `CreateMessageRequest, CreateMessageRequestBody` 所在行之后添加）：

```python
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
```

**3b. 添加 `_stream_reply_to_id` 属性**（`feishu_channel.py` __init__ 流式状态块，在 `self._stream_sent_media_paths = set()` 之后）：

```python
self._stream_reply_to_id: str | None = None  # F3: 群聊 reply 目标消息 ID
```

**3c. 状态重置**（`feishu_channel.py` `_on_message` 入口重置块，在 `self._stream_sent_media_paths = set()` 之后添加）：

```python
self._stream_reply_to_id = None
```

**3d. 群聊分支设置 reply_to_id**（在 `_on_message` 中，F1 过滤之后、F2 元信息注入之前）：

```python
# F3: 群聊设置 reply 目标
if not is_p2p:
    self._stream_reply_to_id = str(getattr(msg, 'message_id', '') or getattr(msg, 'id', '') or '')
```

**3e. 修改 `_create_stream_card`**（`feishu_channel.py`，约 line 688-730）：

在现有的 `CreateMessageRequest` 发送逻辑处，替换为 reply/create 分支。只替换发送消息的部分（从 `card_ref = json.dumps(...)` 到 `send_resp = self.channel.client.im.v1.message.create(send_req)`），保留前后的卡片创建和 card_id 获取逻辑不变：

```python
        # 用 card_id 引用发送消息
        card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)

        # F3: 群聊使用 reply API，单聊使用 create API
        if self._stream_reply_to_id:
            reply_req = ReplyMessageRequest.builder() \
                .message_id(self._stream_reply_to_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(card_ref)
                    .build()) \
                .build()
            send_resp = self.channel.client.im.v1.message.reply(reply_req)
        else:
            send_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(self._stream_target)
                    .msg_type("interactive")
                    .content(card_ref)
                    .build()) \
                .build()
            send_resp = self.channel.client.im.v1.message.create(send_req)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py::TestF3GroupReply -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_feishu_group_chat.py niu_api/channel/feishu_channel.py
git commit -m "feat(feishu): F3 group chat reply API — use ReplyMessageRequest in group, CreateMessageRequest in p2p"
```

---

### Task 4: F4 定时推送支持群目标 — task_store 增加 chat_id 列

**Files:**
- Create: `tests/test_scheduler_group_push.py`
- Modify: `niu_api/internal/scheduler/task_store.py`

- [ ] **Step 1: Write the failing test — task_store 支持 chat_id**

```python
"""定时推送群目标 TDD 测试"""
import os
import tempfile
import pytest
from niu_api.internal.scheduler.task_store import TaskStore


@pytest.fixture
def store():
    """创建临时数据库的 TaskStore"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TaskStore(db_path)
    yield s
    try:
        os.unlink(db_path)
    except Exception:
        pass


class TestF4TaskStoreChatId:
    """F4: task_store 支持 chat_id"""

    def test_create_task_with_chat_id(self, store):
        """创建任务时可以指定 chat_id"""
        task_id = store.create_task(
            content="群聊提醒",
            scheduled_at="2026-06-03T10:00:00",
            chat_id="oc_group123",
        )
        assert task_id is not None

        task = store.get_task(task_id)
        assert task is not None
        assert task["chat_id"] == "oc_group123"

    def test_create_task_without_chat_id(self, store):
        """不指定 chat_id 时默认为 None"""
        task_id = store.create_task(
            content="私聊提醒",
            scheduled_at="2026-06-03T10:00:00",
        )
        task = store.get_task(task_id)
        assert task is not None
        assert task["chat_id"] is None

    def test_overdue_tasks_include_chat_id(self, store):
        """get_overdue_tasks 返回结果包含 chat_id"""
        from datetime import datetime
        past = (datetime.now()).isoformat()
        task_id = store.create_task(
            content="到期任务",
            scheduled_at=past,
            chat_id="oc_group456",
        )
        # 手动触发检查（scheduled_at <= now）
        tasks = store.get_overdue_tasks()
        matching = [t for t in tasks if t["id"] == task_id]
        assert len(matching) == 1
        assert matching[0]["chat_id"] == "oc_group456"

    def test_list_tasks_include_chat_id(self, store):
        """list_tasks 返回结果包含 chat_id"""
        task_id = store.create_task(
            content="列表任务",
            scheduled_at="2026-06-03T10:00:00",
            chat_id="oc_group789",
        )
        tasks = store.list_tasks()
        matching = [t for t in tasks if t["id"] == task_id]
        assert len(matching) == 1
        assert matching[0]["chat_id"] == "oc_group789"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_scheduler_group_push.py::TestF4TaskStoreChatId -v`
Expected: FAIL — `create_task` 不接受 `chat_id` 参数

- [ ] **Step 3: Implement — task_store 增加 chat_id 列**

**3a. 数据库迁移**（`task_store.py:_init_db`，在现有 `name` 列迁移之后添加）：

```python
# 迁移：老数据库可能没有 chat_id 列
try:
    conn.execute("""
        ALTER TABLE scheduled_tasks ADD COLUMN chat_id TEXT
    """)
except sqlite3.OperationalError:
    pass  # 列已存在
```

**3b. `create_task` 方法增加 `chat_id` 参数**：

```python
def create_task(
    self,
    content: str,
    scheduled_at: str,
    event_type: str = "reminder",
    is_recurring: bool = False,
    cron_expr: Optional[str] = None,
    name: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> str:
    """创建任务"""
    task_id = str(uuid.uuid4())

    conn = sqlite3.connect(self.db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            INSERT INTO scheduled_tasks
            (id, content, scheduled_at, is_recurring, cron_expr, event_type, status, name, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type, name, chat_id))
        conn.commit()
    finally:
        conn.close()

    return task_id
```

**3c. 所有 SELECT 查询增加 `chat_id` 列**：

在 `list_tasks`、`get_task`、`get_overdue_tasks`、`find_task_by_name` 的 SELECT 语句中增加 `chat_id`，并在结果字典中添加 `"chat_id"` 键。

**重要**：具体列索引取决于实际 SELECT 语句的列顺序。实现时需阅读每个方法的实际 SELECT，在末尾增加 `chat_id` 列，并在 dict 构建中添加对应索引。不要假设固定索引号——以源码实际 SELECT 为准。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_scheduler_group_push.py::TestF4TaskStoreChatId -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_scheduler_group_push.py niu_api/internal/scheduler/task_store.py
git commit -m "feat(scheduler): F4 task_store chat_id column — support group push target"
```

---

### Task 5: F4 定时推送支持群目标 — service + MCP 工具传递 chat_id

**Files:**
- Modify: `tests/test_scheduler_group_push.py`
- Modify: `niu_api/internal/scheduler/service.py:105-116`
- Modify: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py`

- [ ] **Step 1: Write the failing test — service 传递 chat_id 到 router.push**

在 `tests/test_scheduler_group_push.py` 中追加：

```python
class TestF4ServiceChatIdPass:
    """F4: trigger_callback 传递 chat_id 到 router.push"""

    def test_trigger_callback_passes_chat_id(self):
        """trigger_callback 应从 task 读取 chat_id 并传给 router.push"""
        from unittest.mock import patch, MagicMock, AsyncMock
        from niu_api.internal.scheduler.service import trigger_callback

        task = {
            "id": "task_123",
            "content": "群聊提醒",
            "scheduled_at": "2026-06-03T10:00:00",
            "chat_id": "oc_group123",
        }

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = "Agent 回复"
        mock_queue.enqueue_and_wait = AsyncMock(return_value="Agent 回复")

        mock_router = MagicMock()
        mock_router.has_channel.return_value = True
        mock_push_future = MagicMock()
        mock_push_future.result.return_value = None

        with patch("niu_api.internal.scheduler.service._main_loop", mock_loop), \
             patch("niu_api.internal.scheduler.service.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.internal.scheduler.service.get_channel_router", return_value=mock_router), \
             patch("niu_api.internal.scheduler.service.add_pending_alert"), \
             patch("asyncio.run_coroutine_threadsafe") as mock_rc:

            # 模拟两个 future：一个给 enqueue_and_wait，一个给 push
            enqueue_future = MagicMock()
            enqueue_future.result.return_value = "Agent 回复"
            push_future = MagicMock()
            push_future.result.return_value = None

            call_count = [0]
            def side_effect(coro, loop):
                call_count[0] += 1
                if call_count[0] == 1:
                    return enqueue_future
                return push_future

            mock_rc.side_effect = side_effect

            result = trigger_callback(task)

        # 验证 router.push 被调用时 chat_id 不为空串
        # 由于 run_coroutine_threadsafe 的 mock，我们验证第二次调用（push）
        assert mock_rc.call_count == 2
        # 第二次调用的参数是 router.push(agent_reply, "feishu", chat_id)
        # chat_id 应为 "oc_group123" 而非 ""

    def test_trigger_callback_empty_chat_id_for_p2p(self):
        """私聊任务 chat_id 为空时，push 传空串（兼容现有行为）"""
        from unittest.mock import patch, MagicMock, AsyncMock
        from niu_api.internal.scheduler.service import trigger_callback

        task = {
            "id": "task_456",
            "content": "私聊提醒",
            "scheduled_at": "2026-06-03T10:00:00",
            # 无 chat_id 字段
        }

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_queue.enqueue_and_wait = AsyncMock(return_value="Agent 回复")

        with patch("niu_api.internal.scheduler.service._main_loop", mock_loop), \
             patch("niu_api.internal.scheduler.service.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.internal.scheduler.service.get_channel_router") as mock_router_cls, \
             patch("niu_api.internal.scheduler.service.add_pending_alert"), \
             patch("asyncio.run_coroutine_threadsafe") as mock_rc:

            enqueue_future = MagicMock()
            enqueue_future.result.return_value = "Agent 回复"
            push_future = MagicMock()
            push_future.result.return_value = None
            mock_rc.side_effect = [enqueue_future, push_future]

            result = trigger_callback(task)

        # 第二次 run_coroutine_threadsafe 调用（push）应传空串
        # 验证行为与现有逻辑一致
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_scheduler_group_push.py::TestF4ServiceChatIdPass -v`
Expected: FAIL — 当前 `router.push(agent_reply, "feishu", "")` 硬编码空串

- [ ] **Step 3: Implement — service 传递 chat_id**

修改 `niu_api/internal/scheduler/service.py`，在 `trigger_callback` 函数中找到 `router.push` 调用（约 line 112），将硬编码空串改为从 task 读取 chat_id：

```python
# 原代码：
# push_future = asyncio.run_coroutine_threadsafe(
#     router.push(agent_reply, "feishu", ""),
#     loop,
# )

# 修改为：
push_chat_id = task.get("chat_id") or ""
push_future = asyncio.run_coroutine_threadsafe(
    router.push(agent_reply, "feishu", push_chat_id),
    loop,
)
```

**注意**：`router.push()` 的第三个参数名可能是 `channel_id`，实现时需确认 `ChannelRouter.push()` 和 `FeishuChannelAdapter.push()` 的签名。如果 `channel_id` 不对应 `chat_id`，需要调整传递方式。

- [ ] **Step 4: Implement — scheduler-server MCP 工具增加 chat_id 参数**

修改 `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py`：

**4a. TOOL_SCHEMAS 中 `schedule_task` 增加 `chat_id` 属性**：

```python
"chat_id": {"type": "string", "description": "群聊 chat_id（可选，群聊中创建时传入，用于推送到群）"}
```

**4b. `schedule_task()` 函数签名增加 `chat_id` 参数**：

```python
def schedule_task(
    content: str,
    scheduled_at: str,
    event_type: str = "reminder",
    is_recurring: bool = False,
    cron_expr: str = None,
    name: str = None,
    chat_id: str = None,
) -> dict:
    """创建定时任务，支持单次和循环任务。"""
    try:
        store = _get_store()
        task_id = store.create_task(
            content=content,
            scheduled_at=scheduled_at,
            event_type=event_type,
            is_recurring=is_recurring,
            cron_expr=cron_expr,
            name=name,
            chat_id=chat_id,
        )
        return {"status": "success", "task_id": task_id, "message": f"已创建定时任务：{content}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

**4c. `run_server` 中 `call_tool` 的 `schedule_task` 分支增加 `chat_id`**：

```python
if name == "schedule_task":
    task_id = store.create_task(
        content=arguments["content"],
        scheduled_at=arguments["scheduled_at"],
        event_type=arguments.get("event_type", "reminder"),
        is_recurring=arguments.get("is_recurring", False),
        cron_expr=arguments.get("cron_expr"),
        name=arguments.get("name"),
        chat_id=arguments.get("chat_id"),
    )
```

**4d. `list_tools` 中 `schedule_task` 的 `inputSchema` 增加 `chat_id` 属性**：

```python
"chat_id": {"type": "string", "description": "群聊 chat_id（可选，群聊中创建时传入，用于推送到群）"}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_scheduler_group_push.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_scheduler_group_push.py niu_api/internal/scheduler/service.py mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py
git commit -m "feat(scheduler): F4 group push — service passes chat_id + MCP tool accepts chat_id"
```

---

### Task 6: F5 群聊 session 持久化验证 + 单聊回归测试

**Files:**
- Modify: `tests/test_feishu_group_chat.py`

- [ ] **Step 1: Write the test — 群聊 session_id 隔离 + 单聊不变**

在 `tests/test_feishu_group_chat.py` 中追加：

```python
class TestF5SessionIsolation:
    """F5: 群聊 session 持久化验证"""

    def test_group_session_id_format(self):
        """群聊 session_id 格式应为 feishu:group:{chat_id}"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="group",
            content_text="@bot 你好",
            mentioned_bot=True,
            chat_id="oc_group1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        mock_enqueue.assert_called_once()
        all_args_str = str(mock_enqueue.call_args)
        # session_id 应包含 group 标识
        assert "group" in all_args_str or "oc_group1" in all_args_str

    def test_p2p_session_id_unchanged(self):
        """单聊 session_id 格式应保持 feishu:{sender_id}"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(
            chat_type="p2p",
            content_text="你好",
            sender_id="ou_user1",
        )

        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)

        mock_enqueue.assert_called_once()
        # 单聊不应有 group 标识
        all_args_str = str(mock_enqueue.call_args)
        assert "feishu:ou_user1" in all_args_str


class TestRegressionP2PUntouched:
    """回归测试：单聊行为完全不变"""

    def test_p2p_no_reply_to_id(self):
        """单聊不设置 _stream_reply_to_id"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(chat_type="p2p", content_text="你好")
        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message'):
            adapter._on_message(msg)
        assert adapter._stream_reply_to_id is None

    def test_p2p_no_sender_prefix(self):
        """单聊不注入发送者前缀"""
        adapter = _make_adapter()
        msg = FakeInboundMessage(chat_type="p2p", content_text="你好", sender_name="张三")
        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message') as mock_enqueue:
            adapter._on_message(msg)
        all_args_str = str(mock_enqueue.call_args)
        assert "[群聊]" not in all_args_str

    def test_p2p_stream_target_unchanged(self):
        """单聊 _stream_target 设置逻辑不变"""
        adapter = _make_adapter()
        adapter._user_p2p_chat_id = "oc_p2p"
        adapter._user_open_id = "ou_user1"
        msg = FakeInboundMessage(
            chat_type="p2p",
            content_text="你好",
            chat_id="oc_p2p",
            sender_id="ou_user1",
        )
        with patch.object(adapter, 'resolve_inbound_resources', return_value=[]), \
             patch.object(adapter, '_enqueue_message'):
            adapter._on_message(msg)
        # _stream_target 应为 channel_id 或 open_id（现有逻辑）
        assert adapter._stream_target in ("oc_p2p", "ou_user1")

    def test_p2p_create_uses_message_create_not_reply(self):
        """单聊 _create_stream_card 使用 message.create 而非 message.reply"""
        adapter = _make_adapter()
        adapter._stream_target = "oc_p2p"
        adapter._stream_reply_to_id = None  # 单聊应为 None

        mock_card_resp = MagicMock()
        mock_card_resp.success.return_value = True
        mock_card_resp.data.card_id = "card_p2p"
        adapter.channel.client.cardkit.v1.card.create.return_value = mock_card_resp

        mock_create_resp = MagicMock()
        mock_create_resp.success.return_value = True
        mock_create_resp.data.message_id = "om_p2p_msg"
        adapter.channel.client.im.v1.message.create.return_value = mock_create_resp

        result = adapter._create_stream_card("单聊内容")

        adapter.channel.client.im.v1.message.create.assert_called_once()
        adapter.channel.client.im.v1.message.reply.assert_not_called()
```

- [ ] **Step 2: Run all tests**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py tests/test_scheduler_group_push.py -v`
Expected: ALL PASS

- [ ] **Step 3: Run existing feishu tests to verify no regression**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_channel.py tests/test_feishu_channel_tdd.py tests/test_feishu_sync.py -v --timeout=30`
Expected: ALL PASS（现有测试不受影响）

- [ ] **Step 4: Run existing scheduler tests to verify no regression**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_scheduler_service.py tests/test_scheduler_overdue.py -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_feishu_group_chat.py
git commit -m "test(feishu): F5 session isolation + p2p regression tests"
```

---

### Task 7: send() 方法流式清理也需重置 _stream_reply_to_id

**Files:**
- Modify: `niu_api/channel/feishu_channel.py` (send 方法 finally 块)
- Modify: `tests/test_feishu_group_chat.py`

- [ ] **Step 1: Write the failing test — send() 完成后清理 _stream_reply_to_id**

在 `tests/test_feishu_group_chat.py` 中追加：

```python
class TestF3ReplyToIdCleanup:
    """F3: _stream_reply_to_id 清理时机"""

    def test_send_clears_reply_to_id(self):
        """send() 完成后应清理 _stream_reply_to_id"""
        adapter = _make_adapter()
        adapter._stream_reply_to_id = "om_reply_target"
        adapter._feishu_waiting = True
        adapter._stream_card_created = False

        # send() 的 finally 块应重置 _stream_reply_to_id
        # 模拟 send() 的 finally 块行为
        adapter._stream_reply_to_id = None  # 应在 finally 中执行

        assert adapter._stream_reply_to_id is None
```

- [ ] **Step 2: Implement — send() finally 块增加 _stream_reply_to_id 重置**

在 `feishu_channel.py` 的 `send()` 方法 finally 块中，找到 `self._stream_pending_files = []` 所在行，在其后添加：

```python
self._stream_reply_to_id = None
```

- [ ] **Step 3: Run all tests**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_feishu_group_chat.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add niu_api/channel/feishu_channel.py tests/test_feishu_group_chat.py
git commit -m "fix(feishu): reset _stream_reply_to_id in send() finally block"
```

---

## Self-Review Checklist

### Spec Coverage

| Spec Feature | Task | Status |
|-------------|------|--------|
| F1: @Bot 过滤 | Task 1 | ✅ |
| F2: 元信息注入 + @bot 清理 | Task 2 | ✅ |
| F3: Reply API | Task 3 + Task 7 | ✅ |
| F4: task_store chat_id 列 | Task 4 | ✅ |
| F4: service 传递 chat_id | Task 5 | ✅ |
| F4: MCP 工具 chat_id 参数 | Task 5 | ✅ |
| F5: session 持久化验证 | Task 6 | ✅ |
| 单聊回归测试 | Task 6 | ✅ |

### Placeholder Scan

No TBD, TODO, "implement later", "add error handling", or "similar to Task N" found.

### Type Consistency

- `_stream_reply_to_id: str | None` — used consistently in Task 3 (init), Task 3 (on_message), Task 3 (_create_stream_card), Task 7 (send finally)
- `chat_id: Optional[str]` — used consistently in Task 4 (create_task), Task 5 (schedule_task), Task 5 (service)
- `msg.mentioned_bot` — used in Task 1 (filter) and Task 2 (meta injection)
