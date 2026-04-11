# Page-Agent 浏览器插件打包与集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编译 Page-Agent 浏览器插件，打包到软件发布包，并在系统管理手册中添加安装指南

**Architecture:**
- 保持 Page-Agent 官方 Node.js server 不变（位于 E:/tools/page-agent）
- 使用我们已有的 HTTP 代理（niu_api/page_agent_proxy.py）
- 编译浏览器插件，打包到 `dist/chrome-extension/` 目录
- 在系统管理手册中添加插件安装说明，主Agent可指导用户安装

**Tech Stack:**
- Node.js 20+ (编译 Chrome Extension)
- pnpm (Page-Agent 项目包管理器)
- Chrome Extension Manifest V3
- Markdown (系统管理手册)

---

## 架构说明

### 正确的集成方式

```
用户请求浏览器任务
    ↓
主Agent (读取系统管理手册)
    ↓
指导用户安装插件（如未安装）
    ↓ HTTP POST
Page-Agent Proxy API (localhost:9876/proxy/v1)
    ↓ OpenAI 格式
配置的 LLM (MiniMax/OpenAI/DeepSeek)
    ↓ tool_calls
Page-Agent Chrome Extension (本地编译版本)
    ↓ WebSocket
Node.js MCP Server (@page-agent/mcp)
    ↓
浏览器执行
```

### 关键文件位置

| 组件 | 路径 | 状态 |
|------|------|------|
| Page-Agent 源码 | `E:/tools/page-agent/` | ✅ 已有 |
| Proxy API | `niu_api/page_agent_proxy.py` | ✅ 已有 |
| 浏览器插件源码 | `E:/tools/page-agent/packages/extension/` | ✅ 已有 |
| 插件编译输出 | `dist/chrome-extension/` | ❌ 需创建 |
| 系统管理手册 | `docs/SYSTEM_MANUAL.md` | ✅ 需更新 |
| 用户配置 | `~/.niu/preferences.json` | ✅ 已有 |

---

## Task 1: 编译 Page-Agent 浏览器插件

**Files:**
- Modify: `E:/tools/page-agent/packages/extension/` (编译过程)
- Create: `E:/tools/ai-bot/dist/chrome-extension/` (输出目录)

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

---

## Task 2: 更新系统管理手册

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` (添加插件安装指南)

- [ ] **Step 1: 在系统管理手册中添加 Page-Agent 章节**

在 `docs/SYSTEM_MANUAL.md` 的工具列表章节后添加：

```markdown
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

### 注意事项

- 插件需要保持启用状态
- 首次使用时可能需要刷新网页
- 如遇到问题，可在插件控制面板中查看日志
- 插件不会收集用户隐私数据，所有操作都在本地执行

### 故障排查

**问题1：插件图标不显示**
- 检查是否已开启"开发者模式"
- 检查插件是否已启用
- 尝试重新加载插件

**问题2：无法执行浏览器操作**
- 确认 Proxy API 服务正在运行（localhost:9876）
- 检查 LLM 配置是否正确
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
grep -A 5 "Page-Agent 浏览器自动化插件" E:/tools/ai-bot/docs/SYSTEM_MANUAL.md
```

Expected: 看到新添加的章节内容

- [ ] **Step 3: Commit**

Run:
```bash
cd E:/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add Page-Agent browser extension installation guide"
```

---

## Task 3: 配置 Page-Agent Proxy API

**Files:**
- Check: `niu_api/page_agent_proxy.py` (确认配置正确)
- Check: `config/mcp-servers.yaml` (确认 Node.js server 配置)

- [ ] **Step 1: 验证 Proxy API 配置**

检查 `niu_api/page_agent_proxy.py` 的端口和端点：
- 默认端口：9876
- 端点：`/proxy/v1/chat/completions`

确认代码：
```python
# 第 300-310 行
app = FastAPI(title="Page-Agent Proxy API")
app.include_router(proxy_router, prefix="/proxy/v1", tags=["proxy"])
```

- [ ] **Step 2: 验证 LLM 配置**

检查 `config/user-config.json` 中的 LLM 配置是否正确：
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

- [ ] **Step 3: 验证 Node.js MCP Server 配置**

检查 `config/mcp-servers.yaml` 中是否有 Page-Agent 配置：
```yaml
page-agent:
  command: npx
  args: ["-y", "@page-agent/mcp"]
  env:
    LLM_BASE_URL: "http://localhost:9876/proxy/v1"
    LLM_API_KEY: "dummy"
    LLM_MODEL_NAME: "any"
    PORT: "38401"
```

如果不存在，添加此配置。

- [ ] **Step 4: Commit (如有修改)**

Run:
```bash
cd E:/tools/ai-bot
git add config/mcp-servers.yaml
git commit -m "config: add Page-Agent MCP server configuration"
```

---

## Task 4: 创建插件发布脚本

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
PAGE_AGENT_DIR="E:/tools/page-agent"
OUTPUT_DIR="dist/chrome-extension"

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
mkdir -p "$OUTPUT_DIR"

# 复制插件文件
echo "复制插件文件到 $OUTPUT_DIR..."
cp -r packages/extension/dist/* "$OUTPUT_DIR/"

echo "✓ 浏览器插件编译完成: $OUTPUT_DIR"
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
git commit -m "feat: add Chrome extension build script"
```

---

## Task 5: 测试完整流程

**Files:**
- Test: 端到端测试流程

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

- [ ] **Step 4: 测试自然语言浏览器操作**

通过主Agent发送测试请求：
```
用户："帮我打开百度搜索 Python 教程"
```

Expected:
- 主Agent识别浏览器任务
- 调用 Page-Agent
- 浏览器自动打开百度并搜索

- [ ] **Step 5: 验证系统管理手册可见性**

检查主Agent是否能读取系统管理手册中的 Page-Agent 章节：
```python
# 通过向量检索测试
from agent.vector_search import VectorSearchAdapter
vs = VectorSearchAdapter()
results = vs.search("如何安装浏览器插件", limit=3, min_score=0.5)
```

Expected: 能够检索到相关文档

---

## Task 6: 更新发布流程文档

**Files:**
- Modify: `docs/deployment.md` (或创建新文档)

- [ ] **Step 1: 在发布流程中添加插件打包步骤**

在发布文档中添加：

```markdown
## 发布前检查清单

### 1. 编译浏览器插件

```bash
./scripts/build_chrome_extension.sh
```

确认 `dist/chrome-extension/` 目录存在且包含以下文件：
- manifest.json
- background.js
- content.js
- popup.html
- 图标文件

### 2. 打包发布

发布包应包含：
```
ai-bot-release/
├── dist/
│   └── chrome-extension/  # 浏览器插件
├── niu.exe                # 主程序
├── config/                # 配置文件
└── docs/                  # 文档
```
```

- [ ] **Step 2: Commit**

Run:
```bash
git add docs/deployment.md
git commit -m "docs: add Chrome extension to deployment checklist"
```

---

## 完成标准

- [ ] Page-Agent 浏览器插件编译成功
- [ ] 插件文件已复制到 `dist/chrome-extension/`
- [ ] 系统管理手册包含详细的插件安装指南
- [ ] 主Agent能够读取并指导用户安装插件
- [ ] Proxy API 正常工作
- [ ] 端到端测试通过（自然语言 → 浏览器操作）
- [ ] 编译脚本可重复执行
- [ ] 发布流程文档已更新

---

## 注意事项

### 不要修改的部分

1. **不要修改 Page-Agent Node.js server**
   - 保持官方实现不变
   - 只通过 Proxy API 使用

2. **不要删除已有的 Proxy API**
   - `niu_api/page_agent_proxy.py` 已经过验证
   - 功能完整，无需改动

3. **不要修改向量库**
   - 浏览器工具不通过向量检索发现
   - 主Agent通过系统管理手册了解如何使用

### 需要保留的文件

- ✅ `niu_api/page_agent_proxy.py` - HTTP 代理
- ✅ `E:/tools/page-agent/` - Page-Agent 源码
- ✅ `config/agents/browser-agent.md` - 子Agent配置（如果存在）

### 浏览器插件的定位

- 插件是用户界面层的组件
- 由用户手动安装（主Agent指导）
- 不需要在代码层面集成
- 只需要随软件发布，并提供文档

---

## 后续优化（可选）

1. **自动检测插件状态**
   - 在 Proxy API 中添加插件状态检查接口
   - 主Agent可以检测插件是否已安装

2. **多浏览器支持**
   - 编译 Firefox 版本（如果需要）
   - 添加 Firefox 安装说明

3. **插件版本管理**
   - 在 `dist/chrome-extension/manifest.json` 中记录版本号
   - 发布时自动更新版本号

4. **插件自动更新**
   - 配置插件更新 URL（如果有自己的服务器）
   - 或者在软件更新时提示用户重新安装插件
