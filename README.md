# Niu — 个人知识管理助手

Electron 前端 + Go 启动器 + Python Agent 核心 + MCP 服务器集群的混合架构。

## 项目结构

```
├── main.go              # Go 启动器（构建+监控子进程）
├── niu_api/             # Python API 服务（HTTP/SSE）
├── agent/               # Agent 核心（主循环、LLM抽象、工具注册）
├── mcp-servers/         # MCP 服务器集群（记忆/文件/照片/知识图谱等）
├── ui/assistant/        # Electron 前端
├── config/              # 配置文件（Agent定义、MCP服务器、LLM预设）
├── models/              # 本地模型（向量模型、人脸识别）
├── python/              # 自包含 Python 运行时（打包分发用）
└── docs/                # 设计文档
```

## 用户数据目录

程序运行所需的用户数据模板位于 `config/user-data/`：

| 文件 | 说明 |
|------|------|
| `memory.json` | 用户记忆（身份、偏好、工作目录） |
| `preferences.json` | 存储配置（分类、路径结构、冲突阈值） |
| `skills/*.md` | Skills 技能文件（7个） |

安装后需复制到用户家目录：

- Linux/Mac: `~/.niu/`
- Windows: `%USERPROFILE%\.niu\`

```bash
# Linux/Mac
mkdir -p ~/.niu/skills
cp config/user-data/memory.json ~/.niu/
cp config/user-data/preferences.json ~/.niu/
cp config/user-data/skills/*.md ~/.niu/skills/

# Windows (PowerShell)
mkdir "$env:USERPROFILE\.niu\skills"
copy config\user-data\memory.json "$env:USERPROFILE\.niu\"
copy config\user-data\preferences.json "$env:USERPROFILE\.niu\"
copy config\user-data\skills\*.md "$env:USERPROFILE\.niu\skills\"
```

> 仅当 `~/.niu/` 下对应文件不存在时才复制，避免覆盖用户已有的配置和记忆。

## 快速启动

```bash
go run main.go
```

## 详细文档

见 [docs/SYSTEM_MANUAL.md](docs/SYSTEM_MANUAL.md)。
