# 动态子 Agent 标签页设计

> 日期：2026-08-03
> 状态：设计确认，待写实现计划
> 审查：已通过可行性审查（scout agent，5 项 important 问题已修订）

## 1. 目标

主 Agent 调用子 Agent 时，Chat 页面动态生成新标签页。用户可以在子 Agent tab 中看到子 Agent 的完整工作过程（工具调用、进度、thinking chain、输出），并可以向子 Agent 补充任务信息。子 Agent 遇到问题时可通过 `@user` 向用户提问，形成双向交流。

一期完成全部功能。实现拆分为多个步骤文档（主文档 + 分步骤计划），逐步实现。

## 2. 约束与原则

- **通道隔离**：子 Agent 的所有通讯通道与主 Agent 完全分离，主 Agent 现有代码路径零改动。
- **复用现有机制**：用户→子 Agent 消息复用 `SubagentSupplementQueue.push()`，不引入新交互模式。
- **工作型子 Agent 具备对话能力**：现有子 Agent 都是工作型，用户发消息是补充任务信息（次末信息 / supplement）。但子 Agent 遇到问题可主动 `@user` 向用户提问，用户回答通过 `AskUserFuture` 唤醒。无需单独开发"对话型子 Agent"。
- **关闭逻辑不变**：tab 不可手动关闭。唯一关闭方式是子 Agent 输出 `@end`（子 Agent 语法，非 `/end`）。用户发 `/stop` 走末尾信息终止子 Agent 运行但 tab 不关闭。所有原有逻辑保持不变。
- **前端框架不变**：现有 header、messages、输入区、状态栏、脑区面板等全部保持不变，只新增 tab 栏。
- **去掉消息前缀约定**：不使用 `user:` / `niu-agent:` 前缀，直接用 `SubagentSupplementItem.sender` 字段区分发送者。现有 `format_subagent_supplement` 已格式化为 `[发送者 补充] 内容`，子 Agent 能区分。

## 3. 架构概览

```
┌─────────────────────────────────────────────┐
│  Chat 页面（现有框架不变）                      │
│  ┌───────────────────────────────────────┐  │
│  │ Tab 栏：[主对话] [file-processor] ...  │  │  ← 新增
│  ├───────────────────────────────────────┤  │
│  │  messages 区域                         │  │
│  │  （主对话 tab = 现有 #messages）         │  │
│  │  （子 Agent tab = 独立 messages 容器）   │  │
│  ├───────────────────────────────────────┤  │
│  │  输入区（位置不变，发送目标随 tab 切换）   │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘

事件通道：
  主 Agent SSE  ←→  /api/events/stream             （零改动）
  子 Agent SSE  ←→  /api/subagents/{id}/stream      （新增，独立 EventBus）
  子 Agent 启动通知 → 主 Agent SSE 流 subagent_started 事件（新增事件类型）

消息通道：
  用户 → 主 Agent  ←→  /api/chat/stream              （零改动）
  用户 → 子 Agent  ←→  /api/subagents/{id}/message   （新增，复用 supplement_queue）
  子 Agent → 用户  ←→  @user 阻塞 → 子Agent SSE 推 question 事件 → 用户回答 → POST API set_answer 唤醒
```

## 4. 后端设计

### 4.1 SubagentEventBus — 独立事件总线

**问题**：现有 SSE 架构是全局广播模式（`_event_subscribers` 是全局 `list[asyncio.Queue]`，`_sync_broadcast` 盲目广播到所有订阅者），没有 per-unique_name 路由能力。

**方案**：新建 `SubagentEventBus` 类，维护 `dict[str, list[asyncio.Queue]]`（unique_name → 订阅者队列列表）。

- `notify_subagent_event(unique_name, event_type, data)`：通过 `call_soon_threadsafe` 注入事件到对应 unique_name 的队列。复用 `niu_api.chat._main_loop` 全局引用（与现有 `_sync_broadcast` 相同模式）。
- `subscribe(unique_name)` → `asyncio.Queue`：SSE 端点调用，返回该子 Agent 的事件队列。
- `close(unique_name)`：推送关闭事件并标记断开。所有结束路径调用。
- 事件环形缓冲区：每个 unique_name 维护独立的 `deque(maxlen=100)` 环形缓冲区（与 Queue 是两个独立数据结构），SSE 断线重连时从缓冲区补发最近 100 条事件。

**结束通知覆盖所有路径**（在 `SubagentRegistry.unregister` 中统一注入 `bus.close()` 回调）：
- `@end`（EXITED）
- `/stop`（STOPPED / TERMINATED_BY_SUPPLEMENT）
- 异常崩溃（Exception）
- 超时（MAX_TURNS_EXCEEDED）
- 上下文溢出（CONTEXT_OVERFLOW）
- 同步挂起（INTERCEPTED_SYNC）——不调 unregister，特殊处理

### 4.2 子 Agent 事件推送 — handler 改造

**现状**：子 Agent handler 的 `_is_subagent = True` 导致 `notify_tool_status_sync` 跳过 SSE（handler.py:472/484）。`_run_agent_loop` 的 StreamEvent 消费循环丢弃非 reply 类型（subagent.py:148-175）。

**方案**：
- `_is_subagent` 跳过逻辑**保留不删**——主 Agent SSE 流继续跳过。
- 重构 `tool_before_callback` / `tool_after_callback` 的 `_is_subagent` 分支：跳过主 Agent 推送后，调用 `notify_subagent_event(unique_name, ...)` 推送到 SubagentEventBus。
- 确保 `_subagent_unique_name` 在所有路径设置（同步 L908、异步 L884、回复路径 L841-870 三处统一设置）。
- `_run_agent_loop` 的 StreamEvent 消费循环：`tool_marker` / `persist` / `system` 类型不再丢弃，转发到 `notify_subagent_event`。

**推送内容**（比主 Agent 多 thinking chain）：
- 工具调用状态（tool_status：工具名 + 状态 + 摘要）
- 文本输出（子 Agent 回复文本）
- 系统消息（persist、错误等）
- **thinking chain**（子 Agent 思考过程）——在 `agent_loop.py` LLM 响应处理段新增提取逻辑：检查 `response` 是否有 thinking 属性，有则推送。复用 `thinking_chain.py` 的 `extract_thinking_from_content_blocks` 函数。需验证 LiteLLM 客户端在子 Agent 配置下是否返回 thinking/reasoning_content 数据。

**不推送**：完整工具结果（与主 Agent 一致，只推摘要）。

### 4.3 独立 SSE 端点

新增 `GET /api/subagents/{unique_name}/stream`：
- 用 `StreamingResponse` + `async generator` 实现（与现有 `/api/events/stream` 相同模式）。
- 从 `SubagentEventBus.subscribe(unique_name)` 获取队列，循环 `await q.get()` 推送事件。
- 30s keepalive 心跳。
- 收到 `close` 事件后断开连接。
- unique_name 不在 EventBus 中：返回 404。

### 4.4 用户 → 子 Agent 消息 API

新增 `POST /api/subagents/{unique_name}/message`：
- Body：`{ "content": "用户消息" }`
- **不直接调 `supplement_queue.push()`**——提取 `db_monitor.route_message` 核心逻辑为公共函数 `route_to_subagent(target, sender, content, source='db_monitor')`，POST API 和 db_monitor 都调用它。避免绕过路由逻辑。POST API 场景（`source='post_api'`）：target 必须是子 Agent unique_name（不存在 target==主Agent 场景），孤儿回答返回 404 而非推回主 Agent，不执行降级逻辑。db_monitor 场景（`source='db_monitor'`）：保留原有全部行为。
- 用户消息 `sender="user"`，作为**次末信息**插入子 Agent 上下文（见缝插针），补充任务信息。不用前缀，靠 `sender` 字段区分。
- **`/stop` 处理**：必须同时调 `cancel_pending_ask(unique_name)` + `push("/stop", is_terminate=True)`，与 `runner.py:112-113` 保持一致。只 push 不 cancel 会导致 ask_main_agent 挂起的子 Agent 死锁 300s。
- **`@user` 回答处理**：检测子 Agent 处于 `waiting_for_user` 状态时，调 `AskUserFuture.set_answer(content)` 唤醒（见 4.6）。

**边界处理**：
- 子 Agent 不在 SubagentRegistry 中（已结束）：返回 404。
- 子 Agent 处于 `waiting_for_answer` 挂起状态（@niu-agent）：消息推入 queue，tab 展示"等待主 Agent 回答中"状态。用户消息在主 Agent answer 后送达。
- 子 Agent 处于 `waiting_for_user` 状态（@user）：调 `set_answer` 唤醒，消息即时送达。

### 4.5 子 Agent 启动通知

- `_dispatch_async_subagent` 返回 `(unique_name, confirmation_text)` 元组（目前返回纯文本），`_call_subagent_gen` 从中拿到 unique_name 推送事件。
- 主 Agent SSE 流（`/api/events/stream`）中新增 `subagent_started` 事件类型，携带子 Agent 名称 + unique_name + `is_sync` 标识（同步/异步）。前端收到同步子 Agent 的 `subagent_started` 后主动设置主对话 tab 状态为"子 Agent 工作中"，不依赖后端推送。新增事件类型，不修改现有事件处理逻辑。
- 前端收到 `subagent_started` 后动态创建 tab，并向 `/api/subagents/{unique_name}/stream` 建立独立 SSE 连接。

### 4.6 @user 机制 — 子 Agent 向用户提问

**与 @niu-agent 对称但独立实现**。@niu-agent 是阻塞式（`AskMainAgentFuture` + `PendingAskRegistry` + db_monitor 双链路路由）。@user 路由更简单——不经过 db_monitor 和主 Agent，直接通过 POST API → set_answer 唤醒。

**新增组件**：
- `AskUserFuture`（类似 `AskMainAgentFuture`）：子 Agent 调 `ask_user` 工具时注册 future，阻塞等待用户回答。
- `UserAskRegistry`：管理 `dict[unique_name, AskUserFuture]`。
- `ask_user` 工具：子 Agent 提示词增加 `@user` 语法说明。子 Agent 输出 `@user 问题描述` 时，handler 解析后注册 future、推送 `question` 事件到 SubagentEventBus（前端 tab 高亮显示问题）、阻塞子 Agent 线程。
- 用户回答 → `POST /api/subagents/{id}/message` → 检测 `waiting_for_user` 状态 → `set_answer(content)` 唤醒。
- 阻塞超时：600s（比 @niu-agent 的 300s 更长，因为用户响应可能较慢），超时后子 Agent 自行决策继续或退出。

**子 Agent 状态扩展**：`SubagentRegistry` 的 `RunningSubagent.state` 新增 `waiting_for_user` 值（现有：`running` / `waiting_for_answer`）。

**共存约束**：同一子 Agent 同一时刻只能有一个 Future 挂起（`AskUserFuture` 或 `AskMainAgentFuture`）。因为 ask 阻塞子 Agent 执行循环，阻塞期间不会执行下一条 LLM 输出，不会同时发起第二个 ask。

## 5. 前端设计（需求边界）

> 前端具体设计单独派 Agent 完成，此处只定义需求边界。但 IPC 转发是架构层面改造，在此明确。

### 5.1 Tab 栏

- 位置：messages 区域上方、header 下方。
- 默认只有一个 `[主对话]` tab，即现有全部内容。
- 收到 `subagent_started` 事件后动态新增 tab，标题为子 Agent 名称。
- 新 tab 创建后自动切换过去。

### 5.2 Tab 视图切换

- 点击子 Agent tab：只替换 messages 区域内容为该子 Agent 的工作过程消息。页面其他部分（header、状态栏、脑区面板等）都不变。
- 输入框仍在原位，但在子 Agent tab 下，发送的消息发给该子 Agent（调 `/api/subagents/{unique_name}/message`）。
- 切回 `[主对话]` tab：回到原有主对话视图，输入框发给主 Agent。
- 子 Agent tab 有新消息时，tab 标题加提示点。

### 5.3 Tab 关闭规则

- tab 上**无 × 按钮**，用户不可手动关闭。
- 唯一关闭方式：子 Agent 输出 `@end`，tab 自动关闭。
- 用户可发 `/stop` 终止子 Agent 运行，但 tab 保留直到子 Agent 输出 `@end`。
- 所有原有 `/stop`、`@end`、`@niu-agent` 逻辑保持不变。

### 5.4 子 Agent tab 消息内容

- 工具调用进度（工具名 + 状态 + 摘要）
- 工作步骤
- **thinking chain**（子 Agent 思考过程，帮助用户及时纠偏）
- 子 Agent 回复文本
- 子 Agent `@user` 提问消息（高亮提示用户需要回应）
- 子 Agent 状态变化（运行中 / 等待主 Agent 回答 / 等待用户回答 / 已完成 / 异常终止）
- 体验以工作观察为主，比主对话更详细

### 5.5 前端 IPC 改造方案

现有 `main.js` 的 `startMessageEventStream()` 是单连接模式。子 Agent SSE 需要改造：

- `main.js` 新增 `SubagentSSEManager`：`Map<unique_name, {req, reconnectTimer, buffer}>`，提供 `connect(unique_name)` / `disconnect(unique_name)` / `reconnect(unique_name)` 方法。
- 事件通过 `chatWindow.webContents.send('subagent-event', {unique_name, event})` 转发到 chat.html。
- `preload-chat.js` 新增 `onSubagentEvent(callback)` 接口。
- `chat.html` 按 `unique_name` 路由事件到对应 tab 的 messages 容器。
- `chatWindow.on('closed')` 时断开所有子 Agent SSE 连接。
- 重新打开 chatWindow 时，从 `GET /api/subagents/running` 获取运行中的子 Agent 列表，重建 tab 和 SSE 连接。该 API 需扩展返回 `state` 和 `started_at` 字段（现有只返回 `unique_name`/`agent_type`/`is_sync`）。
- 窗口重开恢复策略：tab 标题和状态可恢复，历史消息仅限 ring buffer 内的最近 100 条。完整历史持久化到 db 是已知限制，一期不做。

## 6. 隔离性分析

| 维度 | 主 Agent | 子 Agent | 隔离方式 |
|---|---|---|---|
| SSE 通道 | `/api/events/stream` | `/api/subagents/{id}/stream` | 独立端点 + 独立 EventBus |
| 事件推送函数 | `notify_tool_status_sync` 等 | `notify_subagent_event` | 独立函数 |
| 事件总线 | `_event_subscribers` 全局广播 | `SubagentEventBus` per-unique_name | 独立类 |
| 消息发送 | `/api/chat/stream` | `/api/subagents/{id}/message` | 独立 API |
| 前端消息容器 | `#messages` | `#messages-{unique_name}` | 独立 DOM |
| 前端 SSE 管理 | `startMessageEventStream()` | `SubagentSSEManager` | 独立管理器 |
| supplement_queue | 主 Agent `_supplement_queue` | 子 Agent `SubagentSupplementQueue` | 已有隔离 |
| thinking chain | 不推送 | 推送 | 推送内容差异 |
| 提问机制 | `@niu-agent` → `AskMainAgentFuture` | `@user` → `AskUserFuture` | 独立 Future + Registry |

主 Agent 代码路径零改动，子 Agent 通道出 bug 不影响主对话。

## 7. 实现拆分

一期完成全部功能，拆分为多个步骤文档：

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 1 | SubagentEventBus + 独立 SSE 端点 | 无 |
| 2 | handler 改造：_subagent_unique_name 统一 + StreamEvent 转发 + notify_subagent_event | 步骤 1 |
| 3 | thinking chain 提取与推送 | 步骤 2 |
| 4 | 用户→子Agent 消息 API + route_to_subagent 公共函数 | 步骤 1 |
| 5 | @user 机制：AskUserFuture + UserAskRegistry + ask_user 工具 + 提示词改造 | 步骤 1（基础部分）；@user 回答处理（set_answer）需在步骤 4 完成后补充 |
| 6 | subagent_started 事件 + _dispatch_async_subagent 返回值改造 | 步骤 2 |
| 7 | 前端 Tab 栏 + 消息区切换 + 输入框切换 | 步骤 6 |
| 8 | 前端 SubagentSSEManager + IPC 改造 | 步骤 7 |
| 9 | 异常处理 + 窗口恢复 + 断线重连 | 步骤 8 |

> 步骤 4（route_to_subagent + POST API 基础消息推送）和步骤 5（AskUserFuture）有部分交叉依赖：步骤 4 的 @user 回答处理（set_answer）依赖步骤 5 的组件。可在步骤 4 中先实现基础消息推送，@user 回答处理留到步骤 5 完成后补充。

## 8. 同步子 Agent tab 体验

同步子 Agent 阻塞主 Agent 线程期间：
- 主对话 tab 显示"子 Agent 工作中"状态（而非通用"处理中"）。前端收到同步子 Agent 的 `subagent_started` 事件（携带 `is_sync=true`）后主动设置此状态。
- 子 Agent tab 正常展示工作过程（`_run_agent_loop` 的 StreamEvent 转发到 SubagentEventBus，轻量级 `call_soon_threadsafe`，性能影响可接受）。
- 主 Agent 工具循环被阻塞等待 `call_subagent` 返回，主 Agent SSE 流不推送 `chat_idle`。
- 需验证大量工具调用时推送频率是否过高。若发现问题，实现时可引入批量/限流（如 100ms 内合并 tool_marker/system 事件）。
