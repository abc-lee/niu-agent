# 飞书通道异步/同步架构整改方案

> **状态**: 审核通过 + 测试验证通过
> **日期**: 2026-05-18
> **问题**: 飞书通道接收消息后卡死，WebSocket 断连，Broken pipe
> **审核结论**: 方案可行，`channel.schedule()` 不存在，改用 `asyncio.run_coroutine_threadsafe`
> **测试验证**: 4/4 假设全部通过（`tests/test_feishu_async_arch.py`）

---

## 1. 问题描述

飞书通道在收到用户消息后完全卡死。用户确认："然后我问了一句在吗直接卡死了"。

**症状**：
- 发送消息后无回复
- 日志出现 `Broken pipe` 错误
- WebSocket 连接断开
- 整个飞书通道不可用

## 2. 根因分析

### 2.1 系统中的三个独立事件循环

```
FastAPI 主循环 (uvicorn)
  ├── /chat, /chat/sync 端点
  ├── SSE 推送
  └── asyncio.Lock (_chat_lock)

LightRAG 守护线程循环 (lightrag-loop)
  └── call_async() 桥接同步→异步

飞书 SDK 后台循环 (lark-channel-bg)
  ├── WebSocket 收发
  ├── 心跳 (_ping_loop, 120s 间隔)
  └── _invoke("message", inbound) → await handler(inbound)
```

### 2.2 问题链路

```
SDK 收到消息
  → _invoke("message", inbound)
    → await _on_message(msg)          # async handler，在 SDK 后台循环中 await
      → await route_in(unified)       # async 函数
        → _chat_sync(content)         # 同步 requests.post(timeout=120)
          → 阻塞 SDK 后台循环 120s    # 心跳无法发送！
            → WebSocket 超时断连
              → Broken pipe
                → 卡死
```

**根因**：`_on_message` 是 async handler，SDK 的 `_invoke` 会 `await` 它。但 handler 内部调用了同步阻塞的 `requests.post(timeout=120)`，直接阻塞了 SDK 后台事件循环，导致心跳无法发送，WebSocket 断连。

### 2.3 SDK `_invoke` 的 handler 调用机制

SDK 内部代码（`channel.py:382-403`）：

```python
result = handler(*args)           # 先同步调用
if inspect.isawaitable(result):   # 检查返回值
    await result                   # 如果是 coroutine，await 它
```

**关键发现**：如果 handler 是**普通同步函数**（返回 None），`_invoke` 不会 await，不会阻塞事件循环。

## 3. 整改方案

### 3.1 核心思路

将 `_on_message` 从 async handler 改为 sync handler。在 sync handler 中：
1. 用 `threading.Thread` 在独立线程中执行阻塞的 `_chat_sync`
2. 线程完成后，通过 `channel.schedule()` 将 `channel.send()` 提交到 SDK 后台循环

这样 SDK 后台循环不会被阻塞，心跳正常，WebSocket 不断连。

### 3.2 回复发送方式

审核发现：`channel.schedule()` **不存在于 lark-oapi SDK**。但 SDK 内部有类似的机制（`_ensure_bg_loop` + `asyncio.run_coroutine_threadsafe`）。

**替代方案**：在 `_on_message`（sync handler，仍在 SDK 后台循环上下文中）捕获 SDK 的 bg loop 引用，然后在工作线程中通过 `asyncio.run_coroutine_threadsafe` 提交 `channel.send()` 协程。

```python
# 在 _on_message 中（此时仍在 SDK bg loop 上下文）
sdk_loop = asyncio.get_event_loop()  # 获取 SDK bg loop

# 在工作线程中
asyncio.run_coroutine_threadsafe(
    self.channel.send(chat_id, {"markdown": reply}),
    sdk_loop
)
```

### 3.3 为什么不用协程提交整个处理流程？

协程仍然在 SDK 后台循环中执行。如果协程内部有同步阻塞调用（`requests.post`），仍然会阻塞循环。所以必须用线程。

## 4. 修改清单

### 4.1 `niu_api/channel/feishu_channel.py` — 核心修改

**改动**：`_on_message` 从 `async def` 改为 `def`，用线程执行阻塞调用，用 `channel.schedule()` 发送回复。

```python
def _on_message(self, msg):
    """处理飞书消息事件（同步 handler，不阻塞 SDK 事件循环）"""
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

        if not self._user_p2p_chat_id:
            self._user_p2p_chat_id = msg.chat_id

        logger.info(f"[FeishuChannel] Received: {unified.content[:50]}...")

        # 在 SDK bg loop 上下文中捕获 loop 引用
        # _on_message 由 SDK _invoke 在 bg loop 中调用，此时 get_event_loop() 返回 SDK bg loop
        sdk_loop = asyncio.get_event_loop()
        chat_id = msg.chat_id

        def _process_and_reply():
            """在独立线程中执行阻塞调用，完成后通过 run_coroutine_threadsafe 发送回复"""
            try:
                reply = self.router.route_in_sync(unified)
                if reply:
                    # 通过 run_coroutine_threadsafe 将 send 协程提交到 SDK bg loop
                    asyncio.run_coroutine_threadsafe(
                        self.channel.send(chat_id, {"markdown": reply}),
                        sdk_loop,
                    )
                    logger.info(f"[FeishuChannel] Replied: {reply[:50]}...")
            except Exception as e:
                logger.error(f"[FeishuChannel] Process/reply error: {e}")

        threading.Thread(target=_process_and_reply, daemon=True).start()

    except Exception as e:
        logger.error(f"[FeishuChannel] Message handler error: {e}")
```

**关键设计决策**：
- `_on_message` 是普通函数（非 async），SDK 的 `_invoke` 检测到返回值不是 awaitable，不会 await，立即返回
- 阻塞的 `_chat_sync` 在独立线程中执行，不阻塞 SDK 后台循环
- 在 `_on_message` 中（仍在 SDK bg loop 上下文）捕获 `sdk_loop` 引用
- 回复通过 `asyncio.run_coroutine_threadsafe(send, sdk_loop)` 提交到 SDK 后台循环，线程安全
- 线程是 `daemon=True`，不阻止进程退出

### 4.2 `niu_api/channel/__init__.py` — 新增 `route_in_sync` 方法

**改动**：ChannelRouter 新增 `route_in_sync` 同步方法，供飞书通道的线程中调用。

```python
def route_in_sync(self, message: UnifiedMessage) -> str:
    """同步路由消息 — 供飞书通道线程中调用"""
    return self._chat_sync(message.content)
```

保留原有 `async def route_in` 不变（Electron 通道可能仍使用）。

### 4.3 不修改的文件

| 文件 | 原因 |
|------|------|
| `niu_api/channel/base.py` | ChannelAdapter 接口不变 |
| `niu_api/__main__.py` | 启动逻辑不变 |
| `niu_api/internal/scheduler/service.py` | scheduler 的跨循环调用已正确 |
| `mcp-servers/feishu-server/` | MCP 服务器与通道架构无关 |
| `_on_reconnecting` / `_on_reconnected` | 已经是同步签名，无需修改 |
| `send` / `push` 方法 | 保持 async，调用方已正确处理跨循环 |

## 5. 架构对比

### 修改前（有 Bug）

```
SDK 后台循环:
  _invoke("message", msg)
    → await _on_message(msg)          # async handler
      → await route_in(unified)       # await
        → _chat_sync(content)         # 同步阻塞 120s
          → 整个循环被阻塞
            → 心跳超时 → 断连 → 卡死
```

### 修改后（正确）

```
SDK 后台循环:
  _invoke("message", msg)
    → _on_message(msg)                # sync handler, 立即返回
      → sdk_loop = get_event_loop()   # 捕获 SDK bg loop 引用
      → threading.Thread(_process)    # 独立线程
        → route_in_sync(unified)      # 同步调用（在线程中）
          → _chat_sync(content)       # 阻塞在线程中，不影响循环
        → run_coroutine_threadsafe(   # 线程安全提交到 SDK 循环
            send(chat_id, reply),
            sdk_loop)
    → 循环继续运行                     # 心跳正常，不断连
```

## 6. 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| `asyncio.get_event_loop()` 在 sync handler 中可能返回错误 loop | 中 | `_on_message` 由 SDK `_invoke` 在 bg loop 线程中调用，此时 `get_event_loop()` 返回 SDK bg loop。需实测验证 |
| 多线程并发调用 `_chat_sync` | 低 | `_chat_sync` 内部有 `requests.post`，天然线程安全 |
| 消息乱序（先发的消息后回复） | 低 | 飞书聊天场景可接受 |
| 线程中异常未捕获 | 低 | `_process_and_reply` 内部有 try/except |
| `run_coroutine_threadsafe` 提交时 SDK bg loop 已关闭 | 低 | daemon 线程 + try/except 兜底 |

## 7. 验证清单

1. 启动应用，发送飞书消息，确认收到回复且不卡死
2. 连续发送多条消息，确认不会串消息或丢失回复
3. 长时间运行（>5分钟），确认 WebSocket 不断连
4. 检查日志中无 "Broken pipe" 错误
5. 定时任务触发时，飞书推送仍然正常（scheduler 路径不受影响）
6. 运行 `pytest tests/` 确认无回归
