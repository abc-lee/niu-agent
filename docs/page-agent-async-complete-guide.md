# Page-Agent 异步模式 - 完整实现指南

## 🎯 核心设计

参考定时任务的通知机制：

```
定时任务到期
    ↓
Go 后台 goroutine 发送通知
    ↓
main.go 收到通知
    ↓
调用 toolloop.ChatWithToolLoop
    ↓
Agent 生成回复
    ↓
存储到 messages 表 + pendingAlerts 队列
    ↓
前端轮询 /api/pending-alerts
    ↓
显示给用户
```

**Page-Agent 异步任务照搬这个模式**：

```
主 Agent 提交异步任务
    ↓
MCP Server 返回 task_id（立即）
    ↓
后台执行任务（不阻塞）
    ↓
任务完成 → 通知主 API
    ↓
主 API 调用 Agent 生成友好消息
    ↓
存储到 messages 表 + pendingAlerts 队列
    ↓
前端轮询 → 显示给用户
```

---

## 📝 已实现的功能

### 1. 知识库注入 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

```javascript
// 任务执行前，自动注入知识库内容
const enhancedTask = await enhanceTaskWithKnowledge(task)

// 示例：
// 输入任务："完成MBTI人格测试"
// 增强后："完成MBTI人格测试
//          ---
//          【知识库参考】
//          MBTI（Myers-Briggs Type Indicator）...
//          外向型特征：...
//          内向型特征：..."
```

### 2. 异步任务工具 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

```javascript
// 新增工具：execute_task_async
mcpServer.registerTool('execute_task_async', {
    description: "Execute a task asynchronously in the background. Returns immediately with a task_id.",
    inputSchema: { task: z.string() }
}, async ({ task }) => {
    const taskId = `task_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`

    // 后台执行（不等待）
    hub.executeTask(enhancedTask, config)
        .then(result => notifyTaskComplete(taskId, result))
        .catch(error => notifyTaskFailed(taskId, error.message))

    // 立即返回
    return {
        content: [{
            type: 'text',
            text: JSON.stringify({
                success: true,
                task_id: taskId,
                status: 'pending'
            })
        }]
    }
})
```

### 3. 任务通知机制 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

```javascript
// 任务完成后，通知主 API
async function notifyTaskComplete(taskId, result) {
    await fetch('http://localhost:9876/async-task/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            task_id: taskId,
            result: result.success ? result.data : null,
            error: result.success ? null : result.data
        })
    })
}
```

### 4. 异步任务 API ✅

**文件**：`niu_api/async_task_api.py`

```python
@router.post("/complete")
async def complete_async_task(request: TaskCompleteRequest):
    """任务完成通知（参考定时任务设计）"""
    task_manager.complete_task(request.task_id, request.result)

    # 添加到待推送队列（类似定时任务）
    from niu_api.alerts_api import add_pending_alert
    add_pending_alert(f"[异步任务完成] {request.result[:200]}")

    return {"success": True}
```

### 5. 任务管理器 ✅

**文件**：`niu_api/async_tasks.py`

```python
class AsyncTaskManager:
    def __init__(self):
        self.tasks: Dict[str, AsyncTask] = {}

    def create_task(self, task_id: str, task: str) -> AsyncTask:
        """创建新任务"""

    def complete_task(self, task_id: str, result: str):
        """标记任务完成"""

    def get_task(self, task_id: str) -> Optional[AsyncTask]:
        """查询任务状态"""
```

---

## 🚀 使用示例

### 场景 1：同步执行（阻塞等待）

```python
# 主 Agent 调用
from mcp import Client

client = Client("http://localhost:38401")

# 同步执行（阻塞直到完成）
result = client.execute_task("""
打开百度首页
搜索 "MBTI人格测试"
返回搜索结果页面的标题
""")

print(result)  # "Task completed. 百度为您找到..."
```

**适用场景**：
- 快速任务（几秒内完成）
- 需要立即获取结果
- 不需要并行工作

### 场景 2：异步执行（后台工作）

```python
# 主 Agent 调用
result = client.execute_task_async("""
完成MBTI人格测试：
1. 打开 https://mbti-test.app/zh-cn/free-personality-test
2. 完成所有题目
3. 返回测试结果（人格类型）
""")

print(result)  # {"task_id": "task_1234567890_abc123", "status": "pending"}

# 主 Agent 可以继续工作...
do_other_work()

# 稍后查询结果
task_status = client.get_task_result("task_1234567890_abc123")
print(task_status)  # {"status": "completed", "result": "你的MBTI类型是..."}
```

**适用场景**：
- 长时间任务（几分钟）
- 需要并行处理其他工作
- 不需要立即获取结果

### 场景 3：知识库增强 + 异步执行

```python
# 任务包含 MBTI 关键词 → 自动注入知识库内容
result = client.execute_task_async("""
完成MBTI人格测试
""")

# MCP Server 自动执行：
# 1. 检测关键词 "MBTI"
# 2. 查询知识库：http://localhost:9876/kb/search?q=MBTI
# 3. 注入知识库内容到任务描述
# 4. 执行增强后的任务
# 5. 完成后通知主 API
# 6. 用户通过前端轮询看到结果
```

---

## 📊 架构流程图

```
┌─────────────────────────────────────────────────────────────────┐
│                        异步任务系统                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  主 Agent: execute_task_async("完成MBTI测试")                   │
│      ↓                                                          │
│  MCP Server:                                                    │
│      ├─ 检测关键词 "MBTI"                                       │
│      ├─ 查询知识库 API                                          │
│      ├─ 注入知识内容到任务                                       │
│      ├─ 后台执行任务（不等待）                                    │
│      └─ 立即返回 task_id                                        │
│      ↓                                                          │
│  主 Agent 继续工作...                                           │
│      ↓                                                          │
│  Page-Agent 完成任务（几分钟后）                                 │
│      ↓                                                          │
│  MCP Server 调用 notifyTaskComplete(task_id, result)           │
│      ↓                                                          │
│  POST http://localhost:9876/async-task/complete                │
│      ↓                                                          │
│  ┌─────────────────────────────────────┐                       │
│  │ async_task_api.py                   │                       │
│  │ ├─ task_manager.complete_task()     │                       │
│  │ └─ add_pending_alert(result)        │                       │
│  └─────────────────────────────────────┘                       │
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

---

## 🔧 关键代码位置

| 功能 | 文件 | 行数 |
|------|------|------|
| 知识库注入 | `mcp-servers/page-agent-mcp/src/index.js` | L29-169 |
| 异步任务工具 | `mcp-servers/page-agent-mcp/src/index.js` | L457-502 |
| 任务通知 | `mcp-servers/page-agent-mcp/src/index.js` | L173-201 |
| 异步任务 API | `niu_api/async_task_api.py` | 全文 |
| 任务管理器 | `niu_api/async_tasks.py` | 全文 |
| 知识库 API | `niu_api/kb.py` | 全文 |

---

## 🧪 测试步骤

### 测试 1：知识库注入

```bash
# 启动服务
python -m niu_api

# 查看日志
tail -f E:/tools/ai-bot/logs/api_stderr.log | grep "kb-"
```

**预期日志**：
```
[kb-enhance] Detected knowledge queries: MBTI
[kb-query] Found 2 results for: MBTI
[kb-enhance] Enhanced task with 1 knowledge items
```

### 测试 2：异步任务

```python
# scripts/test_async_task.py

from mcp import Client

client = Client("http://localhost:38401")

# 提交异步任务
result = client.execute_task_async("打开百度首页，返回标题")
print(result)  # {"task_id": "task_xxx", "status": "pending"}

# 查询状态
import time
time.sleep(5)  # 等待任务完成

status = client.get_task_result(result['task_id'])
print(status)  # {"status": "completed", "result": "百度一下，你就知道"}
```

### 测试 3：前端通知

```bash
# 前端轮询
curl http://localhost:9876/api/pending-alerts

# 预期返回
[
  {
    "content": "[异步任务完成] 百度一下，你就知道",
    "timestamp": "2026-04-11T15:30:00"
  }
]
```

---

## 📚 与定时任务的对比

| 特性 | 定时任务 | Page-Agent 异步任务 |
|------|---------|---------------------|
| 触发方式 | 时间到期 | 任务完成 |
| 通知机制 | Go channel | HTTP POST |
| Agent 处理 | toolloop.ChatWithToolLoop | 直接添加到队列 |
| 前端获取 | 轮询 /api/pending-alerts | 相同 |
| 消息存储 | messages 表 + pendingAlerts | 相同 |

**核心相同点**：
- ✅ 后台执行（不阻塞主流程）
- ✅ 完成后通知主 API
- ✅ 添加到 pendingAlerts 队列
- ✅ 前端轮询获取

---

## ✅ 总结

我已经完整实现了 **Page-Agent 异步模式**，照搬了定时任务的成功设计：

1. ✅ **知识库注入**：任务自动增强
2. ✅ **异步执行**：立即返回，后台工作
3. ✅ **任务通知**：完成后通知主 API
4. ✅ **消息推送**：添加到 pendingAlerts 队列
5. ✅ **前端获取**：轮询 /api/pending-alerts

**现在，主 Agent 可以：**
- ✅ 提交长时间任务给 Page-Agent
- ✅ 立即返回继续工作
- ✅ 任务完成后自动收到通知
- ✅ 知识库内容自动注入任务

**架构清晰、可靠，完全照搬定时任务的成熟方案！** 🎉

---

## 实施完成状态

✅ **已完成**：
1. 知识库注入功能（任务预处理）
2. 异步任务通知机制（照搬定时任务）
3. 主 Agent 激活流程（/chat/sync）
4. pending_alerts 推送队列

**核心实现**：
- `niu_api/async_task_api.py` - 通知 API（照搬 trigger_callback）
- `mcp-servers/page-agent-mcp/src/index.js` - notifyTaskComplete

**工作流程**：
```
Page-Agent 完成任务
    ↓
调用 /api/async-task/notify
    ↓
构建提示词："异步任务完成：{result}"
    ↓
POST /chat/sync（激活主 Agent）
    ↓
主 Agent 生成回复："你的 MBTI 测试完成了..."
    ↓
add_pending_alert(reply)
    ↓
前端轮询 /api/pending-alerts
    ↓
显示给用户
```

**参考实现**：
- `niu_api/internal/scheduler/service.py:trigger_callback` - 定时任务通知
- 本实现完全照搬该模式

**关键代码**：

```python
# niu_api/async_task_api.py
@router.post("/notify")
async def notify_async_task(request: TaskNotifyRequest):
    # 1. 构建提示词（照搬定时任务）
    if request.type == "task_complete":
        prompt = f"🔔 异步任务完成：\n{request.result}\n\n请根据这个结果，给用户一个友好的回复。"

    # 2. 调用 /chat/sync（激活主 Agent）
    response = requests.post("http://localhost:9876/chat/sync", json={...})

    # 3. 添加到 pending_alerts
    if agent_reply:
        add_pending_alert(agent_reply)
```

```javascript
// mcp-servers/page-agent-mcp/src/index.js
async function notifyTaskComplete(taskId, result) {
    await fetch('http://localhost:9876/api/async-task/notify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            type: 'task_complete',
            task_id: taskId,
            result: result.success ? result.data : null
        })
    })
}
```

**测试验证**：
- 测试脚本：`scripts/test_async_notify.py`
- 测试通过：API 响应正常，pending_alerts 正确添加
