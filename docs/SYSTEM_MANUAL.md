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
*21|| 定时任务 | 两类：reminder（到点提醒主 Agent）+ background_script（后台静默执行 Python 脚本，无输出静默、有输出/报错才通知）。支持循环/单次，cron 5 字段触发。推送通道（2026-08-15 起）：定时提醒程序消息只写 DB 唤醒主 Agent（不推 IM，Chat 由 DB 变更刷新）；定时提醒置 IM 标志为真，主 Agent 的话（如"该打开咖啡机了"）经 should_push_im 闸门投递 IM。智能家居订阅触发同通道 |
| 智能记忆 | 自动学习用户偏好和习惯，按脑区优先级差异化遗忘曲线 |
| 浏览器辅助 | Chrome Extension，AI 操作网页 |
| /stop 指令 | 停止当前 Agent 工作，支持 Electron 和 IM 通用 |
| 停止按钮 | 单击：主 Agent 立即返回（≤0.2s）——无论卡在 LLM 通讯、动态注入向量检索还是工具执行，统一可中断执行层放弃当前阻塞等待，后台任务继续跑（结果丢弃）；同步子 Agent 终止，异步子 Agent 不受影响。双击：向用户对话派生的所有子 Agent 推 /stop 并立即返回。程序触发（睡眠整理/定时任务）的子 Agent 不受停止影响 |
| 主 Agent ask_user 工具 | 主 Agent 想与用户交流时通过 ask_user 暂停问话（阻塞等待回答），工作流不中断——显式暂停工具，区别于"停下来问话"退出工具循环 |
| /clear 指令 | 即时清空对话（取消清空前提炼）；忙时先停止 Agent 并唤醒在途睡眠整理（阶段边界自行退出）。支持 Electron 和 IM 通用 |
| /compact 指令 | 手动触发批量压实：与自动触发共用同一纯机械压实函数（秒级、零 LLM、DB 不动），完成后推送新 usage 给前端圆环。仅 Electron |
| /sleep 指令 | 让精灵进入睡眠状态，自动触发 sleep 模式整理（entity-extractor → dream-evolver 多轮循环 → 块摘要可选层；journal 已移出为每日定时任务）。仅 Electron |
| 见缝插针 | Agent 运行期间发送的补充消息自动插入到当前对话上下文（补充在前，当前任务在后） |
| 子 Agent 标签页 | 子 Agent 运行时自动创建独立标签页，实时展示回复/工具状态/思维链/提问；子 Agent 可通过 @user 向用户提问并阻塞等待回答 |
| 上下文使用率圆环 | 主对话显示主 Agent 的真实上下文占用；切换到子 Agent 标签页时显示该子 Agent 的真实占用（每轮 LLM 实际 prompt_tokens / 上下文窗口），切回主对话自动恢复 |
| 异步子 Agent 结果推送 | 续答是否推送 IM 只看当前对话的 IM 标志：IM 用户消息带 chat_id、定时任务强制置位；本地对话无标志不发。子 Agent 返回不改标志 |

**指令机制**：
- `/stop`：通过正常消息通道发送（非独立 API），在 `chat_session` 和 `ChatQueue` 入口拦截并设置全局停止标志。Agent 主循环、handler dispatch 在关键点检查标志并退出。前端停止按钮自动发送 `/stop` 文本。
- `/clear`：即时清除——① `request_stop()` 停主 Agent；② 无条件唤醒睡眠整理管道（`set_spirit_state("idle")`，在途 sleep 管道于阶段边界自行退出）；③ 无限心跳排队拿 `_chat_lock` 后直接 `clear_messages()` 清空会话 + `cleanup_all_tmp()` + 复位全部游标 + 截断 F1/F2/F3 中继文件 + 删除指针块库 + 校准倍率复位（journal.md 本体保留）。支持 Electron 和 IM 通用
- `/compact`：调用 `POST /api/context/tidy {mode:'compact'}`，直达批量压实实现（与自动触发共用 compaction.compact_now_detailed）：纯机械秒级、零 LLM、DB 不动、不经整理队列直接执行；完成后推送新 usage 给前端圆环。阻塞式 UI（系统提示 + 禁用输入 + compact_status 圆环动画），忙时先发 `/stop`。与 `/clear` 的区别：`/clear` 即时清空会话；`/compact` 只压实视图不清空会话，历史全部可经 read_history_block 取回。前端直接 await 执行结果，秒级收敛（压实零 LLM）。**注意**：忙时 `/stop` 仅设置停止标志立即返回，不等 Agent 释放 `_chat_lock`；后端拿锁为无限心跳重试（永不超时放弃），Agent 停止慢时 `/compact` 排队等待而非失败。
- `/sleep`：通过 IPC `enter-sleep` 通知精灵 `setState(SLEEP)`，后者自动触发 `triggerTidy()` → `POST /api/context/tidy {mode:'sleep'}`（entity-extractor → dream-evolver 多轮循环 → 块摘要可选层；journal 已移出为每日 18 点定时任务——见「上下文管理」章节）。**与空闲自动睡眠完全同路径**：同走全局整理队列（投递后立即返回 `{"status":"queued"}`），worker 串行执行；精灵播放睡眠动画，用户发消息时自动唤醒（`onUserActivity` SLEEP→IDLE）。**睡眠状态机检查（仅 sleep）**：排队唤醒时非睡眠 → `cancelled/woke_up`；entity/dream 每步完成后检查，被唤醒 → `interrupted/woke_up`，已推进不回滚下次续跑。**忙碌守卫**：Agent 运行时（精灵 BUSY）忽略 `/sleep`——chat.html 检查 `isProcessing` 提示用户，spirit.html `onEnterSleep` 检查 `currentState === State.BUSY || busyCount > 0` 兜底忽略（`busyCount` 覆盖 ALERT 期间 `onBusyState` 只计数不切态的场景，是忙碌的权威判据），防止 Agent 完成后 `chat_idle` 把精灵从 SLEEP 强制唤醒回 IDLE 的状态冲突。**已知边缘**：非 chat 来源忙碌（如拖文件到精灵窗口入库中，`busyCount>0` 但 chat 的 `isProcessing=false`）时，`/sleep` 会显示「💤 精灵已进入睡眠」提示但精灵兜底忽略——fire-and-forget IPC 模式（同 `notify-busy`）的固有权衡，无状态损坏，入库完成后再发一次即可。
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

**脑区加权检索（全局向量检索 + 图遍历 + 衰减池）：**
- **全局向量检索**：search_multi_lightrag，top_k=10，返回语义最相关的技能和知识
- **图遍历 1 跳**：从向量命中实体出发，遍历 1 跳邻居实体补充注入
- **Ebbinghaus 衰减池**：DecayPool 管理注入实体生命周期，按 Ebbinghaus 遗忘曲线衰减激活度
- 向量检索与图遍历结果用 seen_names 去重，保证不重复注入
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

**已定义的子 Agent（5个）：**

| 子 Agent | 职责 | 触发方式 | 温度 |
|----------|------|----------|------|
| `file-processor` | 文件处理：复制、解析、存储、向量化 | 主 Agent 委托（文件拖入） | 0.2 |
| `event-manager` | 事件管理：创建/查询/删除事件 | 主 Agent 委托 | 0.2 |
| `journal-agent` | 工作日志：自读程序导出的增量对话文件提取工作内容写入日志 | 主 Agent 委托或 journal_daily 定时任务直执行 | 0.3 |
| `entity-extractor` | 内容提炼：从对话筛选有价值内容入库 | 睡眠管线自动调度 | 0.3 |
| `dream-evolver` | 梦境进化：精加工知识图谱 + skill 编写与优化 | 睡眠管线自动调度 | 0.3 |

> `context-manager` 子 Agent 已随压缩体系退役（2026-08-26）；上下文管理由确定性组装器接管，见「上下文管理」章节。

**屏蔽机制：**

`entity-extractor`、`dream-evolver` 两个子 Agent 在 `agent/handler.py` 中被列入 blocked 集合，禁止主 Agent 手动调用：
- `entity-extractor`：由睡眠管线触发
- `dream-evolver`：由睡眠管线触发，在 entity-extractor 之后串行执行

这确保：
- 避免主 Agent 误触发导致重复执行
- 保证执行顺序和时机符合系统设计
- 防止用户对话被不必要的后台任务打断

子 Agent 运行时通过独立事件总线（SubagentEventBus）和专属 SSE 端点向前端标签页推送实时事件（reply/tool_status/thinking_chain/question），子 Agent 可通过 @user 前缀向用户提问并阻塞等待回答。

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

程序触发子 Agent（睡眠管线 / journal_daily 定时任务）时，由 `call_subagent_with_auto_answer` helper 自动回复固定文案“无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作”。

### 上下文管理（上下文组装器，2026-08-26 起）

> 压缩体系（keep/update/delete、模式一二三、context-manager 子 Agent、保护 N 轮、compress 游标）已整体退役。取而代之的是**存储/视图分离**的确定性组装器：messages.db 是真相源永不动，LLM 每轮看到的只是组装视图；主链路零 LLM 承重。

#### 组装视图：分区预算

每次对话开始，`get_context_for_chat` 从 DB 全量读消息并组装视图：

1. **会话单元切割**：消息流按 user 轮 + tool_calls 配对切成完整会话单元
2. **原文窗口装填**：从最新单元向前累加，装不下即止（预算 = `contextWindowSize` × 50%）；窗口起点恒为单元边界，tool 配对完整
3. **指针块归档**：被挤出窗口的完整单元机械写入指针块存储（幂等，同区间已有块则跳过）
4. **输出视图** = [历史索引前导 user 消息] + [窗口内原文消息]；无归档块时省略索引消息

| 分区 | 预算 | 说明 |
|------|------|------|
| 原文窗口 | ≤50% 窗口 | 最近若干完整会话单元逐字原文 |
| 历史索引 | ≤30% 窗口 | 每块一行时间线 FIFO 机械行：`[块#N] 时间~时间 · X条 · 实体:a/b/c · 首问:"…"`；索引超预算时最老相邻块合并为一行 |

**指针块存储**：SQLite 单表 `~/.niu/context_blocks.db`（flock 排它锁），记录每块的 msg_id/rowid 区间、条数、时间范围、实体标签（≤3 个）、首问摘录（≤40 字）。块是派生数据，可从 messages.db 全量重建；启动时挂 lifespan 一致性校验（msg_id 存在性/rowid 单调/count 一致），不一致自动整库重切重建。

#### token 校准倍率

本地 TokenCalculator 估算与服务端真值存在中英文比例漂移，程序维护校准倍率桥接：每次主 Agent 响应后用 `usage.prompt_tokens` 真值 ÷ 同消息集本地估算覆盖更新倍率（仅主 Agent，子 Agent 副模型不混入）；倍率持久化在 `~/.niu/token_calibration.json`，默认 1.15，越界（0.2~10 之外）自动回退。80%/95% 触发判定均基于**校准后估算**。

#### 批量压实：纯机械、零 LLM、秒级

- **触发**：校准后总量估算 ≥80%（组装出口与 runner 真值回调共用滞回闸门 AUTO_GATE——≥80% 触发闩锁、<78% 复位，同轮双触发去重不双压）；**95% 应急线**：保留轮工具输出全部占位符化+仅留最近 1 轮
- **动作**：保留最近 N 个会话单元（`context.keepRecentTurns`，默认 3）→ 其余单元全量转指针块 → 索引行合并（超 30% 预算合并最老相邻块）→ D15 三轮硬约束（压实后校准总量仍超 80% 则先占位符化保留轮内旧工具输出，仍超减轮 3→2→1）
- **无损性**：messages.db 真相源一字不动；任何历史内容可随时经 read_history_block 取回或从 DB 全量重建

#### 历史取回：read_history_block 工具

模型看到索引中的 `[块#N]` 句柄后，调用 `read_history_block(block_id=N)` 即可取回该块的**逐字原文**（时间+角色+内容，tool 输出含 tool_call_id 归属；超大块头尾保留+精简标注）。该工具挂在 session-manager（hidden，仅模型侧使用）；解码说明书写在主 Agent 提示词（config/agents/niu.md）。索引区职责边界=模拟全量上下文的目录页，不做语义检索——图谱兜底深挖走知识图谱工具。

#### 睡眠管道新序

sleep 由闲置 5 分钟触发，投递全局整理队列单 worker 串行执行；执行期按 CP 检查点检查睡眠状态，被唤醒即取消后续步骤（已推进不回滚，下次续跑）：

| 步骤 | 组件 | 说明 |
|------|------|------|
| 1 | entity-extractor | 自读 F1 提炼源文件，`lightrag_insert` 入库，报 processed_line=N 后 relay 剪切 |
| 2 | dream-evolver | 多轮循环：F2→F3 工作集精加工，covered_all 终止，成功删 F2 前缀 |
| 3 | 块摘要增强（可选层） | 对摘要状态 pending 的归档块裸调 lightrag_llm（副模型一次一 call）生成 ≤100 字摘要行替代机械行；失败保 pending 下次重试；活跃对话期自动跳过本轮；默认关闭（`context.blockSummaryEnabled`） |

journal 已移出睡眠管道（见下节）。entity → dream 的顺序依赖保持不变：先入库再精加工，防实体碎片化。文件驱动梦境链（F1/F2/F3 三文件中继，位于 `~/.niu/md/`）机制不变：F1 为 DB 镜像只增不减、F2 无限队列、F3 按 ≤64KB 软预算切分重建；组装器的指针块归档同样只动派生数据不触三文件。

#### journal 定时任务（每日 18 点直执行）

journal-agent 不再进睡眠管道，改为 scheduler 内置定时任务 `journal-daily`（cron `0 18 * * *`）：后台线程**直执行**——从 DB 导出增量消息为临时工作集文件（`~/.niu/md/journal_workset.md`），调 journal-agent 自读提取写入 journal.md，游标自管（`~/.niu/last_journal.json`）。**严禁经 ChatQueue enqueue**——日志内容写进 messages.db 会反污染上下文窗口。避让纪律：活跃对话期复用 scheduler backend-busy 轮询等待（二次确认防抖、超时兜底放行）；运行中重复触发去重跳过；执行失败游标不推进，下轮自动重覆盖同一增量区间。可通过 `context.journalScheduledEnabled=false` 关闭（默认开启）。

#### /compact 新语义

手动 /compact 与自动触发共用同一个批量压实函数：纯机械秒级、零 LLM、无 ChatQueue pause 门禁、不经整理队列直接执行；完成后推送新 usage 给前端圆环。（旧语义「force 全量 keep/update/delete 整理」随压缩体系退役。）

#### /clear 与 /new 清理面

两者同端点（即时清除语义，无清空前提炼）：清空 messages.db → 截断 F1/F2/F3 中继文件 → 复位全部游标 → 删除指针块库 → 校准倍率复位默认值 → 作废内存派生缓存。**journal.md 本体保留**（§8 拍板：日记是长期资产，不随会话清空）。

### 维护注意事项

- MCP 服务器清单变化时（新增/移除 MCP 服务器），同步更新 `config/agent-template.md` 的"可用 MCP 服务器"段
- `mcp_loader.REQUIRED_SERVERS` 改动会影响子 Agent 可用工具，需检查现有通用子 Agent 的 `mcpServers` 字段是否仍有效
- 用户清理 `~/.niu/agents/` 时，下一轮 `chat()` 入口扫描会自动移除对应工具

详细分册见 [manual-general-subagent.md](manual-general-subagent.md)。

## 可选组件安装

出于许可证合规，安装包默认不含以下两个组件。不装也不影响 Niu 主体功能，只是对应子功能不工作。用户按需手动安装。启动器启动时会检测缺失依赖并在 splash 窗口提示用户读 README。

### 脑区社区检测（igraph + leidenalg）

脑区的**社区检测**子功能（自动发现知识图谱中的社区结构、把实体聚类成脑区）依赖 `igraph` + `leidenalg` 两个库。这两个库是 GNU GPL 许可证，**默认不含在安装包里**——不装也能正常使用 Niu 所有其他功能（包括脑区激活/调暗/状态管理），只是脑区社区检测不工作（`region_detector.py` 的 `try/except ImportError` 会优雅降级，不报错）。

如果需要脑区社区检测，用**程序自带的 Python**（不是系统 Python）手动安装：

```bash
# macOS（路径以 /Applications/niu.app 为例）
/Applications/niu.app/Contents/Resources/python/bin/python3 -m pip install igraph==1.0.0 leidenalg==0.11.0

# Windows（路径以解压目录为例，如 D:\Niu）
.\python\Scripts\pip.exe install igraph==1.0.0 leidenalg==0.11.0
```

> ⚠️ **必须用程序自带的 Python**，不能用系统 `pip install`——Niu 运行时用的是自包含环境（macOS: `niu.app/Contents/Resources/python/`，Windows: 解压目录下的 `python/`），装到系统 Python 里 Niu 看不到。

> 📋 许可证说明：`igraph` 和 `leidenalg` 都是 GNU GPL 许可证。用户自行安装=用户与 GPL 许可方建立许可关系，Niu 本身（MIT 许可证）不分发这两个包，不构成 GPL 传染。`leidenalg` 依赖 `igraph`，pip 会自动安装。

> ⚠️ **macOS 装完必须重签名**：向 `niu.app` 内部装包会写入新的 `.so`，这些新文件没有签名，不重签会在加载时被 macOS 拒绝（dlopen 失败）。**Windows 无需此步骤。** 执行（ad-hoc 签名，inside-out 逐个签 `.so`/`.dylib` 再签 bundle 顶层——`codesign --deep` 自 macOS 13.3 起已废弃，不会签新增的 `.so`）：
>
> ```bash
> find /Applications/niu.app/Contents/Resources/python -type f \
>     \( -name "*.so" -o -name "*.dylib" \) -print0 \
>     | xargs -0 -n 1 -P 4 codesign --force --sign -
> codesign --force --sign - /Applications/niu.app
> ```

安装后重启 Niu，脑区社区检测会自动启用（`region_detector.py` 的 `try/except ImportError` 会检测到这两个包可用）。

### 照片处理（人脸识别 + HEIC 支持）

照片处理功能（拖入照片入库、人脸识别、人物管理）依赖 `opencv-python-headless` + `insightface` + `easydict` + `pillow-heif` 四个包。其中 `opencv-python-headless` 捆绑的 FFmpeg 含 GPL 编解码器（libx264/libx265），`pillow-heif` 链接 libx265（GPLv2），出于许可证合规**默认不含在安装包里**——不装也能正常使用 Niu 所有其他功能，只是照片处理不可用。

**macOS**：分三步（装依赖 + 下模型 + 重签名）。**Windows**：分两步（装依赖 + 下模型，无需重签名）。

**第一步：装依赖**

```bash
# macOS（路径以 /Applications/niu.app 为例）
/Applications/niu.app/Contents/Resources/python/bin/python3 -m pip install \
    opencv-python-headless==4.11.0.86 \
    insightface==0.7.3 \
    easydict==1.13 \
    pillow-heif==1.4.0

# Windows（路径以解压目录为例，如 D:\Niu）
.\python\Scripts\pip.exe install \
    opencv-python-headless==4.11.0.86 \
    insightface==0.7.3 \
    easydict==1.13 \
    pillow-heif==1.4.0
```

> ⚠️ **必须用程序自带的 Python**，不能用系统 `pip install`——Niu 运行时用的是自包含环境（macOS: `niu.app/Contents/Resources/python/`，Windows: 解压目录下的 `python/`），装到系统 Python 里 Niu 看不到。

> 📋 许可证说明：`opencv-python-headless` 捆绑 GPL 版 FFmpeg，`pillow-heif` 链接 libx265（GPLv2）。用户自行安装=用户与 GPL 许可方建立许可关系，Niu 本身（MIT 许可证）不分发这些包，不构成 GPL 传染。`insightface` 和 `easydict` 是人脸识别库依赖，一并安装。

**第二步：下载 buffalo_l 模型**

详见下面「人脸识别模型（buffalo_l）」子节。

**第三步（仅 macOS）：重签名**

详见下面「重签名」子节。Windows 无需重签名。

安装后重启 Niu，照片处理功能会自动启用（`__init__.py` 的 `try/except ImportError` 会检测到包可用）。

#### 人脸识别模型（buffalo_l）

照片处理的人脸识别功能依赖 InsightFace 的 `buffalo_l` 模型（~326MB）。出于非商业许可证限制，**模型文件默认不含在安装包里**。

Niu **不会自动下载**模型（避免下载卡死用户以为程序坏了），本地没有模型时人脸识别直接报错，需手动下载安装：

1. 从 InsightFace 官方下载 `buffalo_l.zip`：
   - 地址：https://github.com/deepinsight/insightface/releases/tag/v0.7.3
2. 解压后把 5 个 `.onnx` 文件放到：
   - **macOS**：`/Applications/niu.app/Contents/Resources/models/models/buffalo_l/`
   - **Windows**：`<解压目录>/models/models/buffalo_l/`
   - 5 个文件：`1k3d68.onnx` / `2d106det.onnx` / `det_10g.onnx` / `genderage.onnx` / `w600k_r50.onnx`
   - 文件直接放在该目录下，不要多套一层子目录

> 📋 许可证说明：InsightFace buffalo_l 模型是非商业许可证。用户自行下载=用户与 InsightFace 许可方建立许可关系，Niu 本身不分发这个模型，不承担非商业许可的责任。仅限非商业用途。

> 💡 模型加载后占用 ~326MB 内存，空闲 5 分钟自动卸载（`MODEL_IDLE_TIMEOUT_SECONDS = 300`）。

#### 重签名

向 `niu.app` 内部装包/放模型会写入新的 `.so`/`.onnx`，这些新文件没有签名，不重签会在加载时被 macOS 拒绝（dlopen 失败）。执行（ad-hoc 签名，inside-out 逐个签 `.so`/`.dylib` 再签 bundle 顶层——`codesign --deep` 自 macOS 13.3 起已废弃，不会签新增的 `.so`）：

```bash
# 1. 逐个签 site-packages 里的 .so/.dylib（并行 4 进程）
find /Applications/niu.app/Contents/Resources/python -type f \
    \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 -n 1 -P 4 codesign --force --sign -

# 2. 签 bundle 顶层（不 --deep）
codesign --force --sign - /Applications/niu.app
```

#### 人脸识别不工作怎么排查

人脸识别不工作（拖入照片不响应或报错）时，Agent 应：

1. **判断是否依赖缺失**：检查 site-packages 下是否有 `cv2` / `insightface` / `easydict` / `pillow_heif` 目录（任一缺失=依赖没装或装错位置，见上面「第一步」）。路径：
   - **macOS**：`niu.app/Contents/Resources/python/lib/python3.11/site-packages/`
   - **Windows**：`<解压目录>/python/Lib/site-packages/`
2. **判断是否模型缺失**：检查 `models/models/buffalo_l/` 目录是否含 5 个 `.onnx` 文件。目录不存在或文件不全=模型没装（见上面「人脸识别模型」子节）。路径：
   - **macOS**：`niu.app/Contents/Resources/models/models/buffalo_l/`
   - **Windows**：`<解压目录>/models/models/buffalo_l/`
3. **判断是否没重签名（仅 macOS）**：若依赖和模型都在但加载报 `dlopen`/`code object is not signed` 错误，是装完没重签（见上面「重签名」子节）。Windows 无此问题。启动器启动时会检测缺失依赖并提示，但不会检测签名状态，需用户手动重签。
4. **重启 Niu**：放好后重启，下次用人脸识别会直接从本地加载，不再下载。

## 字体配置

### 配置位置

字体配置在 `~/.niu/preferences.json` 的 `font` 段：

```json
{
  "font": {
    "name": "字体名（CSS font-family 名，自定义）",
    "file": "字体文件名（放在 ~/.niu/fonts/ 目录下，可选）"
  }
}
```

### 字体文件目录

用户自定义字体文件（.ttf/.otf）放在 `~/.niu/fonts/` 目录下。配置里 `file` 字段只填文件名，不填完整路径。

### 系统字体模式

只配 `name` 不配 `file` 时，直接引用系统已安装字体，不内联字体文件、不注入 `@font-face`，只覆盖 `font-family`。

适用于系统已有字体（如 macOS 的 `PingFang SC`、Windows 的 `Microsoft YaHei`），无需下载字体文件。

```json
{
  "font": {
    "name": "PingFang SC"
  }
}
```

### 不配置时的缺省字体

不配置 `font` 段时，不注入任何 `@font-face` 与 `font-family` 覆盖，窗口使用**浏览器系统默认字体**（即 CSS 未指定 `font-family` 时的兜底，通常是系统 sans-serif）。

### 配置示例

假设用户想用“方正楷体”：

1. 把 `FZKai-Z03.ttf` 放到 `~/.niu/fonts/`
2. 编辑 `~/.niu/preferences.json`：

```json
{
  "font": {
    "name": "FZKaiTi",
    "file": "FZKai-Z03.ttf"
  }
}
```

3. 重开对应窗口（或重启 Niu），字体生效

### 配置生效时机

字体配置在窗口创建时由 preload 脚本读取（同步），修改配置后**重开对应窗口**即可生效（不必整个应用重启）。例如改了 chat 字体配置，关掉聊天窗口再打开就生效。

### 容错

以下情况自动降级为系统默认字体（不注入 `@font-face`、不覆盖 `font-family`），不影响使用：
- `font` 段缺 `name` 字段
- `font` 段配了 `file` 但字体文件不存在（`~/.niu/fonts/` 下找不到）
- `preferences.json` JSON 格式损坏

## LLM 调用与知识图谱超时配置

LLM 流式读取超时（`read_timeout`，默认 300s）与 LightRAG 操作超时（`insert_timeout` 600s / `query_timeout` 120s / `delete_timeout` 300s / `status_timeout` 30s / `merge_timeout` 300s）均可通过配置文件调整：`read_timeout` 在 `config/user-config.json` 的 `llm` 与 `lightrag_llm` 段，LightRAG 操作超时在 `~/.niu/preferences.json` 的 `lightrag` 段。缺省值已显式写入两处配置示例，详见《用户操作手册》1.2 LLM 配置与 1.4 知识图谱章节。生效方式：主对话/子 Agent 的 `read_timeout` 修改后重启生效；知识图谱 LLM 调用与 LightRAG 操作超时每次操作实时读取配置，修改后即时生效。

## 分册索引

> 主 Agent 遇到具体问题时按此表判断去哪个子文档查。每条说明该文档解决什么问题、包含哪些功能、什么时候应该去看。

| 分册 | 文件 | 内容 |
|------|------|------|
| 安装部署 | [manual-installation.md](manual-installation.md) | 从零到能运行的 Niu 全流程。覆盖下载安装（DMG 直装）、可选组件（脑区 igraph/leidenalg、人脸 buffalo_l 模型——交叉引用主手册）、源码构建（venv --copies + requirements.txt）、macOS .app 打包（build.sh 8 步流程 + DMG 生成 + Info.plist）、跨架构打包（M 系列 Mac 完整步骤）、Rust 启动器编译（含交叉编译 4 目标）。README 的安装/打包信息已纳入本手册，用户问"怎么装""怎么打包""为什么某个组件不工作"时查这里 |
| 知识检索运维 | [manual-vector-store.md](manual-vector-store.md) | LightRAG 统一架构（取代旧 vector-store + kg-server）的完整运维手册。包含实体类型与 keywords 规范、5 种检索模式（local/global/hybrid/mix/naive）的选用、文档入库流程与参数调优、3 真相源 + 9 派生文件的存储关系图谱、GraphML 损坏检测与 v9 自愈修复机制（第九章）。v2 检测逻辑（2026-07-28）：派生缺失不是损坏，真损坏判定靠 vdb 与 GraphML 数据一致性。**v3（2026-08-14）**：新增 vdb 文件内部一致性检测（matrix/data 行数），不一致时启动自动修复——用户遇到知识图谱回答准确度下降/搜索匹配度降低时**先重启程序**（v3 启动自检自动修复，无需删文件）；重启后仍异常，再删 3 个 vdb 文件重启触发完整重建（9.9 节兜底路径）。遇到知识图谱查询异常、入库失败、存储文件损坏、检索效果差等问题先查这里 |
| 故障排查 | [manual-troubleshooting.md](manual-troubleshooting.md) | 所有功能模块的故障排查指引。覆盖启动问题、人脸识别（含 1.2 节人脸数据直查：误合并拆分、向量归属确认、SQLite 直查语句）、定时任务（reminder 不通知 + background_script 静默/报错/永久删除排查——通知形态说明：定时提醒写 DB Chat 显示 + 蹦高 + 主 Agent 的话推 IM，IM 没收到是主 Agent 的话没发出；含 task_kind/script_file 数据库直查）、知识检索、数据存储、浏览器插件、知识图谱损坏修复（1.7.1 专项，含"删 3 个 vdb 文件重启触发修复"简易指引）等场景的诊断步骤和恢复方法。出现报错、功能不工作、数据异常时先查这里找对应模块的排查路径 |
| 性能优化 | [manual-performance.md](manual-performance.md) | 系统性能调优手册。包含 InsightFace 内存优化（5 分钟空闲自动卸载）、启动速度优化策略、GPU 加速方案（CUDA / DirectML）。遇到内存占用过高、启动慢、人脸识别卡顿等性能问题时查这里 |
| 依赖与模型 | [manual-dependencies.md](manual-dependencies.md) | Python 依赖清单与模型文件管理。包含 agent / 各 MCP 服务器 / 开发依赖的完整列表（numpy<2 + opencv<4.12 隐性约束）、GPU 支持策略（CUDA / DirectML / CPU）、InsightFace buffalo_l 与 bge-base-zh-v1.5 模型用途、国内下载镜像配置。需要重装依赖、确认版本约束、迁移模型文件时查这里 |
| 用户操作 | [manual-user-guide.md](manual-user-guide.md) | 程序启动后用户能做的所有操作指南。包含首次启动流程、LLM 配置（含 `/setup` 设置窗口入口、配置逻辑总览、能力探测档案驱动档位、max_tokens 输出上限配置、火山方舟深度思考模型 + 工具调用配置、reasoning_effort 实测指南、格式化输出能力自动探测、Agent 引导用户配置指南）、上下文窗口阈值、知识图谱查询、记忆管理（长期记忆 + 语义记忆两层）、文件格式支持、常见问题（数据存储位置、离线使用、备份、GPU 加速、卸载）、日志开关与级别配置。遇到用户操作类问题先查这里 |
| 开发者参考 | [manual-developer.md](manual-developer.md) | 面向开发者的工程参考。包含本地开发环境搭建、调试技巧（日志位置、SSE 事件追踪）、API 端点清单、环境变量、版本更新日志。需要改代码、调试 API、查看历史变更时查这里 |
| 文件格式支持 | [manual-file-formats.md](manual-file-formats.md) | 详细说明三种入库能力（文件存储 / 知识图谱 / 照片）的格式支持矩阵。包含 PDF/Word/Excel/PPT/MD/HTML 等格式细节、不支持知识图谱入库的格式（.doc/.xls/.ppt 旧版二进制 + WPS 假 .docx）及原因、照片格式（JPEG/PNG/GIF/BMP/WebP/HEIC）的人脸识别支持。判断某文件能不能入库、为什么入库失败时查这里 |
| 飞书开通 | [manual-feishu-setup.md](manual-feishu-setup.md) | 飞书机器人开通全流程手册（主 Agent 通过 browser-server MCP 工具操作网页）。包含飞书开放平台创建应用、配置事件订阅、获取 App ID/Secret、写入 im-adapters/feishu 配置、Gateway 启动验证、常见开通故障排查。用户要求接入飞书消息时查这里 |
| 高德开通 | [manual-amap-setup.md](manual-amap-setup.md) | 高德地图 API Key 获取流程手册（主 Agent 通过 browser-server 操作网页）。包含注册高德开放平台、创建应用获取 Key、写入 config/user-config.json、验证照片 EXIF 位置解析功能、常见开通故障排查。用户需要照片地点识别功能时查这里 |
| 智能家居开通 | [manual-ha-setup.md](manual-ha-setup.md) | Home Assistant 完整接入手册。包含 Docker 安装部署 HA、创建长期访问令牌、设备集成方法、智能触发配置（场景/自动化/脚本）、条件推送机制（5.1 节——订阅事件写 DB 不推 IM、主 Agent 的话经 should_push_im 投递 IM，与定时任务同通道）、ha-server MCP 服务器启用、所有已验证 API 行为和踩坑记录。用户要求接入 HA 智能家居控制时查这里 |
| MCP与虚拟磁盘 | [manual-mcp-disk.md](manual-mcp-disk.md) | MCP 服务器同进程架构与虚拟磁盘配置手册。包含新增 MCP 服务器完整步骤（目录结构 + TOOL_SCHEMAS + workdir 配置）、虚拟磁盘 YAML 配置格式与路径映射规则、校验规则和常见配置错误排查。主 Agent 可在 `~/.niu/disk/` 自建 MCP server 配置覆盖或新增。需要新增 MCP 服务器、修改虚拟磁盘路径映射、排查 disk 工具调用失败时查这里 |
| IM Gateway 接入 | [manual-im-gateway.md](manual-im-gateway.md) | 面向第三方开发者的 IM 平台接入文档。包含 Gateway + Adapter 分离架构（双进程）、TCP 协议规范、配置文件格式、目录规范、开发新 Adapter（钉钉/Telegram/企业微信等）的完整步骤。需要对接新的 IM 平台或修改 IM 通信协议时查这里 |
| 通用子 Agent | [manual-general-subagent.md](manual-general-subagent.md) | 阶段三通用子 Agent 体系完整说明。包含配置模板（config/agent-template.md）、动态加载机制（chat 入口扫描 ~/.niu/agents/）、MCP 工具映射（mcpServers frontmatter）、主 Agent 创建子 Agent 流程、同步/异步调用模式、与阶段一+二交互能力的衔接、同步子 Agent @niu-agent 询问通道。子 Agent 标签页（动态 Tab + 独立 SSE 事件通道）、@user 用户提问机制、@end 优先级规则、同步子 Agent SSE 404 竞态修复（pre_register + is_closing）、SubagentEventBus 独立事件总线（ring buffer + epoch 机制）。需要理解或调试子 Agent 标签页、事件推送、@user 提问、SSE 竞态问题时查这里 |
