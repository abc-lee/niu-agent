# Page-Agent MCP 源代码分析

**分析时间**: 2026-04-11
**源代码版本**: v1.7.1
**仓库地址**: https://github.com/alibaba/page-agent

---

## 一、项目结构

```
packages/mcp/
├── README.md              # 使用文档
├── package.json           # NPM 包配置
└── src/
    ├── index.js          # MCP 服务器入口
    ├── hub-bridge.js     # WebSocket 桥接层
    └── launcher.html     # 启动器页面（模板）
```

---

## 二、核心组件分析

### 2.1 MCP 服务器入口 (`index.js`)

**主要职责**:
1. 启动 MCP Server (stdio 协议)
2. 启动 HTTP + WebSocket 服务器
3. 注册 MCP 工具
4. 处理工具调用请求

**关键代码**:

```javascript
// 1. 创建 HubBridge (WebSocket 服务器)
const hub = new HubBridge(port)
await hub.start()

// 2. 打开启动器页面
const url = `http://localhost:${port}`
exec(`${cmd} "${url}"`)  // 在默认浏览器中打开

// 3. 创建 MCP Server
const mcpServer = new McpServer({ name: 'page-agent', version: '1.5.8' })

// 4. 注册工具
mcpServer.registerTool('execute_task', {...}, async ({ task }) => {...})
mcpServer.registerTool('get_status', {...}, async () => {...})
mcpServer.registerTool('stop_task', {...}, async () => {...})

// 5. 连接到 stdio
const transport = new StdioServerTransport()
await mcpServer.connect(transport)
```

**环境变量配置**:
```javascript
const env = process.env
const llmConfig = {}
if (env.LLM_BASE_URL) llmConfig.baseURL = env.LLM_BASE_URL
if (env.LLM_MODEL_NAME) llmConfig.model = env.LLM_MODEL_NAME
if (env.LLM_API_KEY) llmConfig.apiKey = env.LLM_API_KEY
```

---

### 2.2 HubBridge (`hub-bridge.js`)

**主要职责**:
1. 提供 HTTP 服务器（提供启动器页面）
2. 提供 WebSocket 服务器（与浏览器扩展通信）
3. 管理任务执行状态

**架构**:

```
┌──────────────┐
│  HTTP Server │ → 提供 launcher.html
└──────┬───────┘
       │
┌──────▼───────┐
│ WebSocket    │ → 与 Hub Tab 通信
│ Server       │
└──────────────┘
```

**关键方法**:

```javascript
class HubBridge {
  // 启动服务器
  async start() {
    return new Promise((resolve, reject) => {
      this.#httpServer.listen(this.port, LOOPBACK_HOST, () => {
        resolve()
      })
    })
  }

  // 执行任务
  async executeTask(task, config) {
    if (!this.connected) throw new Error('Hub is not connected')
    if (this.#pendingTask) throw new Error('Agent is already running a task.')

    return new Promise((resolve, reject) => {
      this.#pendingTask = { resolve, reject }
      this.#hub.send(JSON.stringify({ type: 'execute', task, config }))
    })
  }

  // 停止任务
  stopTask() {
    if (this.connected) {
      this.#hub.send(JSON.stringify({ type: 'stop' }))
    }
  }
}
```

**WebSocket 消息协议**:

```javascript
// 发送到 Hub Tab
{
  type: 'execute',
  task: '自然语言任务描述',
  config: { baseURL, model, apiKey }  // 可选
}

{
  type: 'stop'
}

// 从 Hub Tab 接收
{
  type: 'result',
  success: boolean,
  data: string
}

{
  type: 'error',
  message: string
}
```

---

## 三、MCP 工具详细分析

### 3.1 `execute_task` 工具

**Schema 定义**:
```javascript
{
  description: "Execute a task in user's browser.",
  inputSchema: {
    task: z.string().describe(
      'Task description. Give specific instructions for the task. ' +
      'Steps preferable. And the information you want to get after the task is done.'
    ),
  },
}
```

**实现逻辑**:
```javascript
async ({ task }) => {
  try {
    const config = Object.keys(llmConfig).length > 0 ? llmConfig : undefined
    const result = await hub.executeTask(task, config)
    return {
      content: [{
        type: 'text',
        text: result.success
          ? `Task completed.\n\n${result.data}`
          : `Task failed.\n\n${result.data}`,
      }],
    }
  } catch (err) {
    return {
      content: [{ type: 'text', text: `Error: ${err.message}` }],
      isError: true,
    }
  }
}
```

**限制**:
- ❌ 只接受字符串参数，无法传入结构化数据
- ❌ 无法传入回调函数或自定义逻辑
- ❌ 任务执行完全由浏览器扩展控制

---

### 3.2 `get_status` 工具

**Schema 定义**:
```javascript
{
  description: 'Check the current status of the Page Agent hub.',
}
```

**实现逻辑**:
```javascript
async () => ({
  content: [{
    type: 'text',
    text: JSON.stringify({ connected: hub.connected, busy: hub.busy }, null, 2),
  }],
})
```

**返回值示例**:
```json
{
  "connected": true,
  "busy": false
}
```

---

### 3.3 `stop_task` 工具

**Schema 定义**:
```javascript
{
  description: 'Stop the currently running browser automation task.',
}
```

**实现逻辑**:
```javascript
async () => {
  hub.stopTask()
  return { content: [{ type: 'text', text: 'Stop signal sent.' }] }
}
```

---

## 四、自定义工具注入可行性分析

### 4.1 当前架构的限制

**问题 1: 工具注册在编译时固定**
```javascript
// 工具在服务器启动时就已注册
mcpServer.registerTool('execute_task', {...}, async ({ task }) => {...})
mcpServer.registerTool('get_status', {...}, async () => {...})
mcpServer.registerTool('stop_task', {...}, async () => {...})

// 没有暴露运行时注册接口
```

**问题 2: HubBridge 不支持工具传递**
```javascript
// executeTask 只接受字符串和配置
async executeTask(task, config) {
  // ...
  this.#hub.send(JSON.stringify({ type: 'execute', task, config }))
  //      没有传递工具列表 ↑
}
```

**问题 3: 浏览器扩展架构限制**
- Hub Tab 使用 MultiPage Agent，有自己的工具集
- WebSocket 协议不支持动态工具注入
- 扩展端的工具定义在编译时固定

---

### 4.2 改造方案

#### 方案 A: 扩展 MCP 工具列表

**修改点 1**: 添加工具注册 API

```javascript
// 在 index.js 中添加
const customTools = []

mcpServer.registerTool(
  'register_custom_tool',
  {
    description: 'Register a custom browser automation tool',
    inputSchema: {
      name: z.string().describe('Tool name'),
      description: z.string().describe('Tool description'),
      code: z.string().describe('JavaScript code to execute'),
    },
  },
  async ({ name, description, code }) => {
    customTools.push({ name, description, code })
    return { content: [{ type: 'text', text: `Tool '${name}' registered.` }] }
  }
)
```

**修改点 2**: 修改 execute_task 支持工具选择

```javascript
async ({ task, tools }) => {
  const result = await hub.executeTask(task, {
    ...config,
    tools: tools || customTools,  // 传入自定义工具
  })
  // ...
}
```

**挑战**:
- 需要修改 WebSocket 协议
- 需要修改浏览器扩展代码
- 工作量大，可能破坏现有功能

---

#### 方案 B: 直接暴露底层操作

**修改点**: 注册细粒度工具

```javascript
mcpServer.registerTool(
  'browser_navigate',
  {
    description: 'Navigate to a URL',
    inputSchema: { url: z.string().url() },
  },
  async ({ url }) => {
    const result = await hub.executeCommand('navigate', { url })
    return { content: [{ type: 'text', text: result.data }] }
  }
)

mcpServer.registerTool(
  'browser_click',
  {
    description: 'Click an element',
    inputSchema: {
      selector: z.string().describe('CSS selector'),
    },
  },
  async ({ selector }) => {
    const result = await hub.executeCommand('click', { selector })
    return { content: [{ type: 'text', text: result.data }] }
  }
)

// ... 更多细粒度工具
```

**挑战**:
- 需要修改 HubBridge，添加 `executeCommand` 方法
- 需要修改浏览器扩展，支持命令式调用
- 工作量巨大，等于重写核心逻辑

---

#### 方案 C: 支持工具代码注入

**修改点**: 传递可执行代码

```javascript
mcpServer.registerTool(
  'browser_execute',
  {
    description: 'Execute custom JavaScript in the browser',
    inputSchema: {
      code: z.string().describe('JavaScript code to execute'),
    },
  },
  async ({ code }) => {
    const result = await hub.executeCode(code)
    return { content: [{ type: 'text', text: result.data }] }
  }
)
```

**优势**:
- 灵活性最高
- 改造成本相对较低

**挑战**:
- 安全风险（恶意代码注入）
- 需要添加沙箱隔离机制
- 用户体验不如声明式工具

---

## 五、结论

### 5.1 技术限制

**Page-Agent 的设计理念决定了它不适合自定义工具注入**:

1. **自然语言优先**: 架构围绕自然语言任务描述设计
2. **封闭生态**: 浏览器扩展 + MCP Server 的封闭架构
3. **粗粒度抽象**: 只暴露高层任务执行接口

### 5.2 改造成本评估

| 改造方案 | 工作量 | 风险 | 可行性 |
|---------|-------|------|-------|
| 方案 A: 扩展工具列表 | 高 | 高 | 低 |
| 方案 B: 暴露底层操作 | 极高 | 极高 | 低 |
| 方案 C: 代码注入 | 中 | 高 | 中 |

### 5.3 最终建议

**不推荐改造 Page-Agent**，原因：

1. **架构不匹配**: 需要彻底重构核心逻辑
2. **维护成本高**: 需要 fork 并持续维护
3. **有更好的替代**: Playwright-MCP 开箱即用

**推荐使用 Playwright-MCP**:
- 官方维护，质量有保证
- 丰富的工具集，支持细粒度操作
- 社区活跃，问题易解决

---

**文档生成时间**: 2026-04-11 19:20:00
**分析工具**: 源代码阅读 + 架构分析
**参考版本**: page-agent v1.7.1
