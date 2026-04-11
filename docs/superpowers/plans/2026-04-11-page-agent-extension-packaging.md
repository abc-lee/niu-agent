# Page-Agent 浏览器插件打包与配置完善计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-step. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编译 Page-Agent 浏览器插件随包发布，并在系统管理手册中添加插件安装指导说明

**Architecture:**
- **系统集成已完成**：browser-agent 子Agent + page-agent-server MCP Server 已配置
- **只需补充**：编译插件、随包发布、文档说明
- **调用流程**：用户请求 → 主Agent → chat-with-browser-agent → browser-agent 子Agent → page-agent-server 工具 → Chrome 扩展 → 浏览器

**Tech Stack:**
- Node.js 20+ (编译 Chrome Extension)
- pnpm (Page-Agent 项目包管理器)
- Chrome Extension Manifest V3
- Markdown (系统管理手册)

---

## 当前集成状态（已完成 ✅）

### 架构概览

```
用户: "帮我打开百度搜索Python教程"
    ↓
主Agent (直接调用 MCP 工具)
    ↓ page-agent-mcp/execute_task({"task": "..."})
    ↓
Page-Agent MCP Server (Node.js)
    ↓ 3 个工具
    ├─ execute_task(task)
    ├─ get_status()
    └─ stop_task()
    ↓
WebSocket 客户端 (ws://localhost:38401)
    ↓
Page-Agent Chrome 扩展
    ↓
浏览器执行
```

### 已配置的文件

| 文件 | 状态 | 说明 |
|------|------|------|
| `config/mcp-servers.yaml` | ✅ 已配置 | page-agent-mcp (Node.js) 配置 |
| `mcp-servers/page-agent-mcp/` | ✅ 已实现 | Node.js MCP Server (官方实现) |
| Chrome 扩展 | ❌ 缺失 | 需要编译并随包发布 |

---

## Task 1: 编译 Page-Agent 浏览器插件

**Files:**
- Source: `E:/tools/page-agent/packages/extension/`
- Output: `dist/chrome-extension/`

- [ ] **Step 1: 检查 Page-Agent 编译依赖**

Run:
```bash
cd E:/tools/page-agent
node --version
pnpm --version
```

Expected: Node.js >= 20, pnpm 已安装

- [ ] **Step 2: 安装 Page-Agent 项目依赖**

Run:
```bash
cd E:/tools/page-agent
pnpm install
```

Expected: 所有依赖安装成功

- [ ] **Step 3: 编译浏览器插件**

Run:
```bash
cd E:/tools/page-agent
pnpm run build:ext
```

Expected: 编译成功，输出到 `packages/extension/dist/`

- [ ] **Step 4: 验证编译输出**

Run:
```bash
ls -la E:/tools/page-agent/packages/extension/dist/
```

Expected: 看到 `manifest.json` 和其他插件文件

- [ ] **Step 5: 创建目标目录并复制插件**

Run:
```bash
mkdir -p E:/tools/ai-bot/dist/chrome-extension
cp -r E:/tools/page-agent/packages/extension/dist/* E:/tools/ai-bot/dist/chrome-extension/
ls -la E:/tools/ai-bot/dist/chrome-extension/
```

Expected: 插件文件已复制到 `dist/chrome-extension/`

- [ ] **Step 6: Commit**

Run:
```bash
cd E:/tools/ai-bot
git add dist/chrome-extension/
git commit -m "feat: add Page-Agent Chrome extension to distribution package"
```

---

## Task 2: 在系统管理手册中添加插件安装说明

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` (在末尾添加新章节)

- [ ] **Step 1: 在系统管理手册末尾添加 Page-Agent 章节**

在 `docs/SYSTEM_MANUAL.md` 的末尾添加：

```markdown

---

## Page-Agent 浏览器自动化插件

### 功能说明

Page-Agent 是一个浏览器自动化工具，让用户可以通过自然语言控制浏览器执行任务。

**支持的功能**：
- 打开网页、点击按钮、填写表单
- 搜索信息、提取页面内容
- 截图、数据抓取
- 复杂的多步骤自动化操作

### 插件安装步骤

**适用场景**：当用户需要使用浏览器自动化功能时，主Agent应指导用户安装插件。

#### 步骤1：检查插件文件

软件发布包中包含 Page-Agent 浏览器插件，位置：
```
dist/chrome-extension/
├── manifest.json
├── background.js
├── content.js
└── ...
```

#### 步骤2：安装插件到 Chrome

指导用户按以下步骤操作：

1. 打开 Chrome 浏览器
2. 在地址栏输入：`chrome://extensions/`
3. 开启右上角的"开发者模式"开关
4. 点击"加载已解压的扩展程序"按钮
5. 选择软件目录下的 `dist/chrome-extension/` 文件夹
6. 点击"选择文件夹"

#### 步骤3：验证安装成功

安装后，浏览器右上角会出现 Page-Agent 图标。点击图标可以查看插件状态。

### 使用示例

用户只需用自然语言描述任务，例如：
- "帮我打开百度搜索 Python 教程"
- "去 GitHub 找最新的 React 项目"
- "填写这个注册表单，用户名是 xxx"
- "把这个网页的内容保存下来"

主Agent会自动识别浏览器相关请求，调用 Page-Agent 执行。

### 常见问题

**Q1: 插件图标不显示？**
- 检查是否已开启"开发者模式"
- 检查插件是否已启用（扩展页面中的开关）
- 尝试刷新扩展页面

**Q2: 无法执行浏览器操作？**
- 确认 Page-Agent 服务正在运行（后台自动启动）
- 检查浏览器控制台是否有错误信息
- 尝试重新加载插件

**Q3: 页面内容无法识别？**
- 等待页面完全加载
- 刷新当前网页
- 检查是否有弹窗被浏览器拦截

**Q4: 某些网站无法操作？**
- 部分网站可能有安全限制
- 检查浏览器是否阻止了插件运行
- 查看插件控制面板的日志信息

### 技术细节

**WebSocket 连接**：
- 插件通过 WebSocket 连接到本地服务（端口 38401）
- 连接状态可在插件控制面板中查看

**数据安全**：
- 所有浏览器操作都在本地执行
- 不会上传用户数据到外部服务器
- 插件不会收集用户隐私信息

### 相关链接

- Page-Agent GitHub: https://github.com/alibaba/page-agent
- Page-Agent 文档: https://alibaba.github.io/page-agent/
```

- [ ] **Step 2: 验证文档格式**

Run:
```bash
tail -50 E:/tools/ai-bot/docs/SYSTEM_MANUAL.md
```

Expected: 看到新添加的 Page-Agent 章节

- [ ] **Step 3: Commit**

Run:
```bash
cd E:/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add Page-Agent Chrome extension installation guide in system manual"
```

---

## Task 3: 创建插件编译脚本

**Files:**
- Create: `scripts/build_chrome_extension.sh`

- [ ] **Step 1: 创建编译脚本**

创建文件 `scripts/build_chrome_extension.sh`：

```bash
#!/bin/bash
# 编译 Page-Agent 浏览器插件并打包到 dist 目录

set -e

echo "=== 编译 Page-Agent 浏览器插件 ==="

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 设置路径
PAGE_AGENT_DIR="$PROJECT_ROOT/../page-agent"
OUTPUT_DIR="$PROJECT_ROOT/dist/chrome-extension"

# 检查 Page-Agent 源码是否存在
if [ ! -d "$PAGE_AGENT_DIR" ]; then
    echo "错误: Page-Agent 源码不存在: $PAGE_AGENT_DIR"
    echo "请先克隆 Page-Agent 项目到与 ai-bot 同级目录"
    exit 1
fi

cd "$PAGE_AGENT_DIR"

# 检查依赖
if [ ! -d "node_modules" ]; then
    echo "安装依赖..."
    pnpm install
fi

# 编译插件
echo "编译浏览器插件..."
pnpm run build:ext

# 检查编译输出
if [ ! -d "packages/extension/dist" ]; then
    echo "错误: 编译失败，未找到输出目录"
    exit 1
fi

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 复制插件文件
echo "复制插件文件到 $OUTPUT_DIR ..."
cp -r packages/extension/dist/* "$OUTPUT_DIR/"

echo "✓ 浏览器插件编译完成"
echo "插件位置: $OUTPUT_DIR"
ls -la "$OUTPUT_DIR"
```

- [ ] **Step 2: 设置脚本权限**

Run:
```bash
chmod +x scripts/build_chrome_extension.sh
```

- [ ] **Step 3: 测试编译脚本**

Run:
```bash
cd E:/tools/ai-bot
./scripts/build_chrome_extension.sh
```

Expected: 脚本成功执行，插件文件出现在 `dist/chrome-extension/`

- [ ] **Step 4: Commit**

Run:
```bash
git add scripts/build_chrome_extension.sh
git commit -m "feat: add Chrome extension build script for release packaging"
```

---

## Task 4: 验证系统集成

**Files:**
- Check: 已有的集成配置

- [ ] **Step 1: 验证 browser-agent 子Agent配置**

Run:
```bash
cat E:/tools/ai-bot/config/agents/browser-agent.md
```

Expected: 看到 `mcpServers: [page-agent-server]` 配置

- [ ] **Step 2: 验证 MCP Server 注册**

Run:
```bash
grep -n "page-agent-server" E:/tools/ai-bot/agent/mcp_loader.py
```

Expected: 在 REQUIRED_SERVERS 列表中看到 `("page-agent-server", "niu_page_agent")`

- [ ] **Step 4: 验证工具注册**

Run:
```bash
cd E:/tools/ai-bot && python -c "
from agent.tool_registry import get_registry
registry = get_registry()
tools = registry.get_schemas()
print('已注册的 MCP 工具:')
for tool in tools:
    name = tool.get('name', '')
    if 'page-agent' in name:
        print(f\"  - {name}\")
"
```

Expected: 看到 page-agent-mcp 的工具（execute_task, get_status, stop_task）

- [ ] **Step 5: 验证主Agent配置**

Run:
```bash
grep "page-agent-mcp" E:/tools/ai-bot/config/agents/niu.md
```

Expected: 看到主Agent配置了 page-agent-mcp

---

## Task 5: 更新发布文档

**Files:**
- Create: `docs/release-checklist.md` 或更新现有发布文档

- [ ] **Step 1: 创建发布检查清单**

创建文件 `docs/release-checklist.md`：

```markdown
# 软件发布检查清单

## 发布前准备

### 1. 编译浏览器插件

```bash
./scripts/build_chrome_extension.sh
```

确认 `dist/chrome-extension/` 目录存在，包含以下文件：
- manifest.json
- background.js
- content.js
- popup.html
- 图标文件

### 2. 打包发布

发布包结构：
```
ai-bot-release-vX.X.X/
├── dist/
│   └── chrome-extension/  # Page-Agent 浏览器插件
├── niu.exe                # 主程序（Windows）
├── config/                # 配置文件模板
├── docs/                  # 文档
│   └── SYSTEM_MANUAL.md   # 系统管理手册
└── README.md              # 说明文档
```

### 3. 功能验证

- [ ] 启动 API 服务成功
- [ ] Chrome 扩展安装成功
- [ ] 浏览器自动化功能正常
- [ ] 系统管理手册可访问

### 4. 文档检查

- [ ] 系统管理手册包含插件安装说明
- [ ] README 包含快速开始指南
- [ ] 版本号已更新

## 发布后

- [ ] 创建 Git tag
- [ ] 上传发布包
- [ ] 更新更新日志
```

- [ ] **Step 2: Commit**

Run:
```bash
git add docs/release-checklist.md
git commit -m "docs: add release checklist for Chrome extension packaging"
```

---

## 完成标准

- [ ] Page-Agent 浏览器插件编译成功
- [ ] 插件文件已复制到 `dist/chrome-extension/`
- [ ] 系统管理手册包含详细的插件安装说明
- [ ] 主Agent可以指导用户安装插件
- [ ] 编译脚本可重复执行
- [ ] 发布文档已更新

---

## 集成架构说明

### 正确的架构（主Agent直接调用）

```
用户: "帮我打开百度搜索Python教程"
    ↓
主Agent (niu)
    ↓ 发现 MCP 工具
    ↓ 直接调用: page-agent-mcp/execute_task({"task": "打开百度搜索Python教程"})
    ↓
Page-Agent MCP Server (Node.js, 官方实现)
    ├─ execute_task(task) - 执行自然语言浏览器任务
    ├─ get_status() - 查询连接状态
    └─ stop_task() - 停止当前任务
    ↓
WebSocket 连接 (ws://localhost:38401)
    ↓
Page-Agent Chrome 扩展
    ↓ 接收命令
    ↓ 执行 DOM 操作
浏览器执行
```

### 配置文件说明

| 文件 | 作用 | 状态 |
|------|------|------|
| `config/mcp-servers.yaml` | page-agent-mcp 配置 | ✅ 已配置 |
| `config/agents/niu.md` | 主Agent配置，包含 page-agent-mcp | ✅ 已配置 |
| `mcp-servers/page-agent-mcp/` | Node.js MCP Server (官方) | ✅ 已有 |

### 调用流程示例

**用户**: "帮我打开百度搜索 Python 教程"

**执行流程**:
1. 主Agent识别浏览器任务
2. 主Agent调用 `page-agent-mcp/execute_task({"task": "打开百度搜索Python教程"})`
3. Page-Agent MCP Server 通过 WebSocket 发送命令给 Chrome 扩展
4. Chrome 扩展执行浏览器操作（导航、输入、点击等）
5. 返回结果给主Agent
6. 主Agent向用户汇报完成

---

## 注意事项

### 不需要修改的部分

1. **不需要修改 Page-Agent MCP Server**
   - 保持官方 Node.js 实现不变
   - 3 个工具已正确实现

2. **不需要修改主Agent配置**
   - `config/agents/niu.md` 已包含 page-agent-mcp

3. **不需要修改工具注册逻辑**
   - Page-Agent 通过 MCP 协议工作

### 需要补充的部分

1. **浏览器插件编译**
   - 从 Page-Agent 源码编译
   - 放到发布包中

2. **系统管理手册**
   - 添加插件安装说明
   - 主Agent可以指导用户

3. **发布流程**
   - 确保插件随包发布
   - 更新发布文档

### 关键点

- **使用官方实现**：page-agent-mcp (Node.js 版本)
- **3 个工具**：execute_task, get_status, stop_task
- **主Agent直接调用**：不需要子Agent
- **用户无需手动配置**，插件安装后自动工作
- **文档是给主Agent看的**，指导用户安装插件
