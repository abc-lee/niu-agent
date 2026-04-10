# 交互习惯库（Interaction Habits）设计方案

> 版本：v1.1
> 日期：2026-04-09
> 目标：通过梦境整理机制，让 Agent 从对话中学习用户的个性化表达方式和性格特征，形成自我进化的交互习惯库

---

## 1. Context

**问题背景**：

我们刚完成的 TDD 流水线生成了通用 query patterns，覆盖 80% 的通用场景。但每个用户：

- 说话方式不同（工具方言）
- 语气习惯不同（用户状态）
- 个人情况不同（个性化记忆）

**解决思路**：

复用现有的"梦境整理"机制（context-manager 睡眠整理），在 Agent 入睡时分析对话记录，学习三个维度的个性化内容，形成写-删-置信度的动态闭环。主 Agent 读取这个习惯库，构建用户性格画像，并拥有主动自我纠正的能力。

---

## 2. 核心概念

### 2.1 交互习惯库（Interaction Habits）

**定义**：记录用户个性化特征的统一知识库，包含三类内容，全部存储在向量库中，通过 metadata 区分类型。

**三类内容**：

| 类型 | 内容 | 示例 | metadata.type |
|------|------|------|--------------|
| **工具方言** | 用户说 X → 应该调用工具 Y | "赶紧叫下我" → schedule_task | `tool_dialect` |
| **用户状态** | 语气词 → 心情推断 | "赶紧" → 焦虑/催促 | `user_state` |
| **用户画像** | 关于用户的个性化记忆 | "用户家里有两只猫" | `user_profile` |

**统一存储**：所有 Interaction Habits 存在同一张表，通过 metadata 区分。

```python
# 类型1：工具方言（Tool Dialect）
{
    "id": "dialect:scheduler:schedule_task:003",
    "content": "赶紧叫下我",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "tool_dialect",
        "language": "zh",
        "target_tool": "scheduler-server/schedule_task",
        "refined_query": "schedule task",
        "source": "personal",
        "confidence": {
            "success_count": 5,
            "fail_count": 0,
            "last_used": "2026-04-09",
            "learned_from": "user_correction"  # "user_correction" | "retry_success" | "generalization"
        }
    }
}

# 类型2：用户状态（User State）
{
    "id": "state:语气:001",
    "content": "赶紧",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "user_state",
        "language": "zh",
        "state_tags": ["anxious", "impatient", "urging"],
        "description": "用户在催促，通常说明事情紧急",
        "source": "inferred",
        "confidence": {
            "success_count": 3,
            "fail_count": 0,
            "last_used": "2026-04-09"
        }
    }
}

# 类型3：用户画像（User Profile）
{
    "id": "profile:fact:003",
    "content": "用户家里有两只猫，名字叫小白和小黑",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "user_profile",
        "subtype": "fact",              # "fact" | "preference" | "habit" | "personality"
        "language": "zh",
        "description": "关于用户的个人事实",
        "source": "conversation_extract",
        "confidence": {
            "success_count": 2,
            "fail_count": 0,
            "last_verified": "2026-04-09"
        }
    }
}
```

### 2.2 置信度机制

每个 Interaction Habit 携带置信度：

| 状态 | success_count | fail_count | 行为 |
|------|--------------|-----------|------|
| 新增（未验证） | 0 | 0 | 低置信度，慎用 |
| 多次成功 | ≥3 | 0 | 高置信度，优先匹配 |
| 出现过失败 | ≥1 | ≥1 | 中置信度，持续观察 |
| 失败过多 | ≥1 | ≥3 | 自动删除或降权 |

### 2.3 完整的生命周期闭环

```
┌─────────────────────────────────────────────────────────────────┐
│                        日常对话                                   │
│  用户说 X → Agent 调用工具 Y → 结果正确/错误                       │
└──────────────────────────┬──────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│                        入睡触发                                   │
│  触发时机：/api/context/tidy (mode=sleep)                        │
└──────────────────────────┬──────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│                     梦境整理（context-manager）                    │
│                                                                  │
│  ① 工具方言提取                                                │
│     用户纠正 X → Y 成功 → 写入 dialect:X → Y                    │
│                                                                  │
│  ② 用户状态推断                                                │
│     发现语气词 Z → 推断状态 → 写入 state:Z → tags                │
│                                                                  │
│  ③ 用户画像更新                                                │
│     发现用户事实 P → 写入 profile:fact:P                          │
│                                                                  │
│  ④ 置信度检查                                                  │
│     fail_count ≥ 3 → 删除 → 标记"需重新学习"                      │
└──────────────────────────┬──────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│                  Interaction Habits 库（向量库）                    │
│                                                                  │
│  dialect:*   — 工具方言（影响递归检索匹配）                       │
│  state:*     — 用户状态（影响 Agent 语气）                       │
│  profile:*    — 用户画像（影响对话内容）                           │
└──────────────────────────┬──────────────────────────────────────┘
                           ↓
         ┌─────────────────┼─────────────────┐
         ↓                 ↓                 ↓
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   递归检索时    │ │   对话生成时    │ │   主动自我纠错  │
│  优先匹配高分   │ │  参考用户画像   │ │  Agent 发现推断  │
│  个性化方言     │ │  和当前状态     │ │  错误，主动调整  │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

---

## 3. 三类内容的详细设计

### 3.1 工具方言（Tool Dialect）

**来源**：
- 用户纠正（最可靠）："不是提醒，是取消"
- 工具调用失败后重试成功
- 通用 patterns 匹配成功后的个性化变体

**学习触发**：
```
场景1：用户纠正
  用户："不是提醒，是取消那个"
  Agent：理解了，调用 cancel_task
  梦境整理：发现 X="不是提醒，是取消那个" → 目标工具 cancel_task
  写入：dialect → cancel_task

场景2：重试成功
  用户："删掉下午3点的会"
  Agent 第1次：调用 schedule_task（错误）→ 失败
  Agent 第2次：改为 cancel_task → 成功
  梦境整理：发现 X="删掉下午3点的会" → cancel_task
  写入：dialect → cancel_task
```

**置信度更新**：
- 每次该方言被成功用于匹配工具：success_count++
- 每次匹配失败：fail_count++

### 3.2 用户状态（User State）

**来源**：从对话的语气词推断用户当前情绪状态

**学习触发**：
```
用户："赶紧叫下我"（语气急促）
Agent：正常处理
梦境整理：发现语气词"赶紧"，关联"催促/紧急"标签
写入：state → {"赶紧": ["anxious", "impatient", "urging"]}

用户："没事，慢慢来"（语气平和）
梦境整理：发现语气词"慢慢来"，关联"relaxed"标签
写入：state → {"慢慢来": ["relaxed", "patient"]}
```

**应用场景**：
- Agent 回复语气调整：用户焦虑 → 回复更简洁直接
- 优先级判断：用户紧急 → 优先处理当前任务

**状态标签库**：
```
positive: [pleased, satisfied, grateful, relaxed]
negative: [frustrated, impatient, anxious, annoyed]
urgent: [urgent, rushing, immediate, now]
neutral: [calm, normal, patient]
```

### 3.3 用户画像（User Profile）

**来源**：从对话中提取关于用户的个人事实和偏好

**子类型**：
- `fact`：事实（"用户家里有两只猫"）
- `preference`：偏好（"用户喜欢用 markdown 格式"）
- `habit`：习惯（"用户经常在下午3点开会"）
- `personality`：性格（"用户比较谨慎，会多次确认"）

**学习触发**：
```
用户："帮我找一下上次去北京拍的照片"
梦境整理：发现用户去北京旅游过
写入：profile:fact → "用户去过北京"

用户："用表格展示吧"
Agent：理解并按表格输出
梦境整理：发现用户偏好表格
写入：profile:preference → "用户喜欢用表格展示数据"
```

**应用场景**：
- 对话内容参考：Agent 知道用户有猫，就不会推荐狗粮
- 语气风格参考：用户偏好简洁 → Agent 回复简短

---

## 4. 主 Agent 的自我认知能力

### 4.1 主 Agent 读取 Interaction Habits

**读取时机**：每轮对话开始前，动态注入

```
向量库检索 → 查询 interaction_habit 类型的记录
    ↓
构建用户画像上下文：
  - 最近推断的状态：{语气词 → 状态标签}
  - 已知偏好：{偏好类别 → 具体偏好}
  - 性格特征：{性格标签}
    ↓
注入到工作记忆 → 影响本轮对话策略
```

### 4.2 主 Agent 主动自我纠正

**触发条件**：
- Agent 推断的用户状态与实际不符（用户反馈）
- Agent 使用了错误的方言匹配
- Agent 对用户的理解被明确纠正

**纠正流程**：
```
Agent 推断：用户说"还行" → 状态=neutral
用户反馈：不对，我说还行就是不满意，你没看出来吗
Agent：理解纠正 → 更新 state:"还行" → {neutral} → {dissatisfied}
    ↓
更新向量库：覆盖/新增 state 条目
```

**这意味着 Interaction Habits 不只是被动学习，Agent 可以主动改写它。**

### 4.3 系统手册中的主 Agent 指导

**在 SYSTEM_MANUAL.md 中向主 Agent 说明**：

```
## Interaction Habits — 用户交互习惯库

### 你能知道用户的什么？

通过历史对话的梦境整理，系统已经记录了以下内容（可主动查询）：

**工具方言**：
- 用户独特的表达方式 → 工具映射
- 例："赶紧叫下我" → schedule_task（置信度 0.85）

**用户状态标签**：
- 用户说话时的语气习惯
- 例："赶紧" → 推断用户可能焦虑/催促（置信度 0.7）

**用户画像**：
- 关于用户的个人事实、偏好、习惯、性格
- 例：用户家里有两只猫、喜欢表格展示

### 你能做什么？

1. **读取习惯**：对话开始时，主动检索 relevant 的习惯记录
2. **应用习惯**：根据用户画像选择合适的回复方式
3. **纠正错误**：如果你的推断被用户纠正，更新对应的记录
4. **发现新习惯**：如果发现用户用了新的表达方式，记录下来

### 格式参考

查询接口：向量库检索，metadata.category="interaction_habit"

记忆更新：通过 memory-server/recall 或 config-manager 工具更新
```

---

## 5. 梦境整理的提示词扩展

现有 `context-manager.md` 需要扩展以下内容：

### 5.1 工具方言提取

```markdown
## 工具方言提取

在对话中识别以下模式：

1. 用户纠正模式：
   用户说 X → Agent 调用了工具 Y → 用户说"不对/不是/改成"
   → 提取 X 作为方言，正确工具为 Z

2. 工具失败模式：
   用户说 X → 工具 Y 调用失败 → 后续成功
   → 提取 X 作为方言，正确工具为最终成功的工具

3. 表达多样性：
   同一意图用不同表达 → 识别用户偏好的表达方式

对每条提取的方言：
- 写入向量库，metadata.type="tool_dialect"
- 设置 source="personal"，confidence={success_count:1, fail_count:0}
```

### 5.2 用户状态推断

```markdown
## 用户状态推断

从对话语气词推断用户当前状态：

语气词 → 状态标签映射（需记录）：
- "赶紧/快点/马上" → anxious, impatient, urgent
- "没事/慢慢来/不急" → relaxed, patient
- "谢谢/好的/可以" → positive, satisfied
- "不对/不是/错" → frustrated, correcting

对每次推断：
- 写入向量库，metadata.type="user_state"
- 标注语气词、推断的状态标签、置信度
```

### 5.3 用户画像提取

```markdown
## 用户画像提取

从对话中识别：

1. 个人事实：用户提到的家人、宠物、地点、事件
2. 偏好表达：用户说"我喜欢/我不喜欢/我习惯"
3. 行为习惯：用户反复做的行为模式
4. 性格特征：用户的沟通风格、确认习惯、决策方式

对每条画像：
- 判断 subtype（fact/preference/habit/personality）
- 写入向量库，metadata.type="user_profile"
- 记录来源对话（用于回溯验证）
```

---

## 6. 与现有系统的关系

| 组件 | 关系 | 说明 |
|------|------|------|
| 向量库 documents 表 | 共用存储 | 通用 + 个性化 Interaction Habits 统一存储 |
| TDD 流水线 | 初始化 | 生成通用 tool_dialect 作为基础 |
| SkillSync | 共用同步 | Interaction Habits 通过同一机制同步 |
| ExperienceSummarizer | 扩展 | 从"错误提取 skill"扩展到三类内容 |
| context-manager | 核心 | 梦境整理是主动学习的触发时机 |
| SYSTEM_MANUAL.md | 指导文档 | 主 Agent 读取，理解如何应用和更新习惯库 |

---

## 7. 置信度机制（扩展版）

### 7.1 通用规则

```python
def update_confidence(record, result: str):
    """result: "success" | "fail" | "verify" """
    if result == "success":
        record.confidence.success_count += 1
    elif result == "fail":
        record.confidence.fail_count += 1

    # 删除条件：失败次数达到阈值的 3 倍于成功次数
    if record.confidence.fail_count >= 3 * record.confidence.success_count:
        delete_record(record)
        mark_for_relearning(record)
```

### 7.2 时间衰减

```python
def apply_decay(record, days_threshold=30):
    """超过 30 天未使用的记录，置信度衰减"""
    if days_since(record.confidence.last_used) > days_threshold:
        record.confidence.success_count = max(0, record.confidence.success_count - 1)
```

### 7.3 来源优先级

不同来源的初始置信度不同：

| 来源 | 初始置信度 | 说明 |
|------|-----------|------|
| user_correction | 高（3次成功才降权） | 用户明确纠正，最可靠 |
| retry_success | 中（2次成功才降权） | 重试成功，有一定验证 |
| generalization | 低（1次失败就警告） | Agent 推断，需要验证 |
| conversation_extract | 中（需后续验证） | 从对话中提取 |

---

## 8. 实现计划

### Phase 1：基础设施（最小可用）

1. **扩展 context-manager.md**：添加三类内容（工具方言、用户状态、用户画像）的提取提示词
2. **添加 metadata 类型**：向现有 `documents` 表添加 type 字段（tool_dialect / user_state / user_profile）
3. **添加置信度更新**：在工具调用成功后更新 confidence
4. **添加自动删除**：fail_count ≥ 3 时自动删除

### Phase 2：主 Agent 集成

1. **更新 SYSTEM_MANUAL.md**：添加 Interaction Habits 章节，说明主 Agent 如何读取和应用
2. **添加读取接口**：在 runner.py 的动态注入中添加 interaction_habit 查询
3. **添加工具调用后更新**：每次工具调用后更新对应 dialect 的置信度

### Phase 3：主动学习

1. **Agent 自我纠正**：Agent 发现推断错误时主动更新记录
2. **主动确认**：用户表达不明确时，Agent 主动询问并记录
3. **批量学习**：一次梦境整理分析整天对话

---

## 9. 关键文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `config/agents/context-manager.md` | 修改 | 扩展三类内容提取提示词 |
| `docs/SYSTEM_MANUAL.md` | 修改 | 添加 Interaction Habits 章节 |
| `agent/vector_search.py` | 修改 | 添加 interaction_habit 类型检索 |
| `agent/runner.py` | 修改 | 动态注入中查询 interaction_habit |
| `agent/handler.py` | 修改 | 工具调用后更新置信度 |

---

## 10. 风险和缓解

| 风险 | 缓解 |
|------|------|
| 错误推断被记录为"事实" | 通过置信度和用户纠正机制双重校验 |
| 用户画像过时 | 添加 last_verified 时间，定期重新确认 |
| 状态推断过于主观 | 只记录观察到的事实（语气词），不直接标记情绪 |
| 习惯库膨胀 | 限制每类记录上限（tool_dialect 每个工具最多 20 条） |
