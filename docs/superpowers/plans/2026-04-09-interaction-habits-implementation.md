# Interaction Habits 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Interaction Habits（交互习惯库），让 Agent 在梦境整理时学习三类个性化内容（工具方言、用户状态、用户画像），主 Agent 读取并主动应用。

**Architecture:** 扩展现有 context-manager 提示词 + 向量库 metadata.type 新增类型 + runner.py 动态注入 + handler.py 置信度更新。无需新建表结构，通过 metadata JSON 扩展现有 documents 表。

**Tech Stack:** Python 3.11+, SQLite（向量库）, loguru

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `config/agents/context-manager.md` | 修改 | 扩展三类内容提取提示词 |
| `docs/SYSTEM_MANUAL.md` | 修改 | 添加 Interaction Habits 章节 |
| `agent/vector_search.py` | 修改 | 新增 interaction_habit 检索和置信度更新函数 |
| `agent/runner.py` | 修改 | 动态注入中添加 interaction_habit 查询 |
| `agent/handler.py` | 修改 | 工具调用成功后更新置信度 |

---

## Task 1: 扩展 context-manager.md 提示词

**Files:**
- Modify: `config/agents/context-manager.md`
- Reference: `docs/superpowers/specs/2026-04-09-interaction-habits-design.md` 第 5 节

- [ ] **Step 1: 读取当前 context-manager.md 内容**

```bash
cat E:/tools/ai-bot/config/agents/context-manager.md
```

- [ ] **Step 2: 在现有内容基础上追加三个新章节**

在文件末尾找到 "## 查找是否有用户明确指出你的错误操作行为" 这一节，在其**之后**添加三个新章节：

```markdown
---

## 工具方言提取（Tool Dialect）

在对话中识别以下模式，提取用户独特的表达方式与工具的映射关系：

### 模式 1：用户纠正
用户说 X → Agent 调用了工具 Y → 用户说"不对/不是/改成/不是这个"
→ 提取 X 作为方言，正确工具为 Z

### 模式 2：工具调用失败后重试成功
用户说 X → 工具 Y 调用失败 → Agent 改为工具 Z 后成功
→ 提取 X 作为方言，正确工具为 Z

### 模式 3：表达多样性
同一意图被用户用不同方式表达多次
→ 识别用户偏好使用的表达方式

对每条提取的方言，写入向量库：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "tool_dialect"
- metadata.source = "personal"
- metadata.confidence = {"success_count": 1, "fail_count": 0}
- metadata.target_tool = "server-name/tool-name"
- metadata.refined_query = 对应的 refined_query

---

## 用户状态推断（User State）

从对话语气词推断用户当前的情绪状态：

### 语气词 → 状态标签映射

以下语气词出现时，记录对应的状态标签：
- "赶紧/快点/马上/立刻" → urgent, impatient, anxious
- "没事/慢慢来/不急/等一下" → relaxed, patient
- "谢谢/好的/可以/行" → positive, satisfied
- "不对/不是/错/重新来" → correcting, frustrated
- "哈哈/笑死/太逗了" → amused, happy
- "算了/就这样吧/随便" → resigned, indifferent

### 记录要求
- 只记录观察到的语气词，不做主观情绪推断
- 标注语气词出现的次数和场景
- 记录用户当前是否有待处理的任务（语气急促可能因为任务堆积）

对每条状态记录，写入向量库：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "user_state"
- metadata.source = "inferred"
- metadata.confidence = {"success_count": 1, "fail_count": 0}
- metadata.state_tags = [推断的状态标签列表]

---

## 用户画像提取（User Profile）

从对话中提取关于用户的个人事实、偏好、习惯和性格特征。

### 提取类型

**事实（fact）**：用户提到的具体信息
- "我家有两只猫" → profile:fact
- "我明天要去北京出差" → profile:fact

**偏好（preference）**：用户明确表达的好恶
- "我喜欢用表格展示" → profile:preference
- "我不喜欢太长" → profile:preference

**习惯（habit）**：用户反复出现的行为模式
- "我每周一早上都会开会" → profile:habit

**性格（personality）**：用户一贯的沟通风格
- "我需要你把所有选项都列出来再做" → profile:personality

### 记录要求
- 只记录从对话中**明确推断**的内容，不做猜测
- 记录来源对话（用于回溯验证）
- 如果发现之前记录的事实被推翻，删除旧记录

对每条画像，写入向量库：
- metadata.level = "l1"
- metadata.category = "interaction_habit"
- metadata.type = "user_profile"
- metadata.subtype = "fact" | "preference" | "habit" | "personality"
- metadata.source = "conversation_extract"
- metadata.confidence = {"success_count": 1, "fail_count": 0}
- metadata.conversation_id = 来源对话 ID
```

- [ ] **Step 3: 验证修改**
```bash
wc -l E:/tools/ai-bot/config/agents/context-manager.md
# 预期：原来约 200 行，增加约 120 行
```

- [ ] **Step 4: Commit**
```bash
cd E:/tools/ai-bot
git add config/agents/context-manager.md
git commit -m "feat(interaction-habits): 扩展context-manager梦境整理，支持工具方言/用户状态/用户画像提取"
```

---

## Task 2: 向量库支持 interaction_habit 检索

**Files:**
- Modify: `agent/vector_search.py`
- Test: `E:/tools/ai-bot/agent/vector_search.py`

- [ ] **Step 1: 读取当前 vector_search.py 的 search() 方法实现**

```bash
sed -n '113,200p' E:/tools/ai-bot/agent/vector_search.py
```

- [ ] **Step 2: 在 VectorSearchAdapter 类中添加三个新方法**

在 `search()` 方法**之后**、`_search_once()` 方法**之前**添加：

```python
    def upsert_interaction_habit(
        self,
        habit_type: str,           # "tool_dialect" | "user_state" | "user_profile"
        content: str,
        metadata: dict,
        habit_id: str = None
    ) -> bool:
        """
        写入或更新 Interaction Habit 到向量库

        Args:
            habit_type: habit type (tool_dialect/user_state/user_profile)
            content: 习惯内容
            metadata: 必须包含 confidence, source, level="l1", category="interaction_habit"
            habit_id: 可选，指定 ID（格式: {type}:{subtype}:{counter}）

        Returns:
            是否成功
        """
        if habit_id is None:
            counter = int(time.time() * 1000) % 100000
            habit_id = f"habit:{habit_type}:{counter}"

        full_metadata = {
            "level": "l1",
            "category": "interaction_habit",
            **metadata
        }

        embedding = self._get_embedding(content)
        if embedding is None:
            return False

        vec = np.array(embedding, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        embedding_blob = vec.tobytes()

        conn = self._get_connection()
        if conn is None:
            return False

        conn.execute(
            """
            INSERT INTO documents (id, content, embedding, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                embedding = excluded.embedding,
                metadata = excluded.metadata
            """,
            (habit_id, content, embedding_blob, json.dumps(full_metadata, ensure_ascii=False)),
        )
        conn.commit()
        return True

    def search_interaction_habits(
        self,
        query: str,
        habit_type: str = None,  # None = all types
        limit: int = 5,
        min_score: float = 0.4
    ) -> list[SearchResult]:
        """
        检索 Interaction Habits

        Args:
            query: 搜索内容
            habit_type: 筛选特定类型（tool_dialect/user_state/user_profile）
            limit: 返回数量
            min_score: 最低分数

        Returns:
            匹配的 SearchResult 列表
        """
        filter_dict = {"level": "l1", "category": "interaction_habit"}
        if habit_type:
            filter_dict["type"] = habit_type
        return self.search(query, limit=limit, min_score=min_score, filter=filter_dict)

    def update_habit_confidence(
        self,
        habit_id: str,
        result: str  # "success" | "fail"
    ) -> bool:
        """
        更新 Interaction Habit 的置信度

        Args:
            habit_id: habit 记录 ID
            result: 调用结果

        Returns:
            是否成功
        """
        conn = self._get_connection()
        if conn is None:
            return False

        # 读取现有 metadata
        row = conn.execute(
            "SELECT metadata FROM documents WHERE id = ?", (habit_id,)
        ).fetchone()
        if not row:
            return False

        metadata = json.loads(row[0])
        conf = metadata.get("confidence", {})

        if result == "success":
            conf["success_count"] = conf.get("success_count", 0) + 1
        elif result == "fail":
            conf["fail_count"] = conf.get("fail_count", 0) + 1

        conf["last_used"] = time.strftime("%Y-%m-%d")
        metadata["confidence"] = conf

        # 删除条件：fail_count >= 3
        if conf.get("fail_count", 0) >= 3:
            conn.execute("DELETE FROM documents WHERE id = ?", (habit_id,))
            conn.commit()
            print(f"[InteractionHabits] Deleted low-confidence habit: {habit_id}", flush=True)
            return True

        # 更新时间戳
        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), habit_id)
        )
        conn.commit()
        return True
```

**注意**：需要确保 `import time` 在文件顶部存在（查看第 10-20 行）。

- [ ] **Step 3: 验证新方法可用**

```bash
cd E:/tools/ai-bot
python -c "
import sys; sys.path.insert(0, 'agent')
from vector_search import VectorSearchAdapter
vs = VectorSearchAdapter()
# 测试新方法存在
print('upsert_interaction_habit:', hasattr(vs, 'upsert_interaction_habit'))
print('search_interaction_habits:', hasattr(vs, 'search_interaction_habits'))
print('update_habit_confidence:', hasattr(vs, 'update_habit_confidence'))
"
```

- [ ] **Step 4: Commit**
```bash
cd E:/tools/ai-bot
git add agent/vector_search.py
git commit -m "feat(interaction-habits): 向量库添加interaction_habit检索和置信度更新支持"
```

---

## Task 3: runner.py 动态注入 interaction_habits

**Files:**
- Modify: `agent/runner.py`
- Reference: `agent/runner.py` 第 276-325 行的 `_inject_dynamic_resources()`

- [ ] **Step 1: 读取 runner.py 的 _inject_dynamic_resources() 方法**

```bash
sed -n '276,330p' E:/tools/ai-bot/agent/runner.py
```

- [ ] **Step 2: 在 _inject_dynamic_resources() 方法末尾添加 interaction_habits 检索**

在方法中找到最后一组搜索（Knowledge/documents），在这组**之后**添加：

```python
        # Interaction Habits 检索（用户画像、状态、工具方言）
        parts.append("\n")
        interaction_habits = self.vector_search.search_interaction_habits(
            query=user_input, limit=3, min_score=0.4
        )
        if interaction_habits:
            parts.append(format_resources_for_prompt(interaction_habits, "交互习惯"))
            print(f"[Debug] Dynamic injection - Interaction Habits: {len(interaction_habits)} results", file=sys.stderr, flush=True)
```

- [ ] **Step 3: Commit**
```bash
cd E:/tools/ai-bot
git add agent/runner.py
git commit -m "feat(interaction-habits): 动态注入中添加interaction_habits检索"
```

---

## Task 4: handler.py 工具调用后更新置信度

**Files:**
- Modify: `agent/handler.py`
- Reference: `agent/handler.py` 第 240 行的 `tool_after_callback()`

- [ ] **Step 1: 读取 handler.py 的 tool_after_callback() 方法**

```bash
sed -n '240,280p' E:/tools/ai-bot/agent/handler.py
```

- [ ] **Step 2: 在 tool_after_callback() 方法末尾添加置信度更新**

在 `tool_after_callback(self, tool_name, args, response, ret)` 方法的最后添加：

```python
        # 更新 Interaction Habits 置信度
        try:
            from ..vector_search import VectorSearchAdapter
            import json as _json

            vs = VectorSearchAdapter()

            # 查找对应工具方言记录并更新
            if hasattr(ret, 'status') and ret.status == "success":
                # 工具调用成功，更新所有相关 dialect 的置信度
                dialect_results = vs.search_interaction_habits(
                    query=str(args), habit_type="tool_dialect", limit=10, min_score=0.3
                )
                for r in dialect_results:
                    if r.metadata.get("target_tool") == tool_name:
                        vs.update_habit_confidence(r.id, "success")

            elif hasattr(ret, 'status') and ret.status == "error":
                # 工具调用失败
                dialect_results = vs.search_interaction_habits(
                    query=str(args), habit_type="tool_dialect", limit=5, min_score=0.3
                )
                for r in dialect_results:
                    if r.metadata.get("target_tool") == tool_name:
                        vs.update_habit_confidence(r.id, "fail")

        except Exception as e:
            # 置信度更新失败不影响主流程
            print(f"[InteractionHabits] Confidence update failed: {e}", flush=True)
```

- [ ] **Step 3: Commit**
```bash
cd E:/tools/ai-bot
git add agent/handler.py
git commit -m "feat(interaction-habits): 工具调用成功后更新对应dialect的置信度"
```

---

## Task 5: 更新 SYSTEM_MANUAL.md 添加 Interaction Habits 章节

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`
- Reference: `docs/superpowers/specs/2026-04-09-interaction-habits-design.md` 第 4.3 节

- [ ] **Step 1: 读取 SYSTEM_MANUAL.md 的当前结构（目录和第3章）**

```bash
head -100 E:/tools/ai-bot/docs/SYSTEM_MANUAL.md
```

- [ ] **Step 2: 在 SYSTEM_MANUAL.md 中添加新章节**

在第 3 章（向量库系统）的第 3.3 节（文档类型）表格**之后**、第 3.4 节（初始化脚本）**之前**添加：

```markdown
### 3.4 交互习惯库（Interaction Habits）

Interaction Habits 是向量库中的第三类个性化记录，记录用户独特的表达方式和性格特征。

#### 三类内容

| 类型 | metadata.type | 说明 |
|------|-------------|------|
| 工具方言 | `tool_dialect` | 用户独特的表达方式 → 工具映射 |
| 用户状态 | `user_state` | 语气词 → 情绪状态推断 |
| 用户画像 | `user_profile` | 关于用户的个人事实、偏好、习惯、性格 |

#### 数据结构

```python
# 工具方言示例
{
    "id": "habit:tool_dialect:123456",
    "content": "赶紧叫下我",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "tool_dialect",
        "target_tool": "scheduler-server/schedule_task",
        "source": "personal",
        "confidence": {
            "success_count": 3,
            "fail_count": 0,
            "last_used": "2026-04-09"
        }
    }
}

# 用户状态示例
{
    "id": "habit:user_state:789012",
    "content": "赶紧",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "user_state",
        "state_tags": ["anxious", "impatient"],
        "source": "inferred"
    }
}

# 用户画像示例
{
    "id": "habit:user_profile:345678",
    "content": "用户家里有两只猫",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "user_profile",
        "subtype": "fact",
        "source": "conversation_extract"
    }
}
```

#### 学习机制

Agent 在睡眠整理（context-manager）时，从对话中学习：

1. **工具方言**：用户纠正 Agent 的工具调用时，学习用户的表达方式
2. **用户状态**：从语气词推断用户的情绪状态（紧迫、平和等）
3. **用户画像**：从对话中提取关于用户的个人事实和偏好

#### 置信度机制

每个 Interaction Habit 携带置信度：
- `success_count`：成功匹配/验证次数
- `fail_count`：失败次数
- 当 `fail_count >= 3` 时，自动删除该记录

#### 查询接口

主 Agent 在每轮对话时，通过 `_inject_dynamic_resources()` 查询 relevant 的 Interaction Habits：
```python
vs.search_interaction_habits(query=user_input, habit_type=None, limit=3)
```

#### 主 Agent 职责

主 Agent 可以：
1. **读取**：对话开始时检索 relevant 的习惯记录
2. **应用**：根据用户画像选择合适的回复方式
3. **纠正**：如果推断被用户纠正，主动更新对应记录
4. **发现**：发现新表达方式时，通过工具调用写入向量库
```

- [ ] **Step 3: Commit**
```bash
cd E:/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "feat(interaction-habits): 系统手册添加Interaction Habits章节"
```

---

## Task 6: 功能验证测试

**Files:**
- Create: `scripts/test_interaction_habits.py`

- [ ] **Step 1: 创建验证脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interaction Habits 功能验证"""
import sys
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, "E:/tools/ai-bot")
from agent.vector_search import VectorSearchAdapter

print("=" * 60)
print("Interaction Habits 功能验证")
print("=" * 60)

vs = VectorSearchAdapter()

# 测试 1：写入工具方言
print("\n[测试1] 写入工具方言...")
success = vs.upsert_interaction_habit(
    habit_type="tool_dialect",
    content="赶紧叫下我",
    metadata={
        "target_tool": "scheduler-server/schedule_task",
        "refined_query": "schedule task",
        "source": "personal",
        "confidence": {"success_count": 1, "fail_count": 0}
    },
    habit_id="habit:tool_dialect:test001"
)
print(f"  upsert: {'✓' if success else '✗'}")

# 测试 2：检索工具方言
print("\n[测试2] 检索工具方言...")
results = vs.search_interaction_habits(
    query="叫下我", habit_type="tool_dialect", limit=3
)
print(f"  找到 {len(results)} 条记录")
for r in results:
    print(f"  - {r.content[:30]}... (score={r.score:.3f})")

# 测试 3：检索所有 interaction_habits
print("\n[测试3] 检索所有 Interaction Habits...")
all_results = vs.search_interaction_habits(query="叫下我", limit=5)
print(f"  找到 {len(all_results)} 条记录")

# 测试 4：更新置信度
print("\n[测试4] 更新置信度（success）...")
r = vs.update_habit_confidence("habit:tool_dialect:test001", "success")
print(f"  update_habit_confidence: {'✓' if r else '✗'}")

# 测试 5：检索验证置信度变化
print("\n[测试5] 验证置信度更新...")
updated = vs.search_interaction_habits(
    query="叫下我", habit_type="tool_dialect", limit=1
)
if updated:
    conf = updated[0].metadata.get("confidence", {})
    print(f"  success_count: {conf.get('success_count', 0)} (预期 2)")
    assert conf.get("success_count", 0) == 2, "置信度更新失败"
    print("  ✓ 置信度更新正确")

print("\n" + "=" * 60)
print("所有测试通过 ✓")
print("=" * 60)
```

- [ ] **Step 2: 运行验证脚本**
```bash
cd E:/tools/ai-bot
python scripts/test_interaction_habits.py
```

- [ ] **Step 3: Commit**
```bash
cd E:/tools/ai-bot
git add scripts/test_interaction_habits.py
git commit -m "feat(interaction-habits): 添加Interaction Habits功能验证脚本"
```

---

## Task 7: 更新 SYSTEM_MANUAL.md 更新日志

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 在 SYSTEM_MANUAL.md 末尾的更新日志中添加本次更新**

```bash
tail -20 E:/tools/ai-bot/docs/SYSTEM_MANUAL.md
```

找到 "## 更新日志" 章节，在第一条之前添加：

```markdown
- 2026-04-09: v0.4.0 — 新增第 3.4 节：交互习惯库（Interaction Habits）
  - 三类内容：工具方言、用户状态、用户画像
  - 置信度机制：success_count/fail_count，自动删除低置信度记录
  - context-manager 梦境整理时学习个性化内容
  - 主 Agent 可读取和应用 Interaction Habits
```

- [ ] **Step 2: Commit**
```bash
cd E:/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "docs(SYSTEM_MANUAL): 更新日志记录Interaction Habits功能"
```

---

## 实施顺序

| 任务 | 依赖 | 说明 |
|------|------|------|
| Task 1 | — | 扩展 context-manager 提示词 |
| Task 2 | Task 1 | 向量库支持 interaction_habit |
| Task 3 | Task 2 | runner.py 动态注入 |
| Task 4 | Task 2 | handler.py 置信度更新 |
| Task 5 | Task 1 | SYSTEM_MANUAL.md 添加章节 |
| Task 6 | Task 2 | 功能验证测试 |
| Task 7 | Task 5 | 更新日志 |

---

## 验证清单

- [ ] `python scripts/test_interaction_habits.py` 全部通过
- [ ] `wc -l config/agents/context-manager.md` 增加了约 120 行
- [ ] SYSTEM_MANUAL.md 第 3.4 节包含三类内容的完整说明
- [ ] 向量库支持 `search_interaction_habits()` 检索
- [ ] handler.py 工具调用后正确更新置信度
