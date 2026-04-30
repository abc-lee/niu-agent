# Niu 个人知识助理 — 系统手册

## 一、系统概述

### 1.1 产品定位

Niu 是一个**本地运行**的个人知识管理助手，核心理念：
- **本地优先**：所有数据存储在本地，隐私可控
- **AI 原生**：每个操作都有 AI 辅助
- **知识沉淀**：文档入库 → 知识图谱 + 向量检索 → 持久记忆

### 1.2 功能列表

| 功能 | 说明 |
|------|------|
| 对话助手 | 多模型支持（OpenAI/Claude/DeepSeek/Qwen/Ollama） |
| 文档入库 | 拖入 PDF/Word/PPT/Excel/MD → 自动解析入库 |
| 知识图谱 | 自动提取实体和关系，支持图谱查询 |
| 语义搜索 | 向量检索 + 递归查询，精准匹配 |
| 人脸识别 | 拖入照片 → 自动检测人脸 → 相册管理 |
| 定时任务 | 自然语言创建提醒，支持循环任务 |
| 智能记忆 | 自动学习用户偏好和习惯 |
| 浏览器辅助 | Chrome Extension，AI 操作网页 |

### 1.3 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 前端 | Electron | 桌面应用 |
| 后端 | Python FastAPI | API 服务 + Agent 核心 |
| 启动器 | Go | 进程管理 + 自动更新 |
| 数据库 | SQLite | 消息/向量/图谱/任务 |
| 向量 | Sentence Transformers | 语义搜索 |
| 人脸 | InsightFace + ONNX | 人脸检测/识别 |
| MCP | 同进程架构 | 工具调用（无 stdio 通信） |

---

## 二、架构设计

### 2.1 MCP 同进程架构

```
旧架构：Agent → stdio → MCP Server 进程（~40秒/10次调用）
新架构：Agent → ToolRegistry → 直接 Python 调用（~0秒/10次调用）
```

**核心组件：**
- **ToolRegistry** (`agent/tool_registry.py`)：全局工具注册中心，`registry.get("server/tool")` 直接调用
- **MCP Loader** (`agent/mcp_loader.py`)：启动时加载所有 MCP 模块，严格验证
- 每个 MCP 服务器模块定义 `TOOL_SCHEMAS` 字典 + 工具函数

**已实现的 MCP 服务器（8个）：**

| 服务器 | 功能 | 预加载 |
|--------|------|--------|
| `photo-server` | 照片管理 + 人脸识别 | Yes |
| `lightrag-server` | 知识图谱 + 向量检索（LightRAG 统一） | Yes |
| `config-manager` | 配置管理 | Yes |
| `memory-server` | 智能记忆 | Yes |
| `scheduler-server` | 定时任务 | Yes |
| `file-parser` | 文档解析 | Yes |
| `session-manager` | 会话管理 | No |
| `browser-server` | 浏览器自动化 | No |
| `nanobot.system` | 内置系统工具 | — |

> `kg-server`、`vector-store`、`embedding-service` 已废弃，由 `lightrag-server` 替代。

### 2.2 工具注入机制

**衰减-覆盖评分模式：**
- 每轮开始：所有活跃工具 -10 分
- 向量检索命中：覆盖为新分数
- 低于 min_score(50) 自动移除

**向量库标签：**
- `l1` — L1 摘要
- `l2` — L2 原文
- `skill` — Skills 文件
- `mcp_tool` — MCP 工具描述
- `interaction_habit` — 交互习惯（工具方言、用户状态、用户画像）

### 2.3 数据流

```
用户输入
  ↓
Agent 主循环 (agent_loop.py)
  ↓
Handler (handler.py) — 工具分发 + 工作记忆
  ↓
ToolRegistry — 同进程调用 MCP 工具
  ↓
MCP 服务器 — 具体功能实现
  ↓
结果返回 → LLM 生成回复
```

### 2.4 目录结构

```
ai-bot/
├── agent/              # Agent 核心
│   ├── generic/        # 通用 Agent 实现
│   ├── tool_registry.py  # 工具注册中心
│   ├── mcp_loader.py   # MCP 加载器
│   └── injector/       # 动态注入
├── niu_api/            # FastAPI 服务
├── mcp-servers/        # MCP 服务器（8个）
├── ui/assistant/       # Electron 前端
├── config/             # 配置文件
├── models/             # 模型文件
├── scripts/            # 运维脚本
├── data/               # 运行时数据（SQLite）
└── docs/               # 文档
```

---

## 分册索引

| 分册 | 文件 | 内容 |
|------|------|------|
| 向量库运维 | [manual-vector-store.md](manual-vector-store.md) | 向量库数据结构、文档类型、交互习惯、metadata、递归查询、初始化 |
| 故障排查 | [manual-troubleshooting.md](manual-troubleshooting.md) | 启动问题、人脸识别、定时任务、向量库、数据、浏览器插件 |
| 性能优化 | [manual-performance.md](manual-performance.md) | 内存优化、启动速度、GPU 加速策略 |
| 依赖与模型 | [manual-dependencies.md](manual-dependencies.md) | Python 依赖、GPU 支持策略、人脸识别模型、向量模型、下载镜像 |
| 用户操作 | [manual-user-guide.md](manual-user-guide.md) | 首次启动、LLM 配置、知识图谱、记忆管理、常见问题 |
| 开发者参考 | [manual-developer.md](manual-developer.md) | 本地开发、调试技巧、API 端点、环境变量、更新日志 |
