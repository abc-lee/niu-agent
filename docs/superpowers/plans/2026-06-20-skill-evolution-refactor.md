# Skill 自我进化系统改造 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将新 skill 创建职责统一收归 dream-evolver，主 Agent 可修改已有 skill 但不能创建新 skill，关闭 ExperienceSummarizer 的 skill 写入，借鉴 SkillOpt 的 Skill-Aware Reflection 方法论增强 dream-evolver 的 skill 修改判断力。

**Architecture:** 三步走——(1) 切断旧路径：删除 Write-SKILL.md、关闭 ExperienceSummarizer；(2) 增强 dream-evolver.md：用它能理解的语言描述 skill 编写规范和工作流（它只看到对话消息和文件，不知道"主 Agent"或"注入"等系统概念）；(3) 后端支持：SkillSync 识别草稿、runner 注入草稿提示。主 Agent 可修改已有 skill，但新 skill 创建统一由 dream-evolver 负责。

**Tech Stack:** Python (handler.py, sync.py, runner.py), Markdown (Agent 配置文件)

---

## 角色视角说明

**主 Agent（niu）**：知道 skill、使用 skill、可修改已有 skill，但不能创建新 skill（没有模板，不知道新的 frontmatter 规范和草稿流程）。新 skill 创建统一由 dream-evolver 负责。

**dream-evolver**：只知道增量对话消息、知识图谱工具、read/write/edit 文件操作、工作目录。不知道"主 Agent"是谁，不知道 SkillSync、注入机制等系统概念。它看到的世界是：消息流 + 知识图谱 + skill 文件。

**SkillSync/runner**：程序化处理，读取 skill 文件同步到 LightRAG、注入到提示词。不需要理解概念，只需要识别 frontmatter 中的 status 字段。

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `memory/skills/Write-SKILL.md` | **删除** | 主 Agent 不再写 skill，此文件无用 |
| `config/agents/niu.md` | 修改 | 更新 skill 编写指引（删除 Write-SKILL.md 引用，禁止创建新 skill，保留修改已有 skill 能力），增加 skill 使用反馈规则 |
| `config/agents/dream-evolver.md` | 修改 | 增强 skill 编写职责，融入 Skill-Aware Reflection |
| `agent/handler.py` | 修改 | 关闭 ExperienceSummarizer 触发逻辑 |
| `agent/experience_summarizer.py` | 保留不删 | 避免 import 报错，但不再被调用 |
| `agent/autonomous_explorer.py` | 修改 | 移除 experience_summarizer 引用 |
| `tests/test_p1/test_experience_summarizer.py` | 修改 | 标记测试为 skip |
| `agent/injector/sync.py` | 修改 | 识别草稿 skill，description 加前缀 |
| `agent/runner.py` | 修改 | 注入草稿 skill 时添加提示 |

---

### Task 1: 删除 Write-SKILL.md + 清理文档引用

**Files:**
- Delete: `memory/skills/Write-SKILL.md`
- Modify: `docs/SYSTEM_MANUAL.md:190`

- [ ] **Step 1: 删除文件**

```bash
rm memory/skills/Write-SKILL.md
```

- [ ] **Step 2: 清理 SYSTEM_MANUAL.md 中的引用**

在 `docs/SYSTEM_MANUAL.md` 中搜索 `Write-SKILL` 相关行（约第 190 行附近），删除或更新该引用行。将原来的 `Write-SKILL.md | 创建新 Skill 的规范...` 改为说明 skill 编写已转移至 dream-evolver。

- [ ] **Step 3: 验证**

Run: `ls memory/skills/Write-SKILL.md 2>&1; grep -rn "Write-SKILL" config/agents/niu.md docs/ --include="*.md"`
Expected: 文件不存在；niu.md 和 SYSTEM_MANUAL.md 中无残留引用。注：dream-evolver.md 中的 Write-SKILL 引用将在 Task 4 中处理，此处不检查

- [ ] **Step 4: Commit**

```bash
git add memory/skills/Write-SKILL.md docs/SYSTEM_MANUAL.md
git commit -m "refactor: delete Write-SKILL.md and clean up references"
```

注：SkillSync 的 watchdog 机制会在文件删除后自动调用 `_delete_skill_from_lightrag("Write-SKILL")` 清理知识图谱中的实体，无需手动处理。

---

### Task 2: 更新主 Agent 的 skill 相关指引 + 增加 skill 使用反馈规则

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 修改 niu.md，更新 skill 编写指引**

将第 168 行：
```
需要编程解决的问题，优先使用程序安装目录下的Python环境，编写的程序可保存在工作目录下。如遇复杂问题可保存编写好的代码，并根据Write-SKILL.md技能编写skill，永久性提高自己的能力。
```

改为：
```
需要编程解决的问题，优先使用程序安装目录下的Python环境，编写的程序可保存在工作目录下。你可以修改已有 skill 的内容（用 edit 工具），但不要创建新 skill 文件——新 skill 的创建（含 frontmatter 规范、草稿流程）由 dream-evolver 统一负责。如发现值得沉淀的新模式，告知用户即可。
```

注：Write-SKILL.md 将在 Task 1 中删除，主 Agent 没有新 skill 的模板和规范，所以禁止创建。但修改已有 skill 不受限制——主 Agent 了解当前任务上下文，改个规则不耗时。

- [ ] **Step 2: 在 niu.md 的 Skills 使用规则部分增加反馈规则**

在第 138 行 `3. **读完就遵循**：严格按照 Skill 文件中的步骤执行，不要自己猜测` 之后、第 140 行 `# 照片与文件引用` 之前插入（即 Skills 使用规则章节的末尾）：

```markdown
### Skill 使用反馈

- **草稿 skill**：使用后**必须**明确说明效果——草稿 skill 尚未验证，你的反馈决定它能否转正。例如："根据 [skill-name] 草稿的指导，成功完成了..."或"按照 [skill-name] 草稿的步骤操作，但在...处遇到问题"
- **成熟 skill**：仅在遇到问题时说明——规则有误、缺少步骤等。正常工作时不需要特别说明
```

- [ ] **Step 3: 验证修改**

Run: `grep -n "Write-SKILL" config/agents/niu.md`
Expected: 无匹配结果

Run: `grep -n "草稿 skill" config/agents/niu.md`
Expected: 有匹配

- [ ] **Step 3: Commit**

```bash
git add config/agents/niu.md
git commit -m "refactor: update skill-writing guidance — remove Write-SKILL.md reference, add draft feedback rule"
```

---

### Task 3: 关闭 ExperienceSummarizer

**Files:**
- Modify: `agent/handler.py:26,461-462,594-651`
- Modify: `agent/autonomous_explorer.py:205-207`
- Modify: `tests/test_p1/test_experience_summarizer.py`

- [ ] **Step 1: 修改 handler.py，移除 ExperienceSummarizer 相关代码（原子操作，必须全部完成才能保存文件）**

⚠️ **这 4 项修改必须在同一次文件写入中完成，中间状态会导致 ImportError/NameError 崩溃。**

需要做四件事，**必须同时完成**，否则中间状态会导致运行时崩溃：

(a) 将第 26 行 `from .experience_summarizer import ExperienceSummarizer, ToolExecution, ExperienceContext` 替换为注释 `# ExperienceSummarizer disabled`

(b) 将第 461-462 行 `self._experience_context` 和 `self._experience_summarizer` 初始化替换为注释

(c) 在 `tool_after_callback` 方法中，删除第 519 行注释 `# 追踪工具执行以供经验总结` 和第 520 行调用 `self._track_tool_execution(tool_name, args, ret)`。**保留**第 516 行注释 `# 追踪工具调用（用于重复检测）` 和第 517 行调用 `self._track_tool_call_for_repeat_detection(tool_name, args)` 不变

(d) 删除整个 `_track_tool_execution` 方法（约第 594-627 行）和整个 `_check_and_summarize_experience` 方法（约第 629-651 行）

- [ ] **Step 2: 修改 autonomous_explorer.py，移除 experience_summarizer 引用**

将第 205-207 行：
```python
        # 检查 experience_summarizer 是否有未处理的上下文
        # 这个实现比较简单，后续可以增强
        return 0
```
改为：
```python
        # ExperienceSummarizer disabled
        return 0
```

- [ ] **Step 3: 修改测试文件，标记为 skip**

在 `tests/test_p1/test_experience_summarizer.py` 文件顶部（import 之后）添加：
```python
import pytest

pytestmark = pytest.mark.skip(reason="ExperienceSummarizer disabled — skill writing now handled by dream-evolver")
```

- [ ] **Step 4: 验证 handler.py 语法正确**

Run: `python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add agent/handler.py agent/autonomous_explorer.py tests/test_p1/test_experience_summarizer.py
git commit -m "refactor: disable ExperienceSummarizer — skill writing unified to dream-evolver"
```

---

### Task 4: 增强 dream-evolver 配置

**核心原则：站在 dream-evolver 的第一人称视角写工作流。它只看到增量消息和文件，规则是知识储备，工作流才是执行路径。**

**Files:**
- Modify: `config/agents/dream-evolver.md`

- [ ] **Step 1: 更新 description**

将第 3 行：
```
description: "梦境进化 - 精加工知识图谱（描述优化、时间链、脑区）+ skill 维护"
```
改为：
```
description: "梦境进化 - 精加工知识图谱 + skill 编写与优化"
```

- [ ] **Step 2: 更新职责边界说明**

将第 13 行：
```
- **dream-evolver**（你）：对知识图谱中的实体进行**精加工**——打标签、建关系、关联脑区、更新画像
```
改为：
```
- **dream-evolver**（你）：对知识图谱中的实体进行**精加工**——打标签、建关系、关联脑区、更新画像；**同时负责编写和优化所有 skill 文件**
```

- [ ] **Step 3: 重写"2项核心任务"部分**

将第 118-173 行（从 `## 2项核心任务` 到 `old_string 必须在文件中唯一匹配（含空白/缩进）`）替换为以下内容。

注意：这是 55 行的大范围替换，建议分两步执行：
1. 先将 `## 2项核心任务` 到 `### 任务2：Skill 维护（次要任务）` 之前（第 118-149 行）替换为"阶段A"和"阶段B"的内容（精加工部分步骤基本不变，只是重新组织为阶段B）
2. 再将 `### 任务2：Skill 维护（次要任务）` 到 `old_string 必须在文件中唯一匹配（含空白/缩进）`（第 150-173 行）替换为"阶段C"和"Skill 文件规范"的内容

- [ ] **Step 3.5: 替换知识图谱工作原理部分中的"主 Agent"引用**

⚠️ **必须在 Step 3 之前执行**——Step 3 替换第 118-173 行后行号会变化，导致 Step 3.5 的行号引用失效。Step 3.5 修改的行（第 23-73 行）在 Step 3 替换范围之前，不影响 Step 3 的 old_string 匹配。

dream-evolver 不知道"主 Agent"是谁，这部分内容需要用它能理解的表述。逐项替换：

1. 第 23 行完整替换：
   old: `你操作的知识图谱是一个**长期记忆系统**。你写入的内容，未来主 Agent 回答用户问题时会检索到。理解"我写了什么 → 用户提问时检索出什么"这个完整链路，你才能写出高质量的图谱数据。`
   new: `你操作的知识图谱是一个**长期记忆系统**。你写入的内容，未来检索时会被返回。理解"我写了什么 → 用户提问时检索出什么"这个完整链路，你才能写出高质量的图谱数据。`
   （后半段"理解'我写了什么...'"无"主 Agent"字样，保留不变）
2. 第 42 行：`当用户问"我之前讨论过什么编程语言？"，主 Agent 会这样检索：` → `当检索"编程语言"相关内容时，过程如下：`
3. 第 45 行：`你的 description 就是检索结果中展示给主 Agent 的内容` → `你的 description 就是检索结果中展示给使用者的内容`
4. 第 50-51 行：`主 Agent 会看到：知识体系脑区 --[包含]--> Python` + `所以你建的关系必须有语义：关系类型要能读成一句话（"用户偏好Python"、"Python属于程序记忆区"）` → `检索时会看到：知识体系脑区 --[包含]--> Python` + 后半句保留
5. 第 61 行：`以后用户问"我擅长什么Web框架？"，主 Agent 检索` → `以后检索"Web框架"时`
6. 第 65 行：`→ 主 Agent 读到 description，知道用户擅长 FastAPI，用于构建API服务` → `→ 检索结果中展示 description，可知用户擅长 FastAPI，用于构建API服务`
7. 第 69 行：`→ 主 Agent 读到关系，确认"用户擅长 FastAPI"` → `→ 从关系中可确认"用户擅长 FastAPI"`
8. 第 73 行：`description 是检索结果的"展示面"——写得模糊，主 Agent 就得不到有用信息` → `description 是检索结果的"展示面"——写得模糊，检索时就得不到有用信息`

```markdown
## 工作流程

你收到增量消息后，按以下流程执行：

### 阶段A：阅读消息，提取信息

逐条阅读收到的全部消息，同时完成以下两项提取：

**A1. 提取实体**（供阶段B精加工用）
- 从消息中识别有持久价值的实体（概念、偏好、技能、事件）
- 注意去重：用 `lightrag_search_entities(query, keywords=实体名, top_k=5)` 检查是否已存在

**A2. 观察 skill 相关信号**（供阶段C用）
- ✦ **明确的 skill 反馈**：assistant 消息中包含"根据…的指导"、"按照…的步骤"、"…的规则与实际不符"等表述——这是最可靠的信号，优先处理
- ✦ **重复模式**：同一种工作方式在消息中出现 2 次以上（例如反复用同一套步骤解决类似问题）
- ✦ **多轮失败后解决**：某个工具或方法连续失败多次，最终找到方案解决
- ✦ **skill 被使用且成功**：assistant 消息中的 tool_calls 包含 `read` 且参数路径包含 skills/ 目录，且后续消息显示任务成功
- ✦ **skill 被使用但失败**：assistant 消息中的 tool_calls 包含 `read` 且参数路径包含 skills/ 目录，但后续消息显示任务失败
- ✦ **有效规则没被遵守**：skill 中的规则是正确的，但对话中的 assistant 行为没有遵循

不需要主动扫描 skill 目录，只关注消息中呈现的信号。

### 阶段B：精加工知识图谱（按顺序执行）

对阶段A提取的实体做精加工，按步骤1→2→3→4顺序执行：

1. **精加工描述**（先做）：优化关键实体的描述
   - `lightrag_insert_entity(name, entity_type, description="实体描述内容")`
   - **实体描述内容 ≤ 80 字符**（硬性要求）
   - 描述只写实体本身的含义，不要添加 L0/L1/L2、weight、decay_rate 等元数据标签

2. **时间链**：建立事件间的时序/因果连接
   - `lightrag_insert_relation(src_id, tgt_id, relation="followed_by")` — 时间顺序
   - `lightrag_insert_relation(src_id, tgt_id, relation="corrected_by")` — 纠正
   - `lightrag_insert_relation(src_id, tgt_id, relation="led_to")` — 因果
   - `lightrag_insert_relation(src_id, tgt_id, relation="resolved_by")` — 解决

3. **脑区关联**：将实体关联到最合适的脑区
   - **先检索现有脑区**：`lightrag_search_entities(query="脑区", top_k=20)` 获取所有脑区节点
   - **判断归属**：看当前实体是否属于某个已有脑区（如已有"Python开发脑区"，新实体"FastAPI"就属于它）
   - **适合就连**：`lightrag_insert_relation(src_id="Python开发脑区", tgt_id="FastAPI", relation="包含")`
   - **不适合不强求**：没有合适的脑区时，连到默认脑区（聊天提及→`聊天历史脑区`，文档产生→`文档库脑区`，技能工具→`知识体系脑区`）
   - **不要手动创建新脑区**——同类实体连到默认脑区多了以后，Leiden 社区发现算法会自动把它们聚类成新脑区

4. **脑区归入**（最后做）：将实体归入对应脑区
   - `lightrag_insert_relation(src_id="脑区名", tgt_id=entity, relation="包含")`
   - 先用 `lightrag_search_entities` 查找实体应归入哪个脑区
   - 判断标准（需用户明确表达，不因随口一提就标注）：
     - `prefers`：用户明确表达偏好（"我喜欢..."、"我更喜欢..."、"我习惯..."）
     - `skilled_in`：用户展示专业技能（代码讨论、技术决策、问题排查），至少出现 2 次相关讨论
     - `knows_about`：用户了解某个领域（提及概念、讨论细节、给出意见），至少出现 1 次深入讨论

### 阶段C：Skill 操作（仅在阶段A观察到信号时执行）

如果阶段A没有观察到任何 skill 相关信号，跳过此阶段，直接输出报告。

如果观察到了信号，按以下步骤操作。**每次处理最多修改 3 个 skill 文件。**

#### 步骤C1：判断操作类型

根据观察到的信号，判断应该做什么：

| 信号 | 操作 |
|------|------|
| assistant 消息中明确反馈 skill 成功 | 如果 skill 状态是 draft → 改为 active |
| assistant 消息中明确反馈 skill 有问题 | 进入步骤 C2 判断 |
| 重复模式（出现 2 次以上）且无对应 skill | 创建新 skill（草稿） |
| 多轮失败后找到方案 | 创建新 skill（草稿），记录坑点 |
| skill 被使用且任务成功（无明确反馈时） | 如果 skill 状态是 draft → 改为 active |
| skill 被使用但任务失败（无明确反馈时） | 进入步骤 C2 判断 |
| 有效规则没被遵守 | 不改正文，在"执行提醒"区域添加提醒 |
| skill 被读取但未被引用 | 视为"未使用"，不触发任何操作 |

识别方法：tool 消息中 `read` 的参数路径包含 skills/ → 说明正在读取 skill 文件；读取 skill 后的 assistant 回复和后续 tool 结果反映任务是否成功。如果 skill 被读取但 assistant 的后续操作中没有引用该 skill 的内容，说明 skill 被跳过了，不应视为"使用"。

#### 步骤C2：判断 skill 失败的原因

当 skill 被使用但任务失败时，必须判断失败原因：

> **"这条规则本身有错吗？还是只是没被遵守？"**

- **规则有错/缺失/不够具体** → 修改 skill 正文
- **规则没错，只是没被遵守** → 不改正文，在"执行提醒"区域添加简短提醒，重申已有规则
- **拿不准时，默认规则没错**——不要因为一次没被遵守就改掉有效规则

#### 步骤C3：执行操作

**创建新 skill：**
1. `read` 查看工作目录下 skills/ 目录中的已有 skill，确认无重复
2. `write` 创建新文件，frontmatter 中 `status: draft`
3. 命名使用动词优先、连字符分隔（如 note-management.md）
4. 内容格式：

```markdown
---
name: skill-name-with-hyphens
description: Use when [触发条件，不写工作流]
status: draft
created: YYYY-MM-DD
last_tested: YYYY-MM-DD
---

# Skill Name

## Overview
核心原则，1-2 句话。

## When to Use
> ⚠️ 此 skill 为草稿状态，使用后请反馈效果

- 触发条件
- 不适用的情况

## Steps
关键步骤。

## Common Mistakes
常见错误和修复。

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
```

**修改已有 skill：**
1. `read` 读取目标 skill 文件
2. `edit(file_path, old_string, new_string)` 修改，old_string 必须在文件中唯一匹配
3. 修改正文时更新 `last_tested` 日期

**验证草稿 skill：**
1. `read` 读取目标 skill 文件
2. 如果信号表明该 skill 被使用且任务成功 → `edit` 把 `status: draft` 改为 `status: active`，同时删除 When to Use 区域下的 `> ⚠️ 此 skill 为草稿状态，使用后请反馈效果` 提示行
3. 如果信号表明该 skill 被使用但任务失败 → 按步骤 C2 处理

**添加执行提醒：**
1. `read` 读取目标 skill 文件
2. `edit` 在 `<!-- 执行提醒 -->` 下方添加一条简短提醒，重申已有规则（不引入新规则）
3. 如果提醒已超过 5 条，合并去重

## Skill 文件规范（知识储备）

创建或修改 skill 时遵循以下规范。

### Frontmatter 格式

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
- `name`：只含字母、数字、连字符，不用下划线、不用中文、不用空格
- `description`：以 "Use when..." 开头，**只写触发条件，不写工作流**。包含具体症状和情境，500 字符以内
- `status`：新建时写 `draft`，验证通过后改为 `active`
- `created`：创建日期
- `last_tested`：最近一次验证或修改日期

### description 写法要点

description 决定了 skill 什么时候被检索到、被使用。

```yaml
# ❌ 差：总结了工作流
description: Use when creating skills - follows RED-GREEN-REFACTOR with testing

# ❌ 差：太模糊
description: Use when working with files

# ✅ 好：只有触发条件
description: Use when processing Office documents (Word, Excel, PowerPoint) that need format conversion or content extraction
```

为什么不能写工作流：使用者看到 description 后可能直接按 description 行动而不读全文。如果 description 包含了简化版工作流，使用者会跳过详细步骤。

### 正文结构

```markdown
# Skill Name

## Overview
核心原则，1-2 句话。使用者读到这里就明白这个 skill 是干什么的。

## When to Use
- 触发条件（什么时候该用）
- 不适用的情况（什么时候不该用）

> ⚠️ 草稿 skill 会在 When to Use 区域显示"此 skill 为草稿状态，使用后请反馈效果"提示。草稿转正后删除此提示行。

## Steps
按顺序列出的操作步骤。每步写清楚做什么、用什么工具、怎么判断结果。

## Common Mistakes
使用者容易犯的错 + 正确做法。

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
```

### 执行提醒区域

每个 skill 文件末尾都有一个 `<!-- 执行提醒 -->` 区域，用 HTML 注释标记。

用途：当 skill 中的规则正确但对话中的 assistant 行为没有遵循时，不改正文，只在这里添加一条简短提醒来重申已有规则。

规则：
- 每条提醒**必须重申已有规则**，不能引入新规则
- 保持简短（一句话）
- 超过 5 条时合并去重

### 不创建 skill 的情况

- 只出现过 1 次的模式（可能是偶然）
- 标准工具用法（如"用 grep 搜索"）
- 可以用简单规则自动化的操作
- 项目特定约定（这些放 CLAUDE.md，不放 skill）
```

- [ ] **Step 4: 更新输出格式**

将输出格式部分中的：
```
Skill 维护：{n5} 个 skill 检查
```
改为：
```
Skill 操作：{n5} 个（新建: {n6}, 修改正文: {n7}, 添加提醒: {n8}, 草稿转正: {n9}）
```

如果阶段C未执行（无信号），报告：
```
Skill 操作：无信号，跳过
```

- [ ] **Step 5: 移除 Write-SKILL.md 的引用**

确认 dream-evolver.md 中不再有 `Write-SKILL` 引用。上面的替换内容已经移除了原版第 165 行的引用。

- [ ] **Step 6: 验证文件**

Run: `grep -n "主 Agent\|SkillSync\|注入" config/agents/dream-evolver.md`
Expected: 无匹配结果（"主 Agent"已被 Step 3.5 全部替换）

Run: `grep -n "^## " config/agents/dream-evolver.md`
Expected: 章节标题顺序为：知识图谱工作原理 → 工作流程 → 阶段A → 阶段B → 阶段C → Skill 文件规范 → 连接优先原则 → ...

- [ ] **Step 7: Commit**

```bash
git add config/agents/dream-evolver.md
git commit -m "feat: enhance dream-evolver with skill workflow and Skill-Aware Reflection"
```

---

### Task 5: 为现有 skill 文件添加 status/dates 和执行提醒区域

**Files:**
- Modify: `memory/skills/brain-region-management.md`
- Modify: `memory/skills/browser-automation.md`
- Modify: `memory/skills/note-management.md`
- Modify: `memory/skills/office-docs.md`
- Modify: `memory/skills/photo-face-display.md`
- Modify: `memory/skills/report-skill.md`

- [ ] **Step 1: 为每个现有 skill 文件添加 frontmatter 字段和末尾区域**

对每个文件：
1. 在 frontmatter 中添加 `status: active`、`created` 和 `last_tested` 字段
2. 在文件末尾添加执行提醒区域：

```markdown

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
```

日期使用文件最后修改日期：
- brain-region-management.md: 2026-06-16
- browser-automation.md: 2026-04-26
- note-management.md: 2026-05-22
- office-docs.md: 2026-04-26
- photo-face-display.md: 2026-06-02
- report-skill.md: 2026-06-17

具体操作：对每个文件用 `read` 读取，然后用 `edit` 修改。

- [ ] **Step 2: 验证所有 skill 文件格式**

Run: `for f in memory/skills/*.md; do echo "=== $f ==="; head -8 "$f" | grep -E "status:|created:|last_tested:"; tail -3 "$f"; echo; done`
Expected: 每个文件都显示新字段，末尾有执行提醒区域

- [ ] **Step 3: Commit**

```bash
git add memory/skills/
git commit -m "refactor: add status/dates and execution-reminder region to existing skills"
```

---

### Task 6: SkillSync 识别草稿状态

**Files:**
- Modify: `agent/injector/sync.py`

- [ ] **Step 1: 在 SkillSync 注入时，给草稿 skill 的 description 加前缀**

sync.py 的 `_inject_skill_to_lightrag` 方法已在开头调用 `fm = parse_yaml_frontmatter(content)` 解析了 frontmatter，直接复用 `fm` 变量即可。

找到 `if not description:` 守卫（约第 440 行 `if not description:`）和紧接其后的 `return False`（第 445 行）+ `logger.warning(...)` 块之后，在下一个注释行（第 447 行 `# Build triggers/tags`）**之前**，插入：

```python
            # 标记草稿 skill（fm 已在上方由 parse_yaml_frontmatter 解析）
            if isinstance(fm, dict) and fm.get("status") == "draft":
                description = f"[草稿] {description}"
```

注意：草稿 skill 创建后，SkillSync 的 watchdog 机制会在约 1 秒内检测到文件变化并同步到 LightRAG。定时扫描作为 fallback，间隔 60 秒。主 Agent 在同一轮对话中创建的草稿 skill，通常要到下一轮才能被注入并看到草稿提示。

插入位置示例（sync.py 约第 440-445 行之后）：

```python
            description = extract_description(content, fm)

            if not description:
                logger.warning(...)
                return False

            # 标记草稿 skill（fm 已在上方由 parse_yaml_frontmatter 解析）
            if isinstance(fm, dict) and fm.get("status") == "draft":
                description = f"[草稿] {description}"
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/injector/sync.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add agent/injector/sync.py
git commit -m "feat: mark draft skills with [草稿] prefix in SkillSync"
```

---

### Task 7: 主 Agent 注入草稿 skill 提示

**Files:**
- Modify: `agent/runner.py`

- [ ] **Step 1: 在 skill 注入摘要中标记草稿**

Task 7 已经在 SkillSync 注入时给草稿 skill 的 description 加了 `[草稿]` 前缀。runner.py 的 `_format_lightrag_entities_for_prompt` 方法格式化 skill 摘要时，直接检查 description 是否以 `[草稿]` 开头即可，不需要额外读文件。

在第 1374 行 `if is_skill_section:` 块内部，第 1375 行 `lines.append(f"   路径: ~/.niu/skills/{display_name}.md")` 之后，插入草稿标记：

```python
                if description.startswith("[草稿]"):
                    lines.append(f"   ⚠️ 草稿skill — 使用后反馈效果")
```

注意：草稿标记**必须**在 `if is_skill_section:` 块内部，否则所有实体（不只是 skill）只要 description 以 `[草稿]` 开头都会显示警告。`description` 变量在第 1368 行赋值，在 `if is_skill_section:` 分支内可访问。`added += 1` 保持在 `if is_skill_section:` 块外面不变。

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "feat: add draft skill indicator in main agent skill injection"
```

---

## 自审记录

### 审查修正追踪

| 审查问题 | 严重度 | 修正 |
|---------|--------|------|
| 草稿验证闭环不可靠 | Critical → 不成立 | dream-evolver 能看到 tool 消息中 read 了 skills/ 目录下的文件，路径明确可识别。已修正信号描述，明确写出了识别方法 |
| SYSTEM_MANUAL.md 遗漏引用 | Important | Task 1 增加 Step 2 清理引用 |
| runner.py 草稿提示实现模糊 | Important | Task 8 改为利用 Task 7 的 [草稿] 前缀，不额外读文件 |
| compat.py task prompt 未更新 | Minor | dream-evolver 的行为主要由系统提示词决定，task prompt 已有"并维护 skill 文件"足够。新工作流细节在系统提示词中 |
| "执行者"表述模糊 | Important | 已改为"对话中的 assistant 行为" |
| 空方法仍在调用链上 | Important | Task 3 修正为直接移除 _track_tool_execution 方法和调用点 |
| .meta-skill-guidance.md 在仓库外 git add 会失败 | → 已砍掉 | 用户确认该文件当前阶段无价值，整个 Task 6 删除 |
| skill 规范写法缺失 | Important | Task 4 增加"Skill 文件规范（知识储备）"段落 |
| 步骤C1信号表用"主 Agent"表述 | Important | 第4轮：改为"assistant 消息中明确反馈" |
| 知识图谱工作原理部分含"主 Agent" | Important | 第4轮：Task 4 增加 Step 3.5 逐项替换 |
| niu.md report-skill.md edit 规则与新策略矛盾 | Important | 第4轮：Task 2 增加 Step 2.5 改为告知用户 |
| Task 3 步骤编号跳号（1→3→4→5→6） | Important | 第4轮：重编号为 1→2→3→4→5 |
| runner.py 插入位置描述模糊 | Important | 第4轮：明确指定第1375行之后、added+=1之前 |
| 草稿 skill 文件缺少内联反馈提示 | Important | 第3轮遗留：When to Use 区域加 `> ⚠️ 此 skill 为草稿状态，使用后请反馈效果` |
| niu.md 第119-120行 report-skill.md 编辑示例残留 | Important → 不再适用 | 第6轮：主 Agent 保留写 skill 能力，report-skill.md 编辑规则无需修改 |
| report-skill.md 第132-138行"主 Agent 可修改"与新策略矛盾 | Important → 不再适用 | 第6轮：同上，主 Agent 仍可修改 skill，原规则保留 |
| Task 2 commit message 与新策略矛盾 | Important | 第6轮：改为"update skill-writing guidance" |
| sync.py 插入位置示例代码过于简化 | Important | 第6轮：精确标注行号（第440、445、447行） |
| runner.py 草稿提示应紧跟描述行而非路径行 | Important | 第6轮：插入位置改到第1371行之后、第1374行之前 |
| Task 3 Step 1(c) 行号不够精确，存在误删风险 | Important | 第6轮：明确指定删除第519行注释+第520行调用 |
| 草稿 skill 闭环存在 SkillSync 同步延迟窗口 | Important | 第6轮：Task 6 增加延迟窗口说明 |
| Step 3.5 第2项"保留不变"与第1项修改同一行自相矛盾 | Important | 第6轮：合并为第1项的注释，删除独立的第2项 |
| Step 3 大范围替换55行易出错 | Important | 第6轮：建议分两步执行 |
| runner.py 草稿提示在 is_skill_section 块外会污染非 skill 实体 | Critical | 第7轮：插入位置改到 if is_skill_section 块内部，路径行之后 |
| Task 1 验证 grep 会搜到 dream-evolver.md 的 Write-SKILL 引用导致误判 | Important | 第7轮：验证命令排除 dream-evolver.md，注明 Task 4 处理 |
| Task 3 原子操作警告不够明确 | Important | 第7轮：加⚠️强调4项修改必须同一次文件写入完成 |
| Step 3.5 第1项只给片段替换，子 Agent 可能遗漏整行上下文 | Important | 第7轮：给出完整 old_string 和 new_string |
| Step 3 和 Step 3.5 执行顺序依赖 | Important | 第7轮：明确 Step 3.5 必须在 Step 3 之前执行 |
| Task 2 Step 2 插入位置"约第138行"不够精确 | Important | 第7轮：精确到第138行之后、第140行之前 |

### 视角审查

| 问题 | 检查 |
|------|------|
| dream-evolver.md 中是否有"主 Agent"、"SkillSync"、"注入"等它不知道的概念？ | 无——全部用它能理解的表述：tool 消息中 read 了 skills/ 目录、对话中的 assistant 行为 |
| Write-SKILL.md 是否已删除？ | 是——Task 1 删除 |
| dream-evolver 是否还引用 Write-SKILL.md？ | 否——Task 4 的替换内容已移除所有引用 |
| 主 Agent 是否还能写 skill？ | 部分——可修改已有 skill，不能创建新 skill（Task 2 明确了这一边界） |
| ExperienceSummarizer 是否已关闭？ | 是——Task 3 |
| dream-evolver 是否有完整工作流？ | 是——阶段A（读消息提取信号）→ 阶段B（精加工）→ 阶段C（skill 操作，仅在有信号时），每步做什么、怎么判断都有明确指引 |
| dream-evolver 是否有 skill 规范写法？ | 是——Task 4 增加了"Skill 文件规范（知识储备）"段落，包含 frontmatter 格式、description 写法、正文结构、执行提醒区域 |
| 草稿验证闭环是否通？ | 是——主 Agent 使用 skill 后必须明确反馈效果（Task 2 新增规则），dream-evolver 优先从明确反馈中识别信号，其次从 tool 消息中推断 |

### Spec 覆盖检查

| 需求 | 对应 Task |
|------|----------|
| 关闭主 Agent 创建新 skill + 保留修改已有 skill + 增加 skill 使用反馈规则 | Task 2 |
| 关闭 ExperienceSummarizer | Task 3 |
| 删除 Write-SKILL.md + 清理引用 | Task 1 |
| dream-evolver 完整工作流（阶段A→B→C） | Task 4 |
| Skill-Aware Reflection（规则有错 vs 没被遵守） | Task 4 |
| Edit Budget（每次最多3个） | Task 4 |
| 草稿→验证→生效闭环 | Task 4 |
| Skill 规范写法（知识储备） | Task 4 |
| 现有 skill 格式升级 | Task 5 |
| SkillSync 草稿标记 | Task 6 |
| 主 Agent 注入草稿提示 | Task 7 |

### Placeholder 扫描

无 TBD/TODO/placeholder。

### 类型一致性

- frontmatter 字段名：`status`、`created`、`last_tested` — 在 Task 4（dream-evolver.md 定义）、Task 5（现有 skill 添加）、Task 6（SkillSync 读取）、Task 7（runner 通过 [草稿] 前缀间接识别）中一致使用
- 执行提醒区域标记：`<!-- 执行提醒 -->` — 在 Task 4（dream-evolver.md 定义）、Task 5（现有 skill 添加）中一致
