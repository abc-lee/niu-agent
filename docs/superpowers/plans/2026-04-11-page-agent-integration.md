# Page-Agent 浏览器自动化完整集成方案

## 一、架构说明

### 1.1 正确的架构

```
用户: "帮我打开百度搜索Python教程"
    ↓
主Agent
    ↓ 调用工具: page-agent-mcp/execute_task
    ↓
Python 包装器 (niu_page_agent.py)
    ↓ HTTP 请求 (localhost:38401)
Node.js MCP Server (index.js)
    ↓ WebSocket
Chrome 扩展
    ↓
浏览器执行
```

### 1.2 组件说明

| 组件 | 位置 | 作用 |
|------|------|------|
| Node.js Server | `mcp-servers/page-agent-mcp/src/index.js` | HTTP + WebSocket 服务器 |
| Python 包装器 | `mcp-servers/page-agent-mcp/src/niu_page_agent.py` | MCP 工具实现，调用 Node.js |
| Chrome 扩展 | `E:/tools/page-agent/packages/extension/` | 浏览器操作执行 |
| Proxy API | `niu_api/page_agent_proxy.py` | OpenAI 兼容代理（可选） |

---

## 二、集成步骤

### Task 1: 启动 Node.js Server

**问题**：Node.js server 需要在系统启动时自动启动

**Files:**
- Check: `main.go` - Go 启动器
- Check: `niu_api/__main__.py` - Python API 启动

**步骤**：

- [ ] **Step 1: 检查 Node.js server 启动方式**

Run:
```bash
node E:/tools/ai-bot/mcp-servers/page-agent-mcp/src/index.js --help
```

Expected: 了解启动参数

- [ ] **Step 2: 在 Go 启动器中添加 Node.js server 启动**

修改 `main.go`，在启动 Python API 之前启动 Node.js server：

```go
// 启动 Page-Agent Node.js server
nodeCmd := exec.Command("node", "mcp-servers/page-agent-mcp/src/index.js")
nodeCmd.Dir = projectRoot
nodeCmd.Stdout = os.Stdout
nodeCmd.Stderr = os.Stderr

if err := nodeCmd.Start(); err != nil {
    log.Printf("Failed to start page-agent-mcp: %v", err)
}

log.Println("Page-Agent MCP server started on port 38401")
```

- [ ] **Step 3: 验证 Node.js server 启动**

Run:
```bash
curl http://localhost:38401
```

Expected: 返回 200 OK

---

### Task 2: 注册 Python 包装器到 REQUIRED_SERVERS

**Files:**
- Modify: `agent/mcp_loader.py`

- [ ] **Step 1: 添加 page-agent-mcp 到 REQUIRED_SERVERS**

修改 `agent/mcp_loader.py` 第 19-28 行：

```python
REQUIRED_SERVERS: List[Tuple[str, str]] = [
    ("photo-server", "niu_photo_server"),
    ("config-manager", "niu_config_manager"),
    ("memory-server", "niu_memory_server"),
    ("vector-store", "niu_vector_store"),
    ("kg-server", "niu_kg_server"),
    ("file-parser", "niu_file_parser"),
    ("session-manager", "niu_session_manager"),
    ("scheduler-server", "niu_scheduler_server"),
    ("page-agent-mcp", "niu_page_agent"),  # 新增
]
```

- [ ] **Step 2: 添加 workdir 到 sys.path**

检查 `mcp-servers/page-agent-mcp/src` 是否会被添加到 sys.path。

修改 `config/mcp-servers.yaml`，确保 workdir 正确：

```yaml
page-agent-mcp:
  command: ${NODE_PATH}
  args:
    - "mcp-servers/page-agent-mcp/src/index.js"
  workdir: mcp-servers/page-agent-mcp/src  # 指向 src 目录
  preload: true
```

- [ ] **Step 3: 验证模块导入**

Run:
```bash
cd E:/tools/ai-bot && python -c "
import sys
sys.path.insert(0, 'mcp-servers/page-agent-mcp/src')
import niu_page_agent
print('✓ niu_page_agent 模块导入成功')
print('工具 Schema 数量:', len(niu_page_agent.get_tool_schemas()))
"
```

Expected: 成功导入，返回 3 个工具

---

### Task 3: 注册工具提示词到向量库

**Files:**
- Modify: `scripts/init_vector_db.py`

- [ ] **Step 1: 添加 page-agent-mcp 工具描述**

在 `scripts/init_vector_db.py` 的 tools 列表中添加：

```python
# ==================== Page-Agent 浏览器自动化工具 ====================
{
    "server": "page-agent-mcp",
    "name": "execute_task",
    "description": "Execute browser automation task in natural language. Use when user says 'open webpage', 'search', 'browse', 'fill form', 'click button', 'extract content', 'login', 'book tickets'. Supports complex multi-step browser operations. Example: execute_task({'task': 'Open Baidu and search Python tutorials'})",
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Natural language task description. Be specific about what to do and what information to extract."
            }
        },
        "required": ["task"]
    }
},
{
    "server": "page-agent-mcp",
    "name": "get_status",
    "description": "Check Page-Agent connection status. Returns {connected, busy}. Use to verify browser extension is working.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
},
{
    "server": "page-agent-mcp",
    "name": "stop_task",
    "description": "Stop currently running browser automation task. Use when task is taking too long or needs to be cancelled.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
},
```

- [ ] **Step 2: 重新初始化向量库**

Run:
```bash
cd E:/tools/ai-bot
python scripts/init_vector_db.py
```

Expected: 3 个 page-agent-mcp 工具注册成功

---

### Task 4: 在主Agent配置中添加 page-agent-mcp

**Files:**
- Check: `config/agents/niu.md`

- [ ] **Step 1: 验证主Agent配置**

确认 `config/agents/niu.md` 中已有：

```yaml
mcpServers:
  - nanobot.system
  - file-parser
  - kg-server
  - vector-store
  - config-manager
  - photo-server
  - page-agent-mcp  # ✅ 已有
```

- [ ] **Step 2: 添加使用示例到主Agent提示词**

在 `config/agents/niu.md` 的提示词中添加：

```markdown
用户说"浏览网页"、"打开网站"、"填表"时：
```
正确：调用 page-agent-mcp/execute_task({"task": "打开百度搜索Python教程"})
错误：不调用工具直接回答
```

**注意**：Page-Agent 是自然语言接口，直接描述任务即可，不需要拆分成多个步骤。
```

---

### Task 5: 编译 Chrome 扩展

**Files:**
- Source: `E:/tools/page-agent/packages/extension/`
- Output: `dist/chrome-extension/`

- [ ] **Step 1: 编译扩展**

Run:
```bash
cd E:/tools/page-agent
pnpm install
pnpm run build:ext
```

- [ ] **Step 2: 复制到发布目录**

Run:
```bash
mkdir -p E:/tools/ai-bot/dist/chrome-extension
cp -r E:/tools/page-agent/packages/extension/dist/* E:/tools/ai-bot/dist/chrome-extension/
```

- [ ] **Step 3: Commit**

```bash
git add dist/chrome-extension/
git commit -m "feat: add Page-Agent Chrome extension to distribution"
```

---

### Task 6: 更新系统管理手册

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 添加 Page-Agent 章节**

在 `docs/SYSTEM_MANUAL.md` 末尾添加：

```markdown

---

## Page-Agent 浏览器自动化

### 功能说明

Page-Agent 是浏览器自动化工具，支持自然语言控制。

**支持功能**：
- 打开网页、点击、输入
- 搜索、提取内容
- 表单填写、登录
- 复杂多步骤任务

### 安装插件

**适用场景**：用户需要浏览器自动化时，主Agent指导安装。

**步骤**：

1. 检查插件文件：`dist/chrome-extension/`
2. 打开 Chrome，访问 `chrome://extensions/`
3. 开启"开发者模式"
4. 点击"加载已解压的扩展程序"
5. 选择 `dist/chrome-extension/` 目录
6. 安装成功，右上角出现图标

### 使用方法

用户用自然语言描述任务即可，例如：
- "打开百度搜索 Python 教程"
- "去 GitHub 登录"
- "填写这个注册表单"

主Agent会自动调用 Page-Agent 执行。

### 故障排查

**问题：插件图标不显示**
- 检查是否开启"开发者模式"
- 检查插件是否启用
- 重新加载插件

**问题：无法执行任务**
- 检查 Node.js server 是否启动（端口 38401）
- 检查浏览器控制台错误
- 查看 Proxy API 是否正常

**问题：任务超时**
- 使用 `get_status` 检查状态
- 使用 `stop_task` 停止任务
- 简化任务描述
```

---

## 三、工具注册验证

### 验证步骤

- [ ] **验证 1: 检查 REQUIRED_SERVERS**

Run:
```bash
grep "page-agent-mcp" E:/tools/ai-bot/agent/mcp_loader.py
```

Expected: 看到 `("page-agent-mcp", "niu_page_agent")`

- [ ] **验证 2: 检查工具注册**

Run:
```bash
cd E:/tools/ai-bot && python -c "
from agent.tool_registry import get_registry
registry = get_registry()
schemas = registry.get_schemas()
page_agent_tools = [s for s in schemas if 'page-agent-mcp' in s.get('name', '')]
print('Page-Agent 工具数量:', len(page_agent_tools))
for tool in page_agent_tools:
    print(f\"  - {tool['name']}\")
"
```

Expected: 看到 3 个工具

- [ ] **验证 3: 测试工具调用**

Run:
```bash
cd E:/tools/ai-bot && python -c "
from agent.tool_registry import get_registry
registry = get_registry()

# 测试 get_status
get_status = registry.get('page-agent-mcp/get_status')
result = get_status()
print('get_status 结果:', result)
"
```

Expected: 返回 `{"connected": ..., "busy": ...}`

- [ ] **验证 4: 测试向量检索**

Run:
```bash
cd E:/tools/ai-bot && python -c "
from agent.vector_search import VectorSearchAdapter
vs = VectorSearchAdapter()
results = vs.search('打开网页', limit=3)
for r in results:
    print(f\"{r.id} (score: {r.score:.4f})\")
"
```

Expected: 能命中 page-agent-mcp 工具

---

## 四、关键文件清单

| 文件 | 修改内容 |
|------|---------|
| `main.go` | 添加 Node.js server 启动 |
| `agent/mcp_loader.py` | REQUIRED_SERVERS 添加 page-agent-mcp |
| `config/mcp-servers.yaml` | 确认 workdir 配置 |
| `config/agents/niu.md` | 添加使用示例 |
| `scripts/init_vector_db.py` | 添加 3 个工具描述 |
| `docs/SYSTEM_MANUAL.md` | 添加插件安装说明 |
| `dist/chrome-extension/` | 编译后的插件文件 |

---

## 五、启动流程

```
1. Go 启动器 (main.go)
   ├─ 启动 Node.js server (port 38401)
   ├─ 启动 Python API (port 9876)
   └─ 监控进程健康

2. Python API 启动
   ├─ load_mcp_tools()
   │   └─ 导入 niu_page_agent 模块
   │       └─ 注册 3 个工具到 ToolRegistry
   └─ 向量库初始化
       └─ 加载工具描述

3. 用户请求
   └─ 主Agent → page-agent-mcp/execute_task
       └─ niu_page_agent.py → HTTP (localhost:38401)
           └─ Node.js server → WebSocket
               └─ Chrome 扩展 → 浏览器
```

---

## 六、注意事项

### 不要做的事情

1. ❌ 不要创建 Python 实现的 MCP Server（已删除）
2. ❌ 不要创建 browser-agent 子Agent（已删除）
3. ❌ 不要在向量库中注册细粒度工具（navigate/click/input 等）

### 必须做的事情

1. ✅ 使用官方 Node.js server
2. ✅ 使用 Python 包装器调用
3. ✅ 注册 3 个工具（execute_task, get_status, stop_task）
4. ✅ 在向量库中注册工具描述
5. ✅ 在系统启动时启动 Node.js server
6. ✅ 随包发布 Chrome 扩展

### 关键配置

- **Node.js server**: 必须在 38401 端口启动
- **Python 包装器**: 通过 HTTP 调用 Node.js server
- **工具注册**: 必须在 REQUIRED_SERVERS 中
- **向量检索**: 工具描述必须注册到向量库
- **主Agent**: 必须配置 page-agent-mcp MCP server

---

## 七、完成标准

- [ ] Node.js server 能在系统启动时自动启动
- [ ] Python 包装器注册到 REQUIRED_SERVERS
- [ ] 3 个工具注册到 ToolRegistry
- [ ] 工具描述注册到向量库
- [ ] 主Agent配置包含 page-agent-mcp
- [ ] Chrome 扩展编译并放到 dist 目录
- [ ] 系统管理手册包含安装说明
- [ ] 能通过向量检索发现浏览器工具
- [ ] 能成功调用 execute_task 执行任务
