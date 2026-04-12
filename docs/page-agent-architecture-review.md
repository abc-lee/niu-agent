# Page-Agent 架构审核报告

> 审核日期：2026-04-11
> 审核范围：从 Python API 到 Chrome Extension 的完整调用链

---

## 一、架构层次总览

```
┌─────────────────────────────────────────────────────────────────┐
│ 层次 1: Python API (niu_api)                                    │
│ 端口: 9876                                                      │
│ 功能:                                                           │
│   - /kb/search                  知识库搜索                      │
│   - /proxy/v1/chat/completions  OpenAI 代理（给扩展使用）       │
│   - /api/async-task/notify      异步任务完成通知                │
│   - /chat/sync                  同步聊天（激活主 Agent）        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ 层次 2: Python 包装器 (niu_page_agent.py)                       │
│ 功能: 提供 Python 函数接口                                       │
│   - execute_task(task) -> str                                   │
│   - get_status() -> str                                         │
│   - stop_task() -> str                                          │
└─────────────────────────────────────────────────────────────────┘
                              ↓ HTTP (port 38402)
┌─────────────────────────────────────────────────────────────────┐
│ 层次 3: Node.js HTTP API (index.js)                             │
│ 端口: 38402                                                     │
│ 端点:                                                           │
│   - POST /execute   执行任务（同步等待）                         │
│   - GET  /status    获取状态                                     │
│   - POST /stop      停止任务                                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓ MCP stdio
┌─────────────────────────────────────────────────────────────────┐
│ 层次 4: Node.js MCP Server (index.js)                           │
│ 协议: stdio (JSON-RPC)                                          │
│ 工具:                                                           │
│   - execute_task  执行任务（异步，立即返回）                      │
│   - get_status    获取状态                                       │
│   - stop_task     停止任务                                       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ 层次 5: Hub Bridge (hub-bridge.js)                              │
│ 端口: 38401 (HTTP + WebSocket)                                  │
│ 功能:                                                           │
│   - HTTP 服务 launcher.html（触发扩展打开 hub）                  │
│   - WebSocket 与 Chrome Extension 通信                           │
│   - executeTask() 方法                                           │
└─────────────────────────────────────────────────────────────────┘
                              ↓ WebSocket
┌─────────────────────────────────────────────────────────────────┐
│ 层次 6: Chrome Extension                                        │
│ 功能:                                                           │
│   - 打开 hub.html 标签页                                         │
│   - 连接到 WebSocket Server (port 38401)                         │
│   - 在浏览器中执行实际操作                                        │
│   - 返回结果/错误                                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、通信路径分析

### 路径 1: 主 Agent 调用（推荐路径）

```
用户请求
    ↓
主 Agent (GenericAgent)
    ↓ ToolRegistry.get("page-agent-server/execute_task")
Node.js MCP Server (stdio)
    ↓ hub.executeTask()
Hub Bridge (WebSocket port 38401)
    ↓ WS send({ type: 'execute', task, config })
Chrome Extension
    ↓ 执行浏览器操作
返回结果
```

**特点**：
- ✅ 标准的 MCP 工具调用流程
- ✅ 与其他 MCP 服务器一致
- ⚠️ **关键差异**：`execute_task` 是异步的（立即返回）

**代码位置**：
- `agent/tool_registry.py` - 工具注册中心
- `mcp-servers/page-agent-mcp/src/index.js:402-455` - MCP 工具定义

---

### 路径 2: Python 直接调用（遗留路径）

```
Python 代码
    ↓ execute_task(task)
Python 包装器 (niu_page_agent.py)
    ↓ HTTP POST http://localhost:38402/execute
Node.js HTTP API
    ↓ hub.executeTask()
Hub Bridge (WebSocket port 38401)
    ↓
Chrome Extension
    ↓
返回结果
```

**特点**：
- ✅ 简单的 HTTP 调用
- ✅ 适合测试和调试
- ⚠️ **不经过 MCP 协议**

**代码位置**：
- `mcp-servers/page-agent-mcp/src/niu_page_agent.py:142-166` - Python 包装器
- `mcp-servers/page-agent-mcp/src/index.js:336-369` - HTTP API 端点

---

## 三、同步/异步设计问题 ⚠️

### 问题 1: HTTP `/execute` vs MCP `execute_task` 的语义矛盾

| 接口 | 类型 | 超时 | 返回值 |
|------|------|------|--------|
| HTTP `POST /execute` | **同步** | 120秒 | `{ success, data }` |
| MCP `execute_task` | **异步** | 立即 | `{ success: true, message: "Task started..." }` |

**问题分析**：

1. **HTTP `/execute` 同步等待**（index.js:336-369）：
   ```javascript
   // 同步执行（等待完成）
   const result = await hub.executeTask(enhancedTask, proxyConfig)

   res.writeHead(200, { 'Content-Type': 'application/json' })
   res.end(JSON.stringify({
       success: result.success,
       data: result.data
   }))
   ```

2. **MCP `execute_task` 异步立即返回**（index.js:412-455）：
   ```javascript
   // 异步执行（不等待）
   hub.executeTask(enhancedTask, proxyConfig)
       .then(result => {
           // 任务完成，通知主 API
           notifyTaskComplete(result)
       })
       .catch(error => {
           // 任务失败
           notifyTaskFailed(error.message)
       })

   // 立即返回
   return {
       content: [{ type: 'text', text: JSON.stringify({
           success: true,
           message: 'Task started in background. Will notify when done.'
       })}]
   }
   ```

**核心矛盾**：
- HTTP `/execute` 假设任务是**短期的、可等待的**（2分钟超时）
- MCP `execute_task` 假设任务是**长期的、后台的**（立即返回）
- **但两者底层调用的是同一个 `hub.executeTask()` 方法！**

---

### 问题 2: 异步通知机制的设计问题

**预期流程**（index.js:175-212）：
```
1. MCP execute_task 立即返回
2. 后台执行任务
3. 任务完成 → notifyTaskComplete()
4. 调用 /api/async-task/notify
5. 调用 /chat/sync 激活主 Agent
6. 主 Agent 处理结果并回复用户
```

**问题点**：

1. **没有任务 ID 跟踪**：
   - 如果用户发起多个任务，无法区分哪个任务完成了
   - 没有任务队列和状态管理

2. **通知机制不可靠**：
   - 如果 `/api/async-task/notify` 调用失败，任务结果丢失
   - 没有重试机制
   - 没有持久化

3. **主 Agent 被动激活**：
   - `/chat/sync` 会强制激活主 Agent
   - 但此时主 Agent 可能正在处理其他任务
   - **上下文混乱风险**

---

### 问题 3: Hub Bridge 的超时设计

**代码位置**：`hub-bridge.js:133-151`

```javascript
// 添加超时保护（2分钟）
return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
        this.#pendingTask = null
        reject(new Error('Task execution timed out after 120s'))
    }, 120000)

    this.#pendingTask = {
        resolve: (r) => {
            clearTimeout(timeout)
            resolve(r)
        },
        reject: (e) => {
            clearTimeout(timeout)
            reject(e)
        }
    }
    this.#hub.send(JSON.stringify({ type: 'execute', task, config }))
})
```

**问题分析**：

1. **超时时间是硬编码的**：
   - HTTP `/execute` 超时 120 秒
   - MCP `execute_task` 也受这个超时影响（即使标记为异步）
   - **但如果 MCP 立即返回，超时应该没有意义**

2. **只有一个 pendingTask**：
   - `this.#pendingTask` 只能存储一个任务
   - **不支持并发任务**
   - 如果在任务执行期间又收到新任务，会抛出 "Agent is already running a task" 错误

3. **超时后状态不一致**：
   - 超时后清理 `pendingTask`，但浏览器扩展可能还在执行
   - 没有真正的"停止"机制（`stopTask()` 只是发送信号）

---

## 四、架构设计矛盾总结

### 矛盾 1: 同步 vs 异步的定位混乱

| 层次 | 假设 | 实际行为 |
|------|------|----------|
| HTTP `/execute` | 短期任务（2分钟） | 同步等待 |
| MCP `execute_task` | 长期后台任务 | 异步立即返回 |
| Hub Bridge | 支持 2 分钟超时 | 单任务模型 |
| Chrome Extension | ？ | 由用户操作决定 |

**核心问题**：
- **没有明确"什么样的任务应该用哪个接口"**
- 如果是短期任务（如"点击按钮"），同步等待是合理的
- 如果是长期任务（如"填写复杂表单"），异步通知更好
- **但当前设计混用了两种模式，导致混乱**

---

### 矛盾 2: 任务粒度的不一致

**schema 描述**（niu_page_agent.py:28-48）：
```
"Execute a browser automation task in interactive mode.

BEHAVIOR:
- Simple operations (navigate, click, read): usually 5-15 seconds
- Complex forms: may take 1-2 minutes to complete
- If initial method fails: browser agent MAY try alternative approaches
- Each call: independent with 2-minute timeout (auto-resets per call)

YOU (Main Agent) CONTROL THE WORKFLOW:
- Break complex tasks into smaller steps
- Each execute_task call is a checkpoint
- If timeout or error: analyze result and decide next steps
- Total control is YOURS through multiple calls
"
```

**问题分析**：

1. **schema 说的是"交互模式"**：
   - 建议主 Agent 拆分任务
   - 每个 `execute_task` 是一个检查点
   - 主 Agent 保留控制权

2. **但 MCP 实现是"异步后台模式"**：
   - 立即返回，不等待结果
   - 主 Agent 丢失控制权
   - 依赖通知机制（不可靠）

3. **HTTP 实现是"同步等待模式"**：
   - 等待结果返回
   - 主 Agent 保留控制权
   - 但超时机制限制了任务复杂度

**结论**：**schema 描述与 MCP 实现不匹配！**

---

### 矛盾 3: 知识库注入的位置问题

**当前实现**（index.js:132-169）：
```javascript
async function enhanceTaskWithKnowledge(task) {
    // 提取查询关键词
    const queries = extractKnowledgeQueries(task)

    // 查询知识库
    const knowledgeParts = []
    for (const query of queries) {
        const knowledge = await queryKnowledgeBase(query)
        if (knowledge) {
            knowledgeParts.push(`【${query}】\n${knowledge}`)
        }
    }

    // 注入知识库内容到任务描述
    const enhancedTask = `
${task}

---

【知识库参考】
以下是相关知识库内容，请参考这些信息完成任务：

${knowledgeParts.join('\n\n---\n\n')}
`
    return enhancedTask
}
```

**问题分析**：

1. **关键词提取过于简单**（index.js:102-125）：
   - 只匹配固定的正则表达式模式
   - 无法理解任务语义

2. **知识库查询是串行的**：
   - 多个关键词顺序查询，延迟累加
   - 应该并行查询

3. **注入位置在 Node.js 层**：
   - 为什么不在 Python API 层注入？
   - Python 层有更丰富的上下文信息（会话历史、用户偏好）
   - Node.js 层只有原始任务描述

4. **硬编码的系统提示词**（index.js:28-52）：
   - `SYSTEM_PROMPTS.knowledge_enhanced` 是固定的
   - 没有根据任务类型动态调整

---

### 矛盾 4: OpenAI 代理的冗余设计

**当前设计**（page_agent_proxy.py + index.js:347-353）：

```
Chrome Extension
    ↓ 需要 LLM 支持
Node.js MCP Server
    ↓ proxyConfig = { baseURL: 'http://localhost:9876/proxy/v1', ... }
Python API (/proxy/v1/chat/completions)
    ↓ 转换格式
LiteLLM Session
    ↓ 调用 LLM
返回结果
```

**问题分析**：

1. **为什么要绕一圈？**：
   - Chrome Extension 需要 LLM 支持
   - 但 Extension 的配置被 Node.js 强制覆盖为本地代理
   - 然后本地代理又转换格式调用 LiteLLM
   - **为什么不让 Extension 直接调用 LLM API？**

2. **可能的理由**：
   - 为了统一管理 API Key（避免在 Extension 中暴露）
   - 为了记录日志和监控
   - 为了使用本地配置的模型

3. **设计矛盾**：
   - 如果是为了安全（隐藏 API Key），那应该在 Node.js 层就处理，不需要回到 Python
   - 如果是为了灵活性（切换模型），那 Python 代理确实合理
   - **但当前实现是：Node.js 强制覆盖配置 → Python 代理读取配置 → 又用回原来的配置**
   - **完全没有意义！**

---

## 五、推荐的解决方案

### 方案 1: 明确同步/异步的使用场景

**建议**：

1. **保留 HTTP `/execute` 为同步模式**：
   - 适合短期任务（< 2分钟）
   - 适合需要立即返回结果的场景
   - 主 Agent 保留控制权

2. **MCP `execute_task` 改为同步模式**（与 HTTP 一致）：
   - 等待任务完成再返回
   - 符合其他 MCP 工具的行为（如 `file-parser/parse_file`）
   - 简化架构，减少异步通知的复杂性

3. **如果确实需要异步模式**：
   - 新增 MCP 工具 `execute_task_async`
   - 明确区分两种模式的用途
   - 实现可靠的任务队列和状态管理

---

### 方案 2: 修复任务粒度的不一致

**建议**：

1. **更新 schema 描述**：
   - 明确 `execute_task` 是同步工具
   - 建议 Agent 拆分复杂任务
   - 提供超时处理指导

2. **实现智能超时**：
   - 根据任务类型动态调整超时时间
   - 提供进度反馈（通过 WebSocket）
   - 允许用户手动延长超时

3. **改进并发支持**：
   - 支持多个 `pendingTask`（通过任务 ID 管理）
   - 或者明确限制：同时只能执行一个浏览器任务

---

### 方案 3: 优化知识库注入位置

**建议**：

1. **移到 Python API 层**：
   - 在主 Agent 调用 `execute_task` 前注入知识库
   - 利用会话上下文理解任务语义
   - 避免在 Node.js 层重复查询

2. **或者完全移除**：
   - 如果主 Agent 已经有能力查询知识库
   - 为什么要在 `execute_task` 内部再查一次？
   - **职责应该清晰划分**

---

### 方案 4: 简化 OpenAI 代理设计

**建议**：

1. **如果目的是隐藏 API Key**：
   - Node.js 直接调用 LLM API
   - 不需要回到 Python 代理

2. **如果目的是统一管理配置**：
   - 保留当前设计，但简化参数传递
   - Node.js 只需要传递任务描述
   - Python 代理自动使用配置

3. **当前的最佳实践**：
   - 保留 Python 代理（为了灵活性和监控）
   - 但优化调用链，减少不必要的格式转换

---

## 六、优先级建议

| 问题 | 严重性 | 建议优先级 |
|------|--------|-----------|
| MCP `execute_task` 异步行为与 schema 不匹配 | 🔴 高 | P0 - 立即修复 |
| 异步通知机制不可靠 | 🔴 高 | P0 - 立即修复 |
| HTTP 同步 vs MCP 异步的语义矛盾 | 🟡 中 | P1 - 近期修复 |
| 单任务模型限制并发 | 🟡 中 | P1 - 近期修复 |
| 知识库注入位置不合理 | 🟢 低 | P2 - 后续优化 |
| OpenAI 代理冗余 | 🟢 低 | P2 - 后续优化 |

---

## 七、附录：关键代码位置

### Python 层

| 文件 | 功能 | 行数 |
|------|------|------|
| `niu_api/__main__.py` | API 主入口 | 68-83 |
| `niu_api/kb.py` | 知识库 API | 全文 |
| `niu_api/page_agent_proxy.py` | OpenAI 代理 | 全文 |
| `niu_api/async_task_api.py` | 异步任务通知 | 全文 |
| `mcp-servers/page-agent-mcp/src/niu_page_agent.py` | Python 包装器 | 全文 |

### Node.js 层

| 文件 | 功能 | 关键行数 |
|------|------|---------|
| `mcp-servers/page-agent-mcp/src/index.js` | MCP Server + HTTP API | 336-369 (HTTP), 402-455 (MCP) |
| `mcp-servers/page-agent-mcp/src/hub-bridge.js` | WebSocket 桥接 | 全文 |

### Chrome Extension

（未在代码库中找到源码，可能是编译后的扩展）

---

## 八、总结

### 核心问题

1. **同步/异步语义混乱**：HTTP 同步 vs MCP 异步，但底层是同一个方法
2. **任务粒度不一致**：schema 说交互模式，实现是后台模式
3. **异步通知不可靠**：没有任务 ID、没有重试、没有持久化
4. **架构层次冗余**：知识库注入位置不合理、OpenAI 代理绕圈

### 推荐方向

1. **统一为同步模式**：最简单，符合其他 MCP 工具的行为
2. **或者明确区分两种模式**：新增 `execute_task_async` 工具
3. **改进任务管理**：支持并发、可靠的异步通知、任务状态跟踪
4. **优化架构层次**：移除冗余的中间层，职责清晰划分

### 下一步行动

1. 确认产品需求：**Page-Agent 应该支持什么样的任务？**
   - 短期交互式任务（点击、读取）→ 同步模式
   - 长期后台任务（复杂表单、多步骤流程）→ 异步模式

2. 根据需求调整架构：
   - 如果主要是短期任务 → 统一为同步模式
   - 如果需要长期任务 → 实现可靠的异步机制

3. 更新文档和 schema：
   - 明确告知 Agent 如何使用
   - 提供最佳实践示例
