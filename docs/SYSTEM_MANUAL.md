# Niu 个人知识助理 — 系统手册

## 一、系统概述

### 1.1 产品定位

Niu 是一个**本地运行**的个人知识管理助手，核心理念：
- **本地优先**：所有数据存储在本地，隐私可控
- **AI 原生**：每个操作都有 AI 辅助
- **知识沉淀**：文档入库 → 知识图谱（LightRAG 统一检索） → 持久记忆

### 1.2 功能列表

| 功能 | 说明 |
|------|------|
| 对话助手 | 多模型支持（OpenAI/Claude/DeepSeek/Qwen/Ollama） |
| 文档入库 | 拖入文档自动入库；部分格式（.doc/.xls/.ppt）仅支持存储，不支持知识图谱 |
| 知识图谱 | 自动提取实体和关系，支持图谱查询 |
| 语义搜索 | LightRAG 统一检索（local/global/hybrid/mix/naive 模式） |
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
| 数据库 | SQLite | 消息/图谱/任务 |
| 知识检索 | LightRAG + Sentence Transformers | 知识图谱 + 语义搜索（统一架构） |
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

**已实现的 MCP 服务器（10个）：**

| 服务器 | 功能 | 预加载 |
|--------|------|--------|
| `photo-server` | 照片管理 + 人脸识别 | Yes |
| `lightrag-server` | 知识图谱 + 语义检索（LightRAG 统一） | Yes |
| `config-manager` | 配置管理 | Yes |
| `memory-server` | 智能记忆 | Yes |
| `scheduler-server` | 定时任务 | Yes |
| `brain-region-server` | 脑区管理（激活/降权/状态） | Yes |
| `file-parser` | 文档解析 | Yes |
| `session-manager` | 会话管理 | No |
| `browser-server` | 浏览器自动化 | No |
| `feishu-server` | 飞书消息收发（日历/任务） | No（可选） |

> `kg-server`、`vector-store`、`embedding-service` 已移除，由 `lightrag-server` 统一替代。`mcp-servers/embedding-service/` 目录仍残留但不再加载。
> `nanobot.system` 为内置系统工具（code_run/read/edit/write），非 MCP 服务器模块，通过 disk 配置管理。
> `feishu-server` 为可选服务器，需配置飞书机器人凭证后才会启用（`optional: true`）。

### 2.2 工具注入机制

**衰减-覆盖评分模式：**
- 每轮开始：所有活跃工具 -10 分
- LightRAG 图检索命中：覆盖为新分数
- 低于 min_score(50) 自动移除

**LightRAG 实体类型（entity_type）：**
- `Skill` — Skills 文件
- `Tool` — MCP 工具描述
- `Person` — 人物（照片识别）
- `Concept` — 概念/知识实体
- `Photo` — 照片摘要

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
├── mcp-servers/        # MCP 服务器（10个）
├── ui/assistant/       # Electron 前端
├── config/             # 配置文件
├── models/             # 模型文件
├── scripts/            # 运维脚本
├── data/               # 运行时数据（SQLite）
└── docs/               # 文档
```

### 2.5 子 Agent 架构

主 Agent 负责对话，子 Agent 负责执行特定任务。子 Agent 通过 `chat-with-{agentName}` 工具调用。

**已定义的子 Agent（6个）：**

| 子 Agent | 职责 | 触发方式 | 温度 |
|----------|------|----------|------|
| `file-processor` | 文件处理：复制、解析、存储、向量化 | 主 Agent 委托（文件拖入） | 0.2 |
| `event-manager` | 事件管理：创建/查询/删除事件 | 主 Agent 委托 | 0.2 |
| `context-manager` | 上下文管理：L0/L1/L2 记忆层级 | auto-tidy 管线自动调度 | 0.2 |
| `journal-agent` | 工作日志：从对话提取工作内容写入日志 | 主 Agent 委托或 auto-tidy | 0.3 |
| `entity-extractor` | 内容提炼：从对话筛选有价值内容入库 | auto-tidy 管线自动调度 | 0.3 |
| `dream-evolver` | 梦境进化：精加工知识图谱 + skill 维护 | auto-tidy 管线自动调度 | 0.3 |

**BLOCKED_SUBAGENTS 机制：**

`context-manager`、`entity-extractor`、`dream-evolver` 三个子 Agent 在 `agent/handler.py` 中被列入 `BLOCKED_SUBAGENTS` 集合，禁止主 Agent 手动调用。它们由 `auto-tidy` 管线按特定时机自动调度，确保：
- 避免主 Agent 误触发导致重复执行
- 保证执行顺序和时机符合系统设计
- 防止用户对话被不必要的后台任务打断

### 2.6 Skills 机制

Skills 是存储在 `memory/skills/` 目录下的 Markdown 文件，定义了特定任务的执行规范和模板。

**核心流程：**
1. Skills 文件通过 `agent/injector/sync.py` 定时同步到 LightRAG 向量库（entity_type = `Skill`）
2. Agent 每轮对话时，通过 `_inject_dynamic_resources()` 按语义搜索匹配相关 Skill
3. 匹配到的 Skill 内容动态注入到 Agent 上下文，指导 Agent 按规范执行任务

**已定义的 Skills：**

| Skill 文件 | 功能 |
|-----------|------|
| `browser-automation.md` | 浏览器自动化操作规范 |
| `note-management.md` | 笔记管理流程 |
| `office-docs.md` | Office 文档处理规范 |
| `photo-face-display.md` | 照片人脸显示规范 |
| `report-skill.md` | 报告生成模板与聚合规则 |
| `Write-SKILL.md` | 创建新 Skill 的规范（RED-GREEN-REFACTOR 流程） |

**report-skill 触发条件：** 当 Agent 编写或整理用户日志、生成周报/月报等报告时，向量检索会自动匹配并注入 `report-skill.md`，Agent 按其中定义的聚合规则和模板生成报告。

---

## 分册索引

| 分册 | 文件 | 内容 |
|------|------|------|
| 知识检索运维 | [manual-vector-store.md](manual-vector-store.md) | LightRAG 知识图谱架构、实体类型、检索模式、文档管理 |
| 故障排查 | [manual-troubleshooting.md](manual-troubleshooting.md) | 启动问题、人脸识别、定时任务、知识检索、数据、浏览器插件 |
| 性能优化 | [manual-performance.md](manual-performance.md) | 内存优化、启动速度、GPU 加速策略 |
| 依赖与模型 | [manual-dependencies.md](manual-dependencies.md) | Python 依赖、GPU 支持策略、人脸识别模型、向量模型、下载镜像 |
| 用户操作 | [manual-user-guide.md](manual-user-guide.md) | 首次启动、LLM 配置、知识图谱、记忆管理、常见问题 |
| 开发者参考 | [manual-developer.md](manual-developer.md) | 本地开发、调试技巧、API 端点、环境变量、更新日志 |
| 文件格式支持 | [manual-file-formats.md](manual-file-formats.md) | 文件存储/知识图谱/照片支持的格式，不支持KG的格式及原因 |
| 飞书开通 | [manual-feishu-setup.md](manual-feishu-setup.md) | 飞书机器人开通流程、浏览器操作步骤、配置写入、故障排查 |
