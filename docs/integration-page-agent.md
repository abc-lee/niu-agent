# Page-Agent 浏览器自动化整合方案

> 版本：v1.0
> 日期：2026-04-11
> 状态：✅ 已完成开发和测试

---

## 一、功能概述

### 1.1 什么是 Page-Agent

Page-Agent 是一个 AI-native 的浏览器自动化工具，允许用户通过自然语言控制浏览器执行复杂任务。

**核心能力**：
- ✅ 打开网页、点击元素、输入文本
- ✅ 提取页面内容、截图、数据抓取
- ✅ 表单填写、自动登录、批量操作
- ✅ 网站浏览、信息搜索、任务自动化

**架构位置**：
```
用户
  ↓ 自然语言指令
主Agent (niu)
  ↓ 委托任务
子Agent (browser-agent)
  ↓ 调用工具
Page-Agent MCP Server
  ↓ HTTP/WebSocket
Chrome浏览器插件
  ↓ 执行操作
真实浏览器
```

### 1.2 与传统自动化的区别

| 对比项 | 传统自动化 | Page-Agent |
|--------|----------|-----------|
| 控制方式 | 代码/脚本 | 自然语言 |
| 适应性 | 依赖选择器 | AI理解页面结构 |
| 学习成本 | 高（需编程） | 低（会说话即可） |
| 动态页面 | 容易失效 | 自动适应 |
| 复杂任务 | 需大量代码 | 一句话搞定 |

---

## 二、架构说明

### 2.1 组件清单

| 组件 | 路径 | 功能 |
|------|------|------|
| **Page-Agent MCP Server** | `mcp-servers/page-agent-server/` | MCP工具实现层（5个工具） |
| **Page-Agent Chrome插件** | `E:\tools\page-agent/` | 浏览器控制层 |
| **Page-Agent Proxy API** | `niu_api/page_agent_proxy.py` | OpenAI兼容代理 |
| **主Agent配置** | `config/agents/niu.md` | 子Agent委托规则 |
| **子Agent定义** | `config/agents/browser-agent.md` | 浏览器Agent提示词 |
| **向量查询模式** | 向量库 `query_pattern:*` | 递归检索桥梁 |

### 2.2 工作流程

```
【场景1：主Agent协作】
用户："帮我搜索Python教程"
  ↓
主Agent：向量检索 "帮我搜索" → 递归命中 query_pattern:browser_search
  ↓
主Agent：发现 browse_web 工具，委托给 browser-agent
  ↓
browser-agent：调用 execute_browser_task({"task": "搜索Python教程"})
  ↓
Page-Agent Server：通过 WebSocket 发送指令给 Chrome 插件
  ↓
Chrome 插件：打开浏览器，执行搜索，返回结果
  ↓
用户：收到搜索结果

【场景2：外部插件直连】
第三方插件（Page-Agent Chrome Extension）
  ↓ HTTP POST
Page-Agent Proxy API (localhost:9876/proxy/v1)
  ↓ LiteLLMSession
配置的LLM（MiniMax/OpenAI/DeepSeek）
  ↓ OpenAI格式响应
第三方插件：解析执行
```

---

## 三、配置和部署

### 3.1 依赖安装

```bash
# Page-Agent MCP Server
cd mcp-servers/page-agent-server
pip install -e .

# Chrome插件（如需手动安装）
# 1. 下载：https://github.com/alibaba/page-agent
# 2. Chrome → 扩展程序 → 开发者模式 → 加载已解压的扩展程序
```

### 3.2 配置文件

**主Agent配置** (`config/agents/niu.md`)：
```yaml
mcpServers:
  - page-agent-server  # ✅ 已注册

agents:
  - browser-agent  # ✅ 子Agent已配置
```

**MCP服务器配置** (`config/mcp-servers.yaml`)：
```yaml
page-agent-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_page_agent"
  workdir: ../mcp-servers/page-agent-server/src
  preload: true  # ✅ 启动时预加载
```

**LLM配置** (`config/user-config.json`)：
```json
{
  "llm": {
    "type": "openai",
    "apiKey": "your-api-key",
    "apiBase": "https://api.minimaxi.com/anthropic/v1/messages",
    "model": "MiniMax-M2.7-highspeed"
  }
}
```

### 3.3 启动服务

```bash
# 方式1：完整启动（推荐）
go run main.go

# 方式2：仅启动API
python -m niu_api

# 检查服务状态
curl http://localhost:9876/health
```

---

## 四、工具列表和API

### 4.1 Page-Agent MCP工具（5个）

#### 1. `browser_navigate` - 导航到指定URL

**用途**：打开网页

**参数**：
```json
{
  "url": "https://www.baidu.com"
}
```

**示例**：
```python
# 主Agent调用（通过子Agent）
chat-with-browser-agent({"task": "打开百度"})
```

---

#### 2. `browser_click` - 点击页面元素

**用途**：点击按钮、链接等

**参数**：
```json
{
  "selector": "button.submit"
}
```

**示例**：
```python
# 点击登录按钮
chat-with-browser-agent({"task": "点击登录按钮"})
```

---

#### 3. `browser_input` - 在元素中输入文本

**用途**：填写表单、输入搜索词

**参数**：
```json
{
  "selector": "input.search",
  "text": "Python教程"
}
```

**示例**：
```python
# 搜索框输入
chat-with-browser-agent({"task": "在搜索框输入Python教程"})
```

---

#### 4. `browser_screenshot` - 截取页面截图

**用途**：保存页面状态

**参数**：无

**返回**：base64编码的图片

**示例**：
```python
# 截图
chat-with-browser-agent({"task": "截取当前页面"})
```

---

#### 5. `execute_browser_task` - 执行复杂任务（粗粒度）

**用途**：批量操作、表单填写、多步骤任务

**参数**：
```json
{
  "task": "填写登录表单",
  "data": {
    "username": "user@example.com",
    "password": "password123"
  }
}
```

**示例**：
```python
# 批量注册账号
chat-with-browser-agent({
  "task": "批量注册3个账号",
  "data": [
    {"username": "user1", "email": "user1@example.com"},
    {"username": "user2", "email": "user2@example.com"},
    {"username": "user3", "email": "user3@example.com"}
  ]
})
```

---

### 4.2 Page-Agent Proxy API

**端点**：`http://localhost:9876/proxy/v1`

**兼容性**：完全兼容 OpenAI API 格式

**端点列表**：

| 端点 | 方法 | 用途 |
|------|------|------|
| `/chat/completions` | POST | 聊天补全（支持tool_calls） |
| `/models` | GET | 列出可用模型 |
| `/health` | GET | 健康检查 |

**请求示例**：
```bash
curl -X POST http://localhost:9876/proxy/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [
      {"role": "user", "content": "打开百度"}
    ],
    "tools": [...]
  }'
```

**响应格式**（OpenAI兼容）：
```json
{
  "id": "chatcmpl-xxx",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "I'll open Baidu...",
      "tool_calls": [{
        "id": "call_xxx",
        "function": {
          "name": "AgentOutput",
          "arguments": "{\"action\": {...}}"
        }
      }]
    }
  }]
}
```

---

## 五、使用场景和示例

### 5.1 场景1：信息搜索

**用户**："帮我搜索Python教程"

**主Agent处理**：
```
1. 向量检索："帮我搜索"
2. 递归命中：query_pattern:browser_search
3. 第二轮检索：发现 browse_web 工具
4. 委托：chat-with-browser-agent({"task": "搜索Python教程"})
```

**browser-agent执行**：
```python
# 自动化流程
browser_navigate({"url": "https://www.baidu.com"})
browser_input({"selector": "input#kw", "text": "Python教程"})
browser_click({"selector": "input[type='submit']"})
browser_screenshot()  # 返回结果页面截图
```

---

### 5.2 场景2：表单填写

**用户**："帮我填写这个注册表单"

**browser-agent执行**：
```python
execute_browser_task({
  "task": "填写注册表单",
  "data": {
    "username": "testuser",
    "email": "test@example.com",
    "password": "password123"
  }
})
```

---

### 5.3 场景3：批量操作

**用户**："帮我买明天北京到上海的机票"

**browser-agent执行**：
```python
execute_browser_task({
  "task": "购买机票",
  "data": {
    "from": "北京",
    "to": "上海",
    "date": "2026-04-12",
    "passenger": "张三"
  }
})
```

---

### 5.4 场景4：数据抓取

**用户**："把这个网页的内容保存下来"

**browser-agent执行**：
```python
# 提取页面文本
browser_navigate({"url": "https://example.com/article"})
browser_screenshot()

# 通过AI提取结构化数据
execute_browser_task({
  "task": "提取文章标题、作者、发布日期和正文"
})
```

---

## 六、与主Agent的协作方式

### 6.1 委托规则

**主Agent职责**：
- ✅ 理解用户意图
- ✅ 通过向量检索发现工具
- ✅ 委托任务给子Agent
- ✅ 整理结果返回用户

**子Agent（browser-agent）职责**：
- ✅ 接收具体任务
- ✅ 调用 Page-Agent 工具
- ✅ 执行浏览器操作
- ✅ 返回执行结果

**❌ 错误示例**：
```python
# 主Agent直接调用工具（违反架构）
browser_navigate({"url": "https://example.com"})
```

**✅ 正确示例**：
```python
# 主Agent委托给子Agent
chat-with-browser-agent({"task": "打开 https://example.com"})
```

---

### 6.2 提示词配置

**主Agent提示词** (`config/agents/niu.md`)：
```markdown
用户说"浏览网页"、"打开网站"、"填表"、"答题"时：
```
正确：调用 chat-with-browser-agent({"task": "打开百度，搜索Python教程"})
错误：直接调用 page-agent 工具（那是子 Agent 才能调用的）
```
```

**子Agent提示词** (`config/agents/browser-agent.md`)：
```markdown
你是浏览器自动化专家，负责执行浏览器相关任务。

可用工具：
- browser_navigate: 打开网页
- browser_click: 点击元素
- browser_input: 输入文本
- browser_screenshot: 截图
- execute_browser_task: 复杂任务

执行流程：
1. 分析任务需求
2. 选择合适工具
3. 执行操作
4. 验证结果
5. 返回给主Agent
```

---

### 6.3 向量检索桥梁

**查询模式**（已注册到向量库）：

| 用户表达 | 查询模式Content | Refined Query | 目标工具 |
|---------|----------------|---------------|---------|
| "帮我搜索XX" | help me search | browser automation search | browse_web |
| "打开XX网站" | open webpage | browser automation open webpage | browse_web |
| "浏览XX网站" | browse website | browser automation browse | browse_web |
| "填写表单" | fill form automatically | browser automation fill form | browse_web |
| "保存网页内容" | save webpage content | browser automation extract | browse_web |
| "订票/买票" | book tickets | browser automation book tickets | browse_web |
| "查找新闻" | find news information | browser automation news | browse_web |

**递归检索效果**：
- 原始相似度：~0.28
- 递归后相似度：0.80+
- 提升：**186%**

---

## 七、最佳实践和注意事项

### 7.1 ✅ 推荐做法

#### 1. 明确任务描述

**好**：
```
"打开百度，搜索Python教程，截图发给我"
```

**不好**：
```
"帮我搜一下"  # 不明确搜什么、在哪搜
```

---

#### 2. 复杂任务拆解

**好**：
```
chat-with-browser-agent({
  "task": "批量注册3个账号",
  "data": [...]
})
```

**不好**：
```
chat-with-browser-agent({"task": "注册账号"})
# 没提供账号信息，需要多次交互
```

---

#### 3. 验证执行结果

```python
# 执行后截图验证
browser_screenshot()
```

---

### 7.2 ⚠️ 注意事项

#### 1. 验证码处理

**问题**：遇到验证码时，Page-Agent无法自动处理

**解决**：
```python
# 方式1：暂停等待人工输入
execute_browser_task({"task": "遇到验证码时暂停，等待人工输入"})

# 方式2：使用验证码识别服务（需额外配置）
```

---

#### 2. 登录状态保持

**问题**：每次执行都是新会话，需要重复登录

**解决**：
```python
# 使用浏览器Profile保持登录状态
# 配置文件：mcp-servers/page-agent-server/config.json
{
  "browser": {
    "user_data_dir": "E:/tmp/chrome_profile"
  }
}
```

---

#### 3. 网络超时

**问题**：页面加载慢导致超时

**解决**：
```python
# 增加超时时间
execute_browser_task({
  "task": "打开慢速网站",
  "timeout": 60000  # 60秒
})
```

---

#### 4. 动态内容等待

**问题**：页面内容动态加载，立即操作会失败

**解决**：
```python
# 显式等待
execute_browser_task({
  "task": "等待'加载完成'元素出现后再操作"
})
```

---

### 7.3 性能优化

#### 1. 批量操作

**推荐**：
```python
# 一次任务完成多步操作
execute_browser_task({
  "task": "打开网站 → 登录 → 填写表单 → 提交"
})
```

**不推荐**：
```python
# 多次调用工具（效率低）
browser_navigate({...})
browser_input({...})
browser_click({...})
```

---

#### 2. 复用浏览器实例

**配置**：
```yaml
# config/mcp-servers.yaml
page-agent-server:
  preload: true  # 启动时预加载，避免重复启动浏览器
```

---

### 7.4 安全注意事项

#### 1. 敏感信息保护

**❌ 危险**：
```python
# 直接传递密码
execute_browser_task({
  "task": "登录",
  "data": {"password": "明文密码"}
})
```

**✅ 安全**：
```python
# 使用环境变量
import os
password = os.environ.get("MY_PASSWORD")
execute_browser_task({
  "task": "登录",
  "data": {"password": password}
})
```

---

#### 2. 权限控制

**主Agent提示词**：
```markdown
⚠️ 权限规则：
- 仅在用户明确要求时执行浏览器操作
- 涉及支付、转账等敏感操作需二次确认
- 不自动保存或上传用户敏感信息
```

---

## 八、故障排查

### 8.1 常见问题

#### 问题1：浏览器无法启动

**症状**：
```
Error: Failed to launch browser
```

**排查**：
1. 检查Chrome是否安装
2. 检查Chrome版本是否兼容
3. 查看日志：`logs/api_stderr.log`

**解决**：
```bash
# 安装Chrome或Chromium
# Windows: 下载安装包
# Linux: sudo apt install chromium-browser
```

---

#### 问题2：工具调用超时

**症状**：
```
TimeoutError: Page load timeout
```

**解决**：
```python
# 增加超时时间
execute_browser_task({
  "task": "...",
  "timeout": 120000  # 2分钟
})
```

---

#### 问题3：元素找不到

**症状**：
```
Error: Element not found: button.submit
```

**解决**：
```python
# 方式1：使用更通用的选择器
browser_click({"selector": "button:contains('提交')"})

# 方式2：等待元素出现
execute_browser_task({
  "task": "等待'提交'按钮出现后再点击"
})
```

---

#### 问题4：向量检索未命中工具

**症状**：
用户说"帮我搜索"，主Agent没有发现浏览器工具

**排查**：
```bash
# 检查查询模式是否注册
python -c "
from agent.vector_search import VectorSearchAdapter
vs = VectorSearchAdapter()
results = vs.search('help me search', limit=5)
for r in results:
    print(r.id, r.score)
"
```

**解决**：
```bash
# 重新初始化向量库
python scripts/init_vector_db.py
```

---

## 九、扩展开发

### 9.1 添加新工具

**步骤**：

1. **定义工具Schema**

编辑 `mcp-servers/page-agent-server/src/niu_page_agent/__init__.py`：

```python
TOOL_SCHEMAS = {
    # 新增工具
    "browser_scroll": {
        "description": "滚动页面到指定位置",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "滚动方向"
                },
                "distance": {
                    "type": "integer",
                    "description": "滚动距离（像素）"
                }
            },
            "required": ["direction"]
        }
    }
}
```

2. **实现工具函数**

编辑 `mcp-servers/page-agent-server/src/niu_page_agent/browser_tools.py`：

```python
def browser_scroll(direction: str, distance: int = 500) -> Dict[str, Any]:
    """
    滚动页面

    Args:
        direction: 滚动方向（up/down）
        distance: 滚动距离（像素）

    Returns:
        执行结果
    """
    # 实现逻辑
    ...
```

3. **注册到向量库**

编辑 `scripts/init_vector_db.py`：

```python
tools = [
    # 新增工具描述
    {
        "server": "page-agent-server",
        "name": "browser_scroll",
        "description": "Scroll page up or down. Use when user says 'scroll', 'scroll down', 'scroll up'.",
        "input_schema": {...}
    }
]
```

4. **重新初始化向量库**

```bash
python scripts/init_vector_db.py
```

---

### 9.2 自定义浏览器配置

**配置文件**：`mcp-servers/page-agent-server/config.json`

```json
{
  "browser": {
    "headless": false,
    "user_data_dir": "E:/tmp/chrome_profile",
    "window_size": [1920, 1080],
    "user_agent": "Mozilla/5.0 ...",
    "proxy": {
      "server": "http://proxy.example.com:8080"
    }
  }
}
```

---

## 十、性能指标

### 10.1 响应时间

| 操作 | 平均耗时 |
|------|---------|
| 打开网页 | ~2秒 |
| 点击元素 | ~0.5秒 |
| 输入文本 | ~0.3秒 |
| 截图 | ~0.5秒 |
| 复杂任务 | 5-30秒 |

### 10.2 资源占用

| 资源 | 占用 |
|------|------|
| 内存（Chrome实例） | ~200MB |
| CPU（空闲时） | <5% |
| CPU（执行时） | 20-50% |

---

## 十一、相关文档

### 官方文档
- Page-Agent GitHub: https://github.com/alibaba/page-agent
- MCP协议规范: https://modelcontextprotocol.io/

### 项目内部文档
- 使用指南: `docs/page-agent-proxy-usage.md`
- 测试报告: `docs/page-agent-test-report.md`
- 向量递归设计: `docs/design-vector-recursive-query.md`
- L1摘要规范: `docs/spec-L1-summary.md`

---

## 十二、总结

### 核心成果

✅ **双层架构**：
- 主Agent：理解意图，委托任务
- 子Agent：执行操作，返回结果

✅ **智能发现**：
- 向量递归检索，自动命中工具
- 相似度提升186%

✅ **易用性**：
- 自然语言控制，无需编程
- 支持复杂任务，批量操作

✅ **可扩展**：
- 工具Schema定义
- 向量库动态注册

### 架构优势

1. **AI-native设计** - 页面理解不依赖选择器
2. **委托模式** - 主Agent专注策略，子Agent专注执行
3. **向量驱动** - 工具发现自动化，无需硬编码
4. **开放兼容** - OpenAI格式代理，支持外部集成

---

**最后更新**：2026-04-11
**维护者**：Niu Assistant Team
