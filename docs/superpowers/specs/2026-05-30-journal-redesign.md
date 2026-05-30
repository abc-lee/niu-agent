# 工作日志与报告系统重构设计

**Goal:** 重建日志功能——新建专职 journal-agent，在上下文压缩前提取工作内容写入单文件日志，支持报告生成。

**Architecture:** 新建 journal-agent（subagent），插入 auto-tidy 管道（压缩前调用）。日志单文件存储，不入知识图谱。report-skill.md 保留供主 Agent 调整报告格式。

**Tech Stack:** Markdown 单文件 + Agent 提示词 + report-skill.md

---

## 一、问题分析

### 1.1 现有方案为什么从未生效

| 组件 | 状态 | 问题 |
|------|------|------|
| entity-extractor.md 日志指令 | 存在（134-145行） | 被埋在文件末尾，auto-tidy task prompt 不提日志，LLM 优先做实体提取而忽略 |
| auto-tidy task prompt | 缺失 | 只说"提取有价值内容入库"，没提醒写日志 |
| journal-skill.md | 存在 | 知识图谱同步规则有 bug（`lightrag_insert_file` 的 doc_id 不当主键，删除静默失败） |
| 定时任务 | 已启用 | daily-journal-check(18:00) + weekly-report-reminder(周一9:00)，启动时自动创建 |
| 实际日志文件 | 零 | 从未写过任何条目 |

### 1.2 核心约束：上下文压缩

上下文压缩后原始对话内容丢失，日志提取必须在压缩之前完成。压缩触发条件：

- **sleep 模式**：上下文使用率 >50% + 用户进入休眠
- **force 模式**：上下文使用率 >80%，强制压缩

auto-tidy 管道的调用顺序是：entity-extractor → dream-evolver → context-manager。日志提取必须插在 context-manager 之前。

### 1.3 为什么不能让 entity-extractor 继续负责

1. entity-extractor 已被 BLOCKED_SUBAGENTS 屏蔽，主 Agent 无法主动调用
2. entity-extractor 职责已重（实体提取+记忆提炼+技能提炼），再加日志会进一步分散注意力
3. entity-extractor 没有用户交互能力，无法确认日志内容

### 1.4 为什么不入知识图谱

1. 日志是时间线性文本，不需要图谱的语义检索能力
2. `lightrag_insert_file` 的 doc_id 参数只当 track_id，不是主键，导致删除静默失败
3. "删旧插新"模式会级联删除 dream-evolver 精加工过的实体
4. 用户需求是"记下来，以后能查到"，Markdown 文件直接读比图谱查询更准确

---

## 二、新方案设计

### 2.1 新建 journal-agent

**配置文件**：`config/agents/journal-agent.md`

| 属性 | 值 |
|------|-----|
| name | journal-agent |
| mode | subagent |
| mcpServers | 无（不需要知识图谱） |
| temperature | 0.3 |
| 基础工具 | read, write, edit, grep |

**职责**：
1. 从对话消息中提取工作内容，追加写入日志文件
2. 读取日志文件生成报告（周报/月报/季报/年报）
3. 读取 `~/.niu/skills/report-skill.md` 获取报告格式模板

**可被主 Agent 调用**：用户说"记录一下"、"写周报"时，主 Agent 通过 `chat-with-journal-agent` 调用。

### 2.1.1 journal-agent.md 提示词核心内容

journal-agent.md 提示词应包含以下内容（从 journal-skill.md 迁移并增强）：

1. **输入格式**：task 模式，接收带 `[id:UUID] [idx:N]` 标注的增量消息文本
2. **工作内容识别**：识别信号包括项目名称、任务进展、会议、决策、代码提交、bug修复等
3. **日志条目格式**：`- HH:MM 一句话概括 | 项目:XXX | 类型:开发/会议/决策/修复/调研/其他 | 状态:完成/进行中/搁置`
4. **写入流程**：
   - 读取 `~/.niu/memory.json` 获取 `workspace.path`，如果缺失则使用 `~/.niu/` 作为 fallback
   - 检查 `{workspace}/journal.md` 是否存在，不存在则创建
   - 追加写入：`write(file_path, content, mode="append")`
   - 同一日期的条目追加在对应 `# YYYY-MM-DD` 标题下；如当天标题不存在，先追加 `# YYYY-MM-DD` 标题
5. **去重**：基于消息 UUID 去重，不重复提取同一消息
6. **游标报告**：操作完成后返回 `{"last_journal_id": "<操作范围内 idx 最大的消息的 UUID>"}`
7. **报告生成**：
   - 读取 `~/.niu/skills/report-skill.md` 获取报告格式模板
   - 用 `grep` 定位日期范围，`read` 读取对应内容
   - 按模板聚合生成报告
8. **职业上下文**：读取 `~/.niu/memory.json` 的 `user.profession`，优先关注与职业相关的工作内容

### 2.2 日志存储

**单文件**：`{workspace}/journal.md`

**格式**：
```markdown
# 2026-05-30
- 14:30 完成用户认证模块重构 | 项目:后端服务 | 类型:开发 | 状态:完成
- 16:00 与产品团队讨论需求优先级 | 项目:产品规划 | 类型:会议 | 状态:进行中

# 2026-05-29
- 10:00 修复登录超时bug | 项目:后端服务 | 类型:修复 | 状态:完成
```

**操作方式**：
- 追加：`write(file_path, content, mode="append")` — 不需要读文件，直接追加
- 查询某天：`grep("2026-05-30", path=journal_path)` 定位行号 → `read(offset=N, limit=50)`
- 查询日期范围：多次 read 调用
- 编辑旧条目：先 read 精确内容，再 edit 替换（低频操作）

**去重机制**：维护已提取消息 ID 集合，避免同一对话内容重复写入。集合持久化到 `~/.niu/last_journal.json`（游标机制，与 entity-extractor/dream-evolver 一致）。

**clear_chat 游标重置**：当用户清空对话（clear_chat）时，必须将 `last_journal.json` 游标重置为空（与 entity-extractor/dream-evolver/compress 一致）。注意：清空对话只是重置游标，不会删除 journal.md 中已写入的日志条目。清空后新一轮对话的内容会从零开始提取。

**文件增长策略**：journal.md 会随时间不断增长。当文件超过 1 年的条目时，journal-agent 在每次写入时自动将 1 年前的条目归档到 `{workspace}/journal-archive/YYYY.md`（按年归档）。归档文件不影响日常追加和查询操作。

### 2.3 auto-tidy 管道插入

**调用顺序**：

```
sleep 模式（>50%+休眠）：
  entity-extractor → dream-evolver → journal-agent → context-manager

force 模式（>80%强制）：
  entity-extractor → dream-evolver → journal-agent → context-manager

轻量 sleep（≤50%+休眠）：
  entity-extractor → dream-evolver → context-manager（不调 journal-agent）
```

**journal-agent 只在即将压缩时调用**。如果上下文使用率 ≤50%，对话内容还在，下次再提取也来得及。

**调用方式**：与 entity-extractor 相同，使用 task 模式（`call_subagent(task=msg_text)`），传入增量消息文本。

### 2.4 Skill 文件处理

| 文件 | 处理 | 原因 |
|------|------|------|
| `journal-skill.md` | **删除** | 日志写入规则写进 journal-agent 提示词，不再需要独立 skill |
| `report-skill.md` | **保留** | 报告格式模板，主 Agent 可通过 edit 工具调整；所有 skill 文件主 Agent 都知道，用户要求修改报告时主 Agent 可直接读 skill 修改 |

### 2.5 主 Agent 交互

**主 Agent 配置**（`config/agents/niu.md`）：
- 子 Agent 列表增加 `journal-agent`
- 委托规则增加：用户说"记录一下" → `chat-with-journal-agent`；用户说"写周报/月报" → `chat-with-journal-agent`

**定时任务**（需更新内容）：
- `daily-journal-check`(18:00)：更新内容为"请调用 journal-agent 检查今天的日志，整理后与用户确认是否完整"，确保主 Agent 委托给 journal-agent 而非自行处理
- `weekly-report-reminder`(周一9:00)：更新内容为"提醒用户本周工作已汇总，询问是否需要生成周报。如需生成，请调用 journal-agent"

### 2.6 entity-extractor 改动

- **删除** entity-extractor.md 中的"工作日志"段落（134-145行）
- entity-extractor 不再负责日志，职责回归纯粹的实体提取+记忆提炼+技能提炼

---

## 三、改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `config/agents/journal-agent.md` | **新建** — Agent 提示词（日志写入规则+报告生成流程） | 无 |
| `config/agents/niu.md` | 子 Agent 列表增加 journal-agent，委托规则增加 | 低 |
| `config/agents/entity-extractor.md` | 删除"工作日志"段落 | 低 |
| `niu_api/compat.py` | auto-tidy 管道插入 journal-agent 调用（sleep>50% 和 force 模式） | 中 |
| `niu_api/__main__.py` | 更新 `_SYSTEM_TASKS` 中两个定时任务的 content，确保主 Agent 委托给 journal-agent | 低 |
| `agent/handler.py` | BLOCKED_SUBAGENTS 不包含 journal-agent（主 Agent 可调用） | 低 |
| `~/.niu/skills/journal-skill.md` | **删除** | 无 |
| `config/user-data/skills/journal-skill.md` | **删除** | 无 |
| `~/.niu/skills/report-skill.md` | 保留，数据源从 LightRAG 改为读 journal.md 文件 | 中 |
| `config/user-data/skills/report-skill.md` | 同上。具体变更：路径从 `{workspace}/journals/YYYY-MM-DD.md` 改为 `{workspace}/journal.md`；查询方式从 `lightrag_query` 改为 `grep`+`read`；删除所有 LightRAG 查询相关指令 | 中 |

---

## 四、不需要改的

- 定时任务初始化（`niu_api/__main__.py`）— 已有，无需修改
- 定时任务初始化（`niu_api/__main__.py`）— 需更新 `_SYSTEM_TASKS` 中的 content
- dream-evolver — 不涉及日志
- context-manager — 不涉及日志
- lightrag-server — 日志不入图谱，不涉及
- SkillSync（`agent/injector/sync.py`）— report-skill.md 保留在 skills 目录，继续同步

---

## 五、验证标准

1. journal-agent 能从增量消息中提取工作内容并追加写入 `journal.md`
2. 上下文使用率 >50%+休眠时，auto-tidy 管道在压缩前调用 journal-agent
3. 上下文使用率 >80%强制压缩时，auto-tidy 管道在压缩前调用 journal-agent
4. 上下文使用率 ≤50%时，不调用 journal-agent
5. 主 Agent 可通过 `chat-with-journal-agent` 主动调用
6. 用户说"记录一下"时主 Agent 调用 journal-agent
7. 用户说"写周报"时主 Agent 调用 journal-agent 生成报告
8. 18:00 定时任务触发主 Agent 确认当天日志
9. 周一 9:00 定时任务触发周报提醒
10. report-skill.md 可被主 Agent 读取和修改
11. 去重机制防止同一内容重复写入
12. 日志不入知识图谱，无碎片化风险
