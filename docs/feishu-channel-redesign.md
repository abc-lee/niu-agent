# 飞书通道生产级重设计方案

> 日期：2026-05-19
> 依据：`docs/analysis-feishu-sdk-capabilities.md` + `docs/analysis-feishu-channel-defects.md`
> 目标：将飞书通道从玩具级实现升级为生产级基础设施

---

## 1. 问题总结

当前飞书通道存在 **54 个缺陷**（严重 7 / 高 22 / 中 18 / 低 7），核心问题不是某个具体 bug，而是**架构设计上的根本缺陷**：

1. **自循环 HTTP 调用** — 消息处理通过 `requests.post("/chat/sync")` 调用自身 HTTP API，导致上下文丢失、会话混乱、锁竞争
2. **无错误恢复** — 任何异常只打日志，消息丢失无感知，断连后永久不可用
3. **SDK 能力浪费** — SDK 提供了 SafetyPipeline（去重+串行+批处理）、schedule()（线程安全协程提交）、is_ready（健康检查）、stream()（流式回复）等能力，但几乎未使用

---

## 2. 架构重设计

### 2.1 核心原则

1. **消息处理不阻塞 SDK 事件循环** — `_on_message` 必须是同步 handler，立即返回
2. **直接调用 Agent，不走 HTTP** — 消除自循环调用、锁竞争、会话混乱
3. **充分利用 SDK 能力** — schedule()、is_ready、SafetyPipeline、stream()
4. **错误可观测、可恢复** — 每个错误都有处理动作，不留静默丢失

### 2.2 新架构

```
飞书 WS → SDK FeishuChannel → SafetyPipeline（去重+串行+批处理）
  → _on_message(msg)  [同步 handler，立即返回]
    → channel.schedule(_process_message(msg))  [提交到 SDK _bg_loop]
      → _process_message(msg)  [在 _bg_loop 中执行]
        → runner.chat(session_id, content, resources)  [直接调用 Agent]
        → channel.send(chat_id, {"markdown": reply})  [通过 SDK 发送]
```

### 2.3 与旧架构对比

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 消息处理 | `threading.Thread` + `requests.post("/chat/sync")` | `channel.schedule()` + `runner.chat()` |
| 会话管理 | 硬编码 `session_id="feishu"` | 基于 `sender_id` 的独立会话 |
| 锁竞争 | 与前端共享 `_chat_lock` | 无锁竞争，独立处理路径 |
| 消息上下文 | 只传纯文本 | 传递完整 UnifiedMessage（含 resources） |
| 错误处理 | 吞掉异常，只打日志 | 分类处理 + 用户反馈 + 重试 |
| 连接健康 | 无检查 | `is_ready` + `wait_ready()` |
| 重连恢复 | 只打日志 | 重置状态 + 通知上层 |
| 线程管理 | 每消息一线程 | SDK _bg_loop 协程调度 |
| 流式回复 | 无 | SDK `stream()` 打字机效果 |

---

## 3. 模块设计

### 3.1 FeishuChannelAdapter（重写）

**文件**：`niu_api/channel/feishu_channel.py`

**职责**：
- 飞书协议适配（WebSocket 收发）
- 消息格式转换（InboundMessage → UnifiedMessage）
- 用户身份映射（chat_id / open_id 持久化）
- 连接生命周期管理

**不负责**：
- Agent 处理调度（由 ChannelRouter 负责）
- 消息处理逻辑（由 Agent 负责）

**关键改动**：

```python
class FeishuChannelAdapter(ChannelAdapter):
    def __init__(self, app_id, app_secret, channel_router, agent_runner):
        # 1. 修补 SDK 模块级 loop（保留，但改进错误处理）
        self._patch_ws_loop()

        # 2. 创建 SDK FeishuChannel（配置 SafetyPipeline）
        self.channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            outbound=OutboundConfig(
                markdown_converter=MarkdownConverter(tag_md_mode="native"),
                retry=RetryConfig(max_attempts=3, base_delay_ms=1000),
            ),
            safety=SafetyConfig(
                dedup=DedupConfig(ttl_seconds=43200, max_entries=10000),
                text_batch=TextBatchConfig(delay_ms=600),
                chat_queue=ChatQueueConfig(enabled=True),
            ),
        )

        # 3. 注册所有事件
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self.channel.on("error", self._on_error)

        # 4. 引用 agent_runner（直接调用，不走 HTTP）
        self._agent_runner = agent_runner
        self.router = channel_router

        # 5. 用户身份持久化
        self._user_p2p_chat_id = None
        self._user_open_id = None
        self._prefs_path = Path.home() / ".niu" / "preferences.json"
        self._apply_persisted_ids()

    def _on_message(self, msg):
        """同步 handler — 不阻塞 SDK _bg_loop"""
        # 1. 格式转换
        unified = self._to_unified(msg)
        if not unified:
            return

        # 2. 更新推送目标（仅 P2P）
        self._maybe_update_target(msg)

        # 3. 提交到 SDK _bg_loop 处理
        self.channel.schedule(self._process_message(unified))

    async def _process_message(self, unified: UnifiedMessage):
        """在 SDK _bg_loop 中执行 — 直接调用 Agent"""
        try:
            # 直接调用 Agent，不走 HTTP
            session_id = f"feishu:{unified.sender_id}"
            reply = await self._agent_runner.chat_async(
                session_id=session_id,
                message=unified.content,
                resources=unified.resources,
                channel="feishu",
            )
            if reply:
                await self.channel.send(
                    unified.channel_id,
                    {"markdown": reply},
                )
        except Exception as e:
            logger.error(f"[FeishuChannel] Process error: {e}")
            # 发送错误提示给用户
            try:
                await self.channel.send(
                    unified.channel_id,
                    {"text": "处理消息时出错，请稍后重试"},
                )
            except Exception:
                pass
```

### 3.2 ChannelRouter（重构）

**文件**：`niu_api/channel/__init__.py`

**改动**：
- 删除 `_chat_sync()` 自循环 HTTP 调用
- 删除 `route_in_sync()` 同步方法
- `route_in()` 改为直接调用 Agent（非 HTTP）
- 新增 `route_in_with_context()` 传递完整消息上下文

```python
class ChannelRouter:
    async def route_in(self, message: UnifiedMessage) -> str:
        """路由消息到 Agent — 直接调用，不走 HTTP"""
        session_id = f"{message.channel}:{message.sender_id}"
        return await self._agent_runner.chat_async(
            session_id=session_id,
            message=message.content,
            resources=message.resources,
            channel=message.channel,
        )
```

### 3.3 ChannelAdapter 基类（完善）

**文件**：`niu_api/channel/base.py`

**新增方法**：

```python
class ChannelAdapter(ABC):
    @abstractmethod
    async def start(self) -> None:
        """启动通道连接"""

    @abstractmethod
    async def disconnect(self) -> None:
        """断开通道连接"""

    @abstractmethod
    async def send(self, channel_id: str, content: str) -> None:
        """发送消息"""

    @abstractmethod
    async def push(self, channel_id: str, content: str) -> None:
        """主动推送"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """连接状态"""
```

### 3.4 启动逻辑（修正）

**文件**：`niu_api/__main__.py`

**改动**：先启动后注册 + 启动失败重试

```python
# 旧：先注册后启动
channel_router.register("feishu", feishu_adapter)
asyncio.create_task(feishu_adapter.start())

# 新：先启动后注册
try:
    await feishu_adapter.start()  # 阻塞等待连接成功
    channel_router.register("feishu", feishu_adapter)
    logger.info("[Feishu] Channel started and registered")
except Exception as e:
    logger.error(f"[Feishu] Channel start failed: {e}")
    # 后台重试
    asyncio.create_task(_retry_feishu_start(feishu_adapter))
```

---

## 4. 关键设计决策

### 4.1 为什么用 `channel.schedule()` 而不是 `threading.Thread`？

| 维度 | threading.Thread | channel.schedule() |
|------|-----------------|-------------------|
| 线程数 | 每消息一线程，无上限 | _bg_loop 协程调度，单线程 |
| 资源 | 线程创建/销毁开销 | 无，协程切换成本极低 |
| 并发控制 | 无 | SafetyPipeline 的 chat_queue 自动串行化同 chat 消息 |
| 生命周期 | daemon 线程，无法取消 | Future 可追踪、可取消 |
| 错误处理 | 线程内异常需手动捕获 | done-callback 自动记录异常 |

### 4.2 为什么直接调用 Agent 而不走 HTTP？

| 维度 | HTTP 自调用 | 直接调用 |
|------|------------|----------|
| 性能 | 序列化+网络栈+反序列化 | Python 函数调用 |
| 上下文 | 只传纯文本 | 传递完整消息（含 resources） |
| 会话 | 硬编码 session_id | 基于 sender_id 的独立会话 |
| 锁 | 与前端共享 _chat_lock | 无锁竞争 |
| 可靠性 | HTTP 超时=消息丢失 | 函数异常可捕获处理 |

### 4.3 为什么 `_on_message` 是同步 handler？

SDK 的 `_invoke()` 机制：
1. 调用 `handler(*args)` 
2. 检查返回值 `inspect.isawaitable(result)`
3. 如果是 awaitable → `await result`（阻塞 _bg_loop）
4. 如果是普通返回值 → 直接忽略（不阻塞）

同步 handler 返回 None，SDK 不会 await，_bg_loop 立即继续处理下一个事件（包括心跳）。如果用 async handler，SDK 会 await 它，如果 handler 内有阻塞调用，整个 _bg_loop 被阻塞。

### 4.4 SafetyPipeline 配置策略

| 配置项 | 值 | 理由 |
|--------|-----|------|
| dedup.ttl_seconds | 43200 (12h) | 覆盖 WS 重连回填窗口 |
| dedup.max_entries | 10000 | 高频消息场景足够 |
| text_batch.delay_ms | 600 | 用户连续发短消息时合并 |
| chat_queue.enabled | True | 同 chat 消息串行处理，避免乱序 |
| stale_message_window_ms | 1800000 (30min) | 默认值，重启后不回复旧消息 |

---

## 5. 错误处理策略

### 5.1 错误分类

| 错误类型 | 处理方式 | 示例 |
|----------|----------|------|
| 可重试（网络/限流） | 指数退避重试，最多 3 次 | send() 返回 RATE_LIMITED |
| 不可重试（格式/权限） | 记录 + 通知用户 | send() 返回 FORMAT_ERROR |
| 连接断开 | 等待 SDK 自动重连 + 重发 | WebSocket 连接丢失 |
| Agent 处理失败 | 通知用户"处理出错" | runner.chat() 抛异常 |
| 持久化失败 | 记录 warning，不影响消息处理 | preferences.json 写入失败 |

### 5.2 错误通知

- 注册 `channel.on("error", handler)` 集中处理 SDK 内部错误
- Agent 处理失败时发送"处理出错"提示给飞书用户
- 连接断开/重连通过 `reconnecting`/`reconnected` 事件通知上层

---

## 6. 消息可靠性保障

| 场景 | 保障机制 |
|------|----------|
| WS 重连后消息重投 | SafetyPipeline dedup 去重 |
| 同 chat 消息乱序 | SafetyPipeline chat_queue 串行化 |
| 短消息碎片 | SafetyPipeline text_batch 合并 |
| 旧消息重投 | SafetyPipeline stale 检测（30min 窗口） |
| 发送失败 | OutboundSender 自动重试（3 次，指数退避） |
| 回复目标已撤回 | OutboundSender 自动降级为新建消息 |
| Post 格式错误 | OutboundSender 自动降级为纯文本 |

---

## 7. 修改文件清单

| 文件 | 改动类型 | 改动范围 |
|------|----------|----------|
| `niu_api/channel/feishu_channel.py` | **重写** | 整个文件 |
| `niu_api/channel/__init__.py` | **重构** | 删除 _chat_sync，route_in 改为直接调用 |
| `niu_api/channel/base.py` | **完善** | 新增 start/disconnect/is_connected |
| `niu_api/__main__.py` | **修改** | 先启动后注册 + 重试逻辑 |
| `niu_api/internal/scheduler/service.py` | **修改** | 使用 ChannelRouter.push() 接口 |
| `niu_api/chat.py` | **修改** | 新增 chat_async() 供飞书通道直接调用 |

---

## 8. 不修改的文件

- `mcp-servers/feishu-server/` — MCP 服务器与通道架构无关
- `niu_api/channel/electron_channel.py` — Electron 通道不受影响
- `python/lib/python3.11/site-packages/lark_oapi/` — SDK 不修改

---

## 9. 验证标准

1. 启动应用，发送飞书消息，确认收到回复
2. 连续发送多条消息，确认不乱序、不丢失
3. 长时间运行（>30min），确认 WebSocket 不断连
4. 模拟网络断连，确认 SDK 自动重连后通道恢复
5. 同时在飞书和前端发消息，确认不互相阻塞
6. 定时推送触发时，飞书推送仍然正常
7. 纯图片消息不被丢弃
8. 群聊消息不覆盖 P2P chat_id
9. 不同飞书用户有独立会话
10. Agent 处理失败时，飞书用户收到错误提示
