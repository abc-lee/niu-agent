# Page-Agent 浏览器自动化集成设计

> 日期：2026-04-10
> 状态：设计中
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

## 四、典型使用场景

### 4.4 批量表单填写 / 知识竞赛答题

**场景**：用户提供题库，Agent 自动打开网页答题

**题库格式**（推荐 JSON）：
```json
{
  "quiz_url": "https://xxx.com/quiz/123",
  "questions": [
    {"id": 1, "type": "choice", "text": "中国的首都是？", "options": ["A. 北京", "B. 上海", "C. 广州"], "answer": "A"},
    {"id": 2, "type": "fill", "text": "1+1=?", "answer": "2"},
    {"id": 3, "type": "choice", "text": "2+2=?", "options": ["A. 3", "B. 4", "C. 5"], "answer": "B"}
  ]
}
```

**主 Agent 执行流程**：

```
用户：帮我去 xxx.com 答题，题库如下：[题库JSON]
        ↓
主 Agent：解析题库，验证格式
        ↓
主 Agent（第1题）：
  call_subagent("browser-agent", """
  当前任务：
  - 题目：中国的首都是？
  - 答案：A
  - 题目类型：单选题

  请执行：
  1. 打开 https://xxx.com/quiz/123
  2. 定位到第1题
  3. 选择选项"A. 北京"
  4. 点击下一题或确认按钮
  5. 返回执行结果
  """)
        ↓
browser-agent → page-agent/execute_task → 浏览器执行
        ↓
返回：{"success": true, "data": "已选择A，点击了下一题"}
        ↓
主 Agent（第2题）：
  call_subagent("browser-agent", """
  当前任务：
  - 题目：1+1=?
  - 答案：2
  - 题目类型：填空题

  请执行：
  1. 确认当前页面是第2题
  2. 在填空框输入"2"
  3. 点击下一题或确认按钮
  4. 返回执行结果
  """)
        ↓
...（循环直到所有题目完成）
        ↓
主 Agent：汇总结果，计算正确率
        ↓
汇报用户：答题完成，共3题，正确2题，正确率66.7%
```

**主 Agent 代码示例**：

```python
def answer_quiz(quiz_url: str, questions: List[dict]):
    """批量答题主循环"""
    results = []

    # 第1题需要打开网页
    first_result = call_subagent("browser-agent", f"""
    请执行：
    1. 打开 {quiz_url}
    2. 确认页面已加载
    3. 定位到第{questions[0]['id']}题
    4. 执行答题：
       - 题目：{questions[0]['text']}
       - 答案：{questions[0]['answer']}
       - 类型：{questions[0]['type']}
    5. 点击下一题
    6. 返回执行结果
    """)
    results.append(parse_result(first_result))

    # 后续题目假设页面已跳转，直接答题
    for q in questions[1:]:
        result = call_subagent("browser-agent", f"""
        当前任务：
        - 题目：{q['text']}
        - 答案：{q['answer']}
        - 类型：{q['type']}

        请执行：
        1. 确认当前页面是第{q['id']}题
        2. 执行答题（根据type选择/填空）
        3. 点击下一题或提交（如果是最后一题）
        4. 返回执行结果
        """)
        results.append(parse_result(result))

    # 汇总
    success = sum(1 for r in results if r['success'])
    return {
        "total": len(results),
        "success": success,
        "accuracy": success / len(results),
        "details": results
    }
```

### 4.5 其他典型场景

| 场景 | 主 Agent 输入 | 子 Agent 任务示例 |
|------|--------------|------------------|
| 批量注册账号 | 账号信息列表 | "填写邮箱xxx，密码xxx，点击注册" |
| 信息采集 | 目标网站 + 字段 | "提取页面中的姓名、电话、地址" |
| 自动填表 | 表单URL + 数据 | "填写姓名、身份证号、联系电话" |
| 网页测试 | 测试用例 | "点击登录按钮，验证错误提示" |

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

## 六、实施评估

### 6.1 设计评估

**问题：设计是否支持表单填写/批量答题场景？**

✅ **支持**。当前设计通过以下方式支持：

1. **循环调用**：主 Agent 循环调用 `call_subagent("browser-agent", task)`
2. **自然语言指令**：子 Agent 接收"选择A，点击下一题"等指令
3. **结构化返回**：`{success, data}` 格式便于主 Agent 判断执行结果

### 6.2 改动范围评估

| 改动项 | 类型 | 工作量 | 说明 |
|--------|------|--------|------|
| `config/agents/browser-agent.md` | 新增 | 中 | 子 Agent 提示词配置 |
| `mcp-servers/page-agent-mcp/src/__main__.py` | 新增 | 小 | Python 入口点 |
| `mcp-servers/page-agent-mcp/pyproject.toml` | 新增 | 小 | 项目配置 |
| `mcp-servers/page-agent-mcp/src/index.js` | 修改 | 小 | 增加配置读取（约20行） |
| `config/user-config.json` | 修改 | 小 | 增加 openaiCompatibleApiBase 字段 |
| `config/mcp-servers.yaml` | 修改 | 小 | 新增 server 配置 |
| `config/agents/niu.md` | 修改 | 小 | 引用 browser-agent |
| `config/llm-presets.json` | 修改 | 小 | 预设增加 openaiCompatibleApiBase |

**总评**：改动范围**中等**，主要是新增文件和少量配置修改

### 6.3 TDD 必要性评估

**结论**：**不需要 TDD**

原因：
1. **主要是配置变更**：browser-agent.md 是提示词配置，不是复杂逻辑
2. **核心功能 Page-Agent 已验证**：测试 demo 显示功能正常
3. **集成测试优先**：先验证端到端流程，单元测试价值有限

**建议**：采用 **集成测试优先** 策略
1. 先实现最小可用版本
2. 端到端测试表单填写场景
3. 根据测试结果调整提示词

### 6.4 实施顺序建议

```
Phase 1: 基础设施
├── 创建 page-agent-mcp 目录结构
├── 创建 Python 入口点
└── 修改 index.js 增加配置读取

Phase 2: 配置集成
├── 更新 user-config.json
├── 更新 mcp-servers.yaml
└── 创建 browser-agent.md

Phase 3: 端到端测试
├── 测试单个页面操作
├── 测试表单填写流程
└── 测试批量答题场景
```

---

## 七、测试计划

### 6.1 单元测试

- 配置读取逻辑
- MCP 工具调用

### 6.2 集成测试

- 子 Agent 完整流程
- 浏览器操作（打开网页、填写表单、点击按钮）
- 批量处理

---

## 八、变更记录

| 日期 | 变更内容 | 负责人 |
|------|---------|--------|
| 2026-04-10 | 初始设计 | Claude |
| 2026-04-10 | 补充表单填写/批量答题场景、实施评估 | Claude |
