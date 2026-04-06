# 个人知识助理 - 总体实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建一个本地个人知识助理，支持文件管理、知识图谱、人脸识别、语义搜索、IM 消息接入等核心功能。

**Architecture:** Go 核心（基于 Nanobot）+ Python MCP 工具层 + Electron/Svelte 前端，通过 MCP 协议统一通信。

**Tech Stack:** Go, Python, TypeScript, Svelte, Electron, AntV G6, LanceDB, Kuzu, SQLite, InsightFace

---

## 项目结构

```
personal-assistant/
├── cmd/                          # Go 入口
│   └── main.go
├── pkg/                          # Go 核心（基于 Nanobot）
│   ├── agent/                    # Agent 执行循环
│   ├── mcp/                      # MCP 协议处理
│   └── server/                   # HTTP API
├── tools/                        # Python MCP 工具
│   ├── document-parser/          # 文档解析
│   ├── face-recognition/         # 人脸识别
│   ├── vector-store/             # 向量存储
│   ├── knowledge-graph/          # 知识图谱
│   ├── clipboard/                # 剪贴板
│   └── note-manager/             # 便签管理
├── ui/                           # Electron + Svelte 前端
│   ├── electron/                 # Electron 主进程
│   └── src/                      # Svelte 前端
│       ├── components/
│       ├── pages/
│       └── lib/
├── config/                       # 配置文件
├── docs/                         # 文档
│   ├── architecture/             # 架构文档
│   └── plans/                    # 实施计划
└── scripts/                      # 构建脚本
```

---

## Phase 总览

| Phase | 名称 | 核心目标 | 预计工时 |
|-------|------|----------|----------|
| **Phase 1** | 基础框架 | 项目骨架、悬浮窗口、Go 后端 | 2-3 周 |
| **Phase 2** | 文档能力 | 文档解析、向量存储、语义搜索 | 3-4 周 |
| **Phase 3** | 图谱能力 | Kuzu 集成、关系发现、图谱可视化 | 3-4 周 |
| **Phase 4** | 照片能力 | 人脸识别、人脸聚类、照片-人物关联 | 3-4 周 |
| **Phase 5** | 便签能力 | 便签输入、剪贴板捕获、自动关联 | 2-3 周 |
| **Phase 6** | Agent 能力 | 对话功能、周报生成、提醒功能 | 2-3 周 |
| **Phase 7** | IM 接入 | 飞书/钉钉机器人接入 | 2-3 周 |
| **Phase 8** | 打磨优化 | 一键安装、性能优化、体验改进 | 持续 |

---

## Phase 1：基础框架

### 1.1 项目初始化

**Task 1.1.1: 创建项目结构**

```bash
mkdir -p personal-assistant/{cmd,pkg,tools,ui,config,docs,scripts}
mkdir -p personal-assistant/pkg/{agent,mcp,server}
mkdir -p personal-assistant/tools/{document-parser,face-recognition,vector-store,knowledge-graph,clipboard,note-manager}
mkdir -p personal-assistant/ui/{electron,src}
```

**Task 1.1.2: 初始化 Go 模块**

```bash
cd personal-assistant
go mod init github.com/your-org/personal-assistant
```

**Task 1.1.3: 复制 Nanobot 核心代码**

从 `E:\opencode\nanobot\pkg` 复制核心模块到 `pkg/`:
- `pkg/agents` → `pkg/agent`
- `pkg/mcp` → `pkg/mcp`
- `pkg/types` → `pkg/types`
- `pkg/config` → `pkg/config`

---

### 1.2 Go 后端基础

**Task 1.2.1: 创建 main.go 入口**

文件: `cmd/main.go`

```go
package main

import (
    "log"
    "github.com/your-org/personal-assistant/pkg/server"
)

func main() {
    log.Println("Personal Assistant starting...")
    
    // 启动 HTTP 服务器
    s := server.New(":8765")
    if err := s.Start(); err != nil {
        log.Fatal(err)
    }
}
```

**Task 1.2.2: 创建 HTTP 服务器**

文件: `pkg/server/server.go`

```go
package server

import (
    "fmt"
    "net/http"
)

type Server struct {
    addr string
}

func New(addr string) *Server {
    return &Server{addr: addr}
}

func (s *Server) Start() error {
    http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
        w.WriteHeader(http.StatusOK)
        w.Write([]byte("OK"))
    })
    
    fmt.Printf("Server starting on %s\n", s.addr)
    return http.ListenAndServe(s.addr, nil)
}
```

**Task 1.2.3: 测试后端启动**

```bash
go run cmd/main.go
# 访问 http://localhost:8765/health
# 预期返回: OK
```

---

### 1.3 Electron 前端基础

**Task 1.3.1: 初始化 Electron 项目**

```bash
cd ui
npm init -y
npm install electron --save-dev
npm install electron-builder --save-dev
```

**Task 1.3.2: 创建 Electron 主进程**

文件: `ui/electron/main.js`

```javascript
const { app, BrowserWindow } = require('electron');
const path = require('path');

function createWindow() {
    const win = new BrowserWindow({
        width: 400,
        height: 500,
        frame: false,
        transparent: true,
        alwaysOnTop: true,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js')
        }
    });
    
    win.loadFile('src/index.html');
}

app.whenReady().then(() => {
    createWindow();
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});
```

**Task 1.3.3: 创建预加载脚本**

文件: `ui/electron/preload.js`

```javascript
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
    // 后续添加 API
});
```

**Task 1.3.4: 创建基础 HTML**

文件: `ui/src/index.html`

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Personal Assistant</title>
    <style>
        body {
            margin: 0;
            padding: 20px;
            background: rgba(30, 30, 30, 0.95);
            border-radius: 10px;
            color: white;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        .container {
            text-align: center;
        }
        .drop-zone {
            border: 2px dashed #666;
            border-radius: 10px;
            padding: 40px;
            margin: 20px 0;
            cursor: pointer;
        }
        .drop-zone:hover {
            border-color: #4CAF50;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>🤖 个人助理</h2>
        <div class="drop-zone" id="dropZone">
            拖拽文件到这里
        </div>
    </div>
    <script src="renderer.js"></script>
</body>
</html>
```

**Task 1.3.5: 创建渲染进程脚本**

文件: `ui/src/renderer.js`

```javascript
const dropZone = document.getElementById('dropZone');

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.style.borderColor = '#4CAF50';
});

dropZone.addEventListener('dragleave', () => {
    dropZone.style.borderColor = '#666';
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.style.borderColor = '#666';
    
    const files = e.dataTransfer.files;
    console.log('Dropped files:', files);
    
    // TODO: 发送到后端处理
});
```

**Task 1.3.6: 测试 Electron 启动**

```bash
cd ui
npx electron .
# 预期: 显示悬浮窗口，可拖拽文件
```

---

### 1.4 前后端通信

**Task 1.4.1: 添加文件接收 API**

文件: `pkg/server/server.go` (修改)

```go
// 在 Start() 方法中添加
http.HandleFunc("/api/ingest", func(w http.ResponseWriter, r *http.Request) {
    if r.Method != "POST" {
        http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
        return
    }
    
    // 解析 multipart form
    r.ParseMultipartForm(32 << 20) // 32MB
    
    file, header, err := r.FormFile("file")
    if err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }
    defer file.Close()
    
    // TODO: 保存文件并处理
    fmt.Printf("Received file: %s\n", header.Filename)
    
    w.Header().Set("Content-Type", "application/json")
    w.Write([]byte(`{"status": "ok", "file": "` + header.Filename + `"}`))
})
```

**Task 1.4.2: 修改前端发送文件**

文件: `ui/src/renderer.js` (修改)

```javascript
dropZone.addEventListener('drop', async (e) => {
    e.preventDefault();
    dropZone.style.borderColor = '#666';
    
    const files = e.dataTransfer.files;
    
    for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const response = await fetch('http://localhost:8765/api/ingest', {
                method: 'POST',
                body: formData
            });
            const result = await response.json();
            console.log('Upload result:', result);
        } catch (err) {
            console.error('Upload error:', err);
        }
    }
});
```

**Task 1.4.3: 测试前后端通信**

1. 启动后端: `go run cmd/main.go`
2. 启动前端: `npx electron .`
3. 拖拽文件到窗口
4. 预期: 后端控制台显示文件名，前端控制台显示响应

---

### 1.5 Python MCP Server 基础

**Task 1.5.1: 创建 Python 虚拟环境**

```bash
cd tools
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

**Task 1.5.2: 安装 MCP SDK**

```bash
pip install mcp
```

**Task 1.5.3: 创建基础 MCP Server**

文件: `tools/document-parser/server.py`

```python
from mcp.server import Server
from mcp.types import Tool, TextContent

server = Server("document-parser")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="parse_document",
            description="解析文档，提取文本内容",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"}
                },
                "required": ["file_path"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "parse_document":
        # TODO: 实现文档解析
        return [TextContent(
            type="text",
            text=f"Parsed: {arguments['file_path']}"
        )]

if __name__ == "__main__":
    import asyncio
    asyncio.run(server.run())
```

**Task 1.5.4: 测试 MCP Server**

```bash
cd tools/document-parser
python server.py
# 预期: 启动 MCP 服务，等待 stdio 通信
```

---

### 1.6 Go 调用 Python MCP

**Task 1.6.1: 添加 MCP 客户端配置**

文件: `config/mcp-servers.yaml`

```yaml
servers:
  document-parser:
    command: "python"
    args: ["tools/document-parser/server.py"]
    enabled: true
  
  vector-store:
    command: "python"
    args: ["tools/vector-store/server.py"]
    enabled: true
```

**Task 1.6.2: 创建 MCP 客户端管理器**

文件: `pkg/mcp/manager.go`

```go
package mcp

import (
    "encoding/json"
    "fmt"
    "os/exec"
)

type MCPServer struct {
    Name    string
    Command string
    Args    []string
    cmd     *exec.Cmd
}

type MCPManager struct {
    servers map[string]*MCPServer
}

func NewManager() *MCPManager {
    return &MCPManager{
        servers: make(map[string]*MCPServer),
    }
}

func (m *MCPManager) StartServer(name, command string, args []string) error {
    server := &MCPServer{
        Name:    name,
        Command: command,
        Args:    args,
    }
    
    server.cmd = exec.Command(command, args...)
    
    // 启动进程
    if err := server.cmd.Start(); err != nil {
        return fmt.Errorf("failed to start MCP server %s: %w", name, err)
    }
    
    m.servers[name] = server
    return nil
}

func (m *MCPManager) CallTool(serverName, toolName string, args map[string]interface{}) (interface{}, error) {
    // TODO: 实现 MCP 协议通信
    return nil, nil
}
```

---

## Phase 1 完成标准

- [ ] Go 后端可启动，响应健康检查
- [ ] Electron 前端可启动，显示悬浮窗口
- [ ] 拖拽文件到前端，后端收到请求
- [ ] Python MCP Server 可独立启动
- [ ] Go 可启动 Python MCP Server

---

## 后续 Phase 详细计划

详细的 Phase 2-8 实施计划将单独创建：

- `docs/plans/2026-03-22-phase2-document-pipeline.md`
- `docs/plans/2026-03-22-phase3-knowledge-graph.md`
- `docs/plans/2026-03-22-phase4-face-recognition.md`
- `docs/plans/2026-03-22-phase5-notes-clipboard.md`
- `docs/plans/2026-03-22-phase6-agent-capabilities.md`
- `docs/plans/2026-03-22-phase7-im-integration.md`
- `docs/plans/2026-03-22-phase8-polish-optimization.md`

---

## 执行方式

**两种执行选项：**

1. **Subagent-Driven (当前会话)** - 每个任务派发子 Agent 执行，任务间可审查
2. **Parallel Session (新会话)** - 打开新会话，使用 executing-plans 批量执行

选择哪种方式？
