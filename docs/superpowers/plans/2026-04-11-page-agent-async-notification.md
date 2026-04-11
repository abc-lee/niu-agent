# Page-Agent 异步任务通知机制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Page-Agent 能够异步执行任务，完成后通知主 Agent 生成回复，照搬定时任务的成功模式。

**Architecture:**
1. Page-Agent 完成任务后，调用通知 API
2. 通知 API 照搬定时任务的 trigger_callback：构建提示词 → 调用 /chat/sync → 添加到 pending_alerts
3. 主 Agent 被激活，生成回复，前端轮询获取

**Tech Stack:** Python (FastAPI), JavaScript (Node.js MCP Server), HTTP 通信

---

## 文件结构

**需要创建的文件**：
- 无（复用现有文件）

**需要修改的文件**：
- `mcp-servers/page-agent-mcp/src/index.js` - 修改 notifyTaskComplete，删除错误的 session 相关代码
- `niu_api/async_task_api.py` - 重写为照搬 trigger_callback 的通知 API

**需要保留的功能**：
- `mcp-servers/page-agent-mcp/src/index.js` 中的知识库注入功能（正确实现）

**需要删除的文件**：
- `niu_api/async_tasks.py` - AsyncTaskManager 类（不需要，主 Agent 无 session 概念）

---

## Task 1: 清理错误的实现

**Files:**
- Delete: `niu_api/async_tasks.py`
- Modify: `niu_api/__main__.py:33` - 删除 async_task_router 导入

- [ ] **Step 1: 删除不需要的 async_tasks.py**

```bash
rm E:/tools/ai-bot/niu_api/async_tasks.py
```

- [ ] **Step 2: 从 __main__.py 中删除 async_task_router**

```python
# E:/tools/ai-bot/niu_api/__main__.py
# 删除这一行：
from niu_api.async_task_api import router as async_task_router

# 删除这一行：
app.include_router(async_task_router)  # Async Task API for Page-Agent
```

- [ ] **Step 3: 提交清理**

```bash
cd E:/tools/ai-bot
git add -A
git commit -m "refactor: remove incorrect async_tasks implementation

- Delete async_tasks.py (no session concept in main agent)
- Remove async_task_router from __main__.py
- Will re-implement based on scheduler trigger_callback pattern"
```

---

## Task 2: 重写通知 API（照搬 trigger_callback）

**Files:**
- Modify: `niu_api/async_task_api.py` - 完全重写

- [ ] **Step 1: 重写 async_task_api.py（照搬 scheduler/service.py 的 trigger_callback）**

```python
"""
异步任务通知 API

完全照搬定时任务的 trigger_callback 实现：
1. 构建提示词
2. 调用 /chat/sync 激活主 Agent
3. 添加到 pending_alerts 队列
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import requests
from niu_api.alerts import add_pending_alert

router = APIRouter(prefix="/api/async-task", tags=["async-tasks"])


class TaskNotifyRequest(BaseModel):
    """异步任务完成通知请求"""
    type: str  # "task_complete" | "task_failed"
    task_id: str
    result: Optional[str] = None
    error: Optional[str] = None


@router.post("/notify")
async def notify_async_task(request: TaskNotifyRequest):
    """
    异步任务完成通知（照搬 scheduler/service.py 的 trigger_callback）

    参考：niu_api/internal/scheduler/service.py:trigger_callback
    """
    # 1. 构建提示词（照搬定时任务）
    if request.type == "task_complete":
        prompt = f"🔔 异步任务完成：\n{request.result}\n\n请根据这个结果，给用户一个友好的回复。"
    else:
        prompt = f"⚠️ 异步任务失败：\n{request.error}\n\n请告知用户任务执行失败。"

    # 2. 调用 /chat/sync（激活主 Agent，照搬定时任务）
    try:
        response = requests.post(
            "http://localhost:9876/chat/sync",
            json={
                "session_id": "default",  # session_id 被 ignore
                "message": prompt
            },
            timeout=30
        )

        if response.status_code == 200:
            agent_reply = response.json().get("reply", "")

            # 3. 添加到 pending_alerts（照搬定时任务）
            if agent_reply:
                add_pending_alert(agent_reply)

            return {"success": True, "reply": agent_reply}
        else:
            # 即使 API 出错，也发送基础提醒（照搬定时任务）
            fallback_msg = f"异步任务完成：{request.result[:200] if request.result else request.error}"
            add_pending_alert(fallback_msg)
            return {"success": False, "error": f"Chat API returned {response.status_code}"}

    except Exception as e:
        # 异常处理（照搬定时任务）
        fallback_msg = f"异步任务完成：{request.result[:200] if request.result else request.error}"
        add_pending_alert(fallback_msg)
        return {"success": False, "error": str(e)}
```

- [ ] **Step 2: 提交重写**

```bash
cd E:/tools/ai-bot
git add niu_api/async_task_api.py
git commit -m "feat: implement async-task notify API based on scheduler pattern

- Rewrite async_task_api.py to match trigger_callback pattern
- Build prompt -> call /chat/sync -> add to pending_alerts
- Follow scheduler/service.py implementation exactly"
```

---

## Task 3: 修改 MCP Server 的通知函数

**Files:**
- Modify: `mcp-servers/page-agent-mcp/src/index.js:173-201` - 修改 notifyTaskComplete 和 notifyTaskFailed

- [ ] **Step 1: 修改 notifyTaskComplete 函数（照搬定时任务）**

```javascript
/**
 * 通知主 Agent 任务完成（照搬定时任务的 trigger_callback）
 */
async function notifyTaskComplete(taskId, result) {
    try {
        // 调用主 API 的通知接口（类似 trigger_callback 调用 /chat/sync）
        await fetch('http://localhost:9876/api/async-task/notify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'task_complete',
                task_id: taskId,
                result: result.success ? result.data : null,
                error: result.success ? null : result.data
            })
        })
        console.error(`[async-task] Task ${taskId} completed, notified main agent`)
    } catch (error) {
        console.error(`[async-task] Failed to notify completion: ${error.message}`)
    }
}
```

- [ ] **Step 2: 修改 notifyTaskFailed 函数**

```javascript
/**
 * 通知主 Agent 任务失败
 */
async function notifyTaskFailed(taskId, errorMessage) {
    try {
        await fetch('http://localhost:9876/api/async-task/notify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'task_failed',
                task_id: taskId,
                error: errorMessage
            })
        })
        console.error(`[async-task] Task ${taskId} failed: ${errorMessage}`)
    } catch (error) {
        console.error(`[async-task] Failed to notify failure: ${error.message}`)
    }
}
```

- [ ] **Step 3: 提交修改**

```bash
cd E:/tools/ai-bot
git add mcp-servers/page-agent-mcp/src/index.js
git commit -m "feat: update notifyTaskComplete to match scheduler pattern

- Call /api/async-task/notify instead of direct alert
- Follow scheduler/service.py trigger_callback pattern
- Main agent will be activated via /chat/sync"
```

---

## Task 4: 重新注册路由

**Files:**
- Modify: `niu_api/__main__.py:33,165` - 重新注册 async_task_router

- [ ] **Step 1: 添加 async_task_router 导入**

```python
# E:/tools/ai-bot/niu_api/__main__.py
from niu_api.async_task_api import router as async_task_router
```

- [ ] **Step 2: 注册路由**

```python
# E:/tools/ai-bot/niu_api/__main__.py
app.include_router(async_task_router)  # Async Task API (scheduler pattern)
```

- [ ] **Step 3: 提交**

```bash
cd E:/tools/ai-bot
git add niu_api/__main__.py
git commit -m "feat: register async_task_router in main API"
```

---

## Task 5: 测试验证

**Files:**
- Create: `scripts/test_async_task_notification.py`

- [ ] **Step 1: 编写测试脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试异步任务通知机制

验证流程：
1. 模拟 Page-Agent 完成任务
2. 调用通知 API
3. 验证主 Agent 被激活
4. 验证 pending_alerts 队列
"""

import requests
import json

def test_notify_api():
    """测试通知 API"""
    print("\n" + "=" * 80)
    print("测试：异步任务完成通知")
    print("=" * 80)

    # 1. 模拟 Page-Agent 完成任务
    notification = {
        "type": "task_complete",
        "task_id": "test_task_123",
        "result": "MBTI测试完成，你的类型是 INFP"
    }

    print(f"\n1. 发送通知：{notification}")

    # 2. 调用通知 API
    response = requests.post(
        "http://localhost:9876/api/async-task/notify",
        json=notification,
        timeout=30
    )

    print(f"\n2. 通知 API 响应：{response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"   成功：{data['success']}")
        print(f"   主 Agent 回复：{data.get('reply', '')[:100]}")
    else:
        print(f"   失败：{response.text}")
        return False

    # 3. 验证 pending_alerts
    print("\n3. 检查 pending_alerts 队列...")
    alerts_response = requests.get("http://localhost:9876/api/pending-alerts")

    if alerts_response.status_code == 200:
        alerts = alerts_response.json()
        print(f"   队列中有 {len(alerts)} 条消息")
        if alerts:
            print(f"   第一条：{alerts[0]['content'][:100]}")
    else:
        print(f"   失败：{alerts_response.text}")

    return True


def test_failed_notification():
    """测试失败通知"""
    print("\n" + "=" * 80)
    print("测试：异步任务失败通知")
    print("=" * 80)

    notification = {
        "type": "task_failed",
        "task_id": "test_task_456",
        "error": "页面加载超时"
    }

    print(f"\n发送失败通知：{notification}")

    response = requests.post(
        "http://localhost:9876/api/async-task/notify",
        json=notification,
        timeout=30
    )

    if response.status_code == 200:
        data = response.json()
        print(f"成功：{data['success']}")
        return True
    else:
        print(f"失败：{response.text}")
        return False


if __name__ == "__main__":
    print("🔍 异步任务通知机制测试")
    print("=" * 80)

    print("\n前置条件：")
    print("1. python -m niu_api 已启动")
    print("2. 主 Agent 可正常工作")

    input("\n按回车继续...")

    success = test_notify_api()
    success = test_failed_notification() and success

    print("\n" + "=" * 80)
    print("✅ 测试通过" if success else "❌ 测试失败")
```

- [ ] **Step 2: 运行测试**

```bash
# 启动服务
python -m niu_api

# 在另一个终端运行测试
python scripts/test_async_task_notification.py
```

Expected output:
- 通知 API 返回 200
- 主 Agent 生成回复
- pending_alerts 队列有消息

- [ ] **Step 3: 提交测试脚本**

```bash
cd E:/tools/ai-bot
git add scripts/test_async_task_notification.py
git commit -m "test: add async task notification test script"
```

---

## Task 6: 更新文档

**Files:**
- Update: `docs/page-agent-async-complete-guide.md`

- [ ] **Step 1: 更新实施文档**

在文档中添加：

```markdown
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
```

- [ ] **Step 2: 提交文档更新**

```bash
cd E:/tools/ai-bot
git add docs/page-agent-async-complete-guide.md
git commit -m "docs: update async task implementation guide with completion status"
```

---

## 规格覆盖检查

✅ **已覆盖所有需求**：
1. ✅ 异步执行任务（execute_task_async 立即返回）
2. ✅ 任务完成通知主 Agent（照搬 trigger_callback）
3. ✅ 主 Agent 生成回复（/chat/sync 激活）
4. ✅ 前端轮询获取（pending_alerts 队列）
5. ✅ 知识库注入（保留现有实现）

---

## 占位符扫描

✅ **无占位符**：
- 所有代码完整
- 所有命令具体
- 所有路径精确

---

## 类型一致性

✅ **类型一致**：
- TaskNotifyRequest 在 async_task_api.py 定义
- notify_async_task 使用 TaskNotifyRequest
- 前端调用使用相同的 JSON 结构

---

## 执行选项

Plan complete and saved to `docs/superpowers/plans/2026-04-11-page-agent-async-notification.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
