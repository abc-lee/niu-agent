# 工作日志与报告生成系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 2 个 Skill 文件 + 修改 entity-extractor 提示词 + 2 个定时任务，实现工作日志自动记录和报告生成。

**Architecture:** Skill 文件放在 `memory/skills/`，SkillSync 自动同步到 LightRAG。主 Agent 通过 `read` 工具读取 Skill。entity-extractor 通过 `read` 工具读取 journal-skill.md。不涉及 `config/disk/`。

**Tech Stack:** Markdown Skill 文件 + LightRAG 知识图谱 + 定时任务

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| Create | `memory/skills/journal-skill.md` | 日志格式规范 + 写入规则 + 知识图谱同步规则 |
| Create | `memory/skills/report-skill.md` | 报告模板（周报/月报/季报/年报/自定义）+ 生成流程 |
| Modify | `config/agents/entity-extractor.md` | 新增"工作日志"段落，写明先 read Skill 再写日志 |

---

### Task 1: 创建 journal-skill.md

**Files:**
- Create: `memory/skills/journal-skill.md`

- [ ] **Step 1: 创建 journal-skill.md**

```markdown
---
name: journal-skill
description: Use when user mentions work progress, project updates, meetings, decisions, or asks to record/view/edit daily work logs, or when generating reports from work logs
---

# 工作日志技能

## 日志存储

- 目录：`{workspace}/journals/`
- 文件命名：`YYYY-MM-DD.md`（如 `2026-05-17.md`）
- workspace 路径从 `~/.niu/memory.json` 的 `workspace.path` 字段获取

## 日志条目格式

每条日志是一个三级标题，包含时间戳、任务描述和元数据：

```markdown
## 2026-05-17

### 14:30 完成用户认证模块重构
- 项目：后端服务
- 类型：开发
- 状态：完成
- 关键词：JWT, 认证, 重构

### 16:00 与产品团队讨论需求优先级
- 项目：产品规划
- 类型：会议
- 状态：进行中
- 关键词：需求, 优先级, Q3规划
```

**字段说明**：
- 时间：HH:MM 格式，取对话发生的时间
- 标题：一句话概括工作内容
- 项目：所属项目或工作领域
- 类型：开发/会议/决策/修复/调研/其他
- 状态：完成/进行中/搁置
- 关键词：3-5个关键标签，逗号分隔

## 写入规则

1. 识别到工作相关内容时，追加写入当日日志文件
2. 写入流程：
   - `read` 当日文件（如不存在则创建）
   - 在文件末尾追加新条目
   - `write` 写回完整文件内容
3. 同一条工作内容不重复写入（基于对话消息ID追踪）
4. 每日文件以 `## YYYY-MM-DD` 开头作为日期标记

## 知识图谱同步

1. 首次写入日志文件后，调用 `lightrag_insert_file` 入库
2. 日志文件被追加内容后，先 `lightrag_delete_document` 删除旧版本，再 `lightrag_insert_file` 重新入库
3. doc_id 使用文件绝对路径作为固定标识，确保更新时可精确删除

## 去重机制

- 维护已提取消息ID集合，避免同一对话内容重复写入日志
- 如果同一条消息中包含多个工作事项，分别写成独立条目

## 主 Agent 交互规则

- 用户说"记录一下今天做了XXX" → 直接写入当日日志
- 用户问"我今天做了什么" → `read` 当日日志文件展示
- 用户说"修改今天的日志" → 修改后重新入库知识图谱
- 用户说"查看本周日志" → 依次 `read` 本周所有日志文件
```

- [ ] **Step 2: 验证文件创建成功**

Run: `ls -la memory/skills/journal-skill.md`
Expected: 文件存在

- [ ] **Step 3: 提交**

```bash
git add memory/skills/journal-skill.md
git commit -m "feat: 新增工作日志 Skill — journal-skill.md"
```

---

### Task 2: 创建 report-skill.md

**Files:**
- Create: `memory/skills/report-skill.md`

- [ ] **Step 1: 创建 report-skill.md**

```markdown
---
name: report-skill
description: Use when user asks to generate work reports such as weekly report, monthly report, quarterly report, annual report, performance review, or custom time range summary
---

# 报告生成技能

## 支持的报告类型

| 类型 | 时间范围 | 触发方式 |
|------|----------|----------|
| 日报 | 当天 | 每日 18:00 定时任务 |
| 周报 | 本周一至周日 | 周一 9:00 提醒 / 用户主动触发 |
| 月报 | 本月1日至月末 | 用户主动触发 |
| 季报 | 本季度 | 用户主动触发 |
| 年报 | 本年度 | 用户主动触发 |
| 自定义 | 用户指定范围 | 用户主动触发 |

## 报告生成流程

1. 确定时间范围（起止日期）
2. 依次 `read` 该范围内所有日志文件（`{workspace}/journals/YYYY-MM-DD.md`）
3. 用 `lightrag_query` 补充查询相关上下文：
   - 查询模式：`hybrid`
   - 查询内容：项目进展、关键决策、重要成果
4. LLM 聚合总结：
   - 按项目分组
   - 标注进展状态
   - 提取关键成果
   - 识别问题和风险
5. 生成 Markdown 格式报告
6. 可选：用 office-docs Skill 输出为 Word/PPT

## 周报模板

```markdown
# 周报 — {start_date} 至 {end_date}

## 本周工作概览

### {项目名}
- {工作条目}
- {工作条目}

## 关键成果
1. {成果1}
2. {成果2}

## 问题与风险
1. {问题/风险1}
2. {问题/风险2}

## 下周计划
（由用户补充）
```

## 月报模板

```markdown
# 月报 — {year}年{month}月

## 本月工作概览

### {项目名}
- {工作条目汇总}
- 关键进展：{进展描述}

## 数据指标
- {指标1}：{数值}
- {指标2}：{数值}

## 关键成果
1. {成果1}
2. {成果2}

## 问题与风险
1. {问题/风险1}

## 下月计划
（由用户补充）
```

## 季报/年报模板

在月报基础上增加：
- 季度/年度整体回顾段落
- 里程碑达成情况
- 能力成长总结
- 下阶段战略方向

## 自定义报告

用户可指定：
- 时间范围
- 包含/排除的项目
- 报告重点（成果导向 / 问题导向 / 数据导向）
- 输出格式（Markdown / Word / PPT）

## 报告修改规则

用户反馈报告偏好后，主 Agent 可修改此 Skill 文件：
- 用户说"我的周报不需要会议记录" → 移除模板中的会议记录部分
- 用户说"周报要加数据指标" → 增加数据指标部分
- 用户说"报告要按项目优先级排序" → 更新排序规则
```

- [ ] **Step 2: 验证文件创建成功**

Run: `ls -la memory/skills/report-skill.md`
Expected: 文件存在

- [ ] **Step 3: 提交**

```bash
git add memory/skills/report-skill.md
git commit -m "feat: 新增报告生成 Skill — report-skill.md"
```

---

### Task 3: 修改 entity-extractor.md — 新增工作日志段落

**Files:**
- Modify: `config/agents/entity-extractor.md`

- [ ] **Step 1: 在 entity-extractor.md 末尾（`## 禁止` 之前）新增工作日志段落**

在 `## 实体命名规范（自然语言命名规则）` 段落之后、`## 禁止` 段落之前，插入以下内容：

```markdown
## 工作日志

除了记忆提炼和技能提炼，你还负责**工作日志记录**。

### 核心规则

**写日志前，必须先用 `read` 工具读取 Skill 文件获取最新格式和规则：**

```
read(file_path="{项目根目录}/memory/skills/journal-skill.md")
```

读取后严格按照其中的格式、写入规则和知识图谱同步规则执行。

### 识别信号

当对话中出现以下内容时，应提取为工作日志条目：
- 项目名称或工作领域
- 任务进展（开始、完成、搁置）
- 会议、讨论、决策
- 代码提交、bug修复、功能上线
- 调研、学习、文档编写

### 提取内容

每条日志条目包含：
- 时间：对话发生的 HH:MM
- 项目：所属项目或工作领域
- 任务描述：一句话概括
- 状态：完成/进行中/搁置
- 关键词：3-5个标签

### 写入流程

1. `read` journal-skill.md 获取最新规则
2. `read` 当日日志文件（如不存在则创建）
3. 在文件末尾追加新条目
4. `write` 写回完整文件内容
5. 知识图谱同步（首次 insert，更新时 delete + insert）

### 去重

- 基于对话消息ID追踪已提取记录
- 同一条消息不重复写入日志
- 同一条消息中包含多个工作事项时，分别写成独立条目
```

- [ ] **Step 2: 验证修改正确**

Run: `grep -n "工作日志" config/agents/entity-extractor.md`
Expected: 输出包含新增的"工作日志"段落标题

- [ ] **Step 3: 提交**

```bash
git add config/agents/entity-extractor.md
git commit -m "feat: entity-extractor 新增工作日志提取规则 — 先读Skill再写日志"
```

---

### Task 4: 创建每日日志确认定时任务

**Files:**
- None（通过 API 创建定时任务）

- [ ] **Step 1: 通过 API 创建每日 18:00 日志确认任务**

启动应用后，在对话中告诉主 Agent：

> "创建一个每天 18:00 的定时任务，任务内容是：请检查今天的日志，整理后与用户确认是否完整"

或者直接通过 API 调用：

```bash
curl -s -X POST http://localhost:9876/scheduler/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "每日日志确认",
    "cron": "0 18 * * *",
    "prompt": "请检查今天的日志，整理后与用户确认是否完整",
    "enabled": true
  }'
```

Expected: 返回任务创建成功，包含 task_id

- [ ] **Step 2: 验证任务已创建**

Run: `curl -s http://localhost:9876/scheduler/tasks | python3 -m json.tool`
Expected: 任务列表中包含"每日日志确认"

---

### Task 5: 创建每周报告提醒定时任务

**Files:**
- None（通过 API 创建定时任务）

- [ ] **Step 1: 通过 API 创建每周一 9:00 报告提醒任务**

```bash
curl -s -X POST http://localhost:9876/scheduler/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "每周报告提醒",
    "cron": "0 9 * * 1",
    "prompt": "提醒用户本周工作已汇总，询问是否需要生成周报",
    "enabled": true
  }'
```

Expected: 返回任务创建成功，包含 task_id

- [ ] **Step 2: 验证任务已创建**

Run: `curl -s http://localhost:9876/scheduler/tasks | python3 -m json.tool`
Expected: 任务列表中包含"每周报告提醒"

---

### Task 6: 端到端验证

**Files:**
- None

- [ ] **Step 1: 启动应用，确认 SkillSync 同步了新 Skill**

启动应用后检查日志，应看到 journal-skill 和 report-skill 被同步到 LightRAG。

- [ ] **Step 2: 验证 entity-extractor 能读取 journal-skill.md**

在对话中提到工作内容，观察 entity-extractor 是否：
1. 先 `read` journal-skill.md
2. 写入当日日志文件
3. 调用 lightrag_insert_file 入库

- [ ] **Step 3: 验证主 Agent 能生成报告**

对主 Agent 说"帮我写本周的周报"，观察是否：
1. 读取 report-skill.md
2. 读取本周日志文件
3. 用 lightrag_query 补充查询
4. 生成结构化周报

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "feat: 工作日志与报告生成系统 — Skill + 提示词 + 定时任务"
```