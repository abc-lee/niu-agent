# Phase 1: 基础框架 - 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建项目骨架，实现 Go 后端 + Electron 前端 + Python MCP 工具层的基础通信

**Architecture:** Go 核心（基于 Nanobot）通过 MCP 协议调用 Python 工具，Electron/Svelte 前端通过 HTTP 与 Go 后端通信

**Tech Stack:** Go 1.21+, Node.js 20+, Python 3.11+, Electron, Svelte, MCP SDK

**GitHub:** abc-lee/personal-assistant

---

## 项目结构

```
personal-assistant/
├── cmd/                          # Go 入口
│   └── main.go
├── pkg/                          # Go 核心
│   ├── agent/                    # Agent 执行循环
│   ├── mcp/                      # MCP 协议处理
│   ├── server/                   # HTTP API
│   └── config/                   # 配置管理
├── tools/                        # Python MCP 工具
│   ├── document-parser/
│   ├── face-recognition/
│   ├── vector-store/
│   └── knowledge-graph/
├── ui/                           # Electron + Svelte 前端
│   ├── electron/                 # Electron 主进程
│   └── src/                      # Svelte 前端
│       ├── components/
│       ├── pages/
│       └── lib/
├── config/                       # 配置文件
│   └── mcp-servers.yaml
├── docs/                         # 文档
├── scripts/                      # 构建脚本
├── go.mod
├── go.sum
├── package.json
├── requirements.txt
└── README.md
```

---

## Task 1: 初始化项目目录结构

**Files:**
- Create: `personal-assistant/` 根目录
- Create: 所有子目录

**Step 1: 创建根目录**

```bash
mkdir -p personal-assistant
cd personal-assistant
```

**Step 2: 创建 Go 目录结构**

```bash
mkdir -p cmd pkg/agent pkg/mcp pkg/server pkg/config
```

**Step 3: 创建 Python 工具目录**

```bash
mkdir -p tools/document-parser tools/face-recognition tools/vector-store tools/knowledge-graph
```

**Step 4: 创建前端目录**

```bash
mkdir -p ui/electron ui/src/components ui/src/pages ui/src/lib
```

**Step 5: 创建配置和文档目录**

```bash
mkdir -p config docs/plans scripts
```

**Step 6: 验证目录结构**

Run: `tree -L 2` 或 `ls -R`
Expected: 显示完整目录结构

**Step 7: Commit**

```bash
git add .
git commit -m "chore: init project structure"
```

---

## Task 2: 初始化 Go 模块

**Files:**
- Create: `go.mod`
- Create: `go.sum`

**Step 1: 初始化 Go 模块**

```bash
cd personal-assistant
go mod init github.com/abc-lee/personal-assistant
```

Expected: 创建 `go.mod` 文件

**Step 2: 验证 go.mod 内容**

Run: `cat go.mod`
Expected:
```
module github.com/abc-lee/personal-assistant

go 1.21
```

**Step 3: Commit**

```bash
git add go.mod
git commit -m "chore: init go module"
```

---

## Task 3: 创建 Go HTTP 服务器

**Files:**
- Create: `pkg/server/server.go`
- Create: `pkg/server/routes.go`

**Step 1: 写 server.go**

```go
// pkg/server/server.go
package server

import (
	"fmt"
	"log"
	"net/http"
	"time"
)

type Server struct {
	addr   string
	router *http.ServeMux
}

func New(addr string) *Server {
	return &Server{
		addr:   addr,
		router: http.NewServeMux(),
	}
}

func (s *Server) Start() error {
	s.setupRoutes()

	srv := &http.Server{
		Addr:         s.addr,
		Handler:      s.router,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	log.Printf("Server starting on %s", s.addr)
	return srv.ListenAndServe()
}

func (s *Server) setupRoutes() {
	s.router.HandleFunc("/health", s.handleHealth)
	s.router.HandleFunc("/api/status", s.handleStatus)
}
```

**Step 2: 写 routes.go**

```go
// pkg/server/routes.go
package server

import (
	"encoding/json"
	"net/http"
)

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("OK"))
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	response := map[string]interface{}{
		"status":  "running",
		"version": "0.1.0",
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(response)
}
```

**Step 3: 验证编译**

Run: `go build ./pkg/server/...`
Expected: 无错误

**Step 4: Commit**

```bash
git add pkg/server/
git commit -m "feat(server): add basic HTTP server"
```

---

## Task 4: 创建 Go 主入口

**Files:**
- Create: `cmd/main.go`

**Step 1: 写 main.go**

```go
// cmd/main.go
package main

import (
	"log"
	"os"

	"github.com/abc-lee/personal-assistant/pkg/server"
)

func main() {
	// 从环境变量获取端口，默认 8765
	port := os.Getenv("PORT")
	if port == "" {
		port = "8765"
	}

	log.Println("Personal Assistant starting...")

	// 启动 HTTP 服务器
	s := server.New(":" + port)
	if err := s.Start(); err != nil {
		log.Fatal(err)
	}
}
```

**Step 2: 整理依赖**

Run: `go mod tidy`
Expected: 更新 go.sum

**Step 3: 构建并测试**

Run: `go build -o bin/assistant ./cmd && ./bin/assistant &`
Expected: 输出 "Server starting on :8765"

Run: `curl http://localhost:8765/health`
Expected: `OK`

Run: `curl http://localhost:8765/api/status`
Expected: `{"status":"running","version":"0.1.0"}`

**Step 4: Commit**

```bash
git add cmd/main.go go.sum bin/
git commit -m "feat: add main entry point"
```

---

## Task 5: 创建配置管理

**Files:**
- Create: `pkg/config/config.go`
- Create: `config/settings.yaml`

**Step 1: 写 config.go**

```go
// pkg/config/config.go
package config

import (
	"os"
	"path/filepath"
)

type Config struct {
	DataDir string `yaml:"data_dir"`
	Port    string `yaml:"port"`
}

func Load() (*Config, error) {
	cfg := &Config{
		Port:    "8765",
		DataDir: getDefaultDataDir(),
	}

	// 确保数据目录存在
	if err := os.MkdirAll(cfg.DataDir, 0755); err != nil {
		return nil, err
	}

	return cfg, nil
}

func getDefaultDataDir() string {
	homeDir, _ := os.UserHomeDir()
	return filepath.Join(homeDir, ".personal-assistant", "data")
}
```

**Step 2: 写 settings.yaml**

```yaml
# config/settings.yaml
# 个人知识助理配置文件

server:
  port: 8765

storage:
  data_dir: "~/.personal-assistant/data"

llm:
  provider: ollama
  base_url: http://localhost:11434
  model: llama3.2
```

**Step 3: 验证编译**

Run: `go build ./pkg/config/...`
Expected: 无错误

**Step 4: Commit**

```bash
git add pkg/config/ config/
git commit -m "feat(config): add configuration management"
```

---

## Task 6: 初始化 Node.js 前端

**Files:**
- Create: `package.json`
- Create: `package-lock.json`

**Step 1: 创建 package.json**

```json
{
  "name": "personal-assistant",
  "version": "0.1.0",
  "description": "个人知识助理 - 本地知识管理工具",
  "main": "ui/electron/main.js",
  "scripts": {
    "dev": "electron .",
    "build": "electron-builder",
    "start": "electron ."
  },
  "author": "abc-lee",
  "license": "MIT",
  "devDependencies": {
    "electron": "^28.0.0",
    "electron-builder": "^24.9.1"
  }
}
```

**Step 2: 安装依赖**

Run: `npm install`
Expected: 创建 `node_modules/` 和 `package-lock.json`

**Step 3: 创建 .gitignore**

```
# .gitignore
# Dependencies
node_modules/

# Build outputs
bin/
dist/
*.exe

# IDE
.idea/
.vscode/
*.swp

# OS
.DS_Store
Thumbs.db

# Python
__pycache__/
*.pyc
.venv/
venv/

# Data
data/
*.db
*.sqlite
```

**Step 4: Commit**

```bash
git add package.json package-lock.json .gitignore
git commit -m "chore: init node.js project"
```

---

## Task 7: 创建 Electron 主进程

**Files:**
- Create: `ui/electron/main.js`
- Create: `ui/electron/preload.js`

**Step 1: 写 main.js**

```javascript
// ui/electron/main.js
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');

// 开发模式下禁用硬件加速
if (process.env.NODE_ENV === 'development') {
  app.disableHardwareAcceleration();
}

let mainWindow;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 400,
    height: 500,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, '../src/index.html'));

  // 开发模式打开 DevTools
  if (process.env.NODE_ENV === 'development') {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
}

app.whenReady().then(() => {
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// IPC 处理
ipcMain.handle('get-api-status', async () => {
  const http = require('http');
  return new Promise((resolve) => {
    http.get('http://localhost:8765/api/status', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(JSON.parse(data)));
    }).on('error', () => resolve({ status: 'error' }));
  });
});
```

**Step 2: 写 preload.js**

```javascript
// ui/electron/preload.js
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  getStatus: () => ipcRenderer.invoke('get-api-status'),
  sendFile: async (file) => {
    // TODO: 实现文件上传
    console.log('sendFile called:', file);
  }
});
```

**Step 3: Commit**

```bash
git add ui/electron/
git commit -m "feat(electron): add main process"
```

---

## Task 8: 创建基础 HTML 页面

**Files:**
- Create: `ui/src/index.html`
- Create: `ui/src/styles.css`
- Create: `ui/src/renderer.js`

**Step 1: 写 index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>个人助理</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div class="container">
    <div class="header">
      <span class="title">🤖 个人助理</span>
      <span class="status" id="status">●</span>
    </div>
    
    <div class="drop-zone" id="dropZone">
      <div class="drop-icon">📁</div>
      <div class="drop-text">拖拽文件到这里</div>
    </div>
    
    <div class="chat-area" id="chatArea">
      <div class="message assistant">
        你好！我是你的个人助理。
        把文件扔给我，我会帮你整理。
      </div>
    </div>
    
    <div class="input-area">
      <input type="text" id="userInput" placeholder="说点什么..." />
      <button id="sendBtn">发送</button>
    </div>
  </div>
  
  <script src="renderer.js"></script>
</body>
</html>
```

**Step 2: 写 styles.css**

```css
/* ui/src/styles.css */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans SC', sans-serif;
  background: rgba(250, 248, 240, 0.95);
  color: #333;
  border-radius: 12px;
  overflow: hidden;
  -webkit-app-region: drag;
}

.container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: 12px;
  -webkit-app-region: no-drag;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(0,0,0,0.05);
  -webkit-app-region: drag;
}

.title {
  font-size: 16px;
  font-weight: 600;
}

.status {
  font-size: 12px;
  color: #9bc295;
}

.drop-zone {
  border: 2px dashed #c4ddc8;
  border-radius: 8px;
  padding: 20px;
  margin: 10px 0;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s ease;
  background: rgba(255,255,255,0.5);
}

.drop-zone:hover, .drop-zone.dragover {
  border-color: #78b2be;
  background: rgba(120, 178, 190, 0.1);
}

.drop-icon {
  font-size: 32px;
  margin-bottom: 8px;
}

.drop-text {
  font-size: 14px;
  color: #666;
}

.chat-area {
  flex: 1;
  overflow-y: auto;
  padding: 10px 0;
}

.message {
  padding: 10px 14px;
  margin: 8px 0;
  border-radius: 12px;
  max-width: 85%;
  font-size: 14px;
  line-height: 1.5;
}

.message.assistant {
  background: #fff;
  border-radius: 12px 12px 12px 4px;
  margin-right: auto;
}

.message.user {
  background: #78b2be;
  color: white;
  border-radius: 12px 12px 4px 12px;
  margin-left: auto;
}

.input-area {
  display: flex;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid rgba(0,0,0,0.05);
}

.input-area input {
  flex: 1;
  padding: 10px 14px;
  border: 1px solid rgba(0,0,0,0.1);
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  background: white;
}

.input-area input:focus {
  border-color: #78b2be;
}

.input-area button {
  padding: 10px 16px;
  background: #78b2be;
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  transition: background 0.2s;
}

.input-area button:hover {
  background: #6a9fa9;
}
```

**Step 3: 写 renderer.js**

```javascript
// ui/src/renderer.js
const dropZone = document.getElementById('dropZone');
const chatArea = document.getElementById('chatArea');
const userInput = document.getElementById('userInput');
const sendBtn = document.getElementById('sendBtn');
const statusEl = document.getElementById('status');

// 检查后端状态
async function checkStatus() {
  try {
    const status = await window.api.getStatus();
    statusEl.style.color = '#9bc295';
    statusEl.title = '后端已连接';
  } catch (e) {
    statusEl.style.color = '#f8a7c8';
    statusEl.title = '后端未连接';
  }
}

// 添加消息到聊天区
function addMessage(text, type = 'assistant') {
  const msg = document.createElement('div');
  msg.className = `message ${type}`;
  msg.textContent = text;
  chatArea.appendChild(msg);
  chatArea.scrollTop = chatArea.scrollHeight;
}

// 拖拽处理
dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', async (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  
  const files = e.dataTransfer.files;
  for (const file of files) {
    addMessage(`收到文件: ${file.name}`, 'user');
    // TODO: 发送到后端
    setTimeout(() => {
      addMessage(`文件 "${file.name}" 已入库处理`, 'assistant');
    }, 500);
  }
});

// 发送消息
function sendMessage() {
  const text = userInput.value.trim();
  if (!text) return;
  
  addMessage(text, 'user');
  userInput.value = '';
  
  // TODO: 发送到后端
  setTimeout(() => {
    addMessage('收到，我来帮你处理...', 'assistant');
  }, 300);
}

sendBtn.addEventListener('click', sendMessage);
userInput.addEventListener('keypress', (e) => {
  if (e.key === 'Enter') sendMessage();
});

// 初始化
checkStatus();
setInterval(checkStatus, 5000);
```

**Step 4: Commit**

```bash
git add ui/src/
git commit -m "feat(ui): add basic floating assistant UI"
```

---

## Task 9: 创建 Python 虚拟环境和依赖

**Files:**
- Create: `requirements.txt`
- Create: `tools/.gitkeep`

**Step 1: 写 requirements.txt**

```txt
# requirements.txt
# 文档处理
pypdf>=3.0.0
python-docx>=0.8.11
python-pptx>=0.6.21
openpyxl>=3.1.0
beautifulsoup4>=4.12.0

# 人脸识别
insightface>=0.7.3
onnxruntime>=1.15.0
opencv-python-headless>=4.8.0
Pillow>=10.0.0

# 向量与搜索
lancedb>=0.3.0
sentence-transformers>=2.2.0

# 图谱
kuzu>=0.4.0

# MCP 协议
mcp>=1.0.0

# vCard 处理
vobject>=0.9.6

# IM 接入
lark-oapi>=1.0.0
dingtalk-stream>=1.0.0

# 工具
httpx>=0.25.0
loguru>=0.7.0
pyyaml>=6.0
python-magic>=0.4.27
```

**Step 2: 创建 Python 虚拟环境**

```bash
python -m venv .venv
```

**Step 3: 安装依赖**

Windows:
```bash
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS/Linux:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

**Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add python requirements"
```

---

## Task 10: 创建基础 MCP Server

**Files:**
- Create: `tools/document-parser/server.py`
- Create: `tools/document-parser/__init__.py`

**Step 1: 写 server.py**

```python
# tools/document-parser/server.py
"""
文档解析 MCP Server
支持 PDF, Word, Excel, PPT, TXT 等格式的解析
"""
import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

# 创建 MCP Server 实例
server = Server("document-parser")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出可用工具"""
    return [
        Tool(
            name="parse_document",
            description="解析文档，提取文本内容。支持 PDF, Word, Excel, PPT, TXT 等格式。",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径"
                    },
                    "extract_images": {
                        "type": "boolean",
                        "description": "是否提取图片（默认 False）",
                        "default": False
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="get_supported_formats",
            description="获取支持的文档格式列表",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """处理工具调用"""
    
    if name == "parse_document":
        file_path = arguments.get("file_path")
        
        # TODO: 实现实际的文档解析
        result = {
            "status": "success",
            "file_path": file_path,
            "content": f"[文档内容待实现] 解析文件: {file_path}",
            "metadata": {
                "pages": 0,
                "word_count": 0
            }
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2)
        )]
    
    elif name == "get_supported_formats":
        formats = {
            "supported": [
                {"ext": ".pdf", "name": "PDF"},
                {"ext": ".docx", "name": "Word"},
                {"ext": ".xlsx", "name": "Excel"},
                {"ext": ".pptx", "name": "PowerPoint"},
                {"ext": ".txt", "name": "文本"},
                {"ext": ".md", "name": "Markdown"}
            ]
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(formats, ensure_ascii=False, indent=2)
        )]
    
    else:
        return [TextContent(
            type="text",
            text=f"Unknown tool: {name}"
        )]


async def main():
    """启动 MCP Server"""
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 2: 写 __init__.py**

```python
# tools/document-parser/__init__.py
"""文档解析工具"""
```

**Step 3: 测试 MCP Server**

```bash
cd tools/document-parser
python server.py
```

在另一个终端测试（通过 stdin 发送 MCP 请求）:
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python tools/document-parser/server.py
```

**Step 4: Commit**

```bash
git add tools/document-parser/
git commit -m "feat(mcp): add document-parser server"
```

---

## Task 11: 创建 MCP 配置文件

**Files:**
- Create: `config/mcp-servers.yaml`

**Step 1: 写 mcp-servers.yaml**

```yaml
# config/mcp-servers.yaml
# MCP Server 配置
# 定义所有可用的 MCP 工具服务器

servers:
  document-parser:
    description: "文档解析工具 - 支持 PDF, Word, Excel, PPT 等格式"
    command: "python"
    args:
      - "tools/document-parser/server.py"
    enabled: true
    timeout: 60

  vector-store:
    description: "向量存储工具 - 语义搜索和向量索引"
    command: "python"
    args:
      - "tools/vector-store/server.py"
    enabled: false  # 待实现
    timeout: 30

  face-recognition:
    description: "人脸识别工具 - 检测和识别人脸"
    command: "python"
    args:
      - "tools/face-recognition/server.py"
    enabled: false  # 待实现
    timeout: 120

  knowledge-graph:
    description: "知识图谱工具 - 关系发现和图谱查询"
    command: "python"
    args:
      - "tools/knowledge-graph/server.py"
    enabled: false  # 待实现
    timeout: 30
```

**Step 2: Commit**

```bash
git add config/mcp-servers.yaml
git commit -m "feat(config): add mcp servers configuration"
```

---

## Task 12: 创建 README

**Files:**
- Create: `README.md`

**Step 1: 写 README.md**

```markdown
# 个人知识助理

> 扔进来就完事，自动发现一切关系

本地个人知识助理，支持文件管理、知识图谱、人脸识别、语义搜索等功能。

## 功能特性

- 📁 **文件管理** - 自动分类、命名、版本管理
- 🔍 **语义搜索** - 不记文件名，说出意思就能找到
- 👤 **人脸识别** - 照片自动识人，建立人物关系
- 🔗 **知识图谱** - 动态展开关系网络，发现隐藏关联
- 📝 **便签捕获** - 随手记录，自动关联上下文
- 💬 **对话交互** - 自然语言操作，简单直接

## 技术栈

- **后端**: Go (基于 Nanobot 框架)
- **前端**: Electron + Svelte
- **工具层**: Python MCP Servers
- **存储**: LanceDB (向量) + Kuzu (图谱) + SQLite (元数据)

## 开发指南

### 环境要求

- Go 1.21+
- Node.js 20+
- Python 3.11+

### 快速开始

```bash
# 克隆仓库
git clone https://github.com/abc-lee/personal-assistant.git
cd personal-assistant

# 安装前端依赖
npm install

# 安装 Python 依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 启动后端
go run cmd/main.go

# 启动前端 (另一个终端)
npm run dev
```

## 项目结构

```
personal-assistant/
├── cmd/           # Go 入口
├── pkg/           # Go 核心代码
├── tools/         # Python MCP 工具
├── ui/            # Electron + Svelte 前端
├── config/        # 配置文件
└── docs/          # 文档
```

## 许可证

MIT
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README"
```

---

## Phase 1 完成标准

- [ ] Go 后端可启动，响应 `/health` 和 `/api/status`
- [ ] Electron 前端可启动，显示悬浮窗口
- [ ] 拖拽文件到前端，显示反馈信息
- [ ] Python MCP Server 可独立启动
- [ ] 项目结构完整，代码已提交到 Git

---

## 执行方式选择

**Plan complete and saved to `docs/plans/2026-03-22-phase1-basic-framework.md`.**

**两种执行选项：**

1. **Subagent-Driven (当前会话)** - 每个任务派发子 Agent 执行，任务间可审查

2. **Parallel Session (新会话)** - 打开新会话使用 executing-plans 批量执行

选择哪种方式？
