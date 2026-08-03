# 动态子 Agent 标签页设计

> 日期：2026-08-03
> 状态：设计确认，待写实现计划

## 1. 目标

主 Agent 调用子 Agent 时，Chat 页面动态生成新标签页。用户可以在子 Agent tab 中看到子 Agent 的工作过程（工具调用、进度、输出），并可以向子 Agent 补充任务信息。

## 2. 约束与原则

- **通道隔离**：子 Agent 的所有通讯通道与主 Agent 完全分离，主 Agent 现有代码路径零改动。
- **复用现有机制**：用户→子 Agent 消息复用 `SubagentSupplementQueue.push()`，不引入新交互模式。
- **现有子 Agent 都是工作型**：用户发消息是补充任务信息（次末信息 / supplement），不是让子 Agent 回答。对话型子 Agent 是后续课题。
- **关闭逻辑不变**：tab 不可手动关闭。唯一关闭方式是子 Agent 输出 `/end`。`/stop` 走末尾信息终止子 Agent 运行但 tab 不关闭。所有原有逻辑保持不变。
- **前端框架不变**：现有 header、messages、输入区、状态栏、脑区面板等全部保持不变，只新增 tab 栏。

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
  主 Agent SSE  ←→  /api/events/stream        （零改动）
  子 Agent SSE  ←→  /api/subagents/{id}/stream （新增，独立）

消息通道：
  用户 → 主 Agent  ←→  /api/chat/stream        （零改动）
  用户 → 子 Agent  ←→  /api/subagents/{id}/message （新增，复用 supplement_queue）
```

## 4. 后端设计

### 4.1 子 Agent 事件推送 — 独立通道

**现状**：子 Agent handler 的 `_is_subagent = True` 标记导致 `notify_tool_status_sync` 等推送函数跳过 SSE（handler.py:472/484）。子 Agent 工作过程对前端完全不可见。

**方案**：
- `_is_subagent` 跳过逻辑**保留不删**——主 Agent SSE 流继续跳过子 Agent 事件，零改动。
- 新增独立推送函数 `notify_subagent_event(unique_name, event_type, data)`，子 Agent 的工具状态、文本输出、系统消息通过此函数推送到独立通道。
- 子 Agent handler 中，在现有 `_is_subagent` 跳过逻辑之后（或并行），调用独立推送函数。不修改主 Agent 的推送路径。

**推送内容**（与主 Agent 体验对齐）：
- 工具调用状态（tool_status：工具名 + 状态 + 摘要）
- 文本输出（子 Agent 回复文本）
- 系统消息（persist、错误等）

**不推送**：子 Agent 内部 thinking chain、完整工具结果（与主 Agent 一致，只推摘要）。

### 4.2 独立 SSE 端点

新增 `GET /api/subagents/{unique_name}/stream`：
- 前端为每个打开的子 Agent tab 建立独立 SSE 连接。
- 与主 `/api/events/stream` 完全隔离。
- 子 Agent 结束后（`/end`），端点推送关闭事件并断开连接。

### 4.3 用户 → 子 Agent 消息 API

新增 `POST /api/subagents/{unique_name}/message`：
- Body：`{ "content": "用户消息" }`
- 后端调 `supplement_queue.push(content, sender="用户")`——复用现有 supplement 机制。
- 用户消息作为**次末信息**插入子 Agent 上下文（见缝插针），补充任务信息。
- `/stop` 命令走同一 API，后端识别后调 `push("/stop", is_terminate=True)`——与现有 `request_stop_all_subagents` 逻辑一致。

**边界处理**：
- 子 Agent 不在 SubagentRegistry 中（已结束）：返回 404。
- 子 Agent 处于 `waiting_for_answer` 挂起状态（同步子 Agent @niu-agent）：消息推入 queue，但子 Agent 需要主 Agent 用 `answer` 参数才能恢复。用户消息等待。

### 4.4 子 Agent 启动通知

主 Agent 调用子 Agent 时，需要通知前端创建 tab：
- 主 Agent SSE 流中新增一个 `subagent_started` 事件（或复用 tool_status，带子 Agent 名称 + unique_name）。
- 前端收到后动态创建 tab 并建立独立 SSE 连接。

## 5. 前端设计（需求边界）

> 前端具体设计单独派 Agent 完成，此处只定义需求边界。

### 5.1 Tab 栏

- 位置：messages 区域上方、header 下方。
- 默认只有一个 `[主对话]` tab，即现有全部内容。
- 主 Agent 调用子 Agent时，动态新增 tab，标题为子 Agent 名称。
- 新 tab 创建后自动切换过去。

### 5.2 Tab 视图切换

- 点击子 Agent tab：只替换 messages 区域内容为该子 Agent 的工作过程消息。页面其他部分（header、状态栏、脑区面板等）都不变。
- 输入框仍在原位，但在子 Agent tab 下，发送的消息发给该子 Agent（调 `/api/subagents/{unique_name}/message`）。
- 切回 `[主对话]` tab：回到原有主对话视图，输入框发给主 Agent。
- 子 Agent tab 有新消息时，tab 标题加提示点。

### 5.3 Tab 关闭规则

- tab 上**无 × 按钮**，用户不可手动关闭。
- 唯一关闭方式：子 Agent 输出 `/end`，tab 自动关闭。
- 用户可发 `/stop` 终止子 Agent 运行，但 tab 保留直到子 Agent 输出 `/end`。
- 所有原有 `/stop`、`/end` 逻辑保持不变。

### 5.4 子 Agent tab 消息内容

- 工具调用进度（工具名 + 状态 + 摘要）
- 工作步骤
- 子 Agent 回复文本
- 体验与主 Agent 对话一致（粗略进度推送）

## 6. 隔离性分析

| 维度 | 主 Agent | 子 Agent | 隔离方式 |
|---|---|---|---|
| SSE 通道 | `/api/events/stream` | `/api/subagents/{id}/stream` | 独立端点 |
| 事件推送函数 | `notify_tool_status_sync` 等 | `notify_subagent_event` | 独立函数 |
| 消息发送 | `/api/chat/stream` | `/api/subagents/{id}/message` | 独立 API |
| 前端消息容器 | `#messages` | `#messages-{unique_name}` | 独立 DOM |
| supplement_queue | 主 Agent `_supplement_queue` | 子 Agent `SubagentSupplementQueue` | 已有隔离 |

主 Agent 代码路径零改动，子 Agent 通道出 bug 不影响主对话。

## 7. 后续课题（不在本次范围）

- **对话型子 Agent**：专门用于对话而非工作型任务，具体实现待定。
- **同步子 Agent tab 体验**：同步子 Agent 阻塞主 Agent 线程，tab 能展示工作过程但主对话处于等待状态。需验证体验。
- **多子 Agent 并发**：多个 tab 同时活跃时的 UI 交互细节。
