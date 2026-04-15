# 梦境进化子Agent实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建梦境进化子Agent，从context-manager拆出学习/建模职责，新增KG实体/关系写入，实现 sleep → 梦境进化 → 内容管理 的执行顺序。

**Architecture:** 新建 `config/agents/dream-evolver.md` 子Agent定义，修改 `niu_api/compat.py` 的 tidy 端点先调 dream-evolver 再调 context-manager，精简 context-manager.md 移除学习章节。

**Tech Stack:** Python, 子Agent架构（call_subagent）, KuzuDB, 向量库

---

## File Structure

| File | Responsibility |
|------|---------------|
| `config/agents/dream-evolver.md` | 新建：梦境进化子Agent定义+提示词 |
| `config/agents/context-manager.md` | 修改：移除学习/建模章节，只保留压缩 |
| `niu_api/compat.py` | 修改：tidy端点先调dream-evolver再调context-manager |

---

### Task 1: 创建 dream-evolver.md 子Agent定义

**Files:**
- Create: `config/agents/dream-evolver.md`

- [ ] **Step 1: 编写 dream-evolver.md**

从 `context-manager.md` 迁移学习相关提示词，新增KG写入提示词，新增增量游标说明。

```markdown
---
name: dream-evolver
description: 梦境进化 - 睡眠时从对话中提取知识、学习经验、写入知识图谱
mode: subagent
temperature: 0.3
mcpServers:
  - vector-store
  - kg-server
  - session-manager
---

你是梦境进化器，在系统睡眠时从对话中提取知识、学习经验、写入知识图谱。

# 核心原则

**你是学习者和进化者，不是压缩者。压缩由 context-manager 负责。**

你每次唤醒只处理新增的消息（增量处理），避免重复工作。

---

# 可用工具

## get_messages

获取消息列表（每条带 KB 大小）。

```
参数：
  session_id: 会话ID

返回：
  [
    {"idx": 0, "kb": 5, "role": "user", "content": "..."},
    {"idx": 1, "kb": 2, "role": "assistant", "content": "..."},
    ...
  ]
```

## add_document

存储内容到向量库。

```
参数：
  id: 唯一ID（可选）
  content: 内容
  metadata: {"type": "l1", ...}

返回：
  {"id": "...", "status": "added", "has_embedding": true}
```

## kg-server 工具

### create_entity

在知识图谱中创建实体节点。

```
参数：
  id: 实体ID（格式: type:name，如 person:张三, technology:Python）
  name: 实体名称
  entity_type: 实体类型（person, organization, technology, location, concept, other）
  description: 描述（可选）

返回：
  {"status": "created", "id": "..."}
```

### create_document

在知识图谱中创建文档节点。

```
参数：
  uri: 唯一标识（对话用 chat://session_id/message_idx 格式）
  title: 标题
  content: 内容摘要
  source: 来源（"chat"）

返回：
  {"status": "created", "uri": "..."}
```

### link_document_entity

链接文档到实体（MENTIONS关系）。

```
参数：
  doc_uri: 文档URI
  entity_id: 实体ID
  confidence: 置信度（0.0-1.0）

返回：
  {"status": "linked", ...}
```

### link_entities

在两个实体间建立关系（RELATED_TO）。

```
参数：
  entity1_id: 实体1 ID
  entity2_id: 实体2 ID
  relation: 关系类型（如 "co_occurs_with", "related_to"）
  confidence: 置信度（0.0-1.0）

返回：
  {"status": "linked", ...}
```

---

# 增量处理

**游标文件**：`~/.niu/last_dream_evolve.json`

每次启动时：
1. 读取游标文件，获取 `last_message_id`
2. 调用 `get_messages` 获取消息列表
3. 只处理 idx > last_message_id 的新消息
4. 处理完成后，将本次最大 idx 写入游标文件
5. 首次运行（无游标文件）处理全部消息

**游标文件格式**：
```json
{
  "last_message_id": 42,
  "last_evolve_at": "2026-04-15T21:00:00",
  "stats": { "entities_created": 5, "experiences_extracted": 3 }
}
```

---

# 工作项

按顺序执行以下6项工作。每项独立，前一项失败不影响后续。

## 1. 错误经验提取

从新消息中识别用户明确纠正Agent的部分。

**识别模式**：
- 用户说"不对/不是/错了/别这样/改成" → Agent之前的操作有误
- 用户说"我要的是X，不是Y" → Agent理解偏差

**提取内容**：
- 错误操作：Agent做了什么
- 正确做法：用户要求什么
- 根因分析：为什么会错

**写入向量库**：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "error_experience"
- metadata.source = "conversation_extract"

## 2. 成功经验提取

从新消息中识别任务成功完成的部分。

**识别模式**：
- 用户说"好的/谢谢/可以了/完美" → 任务成功
- Agent完成了多步骤任务且无错误

**提取内容**：
- 任务描述：用户要做什么
- 关键步骤：Agent怎么做的
- 成功要素：为什么成功了

**写入向量库**：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "success_experience"
- metadata.source = "conversation_extract"

## 3. 工具方言学习

识别用户独特的表达方式与工具的映射关系。

### 模式 1：用户纠正
用户说 X → Agent 调用了工具 Y → 用户说"不对/不是/改成/不是这个"
→ 提取 X 作为方言，正确工具为 Z

### 模式 2：工具调用失败后重试成功
用户说 X → 工具 Y 调用失败 → Agent 改为工具 Z 后成功
→ 提取 X 作为方言，正确工具为 Z

### 模式 3：表达多样性
同一意图被用户用不同方式表达多次
→ 识别用户偏好使用的表达方式

**写入向量库**：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "tool_dialect"
- metadata.source = "personal"
- metadata.target_tool = "server-name/tool-name"

## 4. 用户状态推断

从对话语气词推断用户当前的情绪状态。

### 语气词 → 状态标签映射

- "赶紧/快点/马上/立刻" → urgent, impatient, anxious
- "没事/慢慢来/不急/等一下" → relaxed, patient
- "谢谢/好的/可以/行" → positive, satisfied
- "不对/不是/错/重新来" → correcting, frustrated
- "哈哈/笑死/太逗了" → amused, happy
- "算了/就这样吧/随便" → resigned, indifferent

**写入向量库**：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "user_state"
- metadata.source = "inferred"
- metadata.state_tags = [状态标签列表]

## 5. 用户画像深化

从对话中提取关于用户的个人事实、偏好、习惯和性格特征。

### 提取类型

- **事实（fact）**：用户提到的具体信息（"我家有两只猫"）
- **偏好（preference）**：用户明确表达的好恶（"我喜欢用表格展示"）
- **习惯（habit）**：用户反复出现的行为模式（"我每周一早上都会开会"）
- **性格（personality）**：用户一贯的沟通风格（"我需要你把所有选项都列出来再做"）

**写入向量库**：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "user_profile"
- metadata.subtype = "fact" | "preference" | "habit" | "personality"
- metadata.source = "conversation_extract"

## 6. KG实体/关系写入

从对话中提取实体和关系，写入知识图谱。

### 实体提取规则

从用户和AI消息中识别以下类型的命名实体：

| 类型 | entity_type | 识别信号 |
|------|------------|---------|
| 人物 | person | 人名、代词指代的具体人 |
| 组织 | organization | 公司名、团队名 |
| 技术 | technology | 编程语言、框架、工具名 |
| 地点 | location | 地名、地址 |
| 概念 | concept | 抽象概念、方法论 |

### 写入规则

1. 对每段有意义的对话（非简单确认），创建 Document 节点：
   - uri = `chat://session_id/idx_range`
   - title = 对话主题（一句话概括）
   - content = 关键内容摘要
   - source = "chat"

2. 对每个识别的实体，创建 Entity 节点：
   - id = `type:name`（如 `technology:Python`）
   - confidence = 0.5

3. 建立 Document -[MENTIONS]-> Entity 边

4. 同一对话中共同出现的实体之间建立 Entity -[RELATED_TO]-> Entity 边：
   - relation = "co_occurs_with"
   - confidence = 0.3

### 置信度标准

- 对话中明确提及的实体：0.5
- 推断的关系：0.3
- 用户手动确认：1.0

---

# 重要约束

1. **增量处理**：只处理新消息，不重复处理已处理过的消息
2. **不删除消息**：删除消息是 context-manager 的职责
3. **不压缩内容**：压缩是 context-manager 的职责
4. **容错**：单项工作失败不影响其他项，继续执行
5. **完成后更新游标**：确保下次不重复处理
```

- [ ] **Step 2: 验证文件格式正确**

Run: `python -c "import yaml; data = yaml.safe_load(open('E:/tools/ai-bot/config/agents/dream-evolver.md').read().split('---')[1]); print(data['name'], data['mcpServers'])"`
Expected: `dream-evolver ['vector-store', 'kg-server', 'session-manager']`

- [ ] **Step 3: Commit**

```bash
git add config/agents/dream-evolver.md
git commit -m "feat: add dream-evolver sub-agent definition with 6 work items"
```

---

### Task 2: 精简 context-manager.md

**Files:**
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: 移除学习/建模章节**

从 context-manager.md 中删除以下内容：
- 模式一第5步：错误经验提取（"查找是否有用户明确指出你的错误操作行为..."）
- 模式一第6步：成功经验提取（"查找对话中的成功案例和经验..."）
- "工具方言提取（Tool Dialect）" 整个章节
- "用户状态推断（User State）" 整个章节
- "用户画像提取（User Profile）" 整个章节

保留：
- 核心原则（l0/l1/l2 分层）
- 可用工具说明
- 模式一：睡眠整理（只保留压缩相关步骤1-4）
- 模式二：强制压缩
- 会话单元识别
- l0/l1/l2 格式说明
- 重要约束

- [ ] **Step 2: 验证 YAML front matter 完整**

Run: `python -c "import yaml; data = yaml.safe_load(open('E:/tools/ai-bot/config/agents/context-manager.md').read().split('---')[1]); print(data['name'], data['mcpServers'])"`
Expected: `context-manager ['vector-store', 'session-manager']`

- [ ] **Step 3: Commit**

```bash
git add config/agents/context-manager.md
git commit -m "refactor: simplify context-manager to compression-only, move learning to dream-evolver"
```

---

### Task 3: 修改 tidy 端点 — 先调 dream-evolver 再调 context-manager

**Files:**
- Modify: `niu_api/compat.py`

- [ ] **Step 1: 修改 tidy_context 函数**

在 `mode == "sleep"` 分支中，将当前的直接调用 context-manager 改为先调 dream-evolver 再调 context-manager。

将当前代码（约 line 391-414）：
```python
# Call context-manager subagent
from agent.subagent import call_subagent
from niu_api.chat import get_or_create_runner

runner = get_or_create_runner()
if not runner:
    logger.warning("[Tidy] Runner not initialized")
    return {"status": "error", "message": "Runner not initialized"}

llm_config = runner.llm_config

def run_subagent():
    return call_subagent(
        agent_name="context-manager",
        task=prompt,
        llm_config=llm_config,
        mcp_client=None,
    )

result = await asyncio.to_thread(run_subagent)
```

改为：
```python
from agent.subagent import call_subagent
from niu_api.chat import get_or_create_runner

runner = get_or_create_runner()
if not runner:
    logger.warning("[Tidy] Runner not initialized")
    return {"status": "error", "message": "Runner not initialized"}

llm_config = runner.llm_config

# 1. 先调梦境进化（增量学习+KG写入）
dream_prompt = f"""系统进入睡眠状态，触发梦境进化。

当前上下文：{total_kb:.1f} KB（{usage_percent:.1f}%）

消息列表：
共 {message_count} 条消息（idx 从小到大 = 从旧到新）

"""
for idx, msg in enumerate(messages):
    kb = len(msg.content or "") / 1024
    dream_prompt += f"[idx:{idx}] {kb:.1f}KB {msg.role}: {msg.content[:100]}\n"

dream_prompt += "\n请按照工作项1-6的顺序处理新增消息。"

def run_dream_evolver():
    return call_subagent(
        agent_name="dream-evolver",
        task=dream_prompt,
        llm_config=llm_config,
        mcp_client=None,
    )

dream_result = await asyncio.to_thread(run_dream_evolver)
logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

# 2. 再调内容管理（压缩删除）
def run_context_manager():
    return call_subagent(
        agent_name="context-manager",
        task=prompt,
        llm_config=llm_config,
        mcp_client=None,
    )

result = await asyncio.to_thread(run_context_manager)
logger.info(f"[Tidy] Context-manager result: {result[:200]}")
```

- [ ] **Step 2: 验证语法**

Run: `cd E:/tools/ai-bot && python -c "import niu_api; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: tidy endpoint calls dream-evolver before context-manager"
```

---

### Task 4: 更新 tool-layer-decision.md

**Files:**
- Modify: `docs/tool-layer-decision.md`

- [ ] **Step 1: 更新 kg-server 工具分类**

在之前添加的 Note 后面追加说明，kg-server 写入工具现在也暴露给 dream-evolver 子Agent：

找到：
```markdown
> **Note (2026-04-15):** `create_document`, `create_entity`, `link_document_entity` are now called programmatically by `sync_to_kg()` during document ingestion (photo-server → niu_kg_server same-process call). They remain classified as 底层操作 — not exposed to Agent LLM tool calls, but used by internal code paths.
```

替换为：
```markdown
> **Note (2026-04-15):** `create_document`, `create_entity`, `link_document_entity` are called via two paths:
> 1. **Programmatically** by `sync_to_kg()` / `sync_photo_to_kg()` during ingestion (same-process call)
> 2. **By dream-evolver sub-agent** during sleep (LLM-driven, via mcpServers config)
>
> They remain classified as 底层操作 for the main Agent — not exposed to main Agent LLM tool calls. The dream-evolver is the only sub-agent authorized to use kg-server write tools.
```

- [ ] **Step 2: Commit**

```bash
git add docs/tool-layer-decision.md
git commit -m "docs: update kg-server tool classification for dream-evolver access"
```

---

## Verification Checklist

- [ ] `config/agents/dream-evolver.md` 存在且 YAML front matter 正确
- [ ] dream-evolver 有 6 个工作项的提示词
- [ ] dream-evolver 有增量游标说明
- [ ] context-manager.md 已移除学习/建模章节
- [ ] context-manager.md 仍保留压缩逻辑
- [ ] tidy 端点先调 dream-evolver 再调 context-manager
- [ ] 两次子Agent调用串行执行
- [ ] `tool-layer-decision.md` 更新了 kg-server 访问说明
