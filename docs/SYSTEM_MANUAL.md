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
| 前端 | Electron | 桌面应用（精灵窗口 + 聊天窗口） |
| 后端 | Python FastAPI | API 服务 + Agent 核心 |
| 启动器 | Rust (Iced splash) | 进程管理 + 启动画面 + 自动更新 |
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
| `ha-server` | 智能家居（Home Assistant 设备控制/场景/自动化） | Yes（可选） |

> `kg-server`、`vector-store`、`embedding-service` 已移除，由 `lightrag-server` 统一替代。`mcp-servers/embedding-service/` 目录仍残留但不再加载。
> `nanobot.system` 为内置系统工具（code_run/read/edit/write），非 MCP 服务器模块，通过 disk 配置管理。
> `ha-server` 为可选服务器，需配置 Home Assistant 长期访问令牌后才会启用（`optional: true`）。
> `feishu-server` 已迁移至 `im-adapters/feishu/` IM Gateway 架构，不再是 MCP 服务器，`mcp-servers/feishu-server/` 为孤儿目录。

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
- `preference` — 用户偏好
- `brainregion` — 脑区实体

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
├── im-adapters/       # IM Gateway 适配器（飞书等）
├── ui/main/           # Electron 前端（合并 assistant/settings/graph 三套）
├── config/             # 配置文件
├── models/             # 模型文件
├── memory/            # 初始模板目录（memory.json/preferences.json/skills，首次运行复制到 ~/.niu/）
├── scripts/            # 运维脚本
├── data/               # 运行时数据（SQLite）
└── docs/               # 文档
```

**初始模板目录 `memory/`：**

`memory/` 是项目的初始模板目录，包含首次运行所需的必要配置文件和 Skills：

| 文件/目录 | 用途 | 复制目标 |
|----------|------|---------|
| `memory.json` | 用户记忆模板（身份、工作目录等初始配置） | `~/.niu/memory.json` |
| `preferences.json` | 存储配置模板（分类、路径结构、冲突阈值等） | `~/.niu/preferences.json` |
| `skills/*.md` | 初始 Skills 模板（脑区管理、浏览器自动化等） | `~/.niu/skills/` |

**自动复制机制**：
- 首次运行或运行目录（`~/.niu/`）中缺少这些文件时，启动器自动把 `memory/` 里的文件复制到 `~/.niu/`
- 复制逻辑在 `launcher/src/main.rs` 的 `initNiuDir()` 函数
- **不覆盖已存在文件**：用户已修改的配置不会被模板覆盖
- 如果 `memory/` 目录在 exeDir 和 cwd 都找不到，模板复制会跳过（开发环境容错）

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

Skills 是存储在 `~/.niu/skills/` 目录下的 Markdown 文件（`memory/skills/` 是仓库内开发副本），定义了特定任务的执行规范和模板。

**核心流程：**
1. Skills 文件通过 `agent/injector/sync.py` 定时同步到 LightRAG 向量库（entity_type = `Skill`）
2. Agent 每轮对话时，通过 `_inject_dynamic_resources()` 按语义搜索匹配相关 Skill
3. 匹配到的 Skill 内容动态注入到 Agent 上下文，指导 Agent 按规范执行任务

**Skill 编写职责：**
- **dream-evolver** 是 skill 生命周期的管理者——负责创建草稿、转正、降级、复活、淘汰全流程：
  - **创建**：观察到信号（重复模式、失败后解决、skill 反馈等）自动创建草稿 skill (status: draft)
  - **转正**：草稿 skill 使用反馈成功 → 转 active
  - **降级**：active skill 反复失败（issue_count ≥ 3）→ 降级为 deprecated（待观察）
  - **复活**：deprecated skill 反馈成功 → 转回 active
  - **淘汰**：deprecated skill 仍失败 → 移动到 `~/.niu/skills/.trash/` 归档
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

**deprecated（待观察）skill：**
- description 加 `[待观察]` 前缀
- runner.py 注入时显示 "⚠️ 待观察skill — 此skill有历史问题，使用后必须反馈效果（成功或失败）"
- 主 Agent 使用 deprecated skill 后必须明确反馈，dream-evolver 据此决定复活或淘汰

**Skill Frontmatter 规范：**

```yaml
---
name: skill-name-with-hyphens
description: Use when [触发条件，不写工作流]
status: draft | active | deprecated
created: YYYY-MM-DD
last_tested: YYYY-MM-DD
issue_count: 0
---
```

字段说明：
- `name`：只含字母、数字、连字符
- `description`：以 "Use when..." 开头，只写触发条件，不写工作流，500 字符以内
- `status`：新建时 `draft`，验证通过后 `active`，反复失败后降级为 `deprecated`（待观察）
- `created`：创建日期
- `last_tested`：最近一次验证或修改日期
- `issue_count`：失败计数，active 状态下累计 ≥3 次降级为 deprecated

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
| `ha-device-control.md` | 智能家居设备控制规范 | active |
| `ha-scene-automation.md` | 智能家居场景与自动化规范 | active |

**report-skill 触发条件：** 当 Agent 编写或整理用户日志、生成周报/月报等报告时，向量检索会自动匹配并注入 `report-skill.md`，Agent 按其中定义的聚合规则和模板生成报告。

### 2.7 日志配置

**配置文件**：`~/.niu/config/user-config.json`（首次启动从 bundle 内 `config/user-config.json` 模板复制）

**配置字段**：

````json
{
  "logging": {
    "enabled": false,
    "level": "INFO"
  }
}
````

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 日志总开关。`false` 时所有日志输出关闭（见下表），`true` 时按 `level` 输出 |
| `level` | string | `"INFO"` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

**关闭日志时（`enabled=false`，缺省）受控的日志源**：

| 日志源 | 文件路径 | 控制方式 |
|--------|----------|----------|
| Python loguru sink | stderr | `logger.disable("")` 全局禁用 |
| Python stdlib logging | stderr | `logging.disable(CRITICAL)` 禁用 10+ 处散落 logger |
| uvicorn 访问日志 | stderr | `log_level="critical"` + `access_log=False` |
| raw_http transport 层日志 | `~/.niu/logs/raw_http/YYYYMMDD/NNNNNN.json` | `install_http_logger()` 不 patch HTTP client + 幂等守卫 |
| raw_http 应用层日志 | `~/.niu/logs/raw_http/YYYYMMDD/NNNNNN_request.json` + `_response.json` | `_write_raw_log()` 静默跳过 |
| LLM interaction 可读日志 | `~/.niu/logs/llm_interaction_YYYYMMDD.log` | `_write_interaction_log()` 静默跳过 |
| 飞书 adapter stderr | `~/.niu/logs/im_adapter_stderr.log` | `subprocess.DEVNULL` 代替文件重定向 |
| /http-log/ HTTP 日志查看服务 | http://localhost:9876/http-log/ | router 不挂载（返回 404） |
| Rust tracing | stderr | `tracing_subscriber` 不 init（tracing 调用静默丢弃） |

**不受日志开关控制的诊断日志**（关键诊断必须保留）：

| 日志源 | 文件路径 | 用途 |
|--------|----------|------|
| launcher 致命错误 | `~/.niu/logs/launcher_error.log` | 启动失败诊断（API 未运行、Electron 启动失败等），用 `time` crate 格式化时间戳 |
| gateway 致命错误 | `~/.niu/logs/gateway_error.log` | 飞书 adapter 启动失败诊断（app_id 配错、端口占用、credentials 缺失） |

**开启日志**（调试时用）：

把 `~/.niu/config/user-config.json` 的 `logging.enabled` 改为 `true`，重启程序。所有受控日志源会按 `level` 字段输出到对应文件或 stderr。

**日志目录**：

所有运行时日志统一写入 `~/.niu/logs/`（macOS/Linux）或 `%USERPROFILE%\.niu\logs\`（Windows），不在 bundle 内（macOS Gatekeeper 禁止运行时改 bundle 内文件）。

**Windows 控制台窗口**：

Windows release build 下，niu.exe 编译为 GUI 子系统（`#![cfg_attr(all(target_os="windows", not(debug_assertions)), windows_subsystem="windows")]`），双击不弹 cmd 窗口。debug build 保留 console 方便调试。

**macOS 控制台窗口**：

macOS 下构造 `niu.app` bundle（`Info.plist` 含 `LSUIElement=true`），Finder 双击不弹 Terminal。命令行 `./niu` 裸二进制仍保留供开发调试。

---

## 通用子 Agent 体系（阶段三）

### 设计目标

- 减少主 Agent 上下文占用（大段工作丢给子 Agent）
- 支持长时任务（异步调用不阻塞主 Agent）
- 支持专业性任务（用户提供专业提示词或文档）

### 模板位置

`config/agent-template.md`——子 Agent 配置模板，含所有可用 MCP 服务器清单和 frontmatter 字段说明。模板本身不被加载，仅供主 Agent 参考编写。

### 配置目录

- `config/agents/`——专用子 Agent（项目内置，启动加载），如 `file-processor.md`、`niu.md`
- `~/.niu/agents/`——通用子 Agent（主 Agent 运行时创建，动态加载）

同名时专用子 Agent 优先（`config/agents/` 先查）。

### 动态加载机制

程序在 `chat()` 入口（每次对话开始时）扫描 `~/.niu/agents/`，与 `NiuRunner._known_user_subagents` 集合对比，发现新 MD 文件就重算 `base_tools_schema`，新子 Agent 的 `chat-with-{name}` 工具自动出现。

- 不用 watchdog / 定时器，复用现有动态组装机制
- 主 Agent 写完 MD 后下一轮对话开始时工具才出现（自然时序）
- YAML 解析失败的 MD 被跳过（不允许坏工具让主 Agent 看到）
- 文件名必须 kebab-case（小写字母/数字/连字符），否则跳过

### MCP 工具映射

子 Agent 的 MCP 工具由 frontmatter `mcpServers` 字段指定（如 `mcpServers: [photo-server, lightrag-server]`）。加载时从已加载的全局 ToolRegistry 过滤，无需额外加载逻辑。如果 `mcpServers` 含未加载的服务器，对应工具缺失但不阻塞（log warning）。

### 主 Agent 创建子 Agent 流程

1. 主 Agent 读 `config/agent-template.md`
2. 主 Agent 用基础工具（读写文档）写新 MD 到 `~/.niu/agents/{name}.md`
3. 主 Agent 当前任务结束
4. 下一轮 `chat()` 入口扫描发现新 MD → 重算 schema → `chat-with-{name}` 工具出现
5. 主 Agent 调用 `chat-with-{name}`（同步或异步）

### 同步 vs 异步调用

- **同步**：主 Agent 阻塞等子 Agent 跑完拿结果。适合短时任务。
- **异步**（`allowAsync: true` + `async_mode: true`）：立即返回"已开始异步工作"，子 Agent 后台跑。适合长时任务。异步子 Agent 完成后自动 push 完成汇报，触发主 Agent 新一轮 LLM 处理（拿结果判断下一步）。

### 与阶段一+二的衔接

- 阶段一：主子 Agent 通信通道（@消息路由、/stop 终止、双击停止）
- 阶段二：异步调用 + ask_main_agent 内存队列 + check_subagent_progress + 5 死锁约束
- 阶段三：通用子 Agent 动态创建 + 加载 + @前缀 content 拦截层（@niu-agent 询问 / @end 结束）

通用子 Agent 完整复用阶段一+二的全部交互能力。

### 同步子 Agent @niu-agent 交互通道

同步子 Agent 调用时，主 Agent 在工具循环里阻塞等待。子 Agent 输出 `@niu-agent 问题` 时，程序拦截层识别后挂起 session，把问题包装成 `[子名] 问题` 作为工具返回值送给主 Agent。主 Agent LLM 看到 JSON 工具结果 `{"status":"success","result":"[子名] 问题"}` 后，调同一 chat-with-xxx 工具回复（task="" + answer="@子名 回答" + unique_name="子名"）。程序从 registry 拿回挂起 session，注入回答后继续跑。

程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy API）时，由 `call_subagent_with_auto_answer` helper 自动回复固定文案"无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"。

### 维护注意事项

- MCP 服务器清单变化时（新增/移除 MCP 服务器），同步更新 `config/agent-template.md` 的"可用 MCP 服务器"段
- `mcp_loader.REQUIRED_SERVERS` 改动会影响子 Agent 可用工具，需检查现有通用子 Agent 的 `mcpServers` 字段是否仍有效
- 用户清理 `~/.niu/agents/` 时，下一轮 `chat()` 入口扫描会自动移除对应工具

详细分册见 [manual-general-subagent.md](manual-general-subagent.md)。

## 分册索引

| 分册 | 文件 | 内容 |
|------|------|------|
| 知识检索运维 | [manual-vector-store.md](manual-vector-store.md) | LightRAG 知识图谱架构、实体类型、检索模式、文档管理、**3 真相源 + 9 派生文件关系、损坏检测与自愈修复机制（第九章）** |
| 故障排查 | [manual-troubleshooting.md](manual-troubleshooting.md) | 启动问题、人脸识别、定时任务、知识检索、数据、浏览器插件、**知识图谱损坏修复故障排查（1.7.1）** |
| 性能优化 | [manual-performance.md](manual-performance.md) | 内存优化、启动速度、GPU 加速策略 |
| 依赖与模型 | [manual-dependencies.md](manual-dependencies.md) | Python 依赖、GPU 支持策略、人脸识别模型、向量模型、下载镜像 |
| 用户操作 | [manual-user-guide.md](manual-user-guide.md) | 首次启动、LLM 配置、知识图谱、记忆管理、常见问题 |
| 开发者参考 | [manual-developer.md](manual-developer.md) | 本地开发、调试技巧、API 端点、环境变量、更新日志 |
| 文件格式支持 | [manual-file-formats.md](manual-file-formats.md) | 文件存储/知识图谱/照片支持的格式，不支持KG的格式及原因 |
| 飞书开通 | [manual-feishu-setup.md](manual-feishu-setup.md) | 飞书机器人开通流程、浏览器操作步骤、配置写入、故障排查 |
| 高德开通 | [manual-amap-setup.md](manual-amap-setup.md) | 高德地图 API Key 获取流程、配置写入、故障排查，用于照片exif位置解析 |
| 智能家居开通 | [manual-ha-setup.md](manual-ha-setup.md) | Home Assistant 安装部署、长期访问令牌、设备集成、智能触发配置、故障排查 |
| MCP与虚拟磁盘 | [manual-mcp-disk.md](manual-mcp-disk.md) | MCP 服务器同进程架构、新增服务器步骤、虚拟磁盘 YAML 配置格式、校验规则 |
| IM Gateway 接入 | [manual-im-gateway.md](manual-im-gateway.md) | Gateway+Adapter 分离架构、TCP 协议、配置格式、目录规范、开发新 Adapter 步骤 |
| 通用子 Agent | [manual-general-subagent.md](manual-general-subagent.md) | 阶段三通用子 Agent 体系：模板、动态加载、MCP 映射、创建流程、同步异步、与阶段一+二衔接 |
