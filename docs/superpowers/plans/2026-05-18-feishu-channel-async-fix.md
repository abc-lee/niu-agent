# 飞书通道异步架构修复 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复飞书通道因 async handler 阻塞 SDK 事件循环导致的卡死问题

**Architecture:** 将 `_on_message` 从 async handler 改为 sync handler + threading，用 `asyncio.run_coroutine_threadsafe` 提交回复到 SDK bg loop

**Tech Stack:** Python asyncio, threading, lark-oapi SDK

**Design Doc:** `docs/feishu-channel-async-fix-design.md`

**Test Verification:** `tests/test_feishu_async_arch.py` — 4/4 假设已验证通过

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `niu_api/channel/feishu_channel.py` | Modify | 核心修改：_on_message 从 async→sync + threading |
| `niu_api/channel/__init__.py` | Modify | 新增 route_in_sync 方法 |
| `tests/test_feishu_channel.py` | Modify | 更新现有测试适配新架构 |

---

### Task 1: ChannelRouter 新增 route_in_sync 方法

**Files:**
- Modify: `niu_api/channel/__init__.py`

- [ ] **Step 1: 在 ChannelRouter 中新增 route_in_sync 方法**

在 `route_in` 方法之后添加：

```python
def route_in_sync(self, message: UnifiedMessage) -> str:
    """同步路由消息 — 供飞书通道线程中调用"""
    return self._chat_sync(message.content)
```

- [ ] **Step 2: 验证语法正确**

Run: `python -c "from niu_api.channel import ChannelRouter; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add niu_api/channel/__init__.py
git commit -m "feat: ChannelRouter 新增 route_in_sync 同步路由方法"
```

---

### Task 2: FeishuChannelAdapter._on_message 改为 sync + threading

**Files:**
- Modify: `niu_api/channel/feishu_channel.py`

- [ ] **Step 1: 添加 import**

在文件顶部添加：

```python
import asyncio
import threading
```

- [ ] **Step 2: 将 _on_message 从 async def 改为 def + threading**

替换整个 `_on_message` 方法：

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
        sdk_loop = asyncio.get_event_loop()
        chat_id = msg.chat_id

        def _process_and_reply():
            """在独立线程中执行阻塞调用，完成后通过 run_coroutine_threadsafe 发送回复"""
            try:
                reply = self.router.route_in_sync(unified)
                if reply:
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

- [ ] **Step 3: 验证语法正确**

Run: `python -c "from niu_api.channel.feishu_channel import FeishuChannelAdapter; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "fix: _on_message 改为 sync handler + threading，不阻塞 SDK 事件循环"
```

---

### Task 3: 更新现有测试

**Files:**
- Modify: `tests/test_channel_base.py`

- [ ] **Step 1: 检查现有测试是否需要更新**

Run: `python -m pytest tests/test_channel_base.py -v`
Expected: 可能需要更新 route_in 相关测试

- [ ] **Step 2: 如果测试失败，更新测试适配新架构**

如果 `test_route_in` 测试了 `route_in` 的 async 行为，确保 `route_in` 仍然可用（我们只新增了 `route_in_sync`，没有删除 `route_in`）。

- [ ] **Step 3: 运行全部测试确认无回归**

Run: `python -m pytest tests/ -v --timeout=60`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: 更新通道测试适配 sync handler 架构"
```

---

### Task 4: 端到端验证

**Files:** 无代码修改

- [ ] **Step 1: 启动应用**

Run: `go run main.go`

- [ ] **Step 2: 通过飞书发送消息**

确认：
1. 收到回复且不卡死
2. 连续发送多条消息正常
3. 日志无 "Broken pipe" 错误
4. 运行 >5 分钟 WebSocket 不断连

- [ ] **Step 3: 验证定时任务推送正常**

确认 scheduler 触发时飞书推送仍然正常。
