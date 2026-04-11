# Page-Agent MCP Integration

## 概述

Page-Agent MCP 是浏览器自动化工具，支持 AI Agent 控制用户浏览器进行网页操作。本次集成实现了与 ai-bot 主系统的对接，支持交互式浏览器控制。

## 架构

```
主Agent (Python)
    ↓ HTTP REST API (port 38402)
Node.js Bridge Service
    ↓ WebSocket (port 38401)
Chrome Extension (Page Agent)
    ↓ Chrome DevTools Protocol
用户浏览器
```

### 核心组件

| 组件 | 端口 | 功能 |
|------|------|------|
| Node.js Bridge | 38401 | WebSocket服务 + HTTP REST API |
| Chrome Extension | - | 浏览器控制（需从商店安装） |
| Python Client | - | HTTP客户端，提供MCP工具接口 |

## 安装

### 1. 安装Chrome扩展

从 Chrome Web Store 安装：
```
https://chromewebstore.google.com/detail/page-agent-ext/akldabonmimlicnjlflnapfeklbfemhj
```

### 2. 启动服务

Node.js服务会在主应用启动时自动启动。如需手动启动：

```bash
cd mcp-servers/page-agent-mcp
npm install
node src/index.js
```

服务启动后会自动打开浏览器launcher页面，触发扩展连接。

### 3. 验证安装

```bash
curl http://localhost:38402/status
# 应返回：{"connected":true,"busy":false}
```

## 使用方法

### 基本原则

**交互式操作的核心：拆分任务为小步骤**

每个 `execute_task` 调用应：
- 只做一件事
- 在10-15秒内完成
- 返回明确的结果或错误
- 让主Agent能在每个步骤后决策

### ✅ 正确用法：分步控制

```python
# 步骤1：打开页面
result1 = execute_task("Navigate to https://example.com, wait for page load, return 'loaded'")
# 主Agent收到：✅ "loaded" (~7秒)

# 步骤2：获取内容
result2 = execute_task("Extract the main heading text from current page")
# 主Agent收到：✅ "Welcome to Example" (~5秒)

# 步骤3：执行操作
result3 = execute_task("Click the 'Submit' button, wait for next page, return 'clicked'")
# 主Agent收到：✅ "clicked" (~10秒)
```

### ❌ 错误用法：一次性大任务

```python
# 不要这样做！会超时
execute_task("""
Open https://example.com,
fill form with name=John email=john@example.com,
submit the form,
wait for confirmation page,
extract the confirmation number
""")
# 问题：整个任务可能需要60-100秒，HTTP连接可能不稳定
```

## 配置

### 工具注册

工具在 `agent/mcp_loader.py` 中手动注册：

```python
# 手动注册 page-agent-mcp（外部 Node.js 服务）
import niu_page_agent
registry.register_server("page-agent-mcp", niu_page_agent)
```

### 代理配置

扩展强制使用 ai-bot 的 LiteLLM 代理：

```javascript
// mcp-servers/page-agent-mcp/src/index.js
const proxyConfig = {
    baseURL: 'http://localhost:9876/proxy/v1',
    model: 'local',
    apiKey: 'local'
}
```

### 超时设置

```python
# mcp-servers/page-agent-mcp/src/niu_page_agent.py
timeout=120  # 2分钟，支持复杂表单操作
```

每个HTTP请求独立计时，自动重置。

## 工具API

### execute_task

执行浏览器自动化任务。

**参数**：
- `task` (string): 任务描述（自然语言）

**返回**：
- 成功：`"Task completed.\n\n{结果数据}"`
- 失败：`"Error: {错误信息}"` 或 `"Task failed.\n\n{失败原因}"`

**示例**：
```python
from niu_page_agent import execute_task

result = execute_task("Open https://google.com, return page title")
print(result)  # "Task completed.\n\nGoogle"
```

### get_status

检查扩展连接状态。

**返回**：JSON字符串 `{"connected": bool, "busy": bool}`

**示例**：
```python
from niu_page_agent import get_status
import json

status = json.loads(get_status())
if status['connected'] and not status['busy']:
    print("Ready to execute tasks")
```

### stop_task

停止当前正在执行的任务。

**返回**：状态消息

**示例**：
```python
from niu_page_agent import stop_task

stop_task()  # 停止超时任务，清理状态
```

## 交互式工作流示例

### MBTI 测试自动化

```python
# 步骤1：打开测试页面
result = execute_task("""
Navigate to https://mbti-test.app/zh-cn/free-personality-test
Wait for page to load
Return 'page loaded'
""")
# 主Agent收到：✅ "page loaded" (~7秒)

# 步骤2：获取第一题
result = execute_task("""
On the current MBTI test page, extract the question text and all options
Do NOT click anything
Return the question and options
""")
# 主Agent收到：
# "第一题：在社交场合中，我的感受是：
#  A: 我喜欢和很多人一起活动
#  B: 我喜欢和少数人深入交流"
# (~10秒)

# 步骤3：主Agent决策
# 主Agent思考：用户性格偏内向，应该选B

# 步骤4：点击选项并获取下一题
result = execute_task("""
On the current MBTI test page, click option B
Wait for the next question to appear
Return the next question text and options
""")
# 主Agent收到：第二题内容 (~13秒)

# 重复步骤3-4直到完成
```

### 表单填写

```python
# 步骤1：打开表单页面
execute_task("Open https://example.com/form, return 'loaded'")

# 步骤2：填写姓名
execute_task("Fill the 'name' input field with 'John Doe', return 'filled'")

# 步骤3：填写邮箱
execute_task("Fill the 'email' input field with 'john@example.com', return 'filled'")

# 步骤4：提交表单
execute_task("Click the submit button, wait for success message, return the message")
```

## 已知限制

### 1. 扩展内部重试机制

Page Agent 扩展有内置的智能重试逻辑，当初始方法失败时会尝试替代方案。这是硬编码行为，无法通过提示词完全控制。

**影响**：
- 简单操作（点击、导航）：快速返回，符合预期
- 复杂操作（搜索、查找）：可能尝试多种方法，导致较长时间

**缓解方案**：
- 拆分任务为小步骤
- 每步只做一件事
- 主Agent根据错误/超时信息决定下一步

### 2. HTTP连接稳定性

长时间任务（>60秒）可能导致HTTP连接不稳定。

**缓解方案**：
- 保持每个任务在10-15秒内完成
- 使用120秒超时作为安全网
- 超时后自动调用 `stop_task()` 清理状态

### 3. 扩展状态管理

扩展可能进入 busy 状态且无法自动恢复。

**解决方法**：
```python
# 检查状态
status = json.loads(get_status())
if status['busy']:
    # 强制停止
    stop_task()
    # 重置扩展连接
    import webbrowser
    webbrowser.open('http://localhost:38401')
```

## 故障排查

### 扩展未连接

**症状**：`get_status()` 返回 `{"connected":false}`

**解决方案**：
1. 确认扩展已安装并启用
2. 打开 launcher 页面：`http://localhost:38401`
3. 检查扩展是否有权限访问 localhost

### 工具未注册

**症状**：主Agent找不到 `page-agent-mcp/execute_task` 工具

**解决方案**：
检查 `agent/mcp_loader.py` 中是否手动注册：
```python
import niu_page_agent
registry.register_server("page-agent-mcp", niu_page_agent)
```

### 任务超时

**症状**：`execute_task` 返回 `"Error: timed out"`

**解决方案**：
1. 拆分任务为更小的步骤
2. 检查扩展是否卡在 busy 状态
3. 调用 `stop_task()` 清理状态

## 测试

### 单元测试

```bash
# 测试Python客户端
cd E:/tools/ai-bot
python -c "
import sys
sys.path.insert(0, 'mcp-servers/page-agent-mcp/src')
from niu_page_agent import get_status, execute_task
print(get_status())
result = execute_task('Return the text: Hello world')
print(result)
"
```

### 集成测试

```bash
# 测试MBTI交互式工作流
python scripts/test_interactive_mbti.py
```

## 相关文件

| 文件 | 功能 |
|------|------|
| `mcp-servers/page-agent-mcp/src/niu_page_agent.py` | Python HTTP客户端 |
| `mcp-servers/page-agent-mcp/src/index.js` | Node.js服务主程序 |
| `mcp-servers/page-agent-mcp/src/hub-bridge.js` | WebSocket桥接 |
| `agent/mcp_loader.py` | 工具注册 |
| `scripts/test_interactive_mbti.py` | 交互式测试脚本 |

## 参考资料

- [Page Agent 扩展](https://chromewebstore.google.com/detail/page-agent-ext/akldabonmimlicnjlflnapfeklbfemhj)
- [Page Agent MCP](https://www.npmjs.com/package/@page-agent/mcp)
- [更新日志](./CHANGELOG-page-agent.md)
