# 统一注入递归检索实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `search_multi()` 统一注入中恢复递归检索能力，同时移除 `hit_tool()` 中的即时向量库检索，使所有向量检索集中在统一注入点执行。

**Architecture:** 保持 `search_multi()` 四桶基础检索不变，在其后追加一轮独立的 MCP 递归检索：用组装数据（三轮对话上下文）检索 query_pattern 桶，检测到 `is_recursive=True` 后用 `refined_query` 仅对 mcp_tool 桶做第二轮检索并替换结果。移除 `_activate_related_skills()` 中的即时 `vs.search()` 调用，改为在统一注入时从 messages 提取工具名作为额外 skill 检索信号。

**Tech Stack:** Python, SQLite (vecdb), sentence-transformers (paraphrase-multilingual-MiniLM-L12-v2)

---

## 当前问题

### 问题1：`hit_tool()` 即时检索违反设计理念

- `tool_lifecycle._activate_related_skills()` 在工具命中时立即调用 `vs.search(query=tool_name, filter={"category":"skill"})`
- 设计理念是三轮对话合并成一条消息再去检索，不应在工具调用中途单独触发
- 即时检索结果通过 `_pending_skills` 中转到 `_inject_dynamic_resources()` 优先注入

### 问题2：`search_multi()` 缺失递归检索

- `search_multi()` 不检查 `is_recursive` 标记，无法触发递归检索
- query_pattern 记录被 categories 字典过滤掉（第479行 `if cat not in categories: continue`）
- 递归检索是数据驱动的，只要检索结果有 `is_recursive=True` 就应自动触发

### 设计约束

- **四桶基础检索不变**：skill/mcp_tool/document/interaction_habit 的检索和分桶逻辑不变
- **递归只影响 mcp_tool 桶**：所有 query_pattern 的 `target_category` 都是 `"mcp_tool"`，`refined_query` 是为工具描述设计的英文关键词
- **用组装数据做递归检索**：不是用工具名，而是用三轮对话上下文。embedding 模型是 `paraphrase-multilingual-MiniLM-L12-v2`，支持多语言中文增强
- **递归检索的目的是用常用用语或方言触发**：如"5分钟后提醒我" → query_pattern → "schedule task" → schedule_task 工具

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `agent/vector_search.py` | `search_multi()` 增加递归检索参数和逻辑 |
| `agent/runner.py` | `_inject_dynamic_resources()` 传入递归参数，移除 `_pending_skills` 逻辑，增加工具名 skill 检索 |
| `agent/tool_lifecycle.py` | `_activate_related_skills()` 移除即时 `vs.search()` 调用，保留同 server 工具激活 |
| `agent/handler.py` | 无变更（`hit_tool()` 接口不变） |

---

## Task 1: `search_multi()` 增加递归检索能力

**Files:**
- Modify: `agent/vector_search.py:425-499`

**原理：** 在 `search_multi()` 完成四桶基础检索后，追加一轮独立的递归检测：
1. 用已有的 `query_vec` 和 `query_norm`（无需重新算 embedding）扫描 query_pattern 记录
2. 检查是否有 `is_recursive=True` 且相似度超过阈值的结果
3. 如果有，用 `refined_query` 算新 embedding，只对 `target_category`（mcp_tool）桶做第二轮检索
4. 用第二轮结果替换 mcp_tool 桶，其他三桶不变

- [ ] **Step 1: 在 `search_multi()` 中添加 `enable_recursion` 参数**

在 `agent/vector_search.py` 第425行，修改 `search_multi` 签名：

```python
def search_multi(
    self, query: str, categories: dict, level: str = "l1",
    enable_recursion: bool = False
) -> dict[str, list[SearchResult]]:
```

- [ ] **Step 2: 在四桶分桶逻辑中，临时加入 query_pattern 桶**

在第472行分桶循环之前，创建一个临时变量保存 query_pattern 结果：

```python
# 3. 计算相似度，按 category 分桶
buckets: dict[str, list] = {cat: [] for cat in categories}
query_pattern_hits: list = []  # 临时存储 query_pattern 匹配结果

for doc_id, content, embedding_blob, metadata_json in docs:
    if not embedding_blob:
        continue
    metadata = json.loads(metadata_json) if metadata_json else {}
    cat = metadata.get("category", "")

    # query_pattern 单独处理（不放入 buckets）
    if cat == "query_pattern" and enable_recursion:
        doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
        score = float(np.dot(query_vec, doc_vec) / (query_norm * np.linalg.norm(doc_vec)))
        if score >= 0.3 and metadata.get("is_recursive") == True:
            query_pattern_hits.append((score, doc_id, content, metadata))
        continue

    if cat not in categories:
        continue

    doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
    score = float(np.dot(query_vec, doc_vec) / (query_norm * np.linalg.norm(doc_vec)))

    cfg = categories[cat]
    if score >= cfg["min_score"]:
        buckets[cat].append((score, doc_id, content, metadata))
```

- [ ] **Step 3: 在四桶排序截断之后，追加递归检索逻辑**

在第498行（`return results`）之前，加入递归检测和执行：

```python
# 5. 递归检索（仅当 enable_recursion=True 且检测到 query_pattern 匹配）
if enable_recursion and query_pattern_hits:
    # 取相似度最高的 query_pattern
    query_pattern_hits.sort(key=lambda x: -x[0])
    best_hit = query_pattern_hits[0]
    refined_query = best_hit[3].get("refined_query", "")
    target_category = best_hit[3].get("category", "mcp_tool")

    if refined_query and target_category in categories:
        print(f"[Recursive Query] {query} → {refined_query} (target: {target_category})",
              file=sys.stderr, flush=True)

        # 用 refined_query 算新 embedding
        refined_embedding = self._get_embedding(refined_query)
        if refined_embedding:
            refined_vec = np.array(refined_embedding, dtype=np.float32)
            refined_norm = np.linalg.norm(refined_vec)

            if refined_norm > 0:
                # 只对 target_category 桶做第二轮检索
                refined_items = []
                for doc_id, content, embedding_blob, metadata_json in docs:
                    if not embedding_blob:
                        continue
                    metadata = json.loads(metadata_json) if metadata_json else {}
                    if metadata.get("category") != target_category:
                        continue
                    if metadata.get("type") == "query_pattern":
                        continue

                    doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                    score = float(np.dot(refined_vec, doc_vec) / (refined_norm * np.linalg.norm(doc_vec)))

                    cfg = categories[target_category]
                    if score >= cfg["min_score"]:
                        refined_items.append((score, doc_id, content, metadata))

                # 排序截断，替换目标桶
                refined_items.sort(key=lambda x: -x[0])
                cfg = categories[target_category]
                results[target_category] = [
                    SearchResult(id=doc_id, content=content, score=score, metadata=metadata)
                    for score, doc_id, content, metadata in refined_items[:cfg["limit"]]
                ]

                print(f"[Recursive Query] {target_category} bucket replaced: {len(results[target_category])} results",
                      file=sys.stderr, flush=True)
```

- [ ] **Step 4: 验证不启用递归时行为不变**

当 `enable_recursion=False`（默认值），`query_pattern_hits` 始终为空列表，递归逻辑不执行。四桶结果与修改前完全一致。

- [ ] **Step 5: 提交**

```bash
git add agent/vector_search.py
git commit -m "feat: add recursion support to search_multi()"
```

---

## Task 2: `_inject_dynamic_resources()` 启用递归 + 移除 `_pending_skills`

**Files:**
- Modify: `agent/runner.py:452-552`

**原理：**
1. 调用 `search_multi()` 时传入 `enable_recursion=True`
2. 移除 `_pending_skills` 相关代码（第473-493行）
3. 从 messages 中提取本轮工具名，对 skill 桶做额外精确检索

- [ ] **Step 1: 移除 `_pending_skills` 逻辑**

删除 `agent/runner.py` 第473-493行的 pending skills 代码块：

```python
# 删除以下代码块：
# 1. 优先注入待注入的Skills（工具命中后检索到的）
pending_skill_names = self.tool_lifecycle.get_pending_skills()
pending_skills = []
try:
    for skill_name in pending_skill_names:
        skills = self.vector_search.search(
            query=skill_name,
            limit=1,
            min_score=0.6,
            filter={"category": "skill", "name": skill_name}
        )
        if skills:
            pending_skills.append(skills[0])

    if pending_skills:
        print(f"[Debug] Pending Skills: {len(pending_skills)} results", file=sys.stderr, flush=True)
finally:
    self.tool_lifecycle.clear_pending_skills()
```

- [ ] **Step 2: `search_multi()` 调用加入 `enable_recursion=True`**

修改第496行：

```python
multi_results = self.vector_search.search_multi(
    query=context,
    categories={
        "skill": {"limit": 3, "min_score": 0.35},
        "mcp_tool": {"limit": 10, "min_score": 0.25},
        "document": {"limit": 8, "min_score": 0.45},
        "interaction_habit": {"limit": 3, "min_score": 0.4},
    },
    enable_recursion=True
)
```

- [ ] **Step 3: 增加工具名 skill 检索**

在 `search_multi()` 调用之后、格式化之前，从 context 中提取工具名做 skill 精确检索：

```python
# 3. 用本轮工具名做 skill 精确检索（替代原 _activate_related_skills 的即时检索）
tool_signal_skills = []
recent_tool_names = self.tool_lifecycle.get_recent_hits()  # 获取本轮命中的工具名
for tool_name in recent_tool_names:
    tool_skills = self.vector_search.search(
        query=tool_name,
        limit=2,
        min_score=0.3,
        filter={"category": "skill"}
    )
    tool_signal_skills.extend(tool_skills)

if tool_signal_skills:
    print(f"[Debug] Tool-signal Skills: {len(tool_signal_skills)} results", file=sys.stderr, flush=True)
```

- [ ] **Step 4: 合并 skill 结果时包含工具名检索结果**

修改第524-525行的 skill 合并逻辑：

```python
# 合并工具名检索Skills、搜索到的Skills（去重）
all_skills = tool_signal_skills + skills
```

- [ ] **Step 5: 更新 debug 日志**

更新第510行的日志，反映递归检索：

```python
print(f"[Debug] Dynamic injection - Skills: {len(skills)}, MCP: {len(mcp_tools)}, Knowledge: {len(knowledge)}, Habits: {len(interaction_habits)}, ToolSignalSkills: {len(tool_signal_skills)}", file=sys.stderr, flush=True)
```

- [ ] **Step 6: 提交**

```bash
git add agent/runner.py
git commit -m "feat: enable recursion in search_multi + remove _pending_skills"
```

---

## Task 3: `_activate_related_skills()` 移除即时向量库检索

**Files:**
- Modify: `agent/tool_lifecycle.py:90-144`

**原理：** `_activate_related_skills()` 保留同 server 工具激活（co-activation），移除 `vs.search()` 调用。新增 `get_recent_hits()` 方法供统一注入时获取本轮工具名。

- [ ] **Step 1: 移除 `_activate_related_skills()` 中的向量库检索**

删除 `agent/tool_lifecycle.py` 第121-144行的向量库检索代码块：

```python
# 删除以下代码块：
# 2. 检索相关 Skills
try:
    from agent.vector_search import get_vector_search

    vs = get_vector_search()
    skills = vs.search(
        query=tool_name,
        limit=2,
        min_score=0.3,
        filter={"category": "skill"}
    )

    for skill in skills:
        skill_name = skill.metadata.get("name", "")
        if skill_name and skill_name not in self._pending_skills:
            self._pending_skills.append(skill_name)

    if skills:
        print(f"[ToolLifecycle] Found skills for {tool_name}: {[s.metadata.get('name') for s in skills]}",
              file=sys.stderr, flush=True)

except Exception as e:
    print(f"[ToolLifecycle] Failed to find skills for {tool_name}: {e}",
          file=sys.stderr, flush=True)
```

- [ ] **Step 2: 新增 `_recent_hits` 和 `get_recent_hits()` 方法**

在 `__init__` 中新增：

```python
self._recent_hits: List[str] = []  # 本轮命中的工具名（每轮清空）
```

在 `hit_tool()` 中记录：

```python
def hit_tool(self, tool_name: str, score: int = None, skip_coactivation: bool = False):
    """记录工具命中"""
    # ... 现有逻辑 ...
    self._recent_hits.append(tool_name)  # 新增：记录本轮命中
```

新增方法：

```python
def get_recent_hits(self) -> List[str]:
    """获取本轮命中的工具名列表（统一注入时调用，调用后清空）"""
    hits = self._recent_hits.copy()
    self._recent_hits.clear()
    return hits
```

- [ ] **Step 3: 移除 `_pending_skills` 相关代码**

删除 `tool_lifecycle.py` 中所有 `_pending_skills` 相关代码：
- `__init__` 中的 `self._pending_skills: List[str] = []`
- `_activate_related_skills()` 中往 `_pending_skills` 追加的代码（已在 Step 1 删除）
- `get_pending_skills()` 方法
- `clear_pending_skills()` 方法
- `clear()` 中的 `self._pending_skills.clear()`

- [ ] **Step 4: 提交**

```bash
git add agent/tool_lifecycle.py
git commit -m "refactor: remove instant vector search from hit_tool, add get_recent_hits()"
```

---

## Task 4: 验证与测试

**Files:**
- Test: `scripts/query_pattern/test_recursive_search.py`（已有，验证递归检索）

- [ ] **Step 1: 启动应用，发送模糊查询验证递归检索**

发送 "5分钟后提醒我吃药"，检查日志中是否出现：
```
[Recursive Query] 5分钟后提醒我吃药 → schedule task (target: mcp_tool)
[Recursive Query] mcp_tool bucket replaced: N results
```

- [ ] **Step 2: 验证四桶基础检索不受递归影响**

发送普通查询（如 "搜索文档"），检查 skill/document/interaction_habit 桶结果与修改前一致。

- [ ] **Step 3: 验证工具名 skill 检索生效**

触发一个 MCP 工具调用，检查下一轮日志中是否出现：
```
[Debug] Tool-signal Skills: N results
```

- [ ] **Step 4: 验证 `_pending_skills` 完全移除**

搜索代码库确认无残留引用：
```bash
grep -rn "_pending_skills\|pending_skill\|get_pending_skills\|clear_pending_skills" agent/
```
预期：无结果

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "feat: unified injection with recursion + remove instant vector search"
```

---

## 时序对比

### 修改前

```
LLM 调用 tool_x
  → handler.dispatch()
    → hit_tool("tool_x")
      → _activate_related_skills("tool_x")
        → vs.search(query="tool_x", filter={"category":"skill"})  ← 即时检索1
        → _pending_skills.append(skill_name)
  → _on_turn_end()
    → _inject_dynamic_resources(context)
      → 取出 _pending_skills → vs.search(query=skill_name) × N  ← 即时检索2
      → search_multi(query=context)  ← 无递归
```

### 修改后

```
LLM 调用 tool_x
  → handler.dispatch()
    → hit_tool("tool_x")
      → _recent_hits.append("tool_x")  ← 仅记录，不检索
  → _on_turn_end()
    → _inject_dynamic_resources(context)
      → search_multi(query=context, enable_recursion=True)  ← 四桶基础检索
        → 检测 query_pattern → is_recursive=True
        → 用 refined_query 只对 mcp_tool 桶做第二轮检索  ← 递归检索
        → 替换 mcp_tool 桶，其他三桶不变
      → get_recent_hits() → vs.search(query="tool_x", filter={"category":"skill"})  ← 工具名 skill 检索
```

**向量库调用次数对比：**
- 修改前：1（即时skill检索）+ N（pending skills精确匹配）+ 1（search_multi）= 2+N 次
- 修改后：1（search_multi含递归）+ M（工具名skill检索）= 1+M 次
- M ≤ N（因为 get_recent_hits 每轮清空，不会累积）
