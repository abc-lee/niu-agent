# 工作日志与报告生成系统设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 助理自动从对话中提取工作内容写入日志，并支持生成周报/月报/季报/年报等报告。

**Architecture:** 纯 Skill 驱动，零代码改动。新增 2 个 Skill 文件（journal-skill.md + report-skill.md）+ 2 个定时任务，复用现有 file-parser、lightrag-server、entity-extractor、office-docs 等基础设施。

**Tech Stack:** Markdown 日志文件 + LightRAG 知识图谱 + 定时任务 + Skill 提示词

---

## 一、背景与动机

AI 助理在日常对话中积累了大量用户工作信息（项目进展、会议内容、技术决策），但这些信息散落在对话历史中，无法被系统化检索和汇总。用户需要：

1. 自动记录每天的工作内容，无需额外操作
2. 随时查询历史工作记录
3. 生成周报/月报/季报/年报/述职报告

## 二、架构设计

### 2.1 数据流

```
日常对话 → entity-extractor 自动提取工作内容
                    ↓
           写入 workspace/journals/YYYY-MM-DD.md
                    ↓
           lightrag_insert_file 入库知识图谱
                    ↓
定时任务(18:00) → 主Agent 读取当日日志 → 整理 → 与用户确认
                    ↓
用户说"写周报" → 主Agent 读取日志文件 + LightRAG查询 → 生成报告
```

### 2.2 核心组件

| 组件 | 类型 | 位置 | 说明 |
|------|------|------|------|
| journal-skill.md | Skill | config/disk/ | 日志格式规范 + 写入规则，主Agent通过disk()读取 |
| report-skill.md | Skill | config/disk/ | 报告模板 + 生成流程，主Agent通过disk()读取 |
| entity-extractor.md | Agent定义 | config/agents/ | 日志写入规则直接写在提示词中（子Agent不会读Skill） |
| 每日日志确认 | 定时任务 | scheduler | 18:00 触发，读取当日日志，与用户确认 |
| 每周报告提醒 | 定时任务 | scheduler | 周一 9:00 触发，提醒生成周报 |

### 2.3 日志存储

- **目录**：`{workspace}/journals/`
- **文件命名**：`YYYY-MM-DD.md`（如 `2026-05-17.md`）
- **格式**：Markdown，含时间戳条目、项目标签、进展状态

### 2.4 知识图谱交互

- **入库**：日志文件通过 `lightrag_insert_file` 入库，LightRAG 自动提取实体（项目、任务、人物）和关系
- **更新**：文件修改后，先 `lightrag_delete_document(doc_id)` 再 `lightrag_insert_file(path)` 重新入库
- **时间链**：dream-evolver 对日志实体建立 `followed_by` 时间链
- **脑区**：region_sync 的 Leiden 社区发现自动将日志实体聚类到"工作日志脑区"
- **查询**：报告生成时用 `lightrag_query` 补充查询相关上下文

## 三、日志 Skill 设计

### 3.1 journal-skill.md 核心内容

**特殊性**：
1. 主 Agent 通过 disk() 读取此 Skill
2. entity-extractor **不会读 Skill 文件**，其日志写入规则直接写在 `config/agents/entity-extractor.md` 提示词中
3. 主 Agent 可根据用户特点随时修改此 Skill 和 entity-extractor.md

**日志条目格式**：

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

**写入规则**：
1. entity-extractor 在对话中识别到工作相关内容时，追加写入当日日志
2. 追加方式：用 `read` 读取当日文件 → 在末尾追加条目 → 用 `write` 写回
3. 如当日文件不存在，用 `write` 创建新文件并写入头部日期标记
4. 同一条工作内容不重复写入（基于对话消息ID追踪已提取记录）

**知识图谱同步规则**：
1. 首次写入日志文件后，调用 `lightrag-server/lightrag_insert_file` 入库
2. 日志文件被追加内容后，先 `lightrag_delete_document` 删除旧版本，再 `lightrag_insert_file` 重新入库
3. doc_id 使用文件路径作为固定标识（`workspace/journals/YYYY-MM-DD.md`），确保更新时可精确删除

**注意**：entity-extractor 已有 `lightrag-server` 工具权限，可直接调用入库。文件读写使用基础工具 `read`/`write`/`edit`，无需额外添加 file-parser。

### 3.2 entity-extractor 的日志提取规则

**关键约束**：entity-extractor 不会自己读 Skill 文件，所有日志写入规则必须直接写在其 agent 定义（`config/agents/entity-extractor.md`）的提示词中。

**当前可用工具**：
- 基础工具：`read`、`write`、`edit`（可直接读写文件）
- MCP 工具：`lightrag-server`（知识图谱入库）
- 无 disk() 工具，无法读取 Skill 文件

**修改 entity-extractor.md**：
1. 在提示词中新增"工作日志"提取规则段落
2. 明确日志文件路径：`{workspace}/journals/YYYY-MM-DD.md`
3. 明确日志条目格式（与 3.1 节格式一致）
4. 明确写入流程：`read` 当日文件 → 追加条目 → `write` 写回
5. 明确知识图谱同步：`lightrag_insert_file` 入库，更新时先 `lightrag_delete_document` 再重新入库

**提取规则**：
- **识别信号**：用户提到项目名称、任务进展、会议、决策、代码提交、bug修复等
- **提取内容**：时间、项目、任务描述、状态（进行中/完成/搁置）、关键词
- **去重机制**：基于对话消息ID追踪已提取记录，避免同一对话内容重复写入日志

### 3.3 主 Agent 的日志交互规则

1. 用户主动说"记录一下今天做了XXX" → 主 Agent 直接写入日志
2. 用户问"我今天做了什么" → 主 Agent 读取当日日志文件展示
3. 用户说"修改今天的日志" → 主 Agent 修改日志文件，然后重新入库知识图谱
4. 每日定时任务触发 → 主 Agent 读取当日日志，整理后询问用户确认

## 四、报告 Skill 设计

### 4.1 report-skill.md 核心内容

**支持的报告类型**：

| 类型 | 时间范围 | 默认触发 |
|------|----------|----------|
| 日报 | 当天 | 每日 18:00 定时任务 |
| 周报 | 本周一至周日 | 周一 9:00 提醒 |
| 月报 | 本月1日至月末 | 用户主动触发 |
| 季报 | 本季度 | 用户主动触发 |
| 年报 | 本年度 | 用户主动触发 |
| 自定义 | 用户指定范围 | 用户主动触发 |

**报告生成流程**：

1. 确定时间范围
2. 读取该范围内所有日志文件（`workspace/journals/*.md`）
3. 用 `lightrag_query` 补充查询相关上下文（项目进展、关键决策）
4. LLM 聚合总结：
   - 按项目/任务分组
   - 标注进展状态
   - 提取关键成果
   - 识别问题和风险
5. 生成报告（Markdown 格式）
6. 可选：用 office-docs Skill 输出为 Word/PPT

**周报模板示例**：

```markdown
# 周报 — 2026-05-11 至 2026-05-17

## 本周工作概览

### 项目A：后端服务
- 完成用户认证模块重构（JWT方案）
- 修复3个bug（登录超时、权限校验、缓存失效）
- 与产品团队讨论Q3需求优先级

### 项目B：前端优化
- 页面加载速度提升30%
- 新增暗色模式支持

## 关键成果
1. 认证模块重构完成，安全性提升
2. 前端性能优化达标

## 问题与风险
1. Q3需求优先级尚未确定，可能影响排期
2. 缓存方案需要进一步评估

## 下周计划
（由用户补充）
```

### 4.2 主 Agent 可修改报告模板

主 Agent 在与用户交互中了解偏好后，可修改 report-skill.md：
- 用户说"我的周报不需要会议记录" → 主 Agent 更新模板，移除会议记录部分
- 用户说"周报要加数据指标" → 主 Agent 更新模板，增加数据指标部分
- 用户说"报告要按项目优先级排序" → 主 Agent 更新模板排序规则

## 五、定时任务设计

### 5.1 每日日志确认（18:00）

**触发词**：`请检查今天的日志，整理后与用户确认是否完整`

**主 Agent 执行流程**：
1. 读取 `workspace/journals/YYYY-MM-DD.md`（当日日志）
2. 如文件不存在，说明今天没有自动记录的内容
3. 如文件存在，整理日志内容，生成简洁摘要
4. 询问用户："今天的日志记录了以下内容，是否完整？需要补充吗？"
5. 用户补充后，更新日志文件并重新入库知识图谱

### 5.2 每周报告提醒（周一 9:00）

**触发词**：`提醒用户本周工作已汇总，询问是否需要生成周报`

**主 Agent 执行流程**：
1. 提醒用户："本周的工作日志已汇总，需要我帮你生成周报吗？"
2. 用户确认后，按 report-skill.md 的流程生成周报
3. 用户可修改报告内容，确认后保存

## 六、YAGNI — 不做的事

- 不建独立 MCP 服务器（journal-server）
- 不建 SQLite 日志数据库（日志就是 Markdown 文件）
- 不做日志版本控制（依赖文件系统和 git）
- 不做多人协作日志
- 不做日志审批流程
- 不做日志加密
- 不做自动发送报告到邮箱/Slack

## 七、演进路径

如果方案 A 验证后发现 Skill 精度不够，可渐进升级：

**Phase 1（当前）**：纯 Skill + 定时任务，零代码改动
**Phase 2（如需）**：给 lightrag-server 新增 `journal_query`（按日期范围查询）和 `journal_summarize`（聚合摘要）2 个工具
**Phase 3（如需）**：独立 journal-server（完整日志 CRUD + 报告引擎）

## 八、验证标准

1. entity-extractor 能从对话中自动提取工作内容并写入日志文件
2. 日志文件能正确入库 LightRAG 知识图谱
3. 日志文件修改后能正确更新知识图谱（delete + insert）
4. 定时任务能触发主 Agent 读取日志并与用户确认
5. 主 Agent 能根据日志文件 + LightRAG 查询生成周报/月报
6. 主 Agent 能根据用户偏好修改 Skill 内容
7. journal-skill.md 和 report-skill.md 主 Agent 可通过 disk() 读取
8. entity-extractor.md 提示词中包含完整的日志写入规则（不依赖 Skill 文件）