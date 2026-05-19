# 飞书通道重设计方案 v2 — 审查报告

> 日期：2026-05-19
> 审查对象：`docs/feishu-channel-redesign.md` (v2)
> 审查依据：`docs/analysis-feishu-sdk-capabilities.md` + `docs/analysis-feishu-channel-defects.md` + `docs/analysis-feishu-sdk-safety-verification.md` + 当前代码实际状态

---

## 1. 总体评估

| 维度 | 评级 | 说明 |
|------|------|------|
| 架构方向 | **通过** | 飞书通道 = 消息映射层，保留 `/chat/sync` HTTP 调用，方向正确 |
| SDK API 兼容性 | **通过** | 核心 API 全部存在且签名匹配（2 处默认值差异无功能风险） |
| SafetyPipeline 配置 | **通过** | 配置类和参数全部存在，方案配置合理 |
| 关键实现细节 | **有阻塞问题** | 3 个阻塞问题 + 5 个高风险问题需修正后才能实施 |
| 回滚策略 | **中风险** | Git 回滚可行，但缺少功能开关和渐进迁移路径 |

---

## 2. 阻塞问题（必须修正才能实施）

### B-1: `/chat/sync` 不支持 `resources` 参数 — 飞书图片/文件资源丢失

**现状**：
- `ChatRequest` 模型只有 `session_id`、`message`、`system_prompt` 三个字段（`niu_api/chat.py:107-112`）
- `ChannelRouter._chat_sync()` 只传 `message.content`（纯文本），不传 `resources`（`niu_api/channel/__init__.py:30-49`）
- 飞书图片/文件消息的 `msg.resources` 被转换为 `UnifiedMessage.resources`，但到达 `/chat/sync` 时被丢弃

**方案声称**：传递完整 `UnifiedMessage`（含 resources），但 `/chat/sync` 端点无法接收 resources。

**影响**：纯图片/文件消息即使不被 `_on_message` 丢弃，也无法被 Agent 处理（Agent 收到的只是空文本）。

**修正方案**：
1. **方案 A**：扩展 `ChatRequest` 增加 `resources` 字段，`/chat/sync` 端点传递给 Agent
2. **方案 B**：飞书通道在 `_chat_sync` 前将 resources 转为文本描述（如 `[图片: xxx.jpg]`），不改 `/chat/sync`
3. **推荐方案 B**：改动最小，不影响前端和其他调用方。飞书通道自己负责 resources → 文本转换

### B-2: `_chat_lock` 竞争 — 飞书与前端互相阻塞

**现状**：
- `/chat/sync` 和 `/chat` 共用 `_chat_lock`（`asyncio.Lock`，绑定 FastAPI 主循环）
- 飞书消息通过 `requests.post("/chat/sync")` 调用端点，端点内 `await _chat_lock.acquire(timeout=60)`
- 前端 SSE 请求也 `await _chat_lock.acquire(timeout=60)`
- 飞书 Agent 处理可能耗时 30-120s，期间前端请求被阻塞 60s 后返回 503

**方案遗漏**：没有提到 `_chat_lock` 竞争问题。

**影响**：飞书用户发消息 → Agent 处理中 → 前端用户发消息 → 等待 60s → 被拒绝。反之亦然。

**修正方案**：
1. **方案 A**：飞书通道使用独立 session_id → 独立 NiuRunner → 独立锁（架构改动大）
2. **方案 B**：`/chat/sync` 使用独立锁 `_feishu_chat_lock`，但 NiuRunner 是全局单例，共享状态仍无保护
3. **方案 C**：将 `_chat_lock` 改为 `asyncio.Semaphore(2)`，允许飞书和前端各占一个槽位（但 NiuRunner 共享状态仍有风险）
4. **推荐**：当前阶段接受互斥（飞书和前端不能同时处理），但将 timeout 从 60s 降到 30s，并在飞书通道侧做重试。这是最小改动，且符合"飞书通道是映射层"的定位。

### B-3: SafetyPipeline text_batch 合并消息 — `_on_message` 输入格式变化

**现状**：
- 方案启用 `SafetyPipeline(text_batch=TextBatchConfig(delay_ms=600))`
- text_batch 会将用户连续发送的短消息合并为一条，合并后的消息格式是 `BatchedMessage`（包含多条原始消息）
- `_on_message` 收到的 `msg` 可能是 `BatchedMessage` 而不是原始 `InboundMessage`
- `BatchedMessage` 的 `content_text` 是合并后的文本，`resources` 是合并后的资源列表

**方案遗漏**：没有提到 text_batch 合并后 `_on_message` 的输入格式变化。

**影响**：
- `msg.chat_id`、`msg.sender_id` 等字段在 `BatchedMessage` 中可能不存在或为合并值
- `msg.raw` 格式不同，`_to_unified()` 可能报错

**修正方案**：
- 方案启用 SafetyPipeline 后，SDK 的 `_invoke` 机制会自动处理：SafetyPipeline 的 `push_message` 最终调用 `on_message` handler，传入的是经过 pipeline 处理后的消息对象
- 需要确认 `BatchedMessage` 的属性兼容性（是否有 `chat_id`、`sender_id` 等属性）
- **建议**：先不启用 text_batch（`TextBatchConfig(delay_ms=0)` 或不配置），只启用 dedup + chat_queue + stale 检测。text_batch 在确认兼容性后再启用。

---

## 3. 高风险问题（实施时必须处理）

### H-1: `_process_and_reply` 中 `requests.post()` 在 async 协程中阻塞 SDK _bg_loop

**方案代码**（`docs/feishu-channel-redesign.md:161-167`）：
```python
async def _process_and_reply(self, unified: UnifiedMessage):
    # 投入会话框（复用现有 /chat/sync 端点）
    session_id = f"feishu:{unified.sender_id}"
    reply = self.router.route_in_sync(unified, session_id=session_id)
```

**问题**：`_process_and_reply` 是 `async def`，通过 `channel.schedule()` 提交到 SDK `_bg_loop`。但 `route_in_sync` 内部调用 `requests.post(timeout=120)`，这是同步阻塞调用。在 async 协程中直接调用同步阻塞函数会阻塞整个 `_bg_loop`。

**对比当前实现**：当前实现用 `threading.Thread` 执行 `_process_and_reply`，阻塞发生在独立线程中，不阻塞 `_bg_loop`。方案改为 `channel.schedule()` 提交 async 协程，反而引入了阻塞风险。

**修正方案**：
- **方案 A**：保持 `_on_message` 为同步 handler，用 `threading.Thread` 执行阻塞调用（当前实现的方式，已验证可行）
- **方案 B**：`_process_and_reply` 内部用 `await asyncio.to_thread(self.router.route_in_sync, unified)` 将阻塞调用提交到线程池
- **推荐方案 A**：当前实现已经用 `threading.Thread` + `run_coroutine_threadsafe` 的方式，且 `_on_message` 是同步 handler。方案 v2 的"改用 `channel.schedule()`"反而引入了新问题。建议保持当前线程方式，但用 `channel.schedule()` 代替 `run_coroutine_threadsafe` 发送回复。

### H-2: SSE 推送未隔离 — 飞书消息出现在前端 SSE

**现状**：
- `/chat/sync` 端点调用 `notify_new_message()` 广播到所有 SSE 订阅者（`niu_api/chat.py:411`）
- 飞书消息通过 `/chat/sync` 处理后，user 和 assistant 消息都会推送到前端 SSE
- 前端会看到飞书用户的消息和 Agent 的回复

**方案遗漏**：没有提到 SSE 推送隔离。

**影响**：前端用户看到飞书对话内容，隐私问题 + UI 混乱。

**修正方案**：
- `/chat/sync` 端点增加 `source` 参数（如 `"feishu"`），`notify_new_message` 根据 source 过滤推送
- 或者：飞书消息使用独立 session_id，SSE 推送时只推当前 session 的消息
- **推荐**：在 `ChatRequest` 增加 `source` 字段，`/chat/sync` 根据 source 决定是否推送 SSE。飞书来源的消息不推 SSE。

### H-3: session_id 策略不区分 P2P/群聊

**方案代码**：`session_id = f"feishu:{unified.sender_id}"`

**问题**：
- P2P 消息：`sender_id` 是用户唯一标识，session_id 正确
- 群聊消息：`sender_id` 是群内某个用户，不同用户在同一群聊会产生不同 session_id，但它们应该共享同一个会话上下文（群聊场景）
- 群聊的 `chat_id` 是群唯一标识，应该用 `chat_id` 构造 session_id

**修正方案**：
```python
if self._is_p2p_message(msg):
    session_id = f"feishu:{msg.sender_id}"
else:
    session_id = f"feishu:group:{msg.chat_id}"
```

### H-4: 消息持久化无会话隔离 — 全局共享 message store

**现状**：
- `get_message_store()` 返回全局单例（`niu_api/chat.py:228`）
- 所有 session 共享同一个 SQLite 数据库
- 飞书消息使用 `session_id="feishu:xxx"`，但 `MessageStore` 不按 session_id 分区

**影响**：飞书消息和前端消息混在同一个数据库中，`context_manager.get_context_for_chat()` 可能加载到前端的历史消息。

**修正方案**：当前 `MessageStore` 已按 session_id 过滤消息（`agent/session_adapter.py` 的 `get_messages` 方法），只要 session_id 不同，历史消息不会混入。需确认 `context_manager` 是否正确使用 session_id 过滤。

### H-5: Scheduler push 代码直接访问 adapter 内部

**现状**：scheduler 的 push 代码直接访问 `feishu_adapter._user_p2p_chat_id` 和 `_user_open_id`。

**方案遗漏**：没有提到 scheduler push 路径的兼容性。

**修正方案**：`FeishuChannelAdapter.push()` 方法已有降级逻辑（chat_id 失败 → open_id 重试），scheduler 应通过 `ChannelRouter.push()` 调用，不直接访问 adapter 内部。

---

## 4. 中风险问题

### M-1: `_on_message` 空消息检查仍可能丢弃纯图片

**方案代码**：`if not content.strip() and not resources: return None`

**问题**：方案已修正（检查 content 和 resources 都为空才跳过），但 `_chat_sync` 只传 `message.content`，resources 信息仍然丢失（见 B-1）。

### M-2: `_on_card_action` 完全未实现

**方案标注**："Phase 4 实现"。但飞书卡片交互是常见场景（按钮点击、表单提交），完全未实现意味着这些消息被忽略。

### M-3: 消息撤回无法取消已提交的 Agent 请求

**现状**：飞书用户撤回消息后，如果消息已提交给 Agent 处理，无法取消。

**建议**：当前阶段不处理（复杂度高），记录为已知限制。

### M-4: 持久化文件写入非原子

**方案已提出修正**：临时文件 + `os.replace()`。但当前实现（`feishu_channel.py:126-145`）仍是直接写入。

### M-5: `_patch_ws_loop()` 时机问题

**现状**：`__init__` 中修补 `ws/client` 模块级 loop，但 `FeishuChannel.__init__` 也会创建 `_bg_loop`。如果 `_patch_ws_loop` 在 `FeishuChannel` 创建之后执行，patch 无效。

**当前实现**：patch 在 `FeishuChannel` 创建之前执行（`feishu_channel.py:21-23`），顺序正确。

---

## 5. 低风险问题

### L-1: `DedupConfig(max_entries=10000)` vs SDK 默认 `5000`

方案显式传值覆盖 SDK 默认值，属于有意识的调优选择，无功能风险。

### L-2: `RetryConfig(base_delay_ms=1000)` vs SDK 默认 `500`

同上，显式调优，无功能风险。

---

## 6. 方案与当前实现的对比

| 维度 | 方案 v2 | 当前实现 | 评估 |
|------|---------|----------|------|
| `_on_message` 类型 | async handler | sync handler | **当前实现更优**（sync 不阻塞 _bg_loop） |
| 消息处理方式 | `channel.schedule()` + async 协程 | `threading.Thread` + `run_coroutine_threadsafe` | **当前实现更优**（阻塞在独立线程中） |
| 回复发送方式 | `await channel.send()` 在协程中 | `run_coroutine_threadsafe(channel.send(), sdk_loop)` | **方案更优**（可用 `channel.schedule()` 代替 `run_coroutine_threadsafe`） |
| SafetyPipeline | 启用 dedup + text_batch + chat_queue | 未启用 | **方案更优**（但 text_batch 需先验证兼容性） |
| session_id | `feishu:{sender_id}` | 硬编码 `"feishu"` | **方案更优**（但需区分 P2P/群聊） |
| 空消息检查 | content + resources 都为空才跳过 | 只检查 content | **方案更优** |
| P2P chat_id 更新 | 仅 P2P 消息才更新 | 无条件更新 | **方案更优** |
| error 事件 | 注册 `on("error")` | 未注册 | **方案更优** |
| SSE 推送隔离 | 未提及 | 未隔离 | **两者都有问题** |
| `_chat_lock` 竞争 | 未提及 | 存在 | **两者都有问题** |

---

## 7. 修正建议汇总

### 必须修正（阻塞实施）

| # | 问题 | 修正 |
|---|------|------|
| B-1 | resources 无法通过 `/chat/sync` | 飞书通道在调用 `_chat_sync` 前将 resources 转为文本描述，不改 `/chat/sync` |
| B-2 | `_chat_lock` 竞争 | 当前阶段接受互斥，飞书 timeout 30s + 重试；后续考虑独立锁 |
| B-3 | text_batch 合并消息格式变化 | 先不启用 text_batch，只启用 dedup + chat_queue + stale |

### 应当修正（高风险）

| # | 问题 | 修正 |
|---|------|------|
| H-1 | async 协程中同步阻塞 | 保持 `_on_message` 为 sync handler + `threading.Thread`，用 `channel.schedule()` 发送回复 |
| H-2 | SSE 推送未隔离 | `ChatRequest` 增加 `source` 字段，飞书来源不推 SSE |
| H-3 | session_id 不区分 P2P/群聊 | P2P 用 `sender_id`，群聊用 `chat_id` |
| H-4 | 消息持久化无隔离 | 确认 `context_manager` 按 session_id 过滤（大概率已正确） |
| H-5 | scheduler 直接访问 adapter 内部 | scheduler 通过 `ChannelRouter.push()` 调用 |

---

## 8. 推荐的渐进实施路径

1. **Phase 1a**：修正当前实现的已知 bug（不改架构）
   - 空消息检查：content + resources 都为空才跳过
   - P2P chat_id：仅 P2P 消息才更新
   - 回复发送：`run_coroutine_threadsafe` → `channel.schedule()`（更安全）
   - 注册 `on("error")` 事件

2. **Phase 1b**：启用 SafetyPipeline（不改消息处理路径）
   - 只启用 dedup + chat_queue + stale（不启用 text_batch）
   - 验证去重 + 串行 + 旧消息过滤

3. **Phase 2**：解决会话隔离问题
   - session_id 区分 P2P/群聊
   - SSE 推送隔离（`source` 字段）
   - resources 转文本描述

4. **Phase 3**：完善错误处理和持久化
   - 原子文件写入
   - Agent 处理失败时通知飞书用户
   - 发送前 `is_ready` 检查

5. **Phase 4**：高级功能
   - text_batch（验证兼容性后启用）
   - 卡片交互
   - 流式回复（`channel.stream()`）