# 飞书全功能对接 Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将飞书作为 IM 通道接入个人 AI 助理，实现消息双向收发和日历/任务单向同步。

**Architecture:** 通道抽象层（ChannelRouter）统一 Electron 和飞书消息路由；飞书 IM 通过 FeishuChannel WebSocket 长连接接入，Agent 无感知；日历/任务通过同步钩子单向写入飞书；认证配置存储在 preferences.json。

**Tech Stack:** Python + lark-oapi SDK (v1.6.5, 含 FeishuChannel WebSocket 支持)

**设计文档:** `docs/superpowers/specs/2026-05-18-feishu-integration-design.md`

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|------|------|
| `niu_api/channel/__init__.py` | ChannelRouter + UnifiedMessage |
| `niu_api/channel/base.py` | UnifiedMessage 数据类 + ChannelAdapter 接口 |
| `niu_api/channel/electron_channel.py` | Electron 通道适配器（包装现有 SSE 推送） |
| `niu_api/channel/feishu_channel.py` | 飞书通道适配器（FeishuChannel WebSocket） |
| `mcp-servers/feishu-server/pyproject.toml` | 飞书 MCP 服务器项目配置 |
| `mcp-servers/feishu-server/src/niu_feishu_server/__init__.py` | TOOL_SCHEMAS + 工具函数 + get_tool_schemas |
| `mcp-servers/feishu-server/src/niu_feishu_server/__main__.py` | MCP stdio 入口点 |
| `mcp-servers/feishu-server/src/niu_feishu_server/client.py` | 飞书 API 客户端（lark-oapi 封装） |
| `mcp-servers/feishu-server/src/niu_feishu_server/sync.py` | 日历/任务同步逻辑 |
| `mcp-servers/feishu-server/src/niu_feishu_server/converter.py` | cron → RRULE 转换器 |
| `config/disk/feishu-server.yaml` | 飞书工具 disk 虚拟磁盘配置 |
| `docs/feishu-app-setup-guide.md` | 飞书应用创建指南 |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `niu_api/__main__.py` | lifespan 中启动飞书通道 |
| `config/mcp-servers.yaml` | 添加 feishu-server 配置 |
| `agent/mcp_loader.py` | 添加 OPTIONAL_SERVERS 机制 + feishu-server |
| `niu_api/internal/scheduler/task_store.py` | 添加 feishu_event_id 列迁移 |
| `niu_api/internal/scheduler/service.py` | trigger_callback 增加飞书推送 |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | schedule_task/cancel_task/update_task 增加飞书同步钩子 |

---

## Task 1: 飞书应用创建指南

**Files:**
- Create: `docs/feishu-app-setup-guide.md`

- [ ] **Step 1: 编写飞书应用创建指南**

```markdown
# 飞书应用创建指南

## 1. 创建自建应用

1. 登录 [飞书开放平台](https://open.feishu.cn/app)
2. 点击「创建企业自建应用」
3. 填写应用名称（如"妞妞 AI 助理"）和描述
4. 记录 `App ID` 和 `App Secret`

## 2. 开启机器人能力

1. 进入应用 → 「添加应用能力」→ 勾选「机器人」
2. 在「事件与回调」→「事件配置」中：
   - 请求方式选择 **长连接（WebSocket）**
   - 不需要配置 Encrypt Key（SDK 自动处理）

## 3. 配置权限

在「权限管理」→「API 权限」中开通以下 scope：

| scope | 用途 |
|-------|------|
| `im:message` | 接收消息 |
| `im:message:send_as_bot` | 发送消息 |
| `calendar:calendar` | 日历读写 |
| `calendar:calendar:readonly` | 日历只读 |
| `docx:document` | 文档读写 |
| `drive:drive` | 云盘读写 |
| `drive:drive:readonly` | 云盘只读 |
| `mail:mail` | 邮件读写发送 |
| `contact:user.base:readonly` | 通讯录只读 |

## 4. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 提交审核（企业自建应用通常自动通过）
3. 审核通过后，应用即可使用

## 5. 配置妞妞

在 `~/.niu/preferences.json` 中添加：

```json
{
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "enabled": true,
    "sync": {
      "calendar": true,
      "task": true
    }
  }
}
```

重启妞妞即可生效。
```

- [ ] **Step 2: Commit**

```bash
git add docs/feishu-app-setup-guide.md
git commit -m "docs: 飞书应用创建指南"
```

---

## Task 2: UnifiedMessage 数据类 + ChannelAdapter 接口

**Files:**
- Create: `niu_api/channel/__init__.py`
- Create: `niu_api/channel/base.py`

- [ ] **Step 1: 创建 `niu_api/channel/base.py`**

```python
"""通道抽象层 — UnifiedMessage 数据类 + ChannelAdapter 接口"""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Any


@dataclass
class UnifiedMessage:
    """统一消息格式，所有通道的消息都转换为此格式"""
    content: str          # 消息文本内容
    channel: str          # "electron" / "feishu" / (未来其他)
    channel_id: str       # 会话标识（飞书 chat_id / Electron session_id）
    sender_id: str        # 发送者标识
    message_type: str     # "text" / "image" / "file" / "post"
    resources: list = field(default_factory=list)  # 附件/媒体信息
    raw: dict = field(default_factory=dict)         # 原始事件数据


class ChannelAdapter(ABC):
    """通道适配器接口"""

    @abstractmethod
    async def send(self, channel_id: str, content: str) -> None:
        """发送消息到指定会话"""

    @abstractmethod
    async def push(self, channel_id: str, content: str) -> None:
        """主动推送（定时提醒等）"""
```

- [ ] **Step 2: 创建 `niu_api/channel/__init__.py`**

```python
"""通道抽象层 — ChannelRouter + UnifiedMessage"""

import asyncio
from typing import Dict, Optional, Callable
from loguru import logger

from .base import UnifiedMessage, ChannelAdapter


class ChannelRouter:
    """统一消息路由器 — 所有通道的消息统一交给 Agent 处理"""

    def __init__(self):
        self.channels: Dict[str, ChannelAdapter] = {}
        self._agent_runner = None  # 延迟注入，避免循环依赖

    def set_agent_runner(self, runner):
        """由 niu_api 启动时注入 NiuRunner 实例"""
        self._agent_runner = runner

    async def route_in(self, message: UnifiedMessage) -> str:
        """所有通道的消息统一交给 Agent 处理"""
        if self._agent_runner is None:
            raise RuntimeError("Agent runner not initialized")

        # runner.chat() 是同步方法，需要在线程池中执行
        reply = await asyncio.to_thread(self._chat_sync, message.content)
        return reply

    def _chat_sync(self, message: str) -> str:
        """同步调用 Agent（在线程池中执行，与 _chat_lock 共享同一把锁）"""
        import requests
        main_url = "http://127.0.0.1:9876"
        try:
            resp = requests.post(
                f"{main_url}/chat/sync",
                json={"session_id": "default", "message": message},
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

    async def route_out(self, reply: str, channel: str, channel_id: str) -> None:
        """回复投递到指定通道"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.send(channel_id, reply)

    async def push(self, content: str, channel: str, channel_id: str) -> None:
        """主动推送（定时提醒等）"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.push(channel_id, content)

    def register(self, name: str, adapter: ChannelAdapter) -> None:
        """注册通道适配器"""
        self.channels[name] = adapter
        logger.info(f"[ChannelRouter] Registered channel: {name}")

    def has_channel(self, name: str) -> bool:
        """检查通道是否已注册"""
        return name in self.channels


# 全局单例
_router: Optional[ChannelRouter] = None


def get_channel_router() -> ChannelRouter:
    """获取全局 ChannelRouter 实例"""
    global _router
    if _router is None:
        _router = ChannelRouter()
    return _router
```

- [ ] **Step 3: Commit**

```bash
git add niu_api/channel/__init__.py niu_api/channel/base.py
git commit -m "feat: 通道抽象层 — UnifiedMessage + ChannelRouter"
```

---

## Task 3: Electron 通道适配器

**Files:**
- Create: `niu_api/channel/electron_channel.py`

- [ ] **Step 1: 创建 Electron 通道适配器**

```python
"""Electron 通道适配器 — 包装现有 SSE 推送"""

from loguru import logger
from .base import ChannelAdapter


class ElectronChannelAdapter(ChannelAdapter):
    """Electron 通道 — 消息已通过 SSE 推送到前端，此适配器主要用于 push"""

    async def send(self, channel_id: str, content: str) -> None:
        """Electron 的消息回复已由 chat_sync/chat 端点自动推送到 SSE"""
        # Electron 通道的回复由 /chat 和 /chat/sync 端点自动通过 SSE 推送
        # 此处无需额外操作
        logger.debug(f"[ElectronChannel] send() called — reply already pushed via SSE")

    async def push(self, channel_id: str, content: str) -> None:
        """主动推送 — 通过 SSE 事件总线推送"""
        from niu_api.chat import notify_new_message_sync
        import uuid

        msg_id = str(uuid.uuid4())
        notify_new_message_sync(msg_id, "assistant", content)
        logger.debug(f"[ElectronChannel] push() — sent via SSE (id={msg_id[:8]})")
```

- [ ] **Step 2: Commit**

```bash
git add niu_api/channel/electron_channel.py
git commit -m "feat: Electron 通道适配器"
```

---

## Task 4: 飞书通道适配器

**Files:**
- Create: `niu_api/channel/feishu_channel.py`

- [ ] **Step 1: 创建飞书通道适配器**

```python
"""飞书通道适配器 — 基于 lark-oapi FeishuChannel WebSocket 长连接"""

from loguru import logger
from .base import UnifiedMessage, ChannelAdapter, ChannelRouter


class FeishuChannelAdapter(ChannelAdapter):
    """飞书通道 — WebSocket 长连接，消息收发，Agent 无感知"""

    def __init__(self, app_id: str, app_secret: str, channel_router: ChannelRouter):
        from lark_oapi.channel import FeishuChannel

        self.channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
        self.router = channel_router
        self._user_p2p_chat_id = None  # 用户P2P会话ID，用于主动推送

        # 注册事件处理器
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)

    async def _on_message(self, msg):
        """处理飞书消息事件"""
        try:
            unified = UnifiedMessage(
                content=msg.content_text or "",
                channel="feishu",
                channel_id=msg.chat_id,
                sender_id=msg.sender_id,
                message_type=msg.raw_content_type or "text",
                resources=msg.resources or [],
                raw=msg.raw or {},
            )

            if not unified.content.strip():
                logger.debug("[FeishuChannel] Empty message, skipping")
                return

            # 记录 P2P chat_id 用于主动推送
            if not self._user_p2p_chat_id:
                self._user_p2p_chat_id = msg.chat_id

            logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

            # 交给 ChannelRouter → Agent 处理
            reply = await self.router.route_in(unified)
            if reply:
                await self.channel.send(msg.chat_id, {"markdown": reply})
                logger.info(f"[FeishuChannel] Replied: {reply[:50]}...")

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    async def _on_card_action(self, action):
        """处理卡片交互事件（Phase 4 实现）"""
        logger.debug(f"[FeishuChannel] Card action received (not implemented yet)")

    async def _on_reconnecting(self, _):
        """WebSocket 重连中"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    async def _on_reconnected(self, _):
        """WebSocket 重连成功"""
        logger.info("[FeishuChannel] WebSocket reconnected")

    async def start(self) -> None:
        """启动 WebSocket 长连接"""
        await self.channel.connect_until_ready(timeout=30)
        logger.info("[FeishuChannel] WebSocket connected")

    async def send(self, chat_id: str, content: str) -> None:
        """发送消息到飞书"""
        await self.channel.send(chat_id, {"markdown": content})

    async def push(self, chat_id: str, content: str) -> None:
        """主动推送（定时提醒等）"""
        # 优先使用记录的 P2P chat_id
        target = chat_id or self._user_p2p_chat_id
        if target:
            await self.channel.send(target, {"markdown": content})
        else:
            logger.warning("[FeishuChannel] No chat_id for push, skipping")

    @property
    def user_p2p_chat_id(self) -> str | None:
        """获取用户 P2P 会话 ID"""
        return self._user_p2p_chat_id
```

- [ ] **Step 2: Commit**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "feat: 飞书通道适配器 — FeishuChannel WebSocket"
```

---

## Task 5: 飞书通道启动集成

**Files:**
- Modify: `niu_api/__main__.py:49-271` (lifespan 函数)

- [ ] **Step 1: 在 lifespan 中注册 Electron 通道**

在 `niu_api/__main__.py` 的 `lifespan()` 函数中，在步骤 6（Mark preload as complete）之后添加通道初始化代码：

```python
    # 6.1. Initialize channel router
    from niu_api.channel import get_channel_router
    from niu_api.channel.electron_channel import ElectronChannelAdapter

    channel_router = get_channel_router()
    channel_router.register("electron", ElectronChannelAdapter())
    logger.info("Channel router initialized (electron channel registered)")
```

插入位置：在行 100（`set_preload_complete()`）之后，行 103（`Save main event loop`）之前。

- [ ] **Step 2: 在 lifespan 中启动飞书通道**

在步骤 6.1 之后添加飞书通道启动代码：

```python
    # 6.2. Start Feishu channel (if configured)
    try:
        import json
        from pathlib import Path
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            feishu_config = prefs.get("feishu", {})
            if feishu_config.get("enabled"):
                from niu_api.channel.feishu_channel import FeishuChannelAdapter

                feishu_adapter = FeishuChannelAdapter(
                    app_id=feishu_config["app_id"],
                    app_secret=feishu_config["app_secret"],
                    channel_router=channel_router,
                )
                channel_router.register("feishu", feishu_adapter)
                # WebSocket 连接在后台启动，不阻塞 lifespan
                asyncio.create_task(feishu_adapter.start())
                logger.info("Feishu channel starting (WebSocket)")
            else:
                logger.info("Feishu channel disabled (not enabled in preferences)")
        else:
            logger.debug("No preferences.json, Feishu channel skipped")
    except Exception as e:
        logger.warning(f"Feishu channel setup failed: {e}")
        # 通道启动失败不影响主服务
```

- [ ] **Step 3: 在 shutdown 中停止飞书通道**

在 `lifespan()` 的 shutdown 部分（行 247-270），在 `stop_scheduler()` 之前添加：

```python
    # 停止飞书通道
    try:
        from niu_api.channel import get_channel_router
        router = get_channel_router()
        feishu_adapter = router.channels.get("feishu")
        if feishu_adapter and hasattr(feishu_adapter.channel, 'disconnect'):
            await feishu_adapter.channel.disconnect()
            logger.info("Feishu channel disconnected")
    except Exception as e:
        logger.warning(f"Failed to disconnect Feishu channel: {e}")
```

- [ ] **Step 4: Commit**

```bash
git add niu_api/__main__.py
git commit -m "feat: 集成飞书通道到 API 启动流程"
```

---

## Task 6: 飞书 MCP 服务器骨架

**Files:**
- Create: `mcp-servers/feishu-server/pyproject.toml`
- Create: `mcp-servers/feishu-server/src/niu_feishu_server/__init__.py`
- Create: `mcp-servers/feishu-server/src/niu_feishu_server/__main__.py`
- Create: `mcp-servers/feishu-server/src/niu_feishu_server/client.py`

- [ ] **Step 1: 创建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "niu_feishu_server"
version = "0.1.0"
description = "Feishu MCP Server for Niu Agent"
requires-python = ">=3.11"
dependencies = [
    "lark-oapi>=1.6.5",
]

[tool.setuptools.packages.find]
where = ["."]
```

- [ ] **Step 2: 创建 `client.py` — 飞书 API 客户端**

```python
"""飞书 API 客户端 — lark-oapi SDK 封装"""

import json
from pathlib import Path
from typing import Optional
from loguru import logger


def _load_feishu_config() -> dict:
    """从 preferences.json 加载飞书配置"""
    prefs_path = Path.home() / ".niu" / "preferences.json"
    if not prefs_path.exists():
        return {}
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        return prefs.get("feishu", {})
    except Exception as e:
        logger.warning(f"Failed to load feishu config: {e}")
        return {}


def get_feishu_client():
    """获取飞书 API 客户端（tenant_access_token 自动管理）"""
    import lark_oapi as lark

    config = _load_feishu_config()
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")

    if not app_id or not app_secret:
        raise ValueError("Feishu app_id/app_secret not configured")

    client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .build()

    return client


def feishu_sync_enabled() -> bool:
    """检查飞书日历同步是否启用"""
    config = _load_feishu_config()
    return config.get("enabled", False) and config.get("sync", {}).get("calendar", False)
```

- [ ] **Step 3: 创建 `__init__.py` — Phase 1 最小 TOOL_SCHEMAS**

```python
"""飞书 MCP 服务器 — Phase 1 最小工具集（日历同步相关）"""

import json
from loguru import logger

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "feishu_calendar_create": {
        "name": "feishu_calendar_create",
        "description": """创建飞书日历事件（由同步钩子调用，不暴露给主 Agent）。

参数：
- summary: 事件标题
- start_time: 开始时间（ISO格式）
- end_time: 结束时间（ISO格式）
- description: 事件描述（可选）
- recurrence: RRULE 重复规则（可选）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "事件标题"},
                "start_time": {"type": "string", "description": "开始时间（ISO格式）"},
                "end_time": {"type": "string", "description": "结束时间（ISO格式）"},
                "description": {"type": "string", "description": "事件描述"},
                "recurrence": {"type": "string", "description": "RRULE 重复规则"},
            },
            "required": ["summary", "start_time", "end_time"]
        },
    },
    "feishu_calendar_cancel": {
        "name": "feishu_calendar_cancel",
        "description": """取消飞书日历事件。

参数：
- event_id: 飞书日历事件ID""",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "飞书日历事件ID"}
            },
            "required": ["event_id"]
        },
    },
    "feishu_calendar_update": {
        "name": "feishu_calendar_update",
        "description": """更新飞书日历事件。

参数：
- event_id: 飞书日历事件ID
- summary: 新标题（可选）
- start_time: 新开始时间（可选）
- end_time: 新结束时间（可选）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "飞书日历事件ID"},
                "summary": {"type": "string", "description": "新标题"},
                "start_time": {"type": "string", "description": "新开始时间"},
                "end_time": {"type": "string", "description": "新结束时间"},
            },
            "required": ["event_id"]
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# ============== 同步工具函数（供 scheduler-server 调用） ==============

def feishu_calendar_create(summary: str, start_time: str, end_time: str,
                           description: str = "", recurrence: str = "") -> str:
    """创建飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import CreateEventRequest, CreateEventRequestBody

        client = get_feishu_client()

        body = CreateEventRequestBody.builder() \
            .summary(summary) \
            .description(description) \
            .start_time({"timestamp": _iso_to_timestamp(start_time)}) \
            .end_time({"timestamp": _iso_to_timestamp(end_time)}) \
            .build()

        if recurrence:
            body.recurrence = [recurrence]

        # 使用主日历
        request = CreateEventRequest.builder() \
            .calendar_id("primary") \
            .request_body(body) \
            .build()

        response = client.calendar.v4.event.create(request)

        if response.success():
            event_id = response.data.event.event_id
            logger.info(f"[Feishu] Calendar event created: {event_id}")
            return json.dumps({"status": "success", "event_id": event_id})
        else:
            logger.error(f"[Feishu] Calendar create failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar create error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def feishu_calendar_cancel(event_id: str) -> str:
    """取消飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import DeleteEventRequest

        client = get_feishu_client()

        request = DeleteEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .build()

        response = client.calendar.v4.event.delete(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event cancelled: {event_id}")
            return json.dumps({"status": "success"})
        else:
            logger.error(f"[Feishu] Calendar cancel failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar cancel error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def feishu_calendar_update(event_id: str, summary: str = None,
                           start_time: str = None, end_time: str = None) -> str:
    """更新飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import PatchEventRequest, PatchEventRequestBody

        client = get_feishu_client()

        body_builder = PatchEventRequestBody.builder()
        if summary:
            body_builder = body_builder.summary(summary)
        if start_time:
            body_builder = body_builder.start_time({"timestamp": _iso_to_timestamp(start_time)})
        if end_time:
            body_builder = body_builder.end_time({"timestamp": _iso_to_timestamp(end_time)})

        request = PatchEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .request_body(body_builder.build()) \
            .build()

        response = client.calendar.v4.event.patch(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event updated: {event_id}")
            return json.dumps({"status": "success"})
        else:
            logger.error(f"[Feishu] Calendar update failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar update error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def _iso_to_timestamp(iso_str: str) -> int:
    """ISO 时间字符串 → Unix 时间戳（秒）"""
    from datetime import datetime
    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp())
```

- [ ] **Step 4: 创建 `__main__.py`**

```python
"""飞书 MCP 服务器入口点"""

from niu_feishu_server import main

if __name__ == "__main__":
    main()
```

注意：`main()` 函数需要在 `__init__.py` 中定义（MCP stdio 模式，Phase 1 不需要独立运行，但保留入口点）。

在 `__init__.py` 末尾添加：

```python
def main():
    """MCP stdio 入口点（Phase 1 不需要独立运行，保留入口）"""
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    import asyncio

    server = Server("feishu-server")

    @server.list_tools()
    async def list_tools():
        return [Tool(**schema) for schema in get_tool_schemas()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name in TOOL_SCHEMAS:
            fn = globals().get(name)
            if fn:
                result = fn(**arguments)
                return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    asyncio.run(server.run())
```

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/feishu-server/
git commit -m "feat: 飞书 MCP 服务器骨架 — 日历同步工具"
```

---

## Task 7: cron → RRULE 转换器

**Files:**
- Create: `mcp-servers/feishu-server/src/niu_feishu_server/converter.py`

- [ ] **Step 1: 创建 cron → RRULE 转换器**

```python
"""cron → RRULE 转换器（单向）

将 5 字段 cron 表达式转换为 RFC5545 RRULE 格式。
不支持的 cron 模式降级为单次事件。
"""

import re
from loguru import logger


# 星期映射：cron(0-6, 0=Sunday) → RRULE(2-letter day codes)
_CRON_DOW_TO_RRULE = {
    0: "SU", 1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA"
}


def cron_to_rrule(cron_expr: str) -> str | None:
    """5 字段 cron → RFC5545 RRULE（单向）

    支持的模式：
    - "0 9 * * *"     → FREQ=DAILY;BYHOUR=9;BYMINUTE=0
    - "0 9 * * 1-5"   → FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0
    - "30 8 * * 1,3,5" → FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=30

    不支持的模式（返回 None，调用方应降级为单次事件）：
    - 步进表达式（*/5, 1-10/2）
    - L（最后一天）
    - #N（第N个星期几）
    - 月/日字段非 *

    Args:
        cron_expr: 5 字段 cron 表达式

    Returns:
        RRULE 字符串，或 None（不支持）
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        logger.warning(f"[converter] Invalid cron (expected 5 fields): {cron_expr}")
        return None

    minute, hour, dom, month, dow = parts

    # 月和日字段必须为 *（不支持特定日期/月份）
    if month != "*" or dom != "*":
        logger.warning(f"[converter] cron with dom/month not supported: {cron_expr}")
        return None

    # 解析分钟和小时（不支持步进）
    if "/" in minute or "/" in hour:
        logger.warning(f"[converter] cron with step not supported: {cron_expr}")
        return None

    try:
        minute_val = _parse_field(minute, 0, 59)
        hour_val = _parse_field(hour, 0, 23)
    except ValueError as e:
        logger.warning(f"[converter] Invalid minute/hour in cron: {cron_expr} ({e})")
        return None

    # 构建 RRULE
    parts_list = []

    if dow == "*":
        # 每天
        parts_list.append("FREQ=DAILY")
    else:
        # 指定星期
        try:
            dow_vals = _parse_dow_field(dow)
        except ValueError as e:
            logger.warning(f"[converter] Invalid dow in cron: {cron_expr} ({e})")
            return None

        rrule_days = ",".join(_CRON_DOW_TO_RRULE[d] for d in dow_vals)
        parts_list.append("FREQ=WEEKLY")
        parts_list.append(f"BYDAY={rrule_days}")

    # 添加小时和分钟
    if len(hour_val) == 1 and len(minute_val) == 1:
        parts_list.append(f"BYHOUR={hour_val[0]}")
        parts_list.append(f"BYMINUTE={minute_val[0]}")
    else:
        # 多个小时/分钟值
        parts_list.append(f"BYHOUR={','.join(str(h) for h in hour_val)}")
        parts_list.append(f"BYMINUTE={','.join(str(m) for m in minute_val)}")

    rrule = ";".join(parts_list)
    logger.debug(f"[converter] {cron_expr} → {rrule}")
    return rrule


def _parse_field(field: str, min_val: int, max_val: int) -> list[int]:
    """解析 cron 字段（支持逗号分隔和范围）"""
    if field == "*":
        return list(range(min_val, max_val + 1))

    values = []
    for part in field.split(","):
        if "-" in part:
            start, end = part.split("-")
            values.extend(range(int(start), int(end) + 1))
        else:
            values.append(int(part))

    return sorted(set(values))


def _parse_dow_field(dow: str) -> list[int]:
    """解析星期字段（0-6, 0=Sunday）"""
    values = []
    for part in dow.split(","):
        if "-" in part:
            start, end = part.split("-")
            values.extend(range(int(start), int(end) + 1))
        else:
            values.append(int(part))

    return sorted(set(v % 7 for v in values))  # 7 → 0 (Sunday)
```

- [ ] **Step 2: Commit**

```bash
git add mcp-servers/feishu-server/src/niu_feishu_server/converter.py
git commit -m "feat: cron → RRULE 转换器（单向）"
```

---

## Task 8: 日历同步逻辑

**Files:**
- Create: `mcp-servers/feishu-server/src/niu_feishu_server/sync.py`

- [ ] **Step 1: 创建日历同步逻辑**

```python
"""飞书日历/任务同步逻辑 — 本地→飞书单向写入"""

from datetime import datetime, timedelta
from loguru import logger

from .converter import cron_to_rrule
from .client import feishu_sync_enabled


def sync_task_to_feishu(task: dict) -> str | None:
    """将本地任务同步到飞书日历

    Args:
        task: 任务字典，包含 id, content, scheduled_at, event_type, cron_expr 等

    Returns:
        飞书日历事件 ID，或 None（同步失败或未启用）
    """
    if not feishu_sync_enabled():
        return None

    try:
        from . import feishu_calendar_create

        summary = task.get("content", "")
        start_time = task.get("scheduled_at", "")

        # 计算结束时间（默认开始时间 + 1 小时）
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = start_dt + timedelta(hours=1)
            end_time = end_dt.isoformat()
        except (ValueError, TypeError):
            logger.warning(f"[sync] Invalid start_time: {start_time}")
            return None

        # 转换 cron → RRULE
        recurrence = ""
        cron_expr = task.get("cron_expr")
        if cron_expr:
            recurrence = cron_to_rrule(cron_expr) or ""
            if cron_expr and not recurrence:
                logger.warning(
                    f"[sync] cron '{cron_expr}' cannot convert to RRULE, "
                    f"creating single event instead"
                )

        # 根据事件类型设置可见性描述
        event_type = task.get("event_type", "reminder")
        description = f"同步自妞妞 AI 助理（类型: {event_type}）"

        result_json = feishu_calendar_create(
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            recurrence=recurrence,
        )

        import json
        result = json.loads(result_json)

        if result.get("status") == "success":
            event_id = result.get("event_id")
            logger.info(f"[sync] Task synced to Feishu: task={task.get('id')}, event={event_id}")
            return event_id
        else:
            logger.warning(f"[sync] Feishu calendar create failed: {result.get('message')}")
            return None

    except Exception as e:
        logger.error(f"[sync] sync_task_to_feishu error: {e}")
        return None


def cancel_feishu_event(event_id: str) -> bool:
    """取消飞书日历事件

    Args:
        event_id: 飞书日历事件 ID

    Returns:
        是否成功
    """
    if not feishu_sync_enabled():
        return True  # 未启用，视为成功

    try:
        from . import feishu_calendar_cancel
        import json

        result_json = feishu_calendar_cancel(event_id=event_id)
        result = json.loads(result_json)

        if result.get("status") == "success":
            logger.info(f"[sync] Feishu event cancelled: {event_id}")
            return True
        else:
            logger.warning(f"[sync] Feishu event cancel failed: {result.get('message')}")
            return False

    except Exception as e:
        logger.error(f"[sync] cancel_feishu_event error: {e}")
        return False


def update_feishu_event(event_id: str, task: dict) -> bool:
    """更新飞书日历事件

    Args:
        event_id: 飞书日历事件 ID
        task: 更新后的任务字典

    Returns:
        是否成功
    """
    if not feishu_sync_enabled():
        return True  # 未启用，视为成功

    try:
        from . import feishu_calendar_update
        import json

        kwargs = {"event_id": event_id}

        if "content" in task:
            kwargs["summary"] = task["content"]
        if "scheduled_at" in task:
            kwargs["start_time"] = task["scheduled_at"]
            try:
                start_dt = datetime.fromisoformat(task["scheduled_at"])
                end_dt = start_dt + timedelta(hours=1)
                kwargs["end_time"] = end_dt.isoformat()
            except (ValueError, TypeError):
                pass

        result_json = feishu_calendar_update(**kwargs)
        result = json.loads(result_json)

        if result.get("status") == "success":
            logger.info(f"[sync] Feishu event updated: {event_id}")
            return True
        else:
            logger.warning(f"[sync] Feishu event update failed: {result.get('message')}")
            return False

    except Exception as e:
        logger.error(f"[sync] update_feishu_event error: {e}")
        return False
```

- [ ] **Step 2: Commit**

```bash
git add mcp-servers/feishu-server/src/niu_feishu_server/sync.py
git commit -m "feat: 飞书日历同步逻辑 — 本地→飞书单向写入"
```

---

## Task 9: 数据库迁移 — feishu_event_id 字段

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py:17-59` (_init_db 方法)
- Modify: `niu_api/internal/scheduler/task_store.py:61-85` (create_task 方法)
- Modify: `niu_api/internal/scheduler/task_store.py:87-126` (list_tasks 方法)
- Modify: `niu_api/internal/scheduler/task_store.py:177-237` (update_task 方法)
- Modify: `niu_api/internal/scheduler/task_store.py:239-268` (get_task 方法)

- [ ] **Step 1: 在 `_init_db()` 中添加 feishu_event_id 迁移**

在 `task_store.py` 行 56（name 列迁移）之后添加：

```python
            # 迁移：老数据库可能没有 feishu_event_id 列
            try:
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN feishu_event_id TEXT
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
```

- [ ] **Step 2: 更新 `create_task()` — 支持 feishu_event_id**

修改 `create_task()` 方法签名和 SQL：

```python
    def create_task(
        self,
        content: str,
        scheduled_at: str,
        event_type: str = "reminder",
        is_recurring: bool = False,
        cron_expr: Optional[str] = None,
        name: Optional[str] = None,
        feishu_event_id: Optional[str] = None
    ) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO scheduled_tasks
                (id, content, scheduled_at, is_recurring, cron_expr, event_type, status, name, feishu_event_id)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type, name, feishu_event_id))
            conn.commit()
        finally:
            conn.close()

        return task_id
```

- [ ] **Step 3: 更新 `list_tasks()` — 返回 feishu_event_id**

修改 SELECT 语句和返回字典，在 `name` 之后添加 `feishu_event_id`：

所有 `SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name` 改为：
`SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, feishu_event_id`

返回字典添加 `"feishu_event_id": row[10]`

- [ ] **Step 4: 更新 `update_task()` — 支持 feishu_event_id**

在 `update_task()` 方法中添加 `feishu_event_id` 参数和更新逻辑：

```python
    def update_task(
        self,
        task_id: str,
        content: Optional[str] = None,
        scheduled_at: Optional[str] = None,
        cron_expr: Optional[str] = None,
        status: Optional[str] = None,
        expected_status: Optional[str] = None,
        name: Optional[str] = None,
        feishu_event_id: Optional[str] = None
    ) -> bool:
```

在 updates 列表构建中添加：

```python
        if feishu_event_id is not None:
            updates.append("feishu_event_id = ?")
            params.append(feishu_event_id)
```

- [ ] **Step 5: 更新 `get_task()` 和 `find_task_by_name()` — 返回 feishu_event_id**

同 list_tasks，修改 SELECT 和返回字典。

- [ ] **Step 6: Commit**

```bash
git add niu_api/internal/scheduler/task_store.py
git commit -m "feat: task_store 添加 feishu_event_id 字段"
```

---

## Task 10: scheduler-server 集成飞书同步钩子

**Files:**
- Modify: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py:155-216` (同步工具函数)

- [ ] **Step 1: 在 `schedule_task()` 中添加飞书同步钩子**

在 `schedule_task()` 函数末尾，`return json.dumps(result)` 之前添加：

```python
    # 飞书日历同步钩子
    try:
        from niu_feishu_server.sync import sync_task_to_feishu
        from niu_feishu_server.client import feishu_sync_enabled

        if feishu_sync_enabled():
            feishu_event_id = sync_task_to_feishu({
                "id": task_id,
                "content": content,
                "scheduled_at": scheduled_at,
                "event_type": event_type,
                "cron_expr": cron_expr,
            })
            if feishu_event_id:
                store.update_task(task_id, feishu_event_id=feishu_event_id)
    except ImportError:
        pass  # feishu-server 未安装，跳过
    except Exception as e:
        logger.warning(f"Feishu sync hook failed in schedule_task: {e}")
```

- [ ] **Step 2: 在 `cancel_task()` 中添加飞书同步钩子**

在 `cancel_task()` 函数中，取消成功后添加：

```python
    # 飞书日历同步钩子 — 取消对应的飞书日历事件
    if success:
        try:
            from niu_feishu_server.sync import cancel_feishu_event
            from niu_feishu_server.client import feishu_sync_enabled

            if feishu_sync_enabled():
                task = store.get_task(task_id)
                if task and task.get("feishu_event_id"):
                    cancel_feishu_event(task["feishu_event_id"])
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Feishu sync hook failed in cancel_task: {e}")
```

- [ ] **Step 3: 在 `update_task()` 中添加飞书同步钩子**

在 `update_task()` 函数中，更新成功后添加：

```python
    # 飞书日历同步钩子 — 更新对应的飞书日历事件
    if success:
        try:
            from niu_feishu_server.sync import update_feishu_event
            from niu_feishu_server.client import feishu_sync_enabled

            if feishu_sync_enabled():
                task = store.get_task(task_id)
                if task and task.get("feishu_event_id"):
                    update_feishu_event(task["feishu_event_id"], {
                        "content": content,
                        "scheduled_at": scheduled_at,
                    })
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Feishu sync hook failed in update_task: {e}")
```

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py
git commit -m "feat: scheduler-server 集成飞书日历同步钩子"
```

---

## Task 11: 定时任务推送增强 — 飞书通道

**Files:**
- Modify: `niu_api/internal/scheduler/service.py:59-134` (trigger_callback 函数)

- [ ] **Step 1: 在 `trigger_callback()` 中添加飞书推送**

在 `trigger_callback()` 函数中，Agent 回复成功后（行 111-118 之间），在 `add_pending_alert("⏰")` 之后添加：

```python
            # 飞书通道推送
            try:
                from niu_api.channel import get_channel_router
                channel_router = get_channel_router()
                if channel_router.has_channel("feishu"):
                    feishu_adapter = channel_router.channels["feishu"]
                    if feishu_adapter.user_p2p_chat_id:
                        import asyncio
                        loop = asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None
                        if loop:
                            asyncio.run_coroutine_threadsafe(
                                channel_router.push(agent_reply, "feishu", feishu_adapter.user_p2p_chat_id),
                                loop
                            )
                        else:
                            logger.debug("[SCHEDULER] No running event loop for Feishu push")
            except Exception as e:
                logger.warning(f"[SCHEDULER] Feishu push failed: {e}")
```

注意：`trigger_callback()` 运行在 scheduler 线程中，不在 asyncio 事件循环中。需要通过 `run_coroutine_threadsafe` 将推送任务注入到主事件循环。

- [ ] **Step 2: Commit**

```bash
git add niu_api/internal/scheduler/service.py
git commit -m "feat: 定时任务推送增强 — 飞书通道"
```

---

## Task 12: MCP 配置注册

**Files:**
- Modify: `config/mcp-servers.yaml` — 添加 feishu-server
- Modify: `agent/mcp_loader.py` — 添加 OPTIONAL_SERVERS 机制

- [ ] **Step 1: 在 `config/mcp-servers.yaml` 中添加 feishu-server**

在 `brain-region-server` 之后添加：

```yaml
feishu-server:
  command: ${PYTHON_PATH}
  args:
  - -m
  - niu_feishu_server
  workdir: mcp-servers/feishu-server/src
  preload: false
  optional: true
  tools:
    feishu_calendar_create:
      visibility: hidden
    feishu_calendar_cancel:
      visibility: hidden
    feishu_calendar_update:
      visibility: hidden
```

关键点：
- `preload: false` — 不在启动时预加载（飞书可能未配置）
- `optional: true` — 加载失败不终止启动
- `visibility: hidden` — 通过 disk() 访问，不暴露给主 Agent

- [ ] **Step 2: 在 `agent/mcp_loader.py` 中添加 OPTIONAL_SERVERS 机制**

在 `REQUIRED_SERVERS` 列表之后添加：

```python
OPTIONAL_SERVERS: List[Tuple[str, str]] = [
    ("feishu-server", "niu_feishu_server"),
]
```

修改 `load_mcp_tools()` 函数，在 REQUIRED_SERVERS 加载之后添加 OPTIONAL_SERVERS 加载逻辑：

```python
    # Load optional servers (failure does not terminate startup)
    optional_servers = [
        ("feishu-server", "niu_feishu_server"),
    ]

    # 也从 YAML 配置中读取 optional 标记
    for server_name, module_name in optional_servers:
        server_config = config.get(server_name, {})
        if not isinstance(server_config, dict) or not server_config:
            logger.debug(f"Optional server {server_name} not configured, skipping")
            continue

        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            visibility_map = None
            if "tools" in server_config:
                visibility_map = server_config["tools"]

            if registry.register_server(server_name, module, visibility_map):
                logger.info(f"Optional server loaded: {server_name}")
            else:
                logger.warning(f"Optional server {server_name} registration failed")

        except ImportError as e:
            logger.debug(f"Optional server {server_name} not available: {e}")
        except Exception as e:
            logger.warning(f"Optional server {server_name} error: {e}")
```

插入位置：在行 137（`logger.info(f"All {len(servers)} servers loaded")`）之后，行 140（`set_registry(registry)`）之前。

- [ ] **Step 3: Commit**

```bash
git add config/mcp-servers.yaml agent/mcp_loader.py
git commit -m "feat: MCP 配置注册 — feishu-server (optional)"
```

---

## Task 13: disk YAML 配置

**Files:**
- Create: `config/disk/feishu-server.yaml`

- [ ] **Step 1: 创建飞书 disk YAML 配置**

```yaml
server: feishu-server
directory: feishu
description: "飞书 — 日历同步"

tools:
  - name: feishu_calendar_create
    category: write
    hidden: true
    short: "创建飞书日历事件"
    long: "创建飞书日历事件（同步钩子调用）"
    parameters:
      - name: summary
        position: 1
        type: string
        required: true
      - name: start_time
        position: 2
        type: string
        required: true
      - name: end_time
        position: 3
        type: string
        required: true
      - name: description
        flag: desc
        type: string
      - name: recurrence
        flag: rrule
        type: string

  - name: feishu_calendar_cancel
    category: admin
    hidden: true
    short: "取消飞书日历事件"
    long: "取消飞书日历事件"
    parameters:
      - name: event_id
        position: 1
        type: string
        required: true

  - name: feishu_calendar_update
    category: write
    hidden: true
    short: "更新飞书日历事件"
    long: "更新飞书日历事件"
    parameters:
      - name: event_id
        position: 1
        type: string
        required: true
      - name: summary
        type: string
      - name: start_time
        type: string
      - name: end_time
        type: string
```

- [ ] **Step 2: Commit**

```bash
git add config/disk/feishu-server.yaml
git commit -m "feat: 飞书 disk YAML 配置"
```

---

## Task 14: 集成测试

**Files:**
- 无新文件（手动端到端测试）

- [ ] **Step 1: 验证 lark-oapi SDK 安装**

```bash
python -c "from lark_oapi.channel import FeishuChannel; print('FeishuChannel OK')"
python -c "import lark_oapi as lark; print(f'lark-oapi version: {lark.__version__}')"
```

Expected: 无 ImportError，版本 >= 1.6.5

- [ ] **Step 2: 验证 feishu-server 模块加载**

```bash
cd mcp-servers/feishu-server/src
python -c "from niu_feishu_server import get_tool_schemas; print(len(get_tool_schemas()), 'tools')"
```

Expected: `3 tools`

- [ ] **Step 3: 验证 cron → RRULE 转换器**

```bash
cd mcp-servers/feishu-server/src
python -c "
from niu_feishu_server.converter import cron_to_rrule
print(cron_to_rrule('0 9 * * *'))
print(cron_to_rrule('0 9 * * 1-5'))
print(cron_to_rrule('30 8 * * 1,3,5'))
print(cron_to_rrule('*/5 * * * *'))  # 不支持，应返回 None
"
```

Expected:
```
FREQ=DAILY;BYHOUR=9;BYMINUTE=0
FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0
FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=30
None
```

- [ ] **Step 4: 验证 task_store 迁移**

```bash
python -c "
from niu_api.internal.scheduler.task_store import TaskStore
store = TaskStore('/tmp/test_feishu_tasks.db')
task_id = store.create_task(content='测试飞书同步', scheduled_at='2026-05-19T09:00:00')
task = store.get_task(task_id)
print('feishu_event_id' in task, task.get('feishu_event_id'))
store.update_task(task_id, feishu_event_id='feishu_evt_123')
task = store.get_task(task_id)
print(task.get('feishu_event_id'))
"
```

Expected:
```
True None
feishu_evt_123
```

- [ ] **Step 5: 验证 ChannelRouter 初始化**

```bash
python -c "
from niu_api.channel import get_channel_router
from niu_api.channel.electron_channel import ElectronChannelAdapter
router = get_channel_router()
router.register('electron', ElectronChannelAdapter())
print('has electron:', router.has_channel('electron'))
print('has feishu:', router.has_channel('feishu'))
"
```

Expected:
```
has electron: True
has feishu: False
```

- [ ] **Step 6: 端到端测试（需要飞书应用配置）**

前提：`~/.niu/preferences.json` 中已配置 `feishu.enabled: true` 和有效的 `app_id`/`app_secret`。

1. 启动应用：`python -m niu_api`
2. 检查日志中是否有 `[FeishuChannel] WebSocket connected`
3. 在飞书中给机器人发私聊消息
4. 检查日志中是否有 `[FeishuChannel] Received: ...` 和 `[FeishuChannel] Replied: ...`
5. 验证飞书中收到机器人回复
6. 创建定时任务，检查日志中是否有 `[sync] Task synced to Feishu`
7. 检查飞书日历中是否出现对应事件

- [ ] **Step 7: Commit 测试结果**

```bash
git commit --allow-empty -m "test: Phase 1 飞书集成端到端测试通过"
```

---

## 自审核清单

### 1. 设计文档覆盖度

| 设计文档章节 | 对应 Task |
|-------------|----------|
| 3.1 UnifiedMessage | Task 2 |
| 3.2 ChannelRouter | Task 2 |
| 3.3 FeishuChannelAdapter | Task 4 |
| 3.4 Electron 通道适配 | Task 3 |
| 3.5 会话映射策略 | Task 4（P2P chat_id 记录） |
| 3.6 定时任务推送增强 | Task 11 |
| 4.1 同步策略 | Task 8 |
| 4.2 实现方式 | Task 10 |
| 4.3 数据库迁移 | Task 9 |
| 4.3 数据映射 | Task 8（sync_task_to_feishu） |
| 4.4 cron→RRULE | Task 7 |
| 5.4 目录结构 | Task 6 |
| 5.5 disk YAML | Task 13 |
| 6.1 认证 | Task 1（指南）+ Task 6（client.py） |
| 6.3 配置存储 | Task 5（preferences.json 读取） |
| 7 代码位置 | Task 2-5 |
| 9 Phase 1 拆分 | Task 1-14 |

### 2. Placeholder 扫描

无 TBD、TODO、implement later 等占位符。

### 3. 类型一致性

- `UnifiedMessage` 在 `base.py` 定义，在 `feishu_channel.py` 和 `channel/__init__.py` 中使用
- `ChannelAdapter` 在 `base.py` 定义，`ElectronChannelAdapter` 和 `FeishuChannelAdapter` 继承
- `ChannelRouter.route_in()` 接受 `UnifiedMessage`，返回 `str`
- `sync_task_to_feishu()` 接受 `dict`，返回 `str | None`
- `feishu_calendar_create/cancel/update()` 返回 `str`（JSON）
- `cron_to_rrule()` 返回 `str | None`
- `TaskStore.create_task()` 新增 `feishu_event_id: Optional[str] = None` 参数
- `TaskStore.update_task()` 新增 `feishu_event_id: Optional[str] = None` 参数
