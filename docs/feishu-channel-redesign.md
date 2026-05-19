# 飞书通道生产级重设计方案 v2

> 日期：2026-05-19
> 依据：`docs/analysis-feishu-sdk-capabilities.md` + `docs/analysis-feishu-channel-defects.md`
> 目标：将飞书通道从玩具级实现升级为生产级基础设施

---

## 1. 核心架构认知

**飞书通道 = 消息映射层**，不是独立的 Agent 调用通道。

```
飞书用户 ←→ FeishuChannelAdapter ←→ 前端会话框（/chat/sync）
```

- 飞书收到的消息 → 投入前端会话框（通过 `/chat/sync`）
- 会话框处理完 → 回复映射回飞书（通过 `channel.send()`）
- 所有 Agent 调度、会话管理、上下文管理都在会话框体系内完成

**现有 `/chat/sync` 自循环 HTTP 调用是正确的方向**，问题不在于"走了 HTTP"，而在于实现太粗糙：
- 没有错误恢复
- 没有利用 SDK 能力（SafetyPipeline、schedule、is_ready）
- 没有断连重连处理
- 消息丢失无感知
- session_id 硬编码
- 消息元数据丢失

---

## 2. 问题总结

当前飞书通道存在 **54 个缺陷**，按影响分类：

### 2.1 连接健壮性（根本问题 — 导致"卡死"）

| 问题 | 现状 | 修复 |
|------|------|------|
| 断连后永久不可用 | `_on_reconnecting`/`_on_reconnected` 只打日志 | 重连后恢复状态，通知上层 |
| 发送前不检查连接 | `send()`/`push()` 不检查 `is_ready` | 先 `wait_ready()` 再发送 |
| SDK error 事件未注册 | SDK 内部错误无法被上层感知 | 注册 `on("error", handler)` |
| 每消息一线程无限制 | `threading.Thread` 无上限 | 改用 `channel.schedule()` + 线程池 |

### 2.2 消息可靠性

| 问题 | 现状 | 修复 |
|------|------|------|
| 纯图片/文件消息被丢弃 | `if not content.strip(): return` | 检查 content 和 resources 都为空才跳过 |
| 群聊消息覆盖 P2P chat_id | 无条件设置 `_user_p2p_chat_id` | 仅 P2P 消息才更新 |
| send() 失败只打日志 | 消息丢失无感知 | 捕获异常 + 重试 + 通知 |
| Agent 返回空回复时静默 | `if reply:` 跳过 | 空回复也发提示 |
| SDK loop 可能已关闭 | `run_coroutine_threadsafe` 抛 RuntimeError | 用 `channel.schedule()` 代替 |

### 2.3 会话映射

| 问题 | 现状 | 修复 |
|------|------|------|
| session_id 硬编码 "feishu" | 所有飞书用户共享会话 | 基于 `sender_id` 生成独立 session_id |
| 消息元数据丢失 | `route_in_sync()` 只传纯文本 | 传递完整 UnifiedMessage |

### 2.4 持久化

| 问题 | 现状 | 修复 |
|------|------|------|
| 非原子文件写入 | 崩溃可能导致 preferences.json 为空 | 临时文件 + rename |
| read-modify-write 竞态 | 并发写入可能互相覆盖 | 加文件锁 |

---

## 3. 架构设计

### 3.1 核心原则

1. **飞书通道是消息映射层** — 不直接调用 Agent，通过 `/chat/sync` 投入会话框
2. **消息处理不阻塞 SDK 事件循环** — `_on_message` 是同步 handler，立即返回
3. **充分利用 SDK 能力** — schedule()、is_ready、SafetyPipeline
4. **错误可观测、可恢复** — 每个错误都有处理动作

### 3.2 新架构

```
飞书 WS → SDK FeishuChannel → SafetyPipeline（去重+串行+批处理）
  → _on_message(msg)  [同步 handler，立即返回]
    → channel.schedule(_process_and_reply(msg))  [提交到 SDK _bg_loop]
      → _process_and_reply(msg)  [在 _bg_loop 中执行]
        → requests.post("/chat/sync", json={session_id, message})  [投入会话框]
        → channel.send(chat_id, {"markdown": reply})  [回复映射回飞书]
```

### 3.3 与旧架构对比

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 消息处理 | `threading.Thread` + `requests.post` | `channel.schedule()` + `requests.post` |
| 会话管理 | 硬编码 `session_id="feishu"` | 基于 `sender_id` 的独立会话 |
| 错误处理 | 吞掉异常，只打日志 | 分类处理 + 用户反馈 + 重试 |
| 连接健康 | 无检查 | `is_ready` + `wait_ready()` |
| 重连恢复 | 只打日志 | 重置状态 + 通知上层 |
| 线程管理 | 每消息一线程，无上限 | SDK _bg_loop 协程调度 |
| SDK SafetyPipeline | 未配置 | 启用 dedup + chat_queue + text_batch |
| SDK error 事件 | 未注册 | 注册并集中处理 |

---

## 4. 模块设计

### 4.1 FeishuChannelAdapter（重写）

**文件**：`niu_api/channel/feishu_channel.py`

```python
class FeishuChannelAdapter(ChannelAdapter):
    def __init__(self, app_id, app_secret, channel_router):
        # 1. 修补 SDK 模块级 loop
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

        # 3. 注册事件（新增 error 事件）
        self.channel.on("message", self._on_message)
        self.channel.on("cardAction", self._on_card_action)
        self.channel.on("reconnecting", self._on_reconnecting)
        self.channel.on("reconnected", self._on_reconnected)
        self.channel.on("error", self._on_error)

        self.router = channel_router
        # ... 持久化初始化不变

    def _on_message(self, msg):
        """同步 handler — 不阻塞 SDK _bg_loop"""
        try:
            unified = self._to_unified(msg)
            if not unified:
                return

            # 仅 P2P 消息才更新推送目标
            if self._is_p2p_message(msg):
                self._update_persisted_ids(msg.chat_id, msg.sender_id)

            logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

            # 提交到 SDK _bg_loop 处理（不再用 threading.Thread）
            self.channel.schedule(self._process_and_reply(unified))

        except Exception as e:
            logger.error(f"[FeishuChannel] Message handler error: {e}")

    async def _process_and_reply(self, unified: UnifiedMessage):
        """在 SDK _bg_loop 中执行 — 投入会话框，回复映射回飞书"""
        try:
            # 投入会话框（复用现有 /chat/sync 端点）
            session_id = f"feishu:{unified.sender_id}"
            reply = self.router.route_in_sync(unified, session_id=session_id)
            if reply:
                # 检查连接状态再发送
                if self.channel.is_ready:
                    await self.channel.send(
                        unified.channel_id,
                        {"markdown": reply},
                    )
                else:
                    logger.warning("[FeishuChannel] Channel not ready, reply dropped")
            else:
                # Agent 返回空回复，发提示
                await self.channel.send(
                    unified.channel_id,
                    {"text": "收到，但无法生成回复"},
                )
        except Exception as e:
            logger.error(f"[FeishuChannel] Process/reply error: {e}")
            # 通知用户
            try:
                if self.channel.is_ready:
                    await self.channel.send(
                        unified.channel_id,
                        {"text": "处理消息时出错，请稍后重试"},
                    )
            except Exception:
                pass

    def _on_error(self, err):
        """SDK 内部错误集中处理"""
        logger.error(f"[FeishuChannel] SDK error: {err}")

    def _on_reconnecting(self, _=None):
        """WebSocket 重连中"""
        logger.warning("[FeishuChannel] WebSocket reconnecting...")

    def _on_reconnected(self, _=None):
        """WebSocket 重连成功 — 恢复状态"""
        logger.info("[FeishuChannel] WebSocket reconnected, channel is ready")

    def _is_p2p_message(self, msg) -> bool:
        """判断是否为 P2P 消息（非群聊）"""
        chat_type = getattr(msg, 'chat_type', None)
        if chat_type:
            return chat_type == "p2p"
        # 降级：无法判断时，不更新（比错误覆盖好）
        return False

    def _to_unified(self, msg) -> UnifiedMessage | None:
        """格式转换 — 纯图片/文件消息不丢弃"""
        content = msg.content_text or ""
        resources = msg.resources or []

        # 只有 content 和 resources 都为空时才跳过
        if not content.strip() and not resources:
            logger.debug("[FeishuChannel] Empty message with no resources, skipping")
            return None

        return UnifiedMessage(
            content=content,
            channel="feishu",
            channel_id=msg.chat_id,
            sender_id=msg.sender_id,
            message_type=msg.raw_content_type or "text",
            resources=resources,
            raw=msg.raw or {},
        )
```

### 4.2 ChannelRouter（小幅修改）

**文件**：`niu_api/channel/__init__.py`

**改动**：
- `route_in_sync()` 新增 `session_id` 参数，不再硬编码
- 传递完整 `UnifiedMessage`（含 resources）

```python
def route_in_sync(self, message: UnifiedMessage, session_id: str = "feishu") -> str:
    """同步路由消息 — 供飞书通道调用"""
    return self._chat_sync(message.content, session_id=session_id)

def _chat_sync(self, message: str, session_id: str = "feishu") -> str:
    """同步调用 /chat/sync 端点"""
    import os
    import requests

    port = os.environ.get("NIU_API_PORT", "9876")
    try:
        resp = requests.post(
            f"http://127.0.0.1:{port}/chat/sync",
            json={"session_id": session_id, "message": message},
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

### 4.3 ChannelAdapter 基类（完善）

**文件**：`niu_api/channel/base.py`

新增 `start()`、`disconnect()`、`is_connected` 到基类契约。

### 4.4 启动逻辑（修正）

**文件**：`niu_api/__main__.py`

**改动**：先启动后注册 + 启动失败后台重试

```python
# 旧：先注册后启动
channel_router.register("feishu", feishu_adapter)
asyncio.create_task(feishu_adapter.start())

# 新：先启动后注册（阻塞等待，确保通道可用再注册）
try:
    await feishu_adapter.start()
    channel_router.register("feishu", feishu_adapter)
    logger.info("[Feishu] Channel started and registered")
except Exception as e:
    logger.error(f"[Feishu] Channel start failed: {e}, will retry in background")
    asyncio.create_task(_retry_feishu_start(feishu_adapter))
```

### 4.5 持久化（原子写入）

**文件**：`niu_api/channel/feishu_channel.py`

`_save_prefs()` 改为原子写入：先写临时文件，再 `os.replace()`。

---

## 5. 关键设计决策

### 5.1 为什么用 `channel.schedule()` 而不是 `threading.Thread`？

| 维度 | threading.Thread | channel.schedule() |
|------|-----------------|-------------------|
| 线程数 | 每消息一线程，无上限 | _bg_loop 协程调度，单线程 |
| 资源 | 线程创建/销毁开销 | 协程切换成本极低 |
| 并发控制 | 无 | SafetyPipeline chat_queue 自动串行化 |
| 生命周期 | daemon 线程，无法取消 | Future 可追踪、可取消 |
| 错误处理 | 线程内异常需手动捕获 | done-callback 自动记录异常 |
| loop 安全 | 需手动捕获 sdk_loop | schedule 内部用 _bg_loop，永远正确 |

### 5.2 为什么保留 `/chat/sync` HTTP 调用？

飞书通道是**消息映射层**，不是独立的 Agent 调用通道。`/chat/sync` 是前端会话框的同步入口，飞书消息通过它投入会话框，复用所有会话管理、上下文管理、消息持久化逻辑。

直接调用 Agent 会引入：
- 跨事件循环问题（`_chat_lock` 绑定 FastAPI 主循环）
- `NiuRunner` 共享状态并发访问
- 重复实现消息持久化
- 重复实现 SSE 推送

### 5.3 为什么 `_on_message` 是同步 handler？

SDK `_invoke()` 机制：同步 handler 返回 None，SDK 不 await，_bg_loop 立即继续处理下一个事件（包括心跳）。async handler 会被 await，如果内部有阻塞调用，整个 _bg_loop 被阻塞。

### 5.4 SafetyPipeline 配置

| 配置项 | 值 | 理由 |
|--------|-----|------|
| dedup.ttl_seconds | 43200 (12h) | 覆盖 WS 重连回填窗口 |
| dedup.max_entries | 10000 | 高频消息场景足够 |
| text_batch.delay_ms | 600 | 用户连续发短消息时合并 |
| chat_queue.enabled | True | 同 chat 消息串行处理，避免乱序 |
| stale_message_window_ms | 1800000 (30min) | 默认值，重启后不回复旧消息 |

---

## 6. 错误处理策略

| 错误类型 | 处理方式 | 示例 |
|----------|----------|------|
| 可重试（网络/限流） | SDK OutboundSender 自动重试 | send() 返回 RATE_LIMITED |
| 不可重试（格式/权限） | SDK 自动降级 + 记录 | send() 返回 FORMAT_ERROR → 降级纯文本 |
| 连接断开 | SDK 自动重连 + `_on_reconnected` 恢复 | WebSocket 连接丢失 |
| Agent 处理失败 | 通知飞书用户"处理出错" | `/chat/sync` 返回错误 |
| 发送前连接未就绪 | 记录 warning，回复丢弃 | `is_ready` 为 False |
| 持久化失败 | 记录 warning，不影响消息处理 | preferences.json 写入失败 |

---

## 7. 消息可靠性保障

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

## 8. 修改文件清单

| 文件 | 改动类型 | 改动范围 |
|------|----------|----------|
| `niu_api/channel/feishu_channel.py` | **重写** | 整个文件 |
| `niu_api/channel/__init__.py` | **小幅修改** | route_in_sync 新增 session_id 参数 |
| `niu_api/channel/base.py` | **完善** | 新增 start/disconnect/is_connected |
| `niu_api/__main__.py` | **修改** | 先启动后注册 + 重试逻辑 |

---

## 9. 不修改的文件

- `niu_api/chat.py` — `/chat/sync` 端点不变
- `niu_api/internal/scheduler/service.py` — scheduler 推送路径不变
- `niu_api/channel/electron_channel.py` — Electron 通道不受影响
- `mcp-servers/feishu-server/` — MCP 服务器与通道架构无关
- `python/lib/python3.11/site-packages/lark_oapi/` — SDK 不修改

---

## 10. 验证标准

1. 启动应用，发送飞书消息，确认收到回复
2. 连续发送多条消息，确认不乱序、不丢失
3. 长时间运行（>30min），确认 WebSocket 不断连
4. 模拟网络断连，确认 SDK 自动重连后通道恢复
5. 同时在飞书和前端发消息，确认不互相阻塞
6. 定时推送触发时，飞书推送仍然正常
7. 纯图片消息不被丢弃
8. 群聊消息不覆盖 P2P chat_id
9. 不同飞书用户有独立会话（基于 sender_id）
10. Agent 处理失败时，飞书用户收到错误提示
