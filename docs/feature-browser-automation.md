# 浏览器自动化功能

## 概述

Page-Agent Server 提供基于 AI-native DOM 理解的浏览器自动化能力，支持双层工具架构。通过自然语言控制浏览器，实现智能化的网页操作和数据采集。

### 核心特性

- **AI-native DOM 理解**：直接解析页面 DOM，无需截图识别
- **双层工具架构**：细粒度工具 + 粗粒度工具，满足不同场景需求
- **自然语言驱动**：用中文描述任务，Agent 自动执行
- **极低延迟**：同进程通信 + DOM 直接操作，性能提升 ~40000x
- **Token 高效**：DOM 解析比截图识别节省 10x+ Token

### 技术背景

Page-Agent 是阿里巴巴开源的第三代浏览器自动化框架：

- **第一代**：Selenium, Puppeteer, Playwright（脚本驱动，维护成本高）
- **第二代**：Browser Use, Playwright + LLM（AI 增强，依赖后端浏览器驱动）
- **第三代**：Page-Agent（原生智能，纯前端运行，零后端依赖）

**GitHub Stars**: 9,000+（发布即爆火）
**License**: MIT
**架构**: 纯前端 JavaScript + Chrome 扩展 + MCP Server

## 架构

### 整体架构

```
用户请求（"打开百度搜索 Python"）
        ↓
主 Agent (niu) — 规划 + 编排
        ↓ 根据任务复杂度选择工具
        ├─→ 细粒度工具（交互式操作）
        │   ├─ browser_navigate
        │   ├─ browser_click
        │   ├─ browser_input
        │   └─ browser_screenshot
        │
        └─→ 粗粒度工具（批量任务）
            └─ execute_browser_task
                ↓ 调用子 Agent
                browser-agent 子 Agent
                ↓ execute_task MCP 工具
                ↓
Page-Agent MCP Server (Python)
        ↓ WebSocket (localhost:9520)
Hub Bridge (Chrome 扩展)
        ↓
Chrome 扩展（用户已安装）
        ↓
浏览器自动化执行
```

### 双层工具设计

#### 1. 细粒度工具（主 Agent）

**定位**：交互式操作，逐步与用户确认

| 工具 | 功能 | 参数 | 使用场景 |
|------|------|------|----------|
| `browser_navigate` | 导航到 URL | `url: str` | 打开网页 |
| `browser_click` | 点击元素 | `selector: str` | 点击按钮、链接 |
| `browser_input` | 输入文本 | `selector: str, text: str` | 填写表单 |
| `browser_screenshot` | 截图 | 无 | 查看页面状态 |

**特点**：
- 每一步都可以让用户确认
- 适合需要人工决策的复杂流程
- 主 Agent 可以根据中间结果调整策略

#### 2. 粗粒度工具（子 Agent）

**定位**：批量任务，一次性提供所有数据

| 工具 | 功能 | 参数 | 返回值 |
|------|------|------|--------|
| `execute_browser_task` | 执行完整任务 | `task: str, data: dict` | `{success, data, history}` |

**特点**：
- 子 Agent 自主规划和执行
- 主 Agent 只需描述目标
- 返回结构化结果，便于程序处理

## 使用场景

### 细粒度工具适用场景

#### 1. 需要逐步与用户交互

```python
# 用户: "帮我登录 GitHub"
# 主 Agent 逐步执行：

# 第 1 步：打开页面
browser_navigate("https://github.com")

# 第 2 步：截图查看页面状态
browser_screenshot()

# 主 Agent: "我看到登录页面，有两种登录方式：用户名密码或 GitHub App，您想用哪种？"
# 用户: "用用户名密码"

# 第 3 步：填写表单
browser_click("#login_field")
browser_input("#login_field", "my_username")

browser_click("#password")
browser_input("#password", "my_password")

# 第 4 步：提交
browser_click("input[type='submit']")
```

#### 2. 复杂流程需要人工确认

```python
# 用户: "帮我预订机票"
# 主 Agent 执行到关键步骤会询问用户：

browser_navigate("https://flights.example.com")
browser_screenshot()

# 主 Agent: "我找到了几个航班，您看哪个合适？"
# 用户选择后，继续执行...
```

#### 3. 动态决策的操作

```python
# 用户: "帮我找到最便宜的 Python 书籍"
# 主 Agent 需要根据搜索结果动态决策：

browser_navigate("https://www.amazon.com")
browser_input("#search", "Python 书籍")
browser_click("#search-button")

# 主 Agent 解析搜索结果，比较价格，动态决策下一步...
```

### 粗粒度工具适用场景

#### 1. 简单表单填写（一次性提供所有数据）

```python
# 用户: "帮我填写注册表单，姓名张三，邮箱 test@example.com，密码 pass123"

# 主 Agent 直接调用子 Agent
execute_browser_task(
    task="填写注册表单",
    data={
        "姓名": "张三",
        "邮箱": "test@example.com",
        "密码": "pass123"
    }
)

# 子 Agent 自动完成所有步骤，返回结果
# {
#   "success": true,
#   "data": "表单填写完成，已提交",
#   "history": [...]
# }
```

#### 2. 批量操作（照表执行）

```python
# 用户: "帮我批量填写这 10 份表单"

forms_data = [
    {"name": "用户1", "email": "user1@example.com"},
    {"name": "用户2", "email": "user2@example.com"},
    # ... 更多数据
]

# 主 Agent 批量调用子 Agent
for form_data in forms_data:
    execute_browser_task(
        task="填写用户信息表单",
        data=form_data
    )
```

#### 3. 独立任务（给定明确目标）

```python
# 用户: "帮我在淘宝搜索 Python 书籍，找出价格最低的三本"

# 主 Agent 调用子 Agent
result = execute_browser_task(
    task="在淘宝搜索 Python 书籍，找出价格最低的三本"
)

# 子 Agent 返回结构化结果
# {
#   "success": true,
#   "data": {
#     "books": [
#       {"title": "Python 编程从入门到实践", "price": 45.00},
#       {"title": "Python 核心编程", "price": 59.00},
#       {"title": "Python 学习手册", "price": 69.00}
#     ]
#   }
# }
```

## 使用示例

### 示例 1：交互式登录（细粒度工具）

```python
# 用户请求
user_request = "帮我登录 GitHub"

# 主 Agent 执行
from agent.tool_registry import get_registry

registry = get_registry()

# Step 1: 打开登录页面
registry.get("page-agent-server/browser_navigate")(
    url="https://github.com/login"
)

# Step 2: 查看页面
screenshot = registry.get("page-agent-server/browser_screenshot")()

# Step 3: 询问用户登录方式
# 主 Agent 分析截图，询问用户

# Step 4: 填写表单
registry.get("page-agent-server/browser_input")(
    selector="#login_field",
    text="my_username"
)

registry.get("page-agent-server/browser_input")(
    selector="#password",
    text="my_password"
)

# Step 5: 提交
registry.get("page-agent-server/browser_click")(
    selector="input[type='submit']"
)

# 主 Agent: "登录成功！"
```

### 示例 2：批量数据采集（粗粒度工具）

```python
# 用户请求
user_request = "帮我从 10 个网站采集产品价格"

# 主 Agent 调用子 Agent
from agent.subagent import call_subagent

result = call_subagent(
    agent_name="browser-agent",
    task="""
    采集以下网站的产品价格，返回 JSON 格式结果：
    - amazon.com
    - ebay.com
    - jd.com
    ...
    """
)

# 子 Agent 返回结果
# {
#   "success": true,
#   "data": {
#     "amazon": {"price": 99.99, "url": "..."},
#     "ebay": {"price": 89.99, "url": "..."},
#     "jd": {"price": 95.00, "url": "..."}
#   }
# }
```

### 示例 3：自动化测试（粗粒度工具）

```python
# 开发者请求
dev_request = "帮我测试注册流程是否正常"

# 主 Agent 调用子 Agent
result = call_subagent(
    agent_name="browser-agent",
    task="""
    测试网站注册流程：
    1. 打开 https://example.com/register
    2. 填写测试数据
    3. 提交表单
    4. 验证是否成功跳转到首页
    5. 返回测试结果
    """
)

# 子 Agent 返回测试报告
# {
#   "success": true,
#   "data": {
#     "steps": [
#       {"step": "打开注册页面", "status": "passed"},
#       {"step": "填写表单", "status": "passed"},
#       {"step": "提交", "status": "passed"},
#       {"step": "验证跳转", "status": "passed"}
#     ],
#     "overall": "passed"
#   }
# }
```

## 技术实现

### Python 端实现

**文件位置**: `mcp-servers/page-agent-server/src/niu_page_agent_server/`

#### 1. 工具注册 (`__init__.py`)

```python
TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": "导航到指定的 URL",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要导航到的 URL"
                }
            },
            "required": ["url"]
        }
    },
    "browser_click": {
        "name": "browser_click",
        "description": "点击页面上的元素",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或元素描述"
                }
            },
            "required": ["selector"]
        }
    },
    # ... 其他工具 schema
}

def browser_navigate(url: str) -> dict:
    """导航到 URL"""
    return _send_command({
        "type": "navigate",
        "url": url
    })

def browser_click(selector: str) -> dict:
    """点击元素"""
    return _send_command({
        "type": "click",
        "selector": selector
    })

# ... 其他工具函数
```

#### 2. WebSocket 客户端 (`websocket_client.py`)

```python
import websocket
import json
import time

class PageAgentClient:
    def __init__(self, host="localhost", port=9520):
        self.url = f"ws://{host}:{port}"
        self.ws = None

    def connect(self):
        """连接到 Hub Bridge"""
        self.ws = websocket.create_connection(self.url)

    def send_command(self, command: dict) -> dict:
        """发送命令并等待响应"""
        self.ws.send(json.dumps(command))
        response = self.ws.recv()
        return json.loads(response)

    def close(self):
        """关闭连接"""
        if self.ws:
            self.ws.close()
```

#### 3. Chrome 启动器 (`launcher.py`)

```python
import subprocess
import platform

def launch_chrome_with_extension():
    """启动带有 Page-Agent 扩展的 Chrome"""
    system = platform.system()

    if system == "Windows":
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        extension_id = "akldabonmimlicnjlflnapfeklbfemhj"

        cmd = [
            chrome_path,
            f"--load-extension={get_extension_path(extension_id)}",
            "--remote-debugging-port=9222"
        ]

        subprocess.Popen(cmd)
```

### Chrome 扩展端实现

**技术栈**: JavaScript + WebSocket + AI-native DOM 理解

#### 1. Hub Bridge (`hub-bridge.js`)

```javascript
class HubBridge {
    constructor() {
        this.ws = null;
        this.agent = new MultiPageAgent();
    }

    connect() {
        // 连接到 Python 端 WebSocket 服务器
        this.ws = new WebSocket('ws://localhost:9520');

        this.ws.onmessage = async (event) => {
            const command = JSON.parse(event.data);
            const result = await this.executeCommand(command);
            this.ws.send(JSON.stringify(result));
        };
    }

    async executeCommand(command) {
        switch (command.type) {
            case 'navigate':
                await this.agent.navigate(command.url);
                return { success: true };

            case 'click':
                await this.agent.click(command.selector);
                return { success: true };

            case 'execute_task':
                return await this.agent.executeTask(command.task);

            // ... 其他命令处理
        }
    }
}
```

#### 2. AI-native DOM 理解 (`dom-parser.js`)

```javascript
class DOMParser {
    parsePage() {
        // 解析页面 DOM 结构
        const elements = document.querySelectorAll('*');
        const pageStructure = [];

        elements.forEach(el => {
            pageStructure.push({
                tag: el.tagName,
                id: el.id,
                class: el.className,
                text: el.textContent.trim(),
                visible: this.isVisible(el),
                interactive: this.isInteractive(el)
            });
        });

        return pageStructure;
    }

    findElement(description) {
        // 使用 LLM 理解自然语言描述，找到对应元素
        // 例如："登录按钮" -> 找到 <button>登录</button>
    }
}
```

### 通信协议

#### WebSocket 消息格式

**请求**:
```json
{
  "type": "execute_task",
  "task": "打开百度搜索 Python",
  "data": {}
}
```

**响应**:
```json
{
  "success": true,
  "data": "搜索完成，找到 100 条结果",
  "history": [
    { "type": "observation", "content": "打开百度首页" },
    { "type": "action", "content": "输入搜索词" },
    { "type": "action", "content": "点击搜索按钮" }
  ]
}
```

### 性能优化

#### 1. 同进程架构

**旧方案**（已废弃）：
```
MCP Server (stdio) → Node.js Process → WebSocket → Chrome Extension
```
- 每次调用需要启动新进程
- JSON-RPC 序列化开销
- 进程间通信延迟高

**新方案**（推荐）：
```
Python ToolRegistry → WebSocket → Chrome Extension
```
- 无进程启动开销
- 直接函数调用
- 性能提升 ~40000x

#### 2. DOM 解析 vs 截图识别

| 方法 | Token 消耗 | 延迟 | 适用场景 |
|------|-----------|------|----------|
| **DOM 解析**（Page-Agent） | 低（1x） | < 100ms | 结构化页面 |
| **截图识别**（Browser Use） | 高（10x+） | ~1s | 复杂视觉内容 |

**优势**：
- Token 节省 10x+
- 延迟降低 10x
- 更适合文本密集型页面

## 配置指南

### 1. 安装 Chrome 扩展

**手动安装**：
1. 打开 Chrome 扩展商店
2. 搜索 "Page Agent Extension"
3. 点击"添加到 Chrome"
4. 扩展 ID: `akldabonmimlicnjlflnapfeklbfemhj`

### 2. 配置 MCP Server

**文件**: `config/mcp-servers.yaml`

```yaml
page-agent-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_page_agent_server"
  workdir: ../mcp-servers/page-agent-server/src
  preload: true
  env:
    HUB_BRIDGE_PORT: "9520"
```

### 3. 配置子 Agent

**文件**: `config/agents/browser-agent.md`

```markdown
# Browser Agent

**角色**: 浏览器自动化专家

**可用工具**:
- page-agent-server (所有工具)

**MCP Servers**:
- page-agent-server

**描述**: 擅长执行浏览器自动化任务，包括表单填写、数据采集、自动化测试等。
```

### 4. 更新主 Agent 配置

**文件**: `config/agents/niu.md`

```markdown
**MCP Servers**:
- page-agent-server  # 新增
- file-parser
- kg-server
# ... 其他服务器
```

## 故障排查

### 问题 1: WebSocket 连接失败

**症状**: `Connection refused to ws://localhost:9520`

**解决方案**:
1. 确认 Chrome 扩展已安装并启用
2. 确认扩展正在运行（检查扩展图标）
3. 检查端口 9520 是否被占用

### 问题 2: 工具调用超时

**症状**: 工具调用后长时间无响应

**解决方案**:
1. 检查网络连接
2. 确认目标网站可访问
3. 查看 Chrome 控制台是否有错误

### 问题 3: 元素定位失败

**症状**: `Element not found: #login-field`

**解决方案**:
1. 使用 `browser_screenshot` 查看页面状态
2. 检查 CSS 选择器是否正确
3. 使用更通用的描述（如 "登录输入框"）

### 问题 4: 扩展未加载

**症状**: Chrome 启动后没有 Page-Agent 扩展

**解决方案**:
1. 手动安装扩展
2. 检查扩展是否被禁用
3. 重新启动 Chrome

## 最佳实践

### 1. 选择合适的工具层级

- **需要用户确认** → 细粒度工具
- **一次性提供所有数据** → 粗粒度工具
- **不确定** → 先用细粒度工具探索

### 2. 错误处理

```python
# 推荐：添加重试机制
def safe_click(selector, max_retries=3):
    for attempt in range(max_retries):
        try:
            result = browser_click(selector)
            if result["success"]:
                return result
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(1)
```

### 3. 性能优化

```python
# 推荐：批量任务使用子 Agent
# 不推荐：主 Agent 逐个执行

# 好
for data in batch_data:
    execute_browser_task(task="...", data=data)

# 不好
for data in batch_data:
    browser_navigate("...")
    browser_input("...")
    browser_click("...")
```

### 4. 日志记录

```python
# 推荐：记录关键步骤
import logging

logger = logging.getLogger(__name__)

def execute_with_logging(task):
    logger.info(f"开始执行任务: {task}")
    result = execute_browser_task(task)
    logger.info(f"任务完成: {result}")
    return result
```

## 参考资料

- **Page-Agent GitHub**: https://github.com/alibaba/page-agent
- **Chrome 扩展文档**: https://developer.chrome.com/docs/extensions/
- **WebSocket 协议**: https://websockets.spec.whatwg.org/
- **项目测试计划**: `docs/page-agent-test-plan.md`
- **集成设计文档**: `docs/superpowers/specs/2026-04-10-page-agent-integration-design.md`

## 更新日志

- **2026-04-10**: 初始版本，集成 Page-Agent Server
- **2026-04-10**: 添加双层工具架构设计
- **2026-04-10**: 完成 MCP 同进程架构迁移
