# Page-Agent 浏览器插件打包与集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-step. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编译 Page-Agent 浏览器插件，打包到软件发布包，并配置 Page-Agent MCP Server 使用本地 Proxy API

**Architecture:**
- 保持 Page-Agent 官方 Node.js server 不变（位于 E:/tools/page-agent）
- 使用已有的 HTTP 代理（niu_api/page_agent_proxy.py）
- 编译浏览器插件，打包到 `dist/chrome-extension/` 目录
- 在 `config/mcp-servers.yaml` 中添加环境变量配置
- 在系统管理手册中添加插件安装说明

**Tech Stack:**
- Node.js 20+ (编译 Chrome Extension)
- pnpm (Page-Agent 项目包管理器)
- Chrome Extension Manifest V3
- YAML 配置文件

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
git commit -m "feat: add Page-Agent Chrome extension to distribution"
```

---

## Task 2: 配置 Page-Agent MCP Server

**Files:**
- Modify: `config/mcp-servers.yaml` (添加环境变量)

- [ ] **Step 1: 查看当前配置**

Run:
```bash
grep -A 6 "page-agent-mcp:" E:/tools/ai-bot/config/mcp-servers.yaml
```

Expected: 看到当前配置，没有 env 字段

- [ ] **Step 2: 添加环境变量配置**

编辑 `config/mcp-servers.yaml`，在 `page-agent-mcp` 配置中添加 env 字段：

```yaml
# Page Agent MCP - Browser automation via Chrome extension (Node.js)
page-agent-mcp:
  command: ${NODE_PATH}
  args:
    - "mcp-servers/page-agent-mcp/src/index.js"
  workdir: .
  preload: true
  env:
    LLM_BASE_URL: "http://localhost:9876/proxy/v1"  # 指向我们的 Proxy API
    LLM_API_KEY: "dummy"                            # Proxy API 会使用主Agent的配置
    LLM_MODEL_NAME: "any"                           # Proxy API 会使用主Agent的配置
    PORT: "38401"                                   # WebSocket 端口
```

- [ ] **Step 3: 验证配置格式**

Run:
```bash
grep -A 12 "page-agent-mcp:" E:/tools/ai-bot/config/mcp-servers.yaml
```

Expected: 看到 env 字段已添加

- [ ] **Step 4: Commit**

Run:
```bash
cd E:/tools/ai-bot
git add config/mcp-servers.yaml
git commit -m "config: add environment variables for Page-Agent MCP server"
```

---

## Task 3: 在系统管理手册中添加插件安装说明

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` (在末尾添加新章节)

- [ ] **Step 1: 在系统管理手册末尾添加 Page-Agent 章节**

在 `docs/SYSTEM_MANUAL.md` 的末尾添加：

```markdown

---

## Page-Agent 浏览器自动化插件

### 简介

Page-Agent 是一个 AI-native 的浏览器自动化工具，允许用户通过自然语言控制浏览器执行复杂任务。

**核心能力**：
- 打开网页、点击元素、输入文本
- 提取页面内容、截图、数据抓取
- 表单填写、自动登录、批量操作
- 网站浏览、信息搜索、任务自动化

### 安装步骤

#### 1. 检查插件文件

软件发布包中已包含 Page-Agent 浏览器插件，位于：
```
dist/chrome-extension/
├── manifest.json
├── background.js
├── content.js
└── ...
```

#### 2. 安装插件到 Chrome

1. 打开 Chrome 浏览器
2. 访问 `chrome://extensions/`
3. 开启右上角的"开发者模式"
4. 点击"加载已解压的扩展程序"
5. 选择 `dist/chrome-extension/` 目录
6. 插件安装完成，会在浏览器右上角显示图标

#### 3. 验证插件状态

点击浏览器右上角的插件图标，应该能看到 Page-Agent 控制面板。

### 使用方法

用户只需用自然语言描述任务，例如：
- "帮我搜索 Python 教程"
- "打开 GitHub 并登录"
- "填写这个注册表单"
- "把这个网页的内容保存下来"

主Agent会自动识别浏览器相关请求，并调用 Page-Agent 执行。

### 故障排查

**问题1：插件图标不显示**
- 检查是否已开启"开发者模式"
- 检查插件是否已启用
- 尝试重新加载插件

**问题2：无法执行浏览器操作**
- 确认 Proxy API 服务正在运行（localhost:9876）
- 检查 `config/mcp-servers.yaml` 中 page-agent-mcp 的配置
- 查看插件控制面板的日志信息

**问题3：页面内容无法识别**
- 刷新当前网页
- 检查是否有弹出窗口或新标签页被拦截
- 确认页面已完全加载

### 相关文档

- Page-Agent GitHub: https://github.com/alibaba/page-agent
- Page-Agent 文档: https://alibaba.github.io/page-agent/
```

- [ ] **Step 2: 验证文档格式**

Run:
```bash
tail -30 E:/tools/ai-bot/docs/SYSTEM_MANUAL.md
```

Expected: 看到新添加的 Page-Agent 章节

- [ ] **Step 3: Commit**

Run:
```bash
cd E:/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add Page-Agent Chrome extension installation guide"
```

---

## Task 4: 创建插件编译脚本

**Files:**
- Create: `scripts/build_chrome_extension.sh`

- [ ] **Step 1: 创建编译脚本**

创建文件 `scripts/build_chrome_extension.sh`：

```bash
#!/bin/bash
# 编译 Page-Agent 浏览器插件并打包到 dist 目录

set -e

echo "=== 编译 Page-Agent 浏览器插件 ==="

# 设置路径
PAGE_AGENT_DIR="../page-agent"
OUTPUT_DIR="../dist/chrome-extension"

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 检查 Page-Agent 源码是否存在
if [ ! -d "$PAGE_AGENT_DIR" ]; then
    echo "错误: Page-Agent 源码不存在: $PAGE_AGENT_DIR"
    exit 1
fi

# 切换到 Page-Agent 目录
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
mkdir -p "$PROJECT_ROOT/$OUTPUT_DIR"

# 复制插件文件
echo "复制插件文件..."
cp -r packages/extension/dist/* "$PROJECT_ROOT/$OUTPUT_DIR/"

echo "✓ 浏览器插件编译完成: $OUTPUT_DIR"
ls -la "$PROJECT_ROOT/$OUTPUT_DIR"
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
git commit -m "feat: add Chrome extension build script"
```

---

## Task 5: 测试完整流程

**Files:**
- Test: 端到端测试

- [ ] **Step 1: 启动 API 服务**

Run:
```bash
cd E:/tools/ai-bot
python -m niu_api
```

Expected: API 服务启动在 localhost:9876

- [ ] **Step 2: 验证 Proxy API 端点**

Run:
```bash
curl http://localhost:9876/proxy/v1/health
```

Expected: 返回 `{"status": "ok"}`

- [ ] **Step 3: 测试插件安装**

在 Chrome 浏览器中：
1. 访问 `chrome://extensions/`
2. 开启"开发者模式"
3. 点击"加载已解压的扩展程序"
4. 选择 `dist/chrome-extension/` 目录

Expected: 插件成功加载，图标出现在浏览器右上角

- [ ] **Step 4: 验证 MCP Server 配置**

检查 Page-Agent MCP Server 是否能读取环境变量：

```bash
cd E:/tools/ai-bot
node -e "
const config = require('./config/mcp-servers.yaml');
console.log('Page-Agent config:', config);
"
```

Expected: 能看到 env 字段中的配置

---

## 完成标准

- [ ] Page-Agent 浏览器插件编译成功
- [ ] 插件文件已复制到 `dist/chrome-extension/`
- [ ] `config/mcp-servers.yaml` 中添加了 env 配置
- [ ] 系统管理手册包含插件安装说明
- [ ] 编译脚本可重复执行
- [ ] 端到端测试通过（插件安装 + API 验证）

---

## 配置原理说明

### Page-Agent MCP Server 配置方式

Page-Agent MCP Server 通过环境变量读取 LLM 配置：

| 环境变量 | 说明 | 我们的配置 |
|---------|------|----------|
| `LLM_BASE_URL` | LLM API 地址 | `http://localhost:9876/proxy/v1` (Proxy API) |
| `LLM_API_KEY` | API 密钥 | `dummy` (Proxy API 会使用主Agent的配置) |
| `LLM_MODEL_NAME` | 模型名称 | `any` (Proxy API 会使用主Agent的配置) |
| `PORT` | WebSocket 端口 | `38401` |

### 配置流向

```
主Agent配置 (config/user-config.json)
    ↓ 已有
主Agent的 LLM API
    ↓
Proxy API (localhost:9876/proxy/v1)
    ↓ 复用主Agent配置
Page-Agent MCP Server (读取环境变量)
    ↓ HTTP 请求
浏览器插件执行
```

### 关键点

1. **Page-Agent 不需要单独的 API Key**
   - 通过 Proxy API 复用主Agent的 LLM 配置
   - 环境变量中的 `LLM_API_KEY` 设为 `dummy` 即可

2. **配置文件位置**
   - Page-Agent 配置: `config/mcp-servers.yaml` 的 env 字段
   - 主Agent配置: `config/user-config.json` (不需要修改)

3. **env 字段如何生效**
   - Node.js 进程启动时读取环境变量
   - MCP Server 代码: `process.env.LLM_BASE_URL`

---

## 注意事项

### 不要修改的部分

1. **不要修改主Agent的 LLM 配置**
   - `config/user-config.json` 保持不变
   - 那是主Agent自己的配置

2. **不要删除已有的 Proxy API**
   - `niu_api/page_agent_proxy.py` 已经过验证
   - 功能完整，无需改动

3. **不要修改向量库**
   - 浏览器工具不通过向量检索发现
   - 主Agent通过系统管理手册了解如何使用

### 浏览器插件的定位

- 插件是用户界面层的组件
- 由用户手动安装（主Agent指导）
- 不需要在代码层面集成
- 只需要随软件发布，并提供文档
