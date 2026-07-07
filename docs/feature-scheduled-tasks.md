# 定时任务系统设计文档

## 1. 概述

实现一个类似 Unix cron 的定时任务系统，让 Agent 能够：
1. 创建定时任务（指定时间提醒用户）
2. 到时间后通过 Agent 处理（可能调用工具）
3. 根据聊天窗口焦点状态决定通知方式：
   - 焦点在聊天窗口 → 直接显示消息
   - 焦点不在聊天窗口 → 小女孩报警，点击后显示

## 2. 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                        定时任务系统                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  用户: "明天下午3点开会"                                         │
│      ↓                                                          │
│  主 Agent 调用 schedule_task 工具                                │
│      ↓                                                          │
│  ┌─────────────────────────────────────┐                       │
│  │ scheduled_tasks 表 (niu.db)         │                       │
│  │ id, content, scheduled_at, status   │                       │
│  └─────────────────────────────────────┘                       │
│      ↓                                                          │
│  Scheduler (Go 后台 goroutine)                                  │
│      ↓ 到时间                                                   │
│  发送 Notification 到 channel                                   │
│      ↓                                                          │
│  main.go 收到通知                                               │
│      ↓                                                          │
│  toolloop.ChatWithToolLoop(sessionID, prompt)                  │
│      ↓                                                          │
│  大模型处理 → 可能调用工具 → 最终回复                            │
│      ↓                                                          │
│  存储消息到 messages 表 + 添加到待推送队列                       │
│      ↓                                                          │
│  前端轮询 /api/pending-alerts (每10秒)                          │
│      ↓                                                          │
│  ┌─────────────────────────────────────────┐                   │
│  │ 聊天窗口是否焦点?                         │                   │
│  │ ├─ 是 → 直接加载历史消息显示              │                   │
│  │ └─ 否 → 小女孩 ALERT 状态，点击后显示     │                   │
│  └─────────────────────────────────────────┘                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 3. 关键流程说明

### 3.1 任务触发流程

1. **Scheduler 检测到到期任务** → 发送 `Notification{Type, Content, TaskID, SessionID}` 到 channel

2. **main.go goroutine 收到通知**：
   ```go
   // 从 window-config.json 读取当前 sessionID
   // 构建提示词: "定时提醒：该「开会」了。请提醒用户。"
   // 调用 toolloop.ChatWithToolLoop
   ```

3. **Agent 处理**：
   - 大模型收到提示词
   - 可能调用工具（如查询天气、发送邮件等）
   - 生成最终回复

4. **存储消息**：
   - 存储 user 消息（提示词）
   - 存储 assistant 消息（回复）
   - 添加到 `pendingAlerts` 队列

### 3.2 前端通知流程

1. **Electron 每10秒轮询** `/api/pending-alerts`
2. **收到消息后判断焦点**：
   - 聊天窗口在焦点 → 消息已存储，下次刷新/加载历史时显示
   - 聊天窗口不在焦点 → 触发小女孩 ALERT 状态，消息缓存到 `pendingAlertMessages`
3. **用户点击小女孩或焦点回到聊天窗口**：
   - 调用 `get-pending-messages` IPC 获取缓存消息
   - 显示消息，清除缓存

## 3. 数据库设计

### 3.1 scheduled_tasks 表

在 `niu.db` 中新增：

```sql
CREATE TABLE scheduled_tasks (
    id TEXT PRIMARY KEY,              -- UUID
    content TEXT NOT NULL,            -- 任务内容 "开会"
    scheduled_at DATETIME NOT NULL,   -- 触发时间 "2026-03-30T15:00:00"
    status TEXT DEFAULT 'pending',    -- pending | triggered | cancelled
    event_type TEXT,                  -- meeting | task | reminder
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    triggered_at DATETIME             -- 实际触发时间
);

-- 索引：快速查询即将触发的任务
CREATE INDEX idx_scheduled_tasks_pending 
ON scheduled_tasks(scheduled_at) 
WHERE status = 'pending';
```

### 3.2 messages 表（复用现有）

```go
// pkg/session/types.go 已有
type Message struct {
    gorm.Model
    SessionID string          // 会话ID
    Role      string          // "assistant" (Agent 发给用户的通知)
    Content   string          // "该开会了"
    Metadata  MessageMetadata // 可存储 {"type": "notification"}
}
```

## 4. 工具设计

### 4.1 工具注册方式

**采用内置工具方式**：在 `pkg/servers/system/server.go` 中注册，类似 `todoWrite` 工具。

**参考**：`AGENTS.md` 说明了两种工具注册方式：
- MCP 服务器工具：`mcp-servers.yaml` + `agents/*.md` 的 `mcpServers` 字段
- 内置工具：`pkg/servers/system/server.go` 直接注册

**选择内置工具的原因**：
- 调度器是 Go 后端核心功能，需要与数据库紧密配合
- 需要访问调度器的通知通道
- 不需要跨进程通信
- 复用现有 `niu.db` 连接

### 4.2 工具列表

| 工具名 | 功能 | 参数 |
|--------|------|------|
| `schedule_task` | 创建定时任务 | content, scheduled_at, event_type |
| `list_scheduled_tasks` | 查询任务列表 | status |
| `cancel_task` | 取消任务 | task_id |

### 4.3 工具注册代码

**位置**：`pkg/servers/system/server.go` 的 `NewServer` 函数

```go
// 在 mcp.NewServerTools(...) 中添加
mcp.NewServerTool("schedule_task", `创建定时任务，到时间后系统会自动提醒用户。

参数：
- content: 任务内容，如 "开会"
- scheduled_at: 触发时间，ISO格式，如 "2026-03-30T15:00:00"
- event_type: 事件类型，可选值：meeting/task/reminder

示例：
schedule_task(content="开会", scheduled_at="2026-03-30T15:00:00", event_type="meeting")

注意：相对时间（明天、下周）必须由 Agent 转换为具体的日期时间。`, s.scheduleTask),

mcp.NewServerTool("list_scheduled_tasks", `查询定时任务列表。

参数：
- status: 可选，筛选状态：pending/triggered/cancelled

返回：任务列表，包含 id、content、scheduled_at、status`, s.listScheduledTasks),

mcp.NewServerTool("cancel_task", `取消定时任务。

参数：
- task_id: 任务ID

返回：取消结果`, s.cancelTask),
```

### 4.4 工具实现文件

新建 `pkg/servers/system/scheduler.go`：

```go
package system

type ScheduleTaskParams struct {
    Content     string `json:"content"`      // 任务内容
    ScheduledAt string `json:"scheduled_at"` // 触发时间 (ISO格式)
    EventType   string `json:"event_type"`   // meeting/task/reminder
}

type ListScheduledTasksParams struct {
    Status string `json:"status"` // pending/triggered/cancelled
}

type CancelTaskParams struct {
    TaskID string `json:"task_id"` // 任务ID
}

func (s *Server) scheduleTask(ctx context.Context, params ScheduleTaskParams) (string, error) {
    // 调用调度器创建任务
}

func (s *Server) listScheduledTasks(ctx context.Context, params ListScheduledTasksParams) (string, error) {
    // 查询任务列表
}

func (s *Server) cancelTask(ctx context.Context, params CancelTaskParams) (string, error) {
    // 取消任务
}
```

### 4.5 Agent 提示词（使用说明）

在 `config/agents/event-manager.md` 中添加工具使用说明：

```markdown
# 定时任务

当用户提到需要在特定时间提醒时，使用 `schedule_task` 工具。

## 创建定时任务

schedule_task(
    content="开会",
    scheduled_at="2026-03-30T15:00:00",
    event_type="meeting"
)

**重要**：
- `scheduled_at` 必须是 ISO 格式的绝对时间
- 相对时间（明天、下周）必须转换为具体日期
- 系统会在指定时间自动提醒用户
```

## 5. 调度器实现

### 5.1 核心代码结构

```
pkg/scheduler/
├── scheduler.go      # 调度器主逻辑
├── store.go          # 数据库操作
└── types.go          # 数据结构
```

### 5.2 scheduler.go 核心逻辑

**注意**：调度器只负责检测到期任务并发送通知，不处理 Agent 调用。Agent 调用在 main.go 中处理。

```go
package scheduler

import (
    "context"
    "time"
    "log/slog"
)

type Scheduler struct {
    db       *gorm.DB
    notifyCh chan Notification  // 通知通道
    ticker   *time.Ticker
    ctx      context.Context
    cancel   context.CancelFunc
    wg       sync.WaitGroup
}

type Notification struct {
    Type      string    // "scheduled_task"
    Content   string    // 任务内容 "开会"
    TaskID    string    // 任务ID
    SessionID string    // 会话ID（创建时记录）
    Timestamp time.Time
}

func NewScheduler(db *gorm.DB) *Scheduler {
    return &Scheduler{
        db:       db,
        notifyCh: make(chan Notification, 100),
    }
}

func (s *Scheduler) Start() {
    s.ctx, s.cancel = context.WithCancel(context.Background())
    s.ticker = time.NewTicker(1 * time.Minute)
    
    s.wg.Add(1)
    go func() {
        defer s.wg.Done()
        for {
            select {
            case <-s.ctx.Done():
                return
            case <-s.ticker.C:
                s.checkAndTrigger()
            }
        }
    }()
}

func (s *Scheduler) checkAndTrigger() {
    now := time.Now()
    
    // 查询到期的任务
    tasks, err := s.store.GetPendingTasksBefore(ctx, now)
    if err != nil {
        slog.Error("查询待触发任务失败", "error", err)
        return
    }
    
    for _, task := range tasks {
        // 发送通知到 channel（由 main.go 处理）
        notification := Notification{
            Type:      "scheduled_task",
            Content:   task.Content,
            TaskID:    task.ID,
            SessionID: task.SessionID,
            Timestamp: time.Now(),
        }
        s.notifyCh <- notification
        
        // 更新任务状态为 triggered
        s.store.MarkTriggered(ctx, task.ID)
        
        slog.Info("任务已触发", "taskID", task.ID, "content", task.Content)
    }
}

func (s *Scheduler) GetNotifyChannel() <-chan Notification {
    return s.notifyCh
}

func (s *Scheduler) Stop() {
    if s.cancel != nil {
        s.cancel()
    }
    if s.ticker != nil {
        s.ticker.Stop()
    }
    close(s.notifyCh)  // 关闭 channel，让读取者退出
    s.wg.Wait()        // 等待 goroutine 退出
}
```

### 5.3 main.go 中的任务处理

收到 Scheduler 通知后，调用 Agent 处理：

```go
// 后台处理定时任务通知
go func() {
    notifyCh := sched.GetNotifyChannel()
    for notification := range notifyCh {
        slog.Info("收到定时任务通知", "taskID", notification.TaskID)
        
        // 构建提示词
        prompt := fmt.Sprintf("定时提醒：该「%s」了。请提醒用户。", notification.Content)
        
        // 从 window-config.json 读取当前 sessionID
        sessionID := readCurrentSessionID()
        
        // 加载历史消息
        storedMessages, _ := sessionManager.DB.GetRecentMessages(ctx, sessionID, 20)
        
        // 构建 inputMessages
        inputMessages := buildInputMessages(storedMessages, prompt)
        
        // 调用 toolloop.ChatWithToolLoop
        resp, err := toolloop.ChatWithToolLoop(ctx, llmClient, rt.Service, completionReq, agentConfig, cfg.MCPServers)
        
        // 存储消息到数据库
        sessionManager.DB.CreateMessage(ctx, &Message{SessionID: sessionID, Role: "user", Content: prompt})
        sessionManager.DB.CreateMessage(ctx, &Message{SessionID: sessionID, Role: "assistant", Content: reply})
        
        // 添加到待推送队列
        AddPendingAlert(reply)
    }
}()
```

## 6. Agent 提示词设计

### 6.1 更新 event-manager.md

在 `config/agents/event-manager.md` 中添加：

```markdown
# 定时任务

当用户提到需要在特定时间提醒时，使用 `schedule_task` 工具创建定时任务。

## 创建定时任务

```
schedule_task(
    content="开会",
    scheduled_at="2026-03-30T15:00:00",
    event_type="meeting"
)
```

**重要**：
- `scheduled_at` 必须是 ISO 格式的绝对时间
- 相对时间（明天、下周）必须转换为具体日期
- 系统会在指定时间自动提醒用户

## 示例

用户："明天下午3点开会，到时候提醒我"
你：调用 schedule_task(content="开会", scheduled_at="2026-03-30T15:00:00", event_type="meeting")
返回："已设置提醒：明天下午3点开会"
```

### 6.2 更新主 Agent (niu.md)

在 `config/agents/niu.md` 中更新事件管理部分：

```markdown
# 事件管理

当用户提到日程、会议、任务、待办事项时，调用子 Agent `event-manager` 处理。

**定时提醒**：
- 用户需要特定时间提醒时，event-manager 会创建定时任务
- 系统会在指定时间主动通知用户
```

## 7. 前端通知设计

### 7.1 通知机制

**采用轮询模式**（而非 SSE）：

- Electron 每 10 秒轮询 `/api/pending-alerts`
- 后端返回待推送消息列表，并清空队列
- 前端根据聊天窗口焦点状态决定如何处理

### 7.2 后端待推送队列

```go
// main.go
var pendingAlerts []PendingAlert
var pendingAlertsMu sync.Mutex

type PendingAlert struct {
    Content   string    `json:"content"`
    Timestamp time.Time `json:"timestamp"`
}

func AddPendingAlert(content string) {
    pendingAlertsMu.Lock()
    defer pendingAlertsMu.Unlock()
    pendingAlerts = append(pendingAlerts, PendingAlert{
        Content:   content,
        Timestamp: time.Now(),
    })
}

func GetAndClearPendingAlerts() []PendingAlert {
    pendingAlertsMu.Lock()
    defer pendingAlertsMu.Unlock()
    alerts := pendingAlerts
    pendingAlerts = nil
    return alerts
}

// API 端点
mux.HandleFunc("/api/pending-alerts", func(w http.ResponseWriter, r *http.Request) {
    alerts := GetAndClearPendingAlerts()
    json.NewEncoder(w).Encode(alerts)
})
```

### 7.3 Electron 轮询逻辑

```javascript
// ui/main/main.js
let pendingAlertMessages = [];
let alertsPollingTimer = null;

function startPendingAlertsPolling() {
  alertsPollingTimer = setInterval(async () => {
    const alerts = await fetchPendingAlerts();
    if (alerts && alerts.length > 0) {
      // 判断聊天窗口是否在焦点
      const chatFocused = chatWindow && chatWindow.isFocused() && chatWindow.isVisible();
      
      if (!chatFocused) {
        // 聊天窗口不在焦点，缓存消息并触发小女孩报警
        alerts.forEach(alert => {
          pendingAlertMessages.push(alert.content);
        });
        
        // 通知小女孩进入 ALERT 状态
        if (spiritWindow) {
          spiritWindow.webContents.send('alert', alerts[alerts.length - 1].content);
        }
      }
      // 如果在焦点，消息已存储到数据库，用户刷新/加载历史时自动显示
    }
  }, 10000);  // 每10秒轮询
}

function stopPendingAlertsPolling() {
  if (alertsPollingTimer) {
    clearInterval(alertsPollingTimer);
    alertsPollingTimer = null;
  }
}

// IPC: 获取待显示消息（聊天窗口获得焦点时调用）
ipcMain.handle('get-pending-messages', async () => {
  const messages = [...pendingAlertMessages];
  pendingAlertMessages = [];  // 清空缓存
  return messages;
});
```

### 7.4 悬浮窗状态机扩展

在 `ui/main/windows/assistant/spirit.html` 中新增 `ALERT` 状态：

```javascript
const State = { 
  IDLE: 'idle', 
  WAKE: 'wake', 
  SLEEP: 'sleep', 
  BUSY: 'busy',
  ALERT: 'alert'  // 新增：有事叫用户
};

// 监听 alert 事件
ipcRenderer.on('alert', (event, content) => {
  setState(State.ALERT);
  // 显示蹦高动画
});
```

### 7.5 聊天窗口焦点处理

```javascript
// chat.html 或 preload-chat.js
window.addEventListener('focus', () => {
  // 获取待显示消息
  ipcRenderer.invoke('get-pending-messages').then(messages => {
    if (messages && messages.length > 0) {
      // 显示消息
      messages.forEach(msg => addMessageToUI('assistant', msg));
    }
  });
});
```

### 7.6 动画资源

需要用户提供：
- `alert.gif` - 蹦高动画

## 8. API 设计

### 8.1 定时任务 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/schedule` | POST | 创建定时任务 |
| `/api/schedule` | GET | 查询任务列表 |
| `/api/schedule/:id` | DELETE | 取消任务 |

### 8.2 待推送消息 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/pending-alerts` | GET | 获取并清空待推送消息队列 |

**返回格式**：
```json
[
  {"content": "该开会了", "timestamp": "2026-03-30T15:00:00Z"},
  {"content": "记得吃药", "timestamp": "2026-03-30T15:30:00Z"}
]
```

### 8.3 消息 API（已有）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat/messages` | GET | 获取历史消息 |

## 9. 实现步骤

### 阶段一：后端核心（已完成）

1. ✅ 创建 `pkg/scheduler/` 包
   - types.go - 数据结构
   - store.go - 数据库操作
   - scheduler.go - 调度器逻辑

2. ✅ 在 `pkg/servers/system/` 中添加 scheduler.go
   - 实现 MCP 工具：schedule_task, list_scheduled_tasks, cancel_task

3. ✅ 在 main.go 中集成调度器
   - 初始化 Scheduler
   - 启动后台 goroutine 监听通知 channel
   - 调用 toolloop.ChatWithToolLoop 处理任务
   - 存储消息 + 添加到待推送队列

### 阶段二：前端通知（已完成）

4. ✅ 更新 `spirit.html` 状态机
   - 新增 ALERT 状态
   - 监听 alert IPC 事件

5. ✅ 更新 `main.js`
   - 添加轮询 `/api/pending-alerts` (每10秒)
   - 焦点判断逻辑
   - 实现 get-pending-messages IPC

6. ✅ 更新 `chat.html`
   - 打开时加载历史消息
   - 焦点时获取待显示消息

### 阶段三：Agent 集成（已完成）

7. ✅ 工具已注册到 `pkg/servers/system/server.go`

## 10. 测试计划

1. **单元测试**
   - 调度器时间检查逻辑
   - 数据库 CRUD 操作

2. **集成测试**
   - 创建任务 → 到期触发 → Agent 处理 → 消息存储
   - 轮询 → 焦点判断 → 小女孩状态变更

3. **端到端测试**
   - 用户说"明天下午3点开会"
   - 等待触发时间
   - 验证悬浮窗状态变更
   - 点击打开聊天窗口看到消息

## 11. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `pkg/scheduler/types.go` | 新增 | 数据结构定义 |
| `pkg/scheduler/store.go` | 新增 | 数据库操作 |
| `pkg/scheduler/scheduler.go` | 新增 | 调度器核心 |
| `pkg/servers/system/scheduler.go` | 新增 | MCP 工具实现 |
| `main.go` | 修改 | 集成调度器 |
| `ui/main/windows/assistant/spirit.html` | 修改 | 新增 ALERT 状态 |
| `ui/main/main.js` | 修改 | SSE 订阅 |
| `ui/main/windows/assistant/chat.html` | 修改 | 加载历史消息 |
| `config/agents/event-manager.md` | 修改 | 添加定时任务说明 |
| `config/agents/niu.md` | 修改 | 更新事件管理部分 |
| `ui/main/windows/assistant/alert.gif` | 新增 | 蹦高动画（用户提供） |

## 12. 注意事项

1. **时区处理**：所有时间使用本地时区（Asia/Shanghai）
2. **任务去重**：相同内容和时间的任务不重复创建
3. **持久化**：任务存储在数据库，重启后恢复
4. **错误处理**：Agent 调用失败时记录日志，不阻塞调度器
