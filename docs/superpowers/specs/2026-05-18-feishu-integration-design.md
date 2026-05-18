# 飞书全功能对接设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将飞书作为 IM 通道接入个人 AI 助理，同时实现日历/任务同步和业务数据操作（文档/邮件/云盘等）。

**Architecture:** 通道抽象层（ChannelRouter）统一 Electron 和飞书消息路由；飞书 IM 通过 FeishuChannel WebSocket 长连接接入，Agent 无感知；日历/任务通过同步钩子单向写入飞书；业务数据操作通过 `/feishu/` disk 虚拟磁盘路径暴露为 MCP 工具。

**Tech Stack:** Python + lark-oapi SDK (v1.6.5, 含 FeishuChannel WebSocket 支持)

---

## 1. 核心设计原则

1. **IM 是通道，不是工具** — 飞书消息收发是基础设施，Agent 不感知消息来源
2. **日历/任务是同步，不是独立工具** — 本地→飞书单向写入，配置开关控制，不暴露给主 Agent
3. **业务数据操作是 MCP 工具** — 通过 `/feishu/` disk 路径访问，同进程调用
4. **通道抽象层** — ChannelRouter 统一路由，未来可扩展微信/钉钉等

> **本设计替代 `docs/feature-im-integration.md`**。旧设计中的权限控制（allow_from/deny_from）、安全脱敏（sanitize_for_im）、重连机制等概念将在本设计中重新实现。旧设计的 `ChannelPlugin` ABC 接口、`MessageRouter` Pipeline、`SessionManager` + `user_bindings` 表等概念不再使用。

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    LLM (主 Agent)                        │
│                                                         │
│  看到的工具:                                             │
│  - disk(command)  ← 唯一的飞书工具入口                    │
│  - bash, read, write, edit, grep  ← 基础工具            │
│  - brain_region_*  ← static MCP 工具                    │
│                                                         │
│  调用飞书: disk("/feishu/doc_read xxx")                  │
│  调用日历: disk("/scheduler/schedule_task xxx") ← 已有    │
└────────────────────────┬────────────────────────────────┘
                         │
                    disk_engine
                         │
         ┌───────────────┼───────────────┐
         │               │               │
    /feishu/doc_*   /feishu/mail_*   /feishu/drive_*
         │               │               │
    ┌────┴────┐    ┌────┴────┐    ┌─────┴─────┐
    │Python   │    │Python   │    │Python MCP │
    │(lark-oapi)│   │(lark-oapi)│   │(lark-oapi)│
    └─────────┘    └─────────┘    └───────────┘
         │               │               │
    ┌────┴───────────────┴───────────────┴────┐
    │           飞书开放平台 API               │
    └────────────────────────────────────────┘

通道层（Agent 无感知）:
┌─────────────────────────────────────────────┐
│              Channel Router                  │
│                                             │
│  Electron Channel ─┐                       │
│  Feishu Channel  ───┼──→ Agent ──┬──→ Electron │
│  (未来) WeChat   ───┘           └──→ Feishu   │
└─────────────────────────────────────────────┘

日历同步层（透明附加动作）:
用户 → event-manager → scheduled_tasks.db → [飞书日历同步] → 飞书日历
```

## 3. 通道抽象层

### 3.1 UnifiedMessage 数据类

```python
@dataclass
class UnifiedMessage:
    content: str          # 消息文本内容
    channel: str          # "electron" / "feishu" / (未来其他)
    channel_id: str       # 会话标识（飞书 chat_id / Electron session_id）
    sender_id: str        # 发送者标识
    message_type: str     # "text" / "image" / "file" / "post"
    resources: list       # 附件/媒体信息
    raw: dict             # 原始事件数据
```

### 3.2 ChannelRouter

**与现有架构的集成方式**：

现有 `niu_api/chat.py` 的 `chat_sync()` 是同步方法，内部持有 `_chat_lock`。飞书通道不能直接调用 `chat_sync()`（会阻塞 WebSocket 事件循环），需要通过 `asyncio.to_thread` 桥接。

```python
class ChannelRouter:
    def __init__(self):
        self.channels = {}
        self._agent_runner = None  # 延迟注入，避免循环依赖

    def set_agent_runner(self, runner):
        """由 niu_api 启动时注入 GenericAgentRunner 实例"""
        self._agent_runner = runner

    async def route_in(self, message: UnifiedMessage) -> str:
        """所有通道的消息统一交给 Agent 处理"""
        if self._agent_runner is None:
            raise RuntimeError("Agent runner not initialized")
        # runner.chat() 是同步方法，需要在线程池中执行
        reply = await asyncio.to_thread(self._agent_runner.chat, message.content)
        return reply

    async def route_out(self, reply: str, channel: str, channel_id: str):
        """回复投递到指定通道"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.send(channel_id, reply)

    async def push(self, content: str, channel: str, channel_id: str):
        """主动推送（定时提醒等）"""
        adapter = self.channels.get(channel)
        if adapter:
            await adapter.push(channel_id, content)

    def register(self, name: str, adapter):
        self.channels[name] = adapter

    def has_channel(self, name: str) -> bool:
        return name in self.channels
```

**并发安全**：`_chat_lock` 保证同一时间只有一个消息在处理。飞书消息通过 `asyncio.to_thread` 进入同步上下文，与 Electron 通道共享同一把锁，不会并发冲突。

### 3.3 FeishuChannelAdapter

基于 `lark_oapi.channel.FeishuChannel`（v1.6.5 原生支持 WebSocket 长连接）。

```python
class FeishuChannelAdapter:
    def __init__(self, app_id, app_secret, channel_router):
        self.channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
        self.router = channel_router  # 依赖注入
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self._user_p2p_chat_id = None  # 用户P2P会话ID，用于主动推送

    async def _on_message(self, msg):
        unified = UnifiedMessage(
            content=msg.content_text,
            channel="feishu",
            channel_id=msg.chat_id,
            sender_id=msg.sender_id,
            message_type=msg.raw_content_type,
            resources=msg.resources or [],
            raw=msg.raw,
        )
        reply = await self.router.route_in(unified)
        if reply:
            await self.channel.send(msg.chat_id, {"markdown": reply})

    async def _on_reconnecting(self, _):
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    async def _on_reconnected(self, _):
        logger.info("[FeishuChannel] WebSocket reconnected")

    async def start(self):
        """启动 WebSocket 长连接"""
        await self.channel.connect_until_ready(timeout=30)

    async def send(self, chat_id: str, content: str):
        """发送消息到飞书"""
        await self.channel.send(chat_id, {"markdown": content})

    async def push(self, chat_id: str, content: str):
        """主动推送（定时提醒等）"""
        await self.channel.send(chat_id, {"markdown": content})
```

**关键特性**：
- `transport="ws"`（默认）— WebSocket 长连接，无需公网 IP
- 群聊消息需要 @bot 才响应（FeishuChannel PolicyConfig 默认行为）
- P2P 私聊直接响应
- 流式回复用 `channel.stream()` 实现逐 token 输出
- 图片/文件消息通过 `channel.download_resource()` 下载
- 断线重连由 FeishuChannel 内置处理，通过 reconnecting/reconnected 事件监控状态

### 3.4 Electron 通道适配

现有 HTTP/SSE 架构包装为 ElectronChannelAdapter，与 FeishuChannelAdapter 实现相同接口。

### 3.5 会话映射策略

飞书消息与 Electron 消息共享同一个 Agent 会话（`session_id="default"`）。

- 飞书 P2P 私聊：`sender_id` 映射到 `session_id="default"`，与 Electron 共享对话历史
- 飞书群聊：群聊消息也映射到 `session_id="default"`（个人助理场景，群聊中只有用户和 bot）
- 跨通道连续性：用户在飞书问了一个问题，在 Electron 上可以看到完整的对话历史

> 群聊多用户场景（多个不同用户在同一个群中与 bot 交互）不在当前设计范围内，未来如需支持，需引入 `sender_id → session_id` 映射表。

### 3.6 定时任务推送增强

在 `niu_api/internal/scheduler/service.py` 的 `trigger_callback` 中，Agent 回复后同时推送到飞书：

```python
# 现有逻辑：Agent 回复 → SSE → Electron
# 新增：如果配置了飞书通道，同时推送
if channel_router.has_channel("feishu") and user_p2p_chat_id:
    await channel_router.push(agent_reply, "feishu", user_p2p_chat_id)
```

## 4. 日历/任务同步

### 4.1 同步策略

**单向写入**：本地 scheduled_tasks.db → 飞书日历，不双向同步。

理由：
- 双向同步需解决冲突（飞书改了、本地也改了）
- 本地是主数据源，飞书是"镜像"
- 飞书日历变更通知仅作为提醒源，不自动回写本地

### 4.2 实现方式

在 `scheduler-server` 的工具函数中增加飞书同步钩子：

```python
def schedule_task(content, scheduled_at, event_type, ...):
    # 1. 写入本地数据库（原有逻辑）
    task_id = store.create_task(...)

    # 2. 如果配置了飞书同步，同时写入飞书日历
    if feishu_sync_enabled():
        try:
            feishu_event_id = feishu_calendar_sync.create_event(
                summary=content,
                start_time=scheduled_at,
                end_time=calculate_end_time(scheduled_at, event_type),
            )
            # 存储 feishu_event_id 映射（用于后续取消/更新）
            store.update_task_feishu_id(task_id, feishu_event_id)
        except Exception as e:
            logger.warning(f"Feishu calendar sync failed: {e}")
            # 同步失败不影响本地操作

    return {"status": "success", "task_id": task_id}
```

同理，`cancel_task` 和 `update_task` 也增加同步钩子。

### 4.3 数据库迁移

`scheduled_tasks` 表需要新增 `feishu_event_id` 字段（参考 `name` 字段的迁移模式）：

```python
# task_store.py _init_db() 中新增
try:
    cursor.execute("ALTER TABLE scheduled_tasks ADD COLUMN feishu_event_id TEXT")
except sqlite3.OperationalError:
    pass  # 字段已存在
```

同时更新 `create_task()`、`update_task()`、`list_tasks()`、`get_task()` 的 SQL 和返回字典，包含 `feishu_event_id` 字段。

### 4.3 数据映射

| 本地字段 | 飞书日历字段 | 映射规则 |
|---------|------------|---------|
| `content` | `summary` | 直接映射 |
| `scheduled_at` | `start_time` | 直接映射 |
| 无 | `end_time` | 默认 start_time + 1小时 |
| `event_type=meeting` | `visibility=default` | 会议类型 |
| `event_type=reminder` | `visibility=private` | 提醒类型 |
| `cron_expr` | `recurrence` (RRULE) | cron → RRULE 转换 |
| 无 | `reminders` | 默认提前15分钟提醒 |

### 4.4 cron → RRULE 转换（单向）

仅实现 `cron_to_rrule()`，不实现反向转换（与单向写入策略一致）。

```python
def cron_to_rrule(cron_expr: str) -> str:
    """5字段cron → RFC5545 RRULE（单向，不保证所有cron模式可转换）"""
    # "0 9 * * 1-5" → "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"
    # "0 8 * * *"  → "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
    # 不支持的cron模式（如 */5 步进、L 最后、#N 第N个星期几）降级为单次事件
    ...
```

**不支持降级策略**：如果 cron 表达式无法精确转换为 RRULE，则创建单次飞书日历事件（不设置 recurrence），并在日志中记录降级原因。

### 4.5 飞书日历事件通知

**FeishuChannel 不支持日历事件订阅**（仅支持 IM 相关事件：message, cardAction, reaction 等）。

日历变更通知需要使用独立的 `lark_oapi.ws.Client` + `EventDispatcherHandler`：

```python
# niu_api/channel/feishu_calendar_listener.py
from lark_oapi.ws import Client as WSClient
from lark_oapi.event import EventDispatcherHandler

class FeishuCalendarListener:
    def __init__(self, app_id, app_secret, feishu_adapter):
        self.app_id = app_id
        self.app_secret = app_secret
        self.feishu_adapter = feishu_adapter  # 用于推送通知到飞书

    def start(self):
        handler = EventDispatcherHandler.builder("", "") \
            .register_p2_calendar_calendar_changed_v4(self._on_calendar_change) \
            .build()
        ws_client = WSClient(self.app_id, self.app_secret, handler)
        ws_client.start()  # 第二个 WebSocket 连接，专门用于日历事件

    async def _on_calendar_change(self, ctx, event):
        # 仅通知，不自动回写本地
        summary = event.event.summary if event.event else "未知日程"
        await self.feishu_adapter.push(
            self.feishu_adapter._user_p2p_chat_id,
            f"飞书日历变更：{summary}"
        )
```

> 注意：这会建立第二个 WebSocket 连接（第一个是 FeishuChannel 的 IM 连接，第二个是日历事件连接）。两个连接独立运行，互不影响。

## 5. 飞书 MCP 工具（disk 路径）

### 5.1 服务器配置

- **服务器名**：`feishu-server`
- **模块名**：`niu_feishu_server`
- **disk 目录**：`/feishu`
- **所有工具 visibility: hidden**（通过 disk 访问）

### 5.2 Phase 1 工具清单

| disk 路径 | 功能 | 实现方式 |
|-----------|------|---------|
| `/feishu/doc_create` | 创建飞书文档 | Python (lark-oapi) |
| `/feishu/doc_read` | 读取文档内容 | Python |
| `/feishu/doc_update` | 更新文档内容 | Python |
| `/feishu/doc_import_md` | Markdown导入飞书文档 | Python (lark-oapi docx API) |
| `/feishu/doc_export_md` | 飞书文档导出Markdown | Python (lark-oapi docx API) |
| `/feishu/mail_list` | 列出邮件 | Python |
| `/feishu/mail_read` | 读取邮件 | Python |
| `/feishu/mail_send` | 发送邮件 | Python |
| `/feishu/contact_search` | 搜索联系人 | Python |
| `/feishu/drive_upload` | 上传文件 | Python |
| `/feishu/drive_download` | 下载文件 | Python |
| `/feishu/drive_list` | 列出文件 | Python |

### 5.3 Phase 2 工具清单

| disk 路径 | 功能 | 实现方式 |
|-----------|------|---------|
| `/feishu/sheet_read` | 读取表格 | Python |
| `/feishu/sheet_write` | 写入表格 | Python |
| `/feishu/wiki_list_spaces` | 列出知识空间 | Python |
| `/feishu/wiki_read_node` | 读取知识节点 | Python |
| `/feishu/base_list_records` | 列出多维表格记录 | Python |
| `/feishu/base_create_record` | 创建多维表格记录 | Python |

### 5.4 目录结构

```
mcp-servers/feishu-server/
  pyproject.toml
  src/niu_feishu_server/
    __init__.py      — TOOL_SCHEMAS + 工具函数 + call_tool + get_tool_schemas
    __main__.py      — 入口点
    client.py        — 飞书 API 客户端（lark-oapi SDK 封装）
    sync.py          — 日历/任务同步逻辑
    converter.py     — cron → RRULE 转换器（单向）
```

### 5.5 disk YAML 配置

`config/disk/feishu-server.yaml`：

```yaml
server: feishu-server
directory: feishu
description: "飞书 — 文档/邮件/云盘/通讯录/表格/知识库"

tools:
  - name: doc_create
    category: write
    short: "创建飞书文档"
    parameters:
      - name: title
        position: 1
        type: string
        required: true
      - name: content
        flag: content
        type: string
        required: false
  - name: doc_read
    category: read
    short: "读取飞书文档"
    parameters:
      - name: document_id
        position: 1
        type: string
        required: true
  - name: doc_import_md
    category: write
    short: "Markdown导入飞书文档"
    parameters:
      - name: file_path
        position: 1
        type: string
        required: true
      - name: folder_token
        flag: folder
        type: string
        required: false
  - name: doc_export_md
    category: read
    short: "飞书文档导出Markdown"
    parameters:
      - name: document_id
        position: 1
        type: string
        required: true
  - name: mail_list
    category: read
    short: "列出飞书邮件"
    parameters:
      - name: folder
        flag: folder
        type: string
        required: false
      - name: limit
        flag: limit
        type: integer
        required: false
  - name: mail_read
    category: read
    short: "读取飞书邮件"
    parameters:
      - name: mail_id
        position: 1
        type: string
        required: true
  - name: mail_send
    category: write
    short: "发送飞书邮件"
    parameters:
      - name: to
        position: 1
        type: string
        required: true
      - name: subject
        flag: subject
        type: string
        required: true
      - name: content
        flag: content
        type: string
        required: true
  - name: contact_search
    category: read
    short: "搜索飞书联系人"
    parameters:
      - name: query
        position: 1
        type: string
        required: true
  - name: drive_list
    category: read
    short: "列出飞书云盘文件"
    parameters:
      - name: folder_token
        flag: folder
        type: string
        required: false
  - name: drive_upload
    category: write
    short: "上传文件到飞书云盘"
    parameters:
      - name: file_path
        position: 1
        type: string
        required: true
      - name: folder_token
        flag: folder
        type: string
        required: true
  - name: drive_download
    category: read
    short: "从飞书云盘下载文件"
    parameters:
      - name: file_token
        position: 1
        type: string
        required: true
```

## 6. 认证设计

### 6.1 飞书应用认证

需要在飞书开放平台创建一个自建应用，获取 `app_id` 和 `app_secret`。

**所需权限（scope）**：

| 模块 | scope | 用途 |
|------|-------|------|
| IM | `im:message`, `im:message:send_as_bot` | 消息收发 |
| 日历 | `calendar:calendar`, `calendar:calendar:readonly` | 日历读写 |
| 文档 | `docx:document`, `drive:drive` | 文档读写 |
| 邮件 | `mail:mail` | 邮件读写发送 |
| 通讯录 | `contact:user.base:readonly` | 联系人搜索 |
| 云盘 | `drive:drive:readonly`, `drive:drive` | 文件管理 |

### 6.2 Token 管理

- **tenant_access_token**（bot 身份）：lark-oapi SDK 自动管理，无需手动刷新
- **user_access_token**（用户身份）：OAuth Device Flow 获取，存 `~/.niu/lark-token.json`，自动刷新
- 大多数操作用 bot 身份即可

### 6.3 配置存储

`~/.niu/preferences.json` 新增：

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

> **安全说明**：`app_secret` 明文存储在 preferences.json 中。这与现有 `config/user-config.json` 中存储 LLM API Key 的模式一致（均为本地桌面应用，无多用户场景）。未来如需增强安全性，可考虑 macOS Keychain 集成，但 Phase 1 不做。

## 7. 通道基础设施代码位置

```
niu_api/
  channel/
    __init__.py              — ChannelRouter
    base.py                  — UnifiedMessage + ChannelRouter 接口
    electron_channel.py      — Electron 通道适配器
    feishu_channel.py        — 飞书通道适配器
```

启动流程（`niu_api/__main__.py`）：

```python
# 现有启动流程之后
feishu_config = preferences.get("feishu", {})
if feishu_config.get("enabled"):
    try:
        feishu_adapter = FeishuChannelAdapter(
            app_id=feishu_config["app_id"],
            app_secret=feishu_config["app_secret"],
            channel_router=channel_router,
        )
        channel_router.register("feishu", feishu_adapter)
        await feishu_adapter.start()  # WebSocket 长连接
        logger.info("[FeishuChannel] Started successfully")
    except Exception as e:
        logger.error(f"[FeishuChannel] Failed to start: {e}")
        # 通道启动失败不影响主服务
```

## 8. 参考资源

### lark-cli 源码（设计参考，不直接使用）

| 文件 | 参考内容 |
|------|---------|
| `internal/event/source/feishu.go` | WebSocket 长连接实现（Go 版，我们用 Python FeishuChannel） |
| `internal/auth/device_flow.go` | OAuth Device Flow 实现（参考逻辑） |
| `shortcuts/im/shortcuts.go` | IM Shortcut 定义（TOOL_SCHEMAS 设计参考） |
| `shortcuts/calendar/shortcuts.go` | Calendar Shortcut 定义 |
| `shortcuts/doc/shortcuts.go` | Doc Shortcut 定义（doc_import_md/doc_export_md 参考） |
| `skills/lark-im/SKILL.md` | IM 技能定义（disk YAML 设计参考） |
| `skills/lark-calendar/SKILL.md` | Calendar 技能定义 |
| `skills/lark-doc/SKILL.md` | Doc 技能定义 |

### lark-oapi Python SDK（实际使用）

| 模块 | 用途 |
|------|------|
| `lark_oapi.channel.FeishuChannel` | IM 通道（WebSocket + 消息收发 + 流式回复） |
| `lark_oapi.ws.Client` | 日历事件订阅（第二个 WebSocket 连接） |
| `lark_oapi.api.calendar.v4` | 日历 CRUD API |
| `lark_oapi.api.docx.v1` | 文档 CRUD API |
| `lark_oapi.api.drive.v1` | 云盘文件 API |
| `lark_oapi.api.mail.v1` | 邮件 API |
| `lark_oapi.api.contact.v3` | 通讯录 API |

### 我们项目集成点

| 文件 | 用途 |
|------|------|
| `agent/tool_registry.py` | ToolRegistry 注册入口 |
| `agent/mcp_loader.py` | MCP 模块加载器（需支持 optional 服务器） |
| `config/mcp-servers.yaml` | MCP 服务器配置 |
| `config/disk/feishu-server.yaml` | disk 虚拟磁盘配置 |
| `niu_api/internal/scheduler/service.py` | 定时任务推送增强 |
| `mcp-servers/scheduler-server/` | 日历同步钩子 |

## 9. 分阶段实施路线图

| 阶段 | 内容 | 工作量 | 交付物 |
|------|------|--------|--------|
| **Phase 1** | 飞书通道 + 认证 + 日历同步 | 5天 | 可用的飞书对话通道 + 日历自动同步 |
| **Phase 2** | 文档 + 云盘 + 邮件 MCP 工具 | 3天 | `/feishu/doc_*`, `/feishu/mail_*`, `/feishu/drive_*` |
| **Phase 3** | 表格 + 知识库 + 通讯录 | 2天 | `/feishu/sheet_*`, `/feishu/wiki_*`, `/feishu/contact_*` |
| **Phase 4** | 流式回复 + 卡片交互 + 媒体处理 | 2天 | 飞书流式输出 + 卡片按钮回调 |

### Phase 1 详细拆分

1. **飞书应用创建指南**（0.5天）— 文档：如何在飞书开放平台创建应用、配置权限
2. **认证配置**（0.5天）— `preferences.json` 新增 feishu 字段 + config-manager 工具
3. **FeishuChannelAdapter**（1.5天）— WebSocket 长连接 + 消息收发 + ChannelRouter
4. **日历同步**（1.5天）— scheduler-server 集成飞书日历写入 + cron↔RRULE 转换
5. **集成测试**（0.5天）— 端到端测试：飞书发消息 → Agent 回复 → 飞书收到
