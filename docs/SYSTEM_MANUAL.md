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
| 智能记忆 | 自动学习用户偏好和习惯，按脑区优先级差异化遗忘曲线 |
| 浏览器辅助 | Chrome Extension，AI 操作网页 |
| /stop 指令 | 停止当前 Agent 工作，支持 Electron 和 IM 通用 |
| /clear 指令 | 先停止 Agent 再清空对话，支持 Electron 和 IM 通用 |
| 见缝插针 | Agent 运行期间发送的补充消息自动插入到当前对话上下文（补充在前，当前任务在后） |

**指令机制**：
- `/stop`：通过正常消息通道发送（非独立 API），在 `chat_session` 和 `ChatQueue` 入口拦截并设置全局停止标志。Agent 主循环、handler dispatch 在关键点检查标志并退出。前端停止按钮自动发送 `/stop` 文本。
- `/clear`：先发送 `/stop` 停止 Agent，等 `chat_idle` 事件后延迟执行 `clearChat()`，避免锁等待阻塞 UI。
- 停止标志生命周期：Agent 循环退出时自动 `clear_stop()`，不留残留影响后续定时任务。用户发新消息时防御性清除。

**见缝插针机制**：
- Agent 运行期间，用户发送的补充消息通过 `enqueue_supplement()` 入队
- `agent_runner_loop` 每轮在 `next_prompt` 注入前读取队列（`drain_supplement()`），将补充消息拼接到 `next_prompt` 前面
- 补充信息作为参考在前，当前任务作为最后内容在后，LLM 优先处理当前任务
- 所有入口（Electron chat_session）统一使用 `enqueue_supplement()`
- 前端发送消息永远不阻塞，UI 状态由 SSE chat_busy/chat_idle 事件驱动

### 1.3 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 前端 | Iced (Rust GPU) | 桌面应用 |
| 后端 | Python FastAPI | API 服务 + Agent 核心 |
| 启动器 | Rust | 进程管理 + Iced GUI + 自动更新 |
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

**脑区加权检索（双路径架构）：**
- **全局向量检索**：search_multi_lightrag，top_k=10，返回语义最相关的技能和知识
- **脑区内过滤检索**：search_within_region，在点亮脑区的成员实体范围内做语义检索，top_k=10
- 两条路径结果用 seen_names 去重，保证不重复注入
- 脑区激活度 > 0.3 的脑区参与过滤检索
- 点亮超过 5 个脑区时，注入提示建议关闭无关脑区

**上下文去重原则**：
- 工具用途描述只在 tools description 中出现，system prompt 不再重复列出
- 动态注入过滤 `mcp_tool`/`tool` 类型实体和内部架构概念，防止工具描述和硬编码内容重复注入
- 子Agent工具按 `mcpToolFilter` 白名单过滤，只注入职责所需工具（向后兼容：无配置时全量注入）

**niu 根节点规则**：
- `niu` 是知识图谱根节点，只与脑区连接，不与普通实体直接连接
- 运行时代码（`lightrag_insert_entity`）不创建 niu→实体锚边
- 实体可达性由脑区 `_region:contains` 边保证

**LightRAG 实体类型（entity_type）：**
- `skill` — Skills 文件
- `tool` — MCP 工具描述
- `person` — 人物（照片识别）
- `concept` — 概念/知识实体
- `photo` — 照片摘要

> 所有 entity_type 和 keywords 统一使用小写存储和比较（写入时 `.lower()`，查询时 `.lower()` 匹配），消除大小写不一致导致的重复实体和 Counter 投票分裂问题。

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
| `context-manager` | 上下文管理：内容压缩 | auto-tidy 管线自动调度 | 0.2 |
| `journal-agent` | 工作日志：从对话提取工作内容写入日志 | 主 Agent 委托或 auto-tidy | 0.3 |
| `entity-extractor` | 内容提炼：从对话筛选有价值内容入库 | auto-tidy 管线自动调度 | 0.3 |
| `dream-evolver` | 梦境进化：精加工知识图谱 + skill 编写与优化 | auto-tidy 管线自动调度 | 0.3 |

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

**Skill 编写职责：**
- **dream-evolver** 是新 skill 的唯一创建者——它根据对话中观察到的信号（重复模式、失败后解决、skill 反馈等）自动创建草稿 skill
- **主 Agent（niu）** 可以修改已有 skill 的内容（用 edit 工具），但不能创建新 skill 文件
- **ExperienceSummarizer** 已关闭，不再生成 skill

**草稿→验证→转正流程：**

```
dream-evolver 观察到信号 → 创建草稿 skill (status: draft)
  ↓
SkillSync 同步到 LightRAG，description 加 [草稿] 前缀
  ↓
runner.py 注入时显示 "⚠️ 草稿skill — 使用后反馈效果"
  ↓
主 Agent 使用草稿 skill 后必须明确反馈效果
  ↓
dream-evolver 从反馈中识别信号 → 转正 (status: active) 或修改
```

**Skill Frontmatter 规范：**

```yaml
---
name: skill-name-with-hyphens
description: Use when [触发条件，不写工作流]
status: draft | active
created: YYYY-MM-DD
last_tested: YYYY-MM-DD
---
```

字段说明：
- `name`：只含字母、数字、连字符
- `description`：以 "Use when..." 开头，只写触发条件，不写工作流，500 字符以内
- `status`：新建时 `draft`，验证通过后 `active`
- `created`：创建日期
- `last_tested`：最近一次验证或修改日期

**Skill 正文结构：**

```markdown
# Skill Name

## Overview
核心原则，1-2 句话。

## When to Use
- 触发条件
- 不适用的情况
（草稿 skill 会在此区域显示"⚠️ 此 skill 为草稿状态，使用后请反馈效果"提示）

## Steps
关键步骤。

## Common Mistakes
常见错误和修复。

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
```

**Skill-Aware Reflection：**

dream-evolver 修改 skill 时遵循 Skill-Aware Reflection 方法论：
- **规则有错**（SKILL_DEFECT）→ 修改 skill 正文
- **规则没错但没被遵守**（EXECUTION_LAPSE）→ 不改正文，只在"执行提醒"区域添加提醒重申已有规则
- 拿不准时默认规则没错，不要因为一次没被遵守就改掉有效规则

**已定义的 Skills：**

| Skill 文件 | 功能 | 状态 |
|-----------|------|------|
| `brain-region-management.md` | 脑区管理规范 | active |
| `browser-automation.md` | 浏览器自动化操作规范 | active |
| `note-management.md` | 笔记管理流程 | active |
| `office-docs.md` | Office 文档处理规范 | active |
| `photo-face-display.md` | 照片人脸显示规范 | active |
| `report-skill.md` | 报告生成模板与聚合规则 | active |

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
| 高德开通 | [manual-amap-setup.md](manual-amap-setup.md) | 高德地图 API Key 获取流程、浏览器操作步骤、配置写入、故障排查 |
