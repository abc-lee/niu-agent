# Page-Agent 浏览器自动化集成设计

> 日期：2026-04-10
> 状态：待实现
> 负责人：Claude

---

## 一、概述

### 1.1 目标

将 Page-Agent（阿里巴巴开源的浏览器自动化框架）作为**子 Agent** 集成到 ai-bot 项目中。主 Agent 负责规划和编排，Page-Agent 子 Agent 负责执行浏览器操作并返回结构化结果。

### 1.2 核心技术方案

**架构模式**：子 Agent 模式（与 file-processor 相同）

- 主 Agent 调用 `call_subagent("browser-agent", task)`
- 子 Agent 创建独立 session，使用 MCP 工具执行
- 返回结构化结果（JSON 格式）

### 1.3 技术背景

**Page-Agent**：
- 纯 JavaScript 实现的 GUI Agent
- 支持自然语言控制浏览器
- 基于 DOM 解析（无需截图），Token 高效
- 提供 Chrome 扩展和 MCP Server
- MIT 开源协议

**Page-Agent 返回格式**（hub-bridge.js）：
```javascript
{
  success: boolean,
  data: string | object,  // 执行结果
  history: [              // 执行历史（用于调试）
    { type: "observation", content: "..." },
    { type: "error", message: "..." },
    { type: "retry", message: "...", attempt: N }
  ]
}
```

---

## 二、架构设计

### 2.1 整体架构

```
用户请求（"打开百度搜索Python"）
        ↓
主 Agent（niu）— 规划 + 编排
        ↓ call_subagent("browser-agent", task)
        ↓
Page-Agent 子 Agent（browser-agent）— 执行浏览器任务
        ↓ 调用 MCP 工具
        ↓ page-agent/execute_task
        ↓
Page-Agent MCP Server（Node.js）
        ↓ WebSocket
Hub Bridge（localhost:9520）
        ↓
Chrome 扩展（用户已安装）
        ↓
浏览器自动化执行
```

### 2.2 与 file-processor 的类比

| 维度 | file-processor | browser-agent |
|------|----------------|---------------|
| 定位 | 文件/照片处理专家 | 浏览器自动化专家 |
| 工具来源 | photo-server MCP | page-agent-mcp MCP |
| 输入 | 文件路径 + 操作类型 | 自然语言任务描述 |
| 输出 | JSON 结构化结果 | JSON 结构化结果 |
| 批量处理 | 目录级批量 | 多标签页并发 |

---

## 三、文件变更清单

### 3.1 新增文件

#### 3.1.1 `config/agents/browser-agent.md`

**子 Agent 配置文件**

#### 3.1.2 `mcp-servers/page-agent-mcp/src/__main__.py`

**Python 入口点**

#### 3.1.3 `mcp-servers/page-agent-mcp/pyproject.toml`

**项目配置**

### 3.2 配置变更

#### 3.2.1 `config/user-config.json`

增加 `openaiCompatibleApiBase` 字段：
```json
{
  "llm": {
    "apiKey": "...",
    "apiBase": "https://api.minimaxi.com/anthropic/v1/messages",
    "openaiCompatibleApiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "model": "MiniMax-M2-highspeed",
    "type": "anthropic"
  }
}
```

#### 3.2.2 `config/mcp-servers.yaml`

新增 page-agent-mcp 配置

#### 3.2.3 `config/agents/niu.md`

新增 browser-agent 子 Agent 引用

### 3.3 Page-Agent MCP Server 变更

#### `mcp-servers/page-agent-mcp/src/index.js`

增加读取 `user-config.json` 的 fallback 逻辑（约 20 行）

---

## 四、browser-agent 子 Agent 设计

### 4.1 核心原则

1. **子 Agent 是专家**：browser-agent 擅长浏览器自动化，不需要主 Agent 精细控制
2. **主 Agent 描述目标**：主 Agent 描述想要的结果，子 Agent 自己规划步骤
3. **结构化返回**：返回 JSON 格式结果，主 Agent 可编程处理

### 4.2 可用工具

| 工具 | 参数 | 返回值 |
|------|------|--------|
| `page-agent/execute_task` | `{ task: string }` | `{ success, data, history }` |
| `page-agent/get_status` | 无 | `{ connected, busy }` |
| `page-agent/stop_task` | 无 | `{ success, message }` |

### 4.3 返回格式

**成功**：
```json
{
  "success": true,
  "data": "已成功打开百度并搜索'Python教程'"
}
```

**失败**：
```json
{
  "success": false,
  "data": "InvokeError: Network request failed"
}
```

---

## 五、实施步骤

### Step 1: 配置变更

1. 更新 `config/user-config.json` 增加 `openaiCompatibleApiBase`
2. 更新 `config/llm-presets.json` 为相关预设增加该字段

### Step 2: 创建 Page-Agent MCP Server 结构

1. 创建 `mcp-servers/page-agent-mcp/src/__main__.py`
2. 创建 `mcp-servers/page-agent-mcp/pyproject.toml`
3. 修改 `mcp-servers/page-agent-mcp/src/index.js` 增加配置读取

### Step 3: 创建 browser-agent 配置

1. 创建 `config/agents/browser-agent.md`

### Step 4: 集成到主 Agent

1. 更新 `config/agents/niu.md` 引用 browser-agent

### Step 5: 测试验证

1. 端到端测试浏览器自动化流程

---

## 六、测试计划

### 6.1 单元测试

- 配置读取逻辑
- MCP 工具调用

### 6.2 集成测试

- 子 Agent 完整流程
- 浏览器操作（打开网页、填写表单、点击按钮）
- 批量处理

---

## 七、变更记录

| 日期 | 变更内容 | 负责人 |
|------|---------|--------|
| 2026-04-10 | 初始设计 | Claude |
