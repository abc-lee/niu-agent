# 聊天持久化与智能记忆管理设计文档

> 版本：v1.1  
> 日期：2026-03-28  
> 状态：Phase 1 已完成  

---

## 1. 概述

### 1.1 背景

当前 Niu 助手的对话历史仅存在于内存中，对话结束后即丢失。用户无法：
- 查看历史对话记录
- 跨会话保持上下文连贯性
- 让助手记住对话中的重要信息

### 1.2 目标

1. **聊天记录持久化**：将对话消息存储到数据库，支持历史检索 ✅ 已完成
2. **智能记忆管理**：从对话中自动提取重要信息，支持语义检索和自动注入 🔜 下一步
3. **无缝集成**：复用现有架构，最小化改动 ✅ 已完成

### 1.3 设计原则

- **复用优先**：利用现有 SQLite、向量存储、MCP 框架
- **渐进式交付**：Phase 1 → 2 → 3，每阶段独立可用
- **性能友好**：异步处理，不阻塞对话流程
- **数据安全**：本地存储，不上传云端

---

## 2. 现状分析

### 2.1 原始架构（Phase 1 实现前）

```
┌─────────────────────────────────────────────────────────────┐
│                     Electron 前端                            │
│  chat.html → main.js (IPC) → HTTP POST /api/chat          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                     Go 后端 (main.go)                       │
│  /api/chat → rt.CallFromCLI() → agents.Complete()         │
│  (无状态，每次请求都是全新会话)                               │
│                                                              │
│  Session 存储: niu.db (SQLite, GORM)                       │
│  对话压缩: compact.go (token > 83.5% 触发)                 │
│  记忆注入: loadMemory() → formatMemoryForPrompt()          │
└─────────────────────────────────────────────────────────────┘
```

**问题**：`/api/chat` 是无状态的，每次调用都创建新的空会话，无法保持对话历史。

### 2.2 当前架构（Phase 1 实现后）

```
┌─────────────────────────────────────────────────────────────┐
│                     Electron 前端                            │
│  chat.html → main.js (IPC) → HTTP POST /api/chat/session  │
│  (携带 sessionId，从 window-config.json 加载)              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                     Go 后端 (main.go)                       │
│  /api/chat/session → 加载历史消息 → 调用 LLM               │
│  (有状态，支持会话持久化)                                     │
│                                                              │
│  消息存储: niu.db (messages 表)                             │
│  Session 存储: niu.db (SQLite, GORM)                       │
│  配置存储: window-config.json (包含 chatSessionId)          │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 关键文件

| 组件 | 文件路径 | 说明 |
|------|----------|------|
| 聊天 UI | `ui/main/windows/assistant/chat.html:411-512` | 消息发送、重试逻辑 |
| 聊天 API | `main.go:574-710` | `/api/chat/session` 端点 ✅ 新增 |
| Session 存储 | `pkg/session/store.go` | GORM CRUD |
| 消息类型 | `pkg/session/types.go:80-110` | Message 结构体 ✅ 新增 |
| 消息 CRUD | `pkg/session/store.go:237-291` | 消息操作方法 ✅ 新增 |
| 前端配置 | `ui/main/main.js:470-505` | sessionId 存储 ✅ 修改 |
| 对话压缩 | `pkg/agents/compact.go:79-181` | 自动压缩历史 |
| 记忆加载 | `main.go:66-171` | 从 memory.json 加载 |
| 记忆注入 | `main.go:303-325` | 注入到 Agent |

---

## 3. Phase 1 实现详情（已完成）

### 3.1 数据库设计

在 `niu.db` 中新增 `messages` 表：

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    metadata JSON,                -- 工具调用、附件等扩展信息
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_messages_created ON messages(created_at);
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 自增主键 |
| `session_id` | TEXT | 关联 Session ID |
| `role` | TEXT | 消息角色：user/assistant/system |
| `content` | TEXT | 消息内容 |
| `metadata` | JSON | 扩展信息：工具调用、附件路径等 |
| `created_at` | DATETIME | 创建时间 |

### 3.2 Go 类型定义

**文件**: `pkg/session/types.go:80-110`

```go
// Message represents a chat message in a session.
type Message struct {
    gorm.Model
    SessionID string          `json:"sessionId" gorm:"index;not null"`
    Role      string          `json:"role" gorm:"not null"` // user | assistant | system
    Content   string          `json:"content" gorm:"not null"`
    Metadata  MessageMetadata `json:"metadata,omitempty" gorm:"type:json"`
}

// MessageMetadata contains additional information about a message.
type MessageMetadata struct {
    ToolCalls []ToolCall `json:"toolCalls,omitempty"` // tool calls in the message
    Error     string     `json:"error,omitempty"`     // error information if any
}

// ToolCall represents a tool call made by the agent.
type ToolCall struct {
    Name   string `json:"name"`
    Args   string `json:"args"`   // JSON string
    Result string `json:"result"` // JSON string
}
```

### 3.3 CRUD 方法

**文件**: `pkg/session/store.go:237-291`

```go
// CreateMessage creates a new message.
func (s *Store) CreateMessage(ctx context.Context, msg *Message) error

// GetMessages returns messages for a session.
func (s *Store) GetMessages(ctx context.Context, sessionID string, opts *MessageQueryOptions) ([]Message, error)

// GetRecentMessages returns the most recent messages for a session.
func (s *Store) GetRecentMessages(ctx context.Context, sessionID string, limit int) ([]Message, error)

// DeleteMessages deletes all messages for a session.
func (s *Store) DeleteMessages(ctx context.Context, sessionID string) error
```

### 3.4 API 端点

**文件**: `main.go:574-710`

新增 `/api/chat/session` 端点：

```go
mux.HandleFunc("/api/chat/session", func(w http.ResponseWriter, r *http.Request) {
    // 1. 接收 sessionId（可选）和 message
    // 2. 从数据库加载会话历史
    // 3. 拼接历史消息 + 新消息
    // 4. 调用 LLM 生成回复
    // 5. 存储用户消息和助手回复到数据库
    // 6. 返回 sessionId 和 reply
})
```

**请求格式**：
```json
{
  "sessionId": "xxx-xxx-xxx",  // 可选，首次不传
  "message": "用户消息"
}
```

**响应格式**：
```json
{
  "sessionId": "xxx-xxx-xxx",
  "reply": "助手回复"
}
```

### 3.5 前端集成

**文件**: `ui/main/main.js:470-505`

修改 `send-message` 处理：

```javascript
ipcMain.handle('send-message', async (event, message) => {
  return new Promise((resolve) => {
    // 从配置加载 sessionId
    const data = JSON.stringify({ 
      sessionId: config.chatSessionId || null, 
      message: message 
    });
    
    // 调用 /api/chat/session
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/session',  // 新端点
      method: 'POST',
      // ...
    }, (res) => {
      // 处理响应
      const result = JSON.parse(body);
      
      // 保存 sessionId 到配置文件
      if (result.sessionId) {
        config.chatSessionId = result.sessionId;
        saveConfig(config);
      }
      
      resolve(result);
    });
  });
});
```

**配置存储**: `ui/main/window-config.json`

```json
{
  "spirit": { "x": null, "y": null },
  "chat": { "x": null, "y": null, "width": 400, "height": 500 },
  "sticky": { "x": null, "y": null },
  "stickySize": 80,
  "chatSessionId": "xxx-xxx-xxx"  // 新增字段
}
```

### 3.6 数据库迁移

**文件**: `pkg/session/store.go:45`

```go
if err := tx.AutoMigrate(&Session{}, &Token{}, &WorkflowRun{}, &Message{}); err != nil {
    return nil, fmt.Errorf("failed to migrate schema: %w", err)
}
```

---

## 4. Phase 2: 智能记忆管理（下一步）

### 4.1 目标

1. **记忆提取**：从对话中自动提取重要信息
2. **语义存储**：将记忆存储到向量数据库
3. **智能注入**：根据上下文自动注入相关记忆

### 4.2 架构设计

详见 `docs/feature-context-management.md`

---

## 5. 实现总结

### 5.1 Phase 1 完成情况

| 任务 | 状态 | 改动文件 |
|------|------|----------|
| 消息类型定义 | ✅ 完成 | `pkg/session/types.go` |
| 数据库迁移 | ✅ 完成 | `pkg/session/store.go` |
| 消息 CRUD 方法 | ✅ 完成 | `pkg/session/store.go` |
| API 端点 | ✅ 完成 | `main.go` |
| 前端集成 | ✅ 完成 | `ui/main/main.js` |
| 配置存储 | ✅ 完成 | `ui/main/window-config.json` |

### 5.2 关键改动

1. **新增文件**：无（所有改动都在现有文件中）
2. **新增代码**：约 200 行
3. **修改文件**：4 个
4. **测试结果**：✅ 通过，性能优于原版本

### 5.3 下一步

1. 专家团队评审设计文档
2. 实现 Phase 2：智能记忆管理
3. 优化上下文压缩策略

---

## 附录

### A. 相关文档

- `docs/feature-context-management.md` — 智能上下文管理设计
- `docs/design-memory-system.md` — 记忆系统完整设计

### B. 备份分支

- `backup/nanobot-original` — 包含原始 NanoBOT 代码和设计文档

---

*文档结束*
