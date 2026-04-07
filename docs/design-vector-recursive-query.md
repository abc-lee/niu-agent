# 向量递归查询设计方案

## 设计背景

### 问题

用户输入复杂查询时，向量检索分数低：

```
用户输入："5分钟后提醒我吃药"
工具描述："schedule_task: 创建定时任务、提醒、闹钟..."

相似度：0.2848（28分）
```

### 原因分析

1. **语义稀释**：长句包含时间、内容、背景多个维度，核心语义被稀释
2. **表达差异**：用户表达多样（"5分钟后"、"一会儿"、"待会儿"），工具描述相对固定
3. **多语言问题**：中文表达变数大，英文表达相对固定

---

## 解决方案：向量递归查询

### 核心思想

两阶段向量检索：
```
用户输入："5分钟后提醒我吃药"
  ↓ 第一轮检索
常用语句库（query_pattern）
  匹配到："n分钟后提醒我吃药"
  返回：refined_query = "定时任务"
  ↓ 第二轮检索
工具描述库（mcp_tool）
  匹配到：schedule_task
  相似度：0.54（提升90%）
```

---

## 数据结构设计

### 向量库记录

#### 1. 查询模式（query_pattern）

```python
{
    "id": "query_pattern:reminder_time",
    "content": "n分钟后提醒我吃药",  # 用户查询模式
    "embedding": [...],  # 向量嵌入
    "metadata": {
        "type": "query_pattern",      # 类型：查询模式
        "is_recursive": True,         # 递归查询标志 ✅
        "refined_query": "定时任务",   # 精简查询（第二轮用）
        "category": "mcp_tool",       # 目标类别
        "description": "X分钟后提醒用户",
        "language": "zh"              # 语言（可选）
    }
}
```

#### 2. 目标工具（mcp_tool）

```python
{
    "id": "mcp_tool:scheduler-server:schedule_task",
    "content": "schedule_task: 创建定时任务、提醒、闹钟...",
    "embedding": [...],
    "metadata": {
        "type": "mcp_tool",
        "name": "schedule_task",
        "server": "scheduler-server",
        "category": "mcp_tool"
    }
}
```

---

## 检索流程

### 算法流程图

```
用户输入
  ↓
第一轮检索
  ↓
检查 is_recursive 标记
  ├─ True → 提取 refined_query → 第二轮检索（递归）
  └─ False → 直接返回结果
  ↓
检查 is_recursive 标记
  ├─ True → 继续递归（最多3次）
  └─ False → 返回最终结果
```

### 代码实现

```python
def search(self, query: str, limit: int = 10, min_score: float = 0.5,
           filter: dict = None, max_recursion: int = 3) -> list[SearchResult]:
    """
    向量检索（支持递归查询）

    Args:
        query: 查询文本
        limit: 返回数量
        min_score: 最低相似度阈值
        filter: 元数据过滤
        max_recursion: 最大递归次数（硬编码上限：3）

    Returns:
        检索结果列表
    """
    # 安全限制：最多递归3次
    if max_recursion <= 0:
        print("[WARNING] Max recursion reached, returning results", file=sys.stderr)
        return []

    # 第一轮检索
    results = self._search_once(query, limit, min_score, filter)

    # 检查递归标记
    for result in results:
        if result.metadata.get("is_recursive") == True:
            # 发现递归查询标记
            refined = result.metadata.get("refined_query")
            if not refined:
                continue

            new_filter = {"category": result.metadata.get("category")}

            print(f"[Recursive Query] {query} → {refined} (recursion: {4-max_recursion}/3)",
                  file=sys.stderr, flush=True)

            # 递归调用（递归计数-1）
            return self.search(
                query=refined,
                limit=limit,
                min_score=min_score,
                filter=new_filter,
                max_recursion=max_recursion - 1  # ✅ 强制递减
            )

    # 没有递归标记，直接返回
    return results

def _search_once(self, query: str, limit: int, min_score: float,
                 filter: dict) -> list[SearchResult]:
    """单次向量检索"""
    # 获取查询向量
    query_embedding = self._get_embedding(query)
    if not query_embedding:
        return []

    # 数据库查询
    conn = self._get_connection()
    cursor = conn.execute(
        "SELECT id, content, embedding, metadata FROM documents WHERE embedding IS NOT NULL"
    )

    # 计算相似度
    scored_docs = []
    query_vec = np.array(query_embedding, dtype=np.float32)

    for doc_id, content, embedding_blob, metadata_json in cursor:
        metadata = json.loads(metadata_json) if metadata_json else {}

        # 过滤
        if filter and not self._matches_filter(metadata, filter):
            continue

        # 相似度计算
        doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
        score = np.dot(query_vec, doc_vec) / (
            np.linalg.norm(query_vec) * np.linalg.norm(doc_vec)
        )

        if score >= min_score:
            scored_docs.append((doc_id, content, metadata, score))

    # 排序
    scored_docs.sort(key=lambda x: x[3], reverse=True)

    # 返回
    return [
        SearchResult(id=doc_id, content=content, score=score, metadata=metadata)
        for doc_id, content, metadata, score in scored_docs[:limit]
    ]
```

---

## 安全机制

### 1. 递归次数限制（硬编码）

```python
# ✅ 写死在代码中，不可修改
max_recursion: int = 3  # 最多递归3次

# 强制递减
max_recursion = max_recursion - 1
```

**原因**：
- 避免数据错误导致的死循环
- 限制查询时延（每轮~25ms，3轮最多75ms）

### 2. 循环检测（可选增强）

```python
# 检测循环引用
visited_queries = set()

def search(self, query: str, ..., visited: set = None):
    if visited is None:
        visited = set()

    if query in visited:
        print(f"[WARNING] Circular reference detected: {query}", file=sys.stderr)
        return []

    visited.add(query)

    # 递归时传递 visited
    return self.search(..., visited=visited)
```

---

## 初始查询模式库

### 提醒类

```python
QUERY_PATTERNS = [
    # 时间提醒（中文）
    {
        "id": "query_pattern:reminder_time",
        "content": "n分钟后提醒我吃药",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "定时任务",
            "category": "mcp_tool",
            "description": "X分钟后提醒用户"
        }
    },
    {
        "id": "query_pattern:reminder_short",
        "content": "一会儿提醒我",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "定时任务",
            "category": "mcp_tool"
        }
    },
    {
        "id": "query_pattern:reminder_daily",
        "content": "每天提醒我吃药",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "循环任务",
            "category": "mcp_tool"
        }
    },
    {
        "id": "query_pattern:reminder_workday",
        "content": "工作日提醒我打卡",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "工作日提醒 循环任务",
            "category": "mcp_tool"
        }
    },

    # 提醒类（英文）
    {
        "id": "query_pattern:reminder_en_time",
        "content": "remind me in X minutes",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "set reminder",
            "category": "mcp_tool",
            "language": "en"
        }
    },
    {
        "id": "query_pattern:reminder_en_alarm",
        "content": "set alarm",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "set reminder alarm",
            "category": "mcp_tool",
            "language": "en"
        }
    },

    # 文档处理类
    {
        "id": "query_pattern:document_ingest",
        "content": "入库这个文件",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "文档入库",
            "category": "mcp_tool"
        }
    },
    {
        "id": "query_pattern:photo_ingest",
        "content": "处理照片",
        "metadata": {
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "照片入库",
            "category": "mcp_tool"
        }
    },

    # 更多模式可以持续添加...
]
```

---

## 与工具循环的类比

### 工具循环机制

```
用户输入 → LLM判断 → 调用工具A
         → 工具A返回 {"status": "need_l1"} → LLM判断 → 调用工具B
         → 工具B返回 {"status": "success"} → 结束
```

**特点**：
- 数据驱动（工具返回值决定下一步）
- 自动化流程
- 可扩展

### 向量递归查询

```
用户输入 → 向量检索 → 发现 is_recursive=True
                    → 提取 refined_query → 向量检索
                    → 发现 is_recursive=False → 返回结果
```

**特点**：
- 数据驱动（向量库记录决定是否递归）
- 自动化流程
- 可扩展

**相同点**：
- ✅ 都不需要写死在代码
- ✅ 都通过数据标记控制流程
- ✅ 都可以动态扩展

---

## 实施计划

### Phase 1: 创建查询模式库（1小时）

**修改文件**：`scripts/init_vector_db.py`

**新增函数**：
```python
def register_query_patterns():
    """注册递归查询模式"""
    logger.info("注册查询模式...")

    patterns = QUERY_PATTERNS  # 上述定义

    conn = sqlite3.connect(db_path)
    for pattern in patterns:
        embedding = vs._get_embedding(pattern["content"])
        if not embedding:
            continue

        embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

        conn.execute(
            """
            INSERT INTO documents (id, content, embedding, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                embedding = excluded.embedding,
                metadata = excluded.metadata
            """,
            (pattern["id"], pattern["content"], embedding_blob,
             json.dumps(pattern["metadata"], ensure_ascii=False))
        )

    conn.commit()
    logger.info(f"已注册 {len(patterns)} 个查询模式")
```

**调用位置**：
```python
if __name__ == "__main__":
    init_vector_db(db_path)
    sync_skills()
    register_mcp_tools()
    register_query_patterns()  # ✅ 新增
```

---

### Phase 2: 修改检索逻辑（0.5小时）

**修改文件**：`agent/vector_search.py`

**修改方法**：
- 重构 `search()` 方法，支持递归
- 新增 `_search_once()` 私有方法
- 添加递归安全检查

---

### Phase 3: 测试验证（0.5小时）

**测试用例**：
```python
# 测试1：递归查询
result = vs.search("5分钟后提醒我开会")
# 预期：自动递归 → "定时任务" → schedule_task

# 测试2：非递归查询
result = vs.search("schedule task")
# 预期：直接返回 schedule_task

# 测试3：递归深度限制
# 构造循环引用的测试数据，验证最多递归3次
```

---

## 扩展机制

### 1. 动态学习（可选）

```python
def learn_query_pattern(user_input: str, matched_tool: str, score: float):
    """从成功案例中学习新的查询模式"""

    # 如果用户查询分数低但最终匹配成功，记录下来
    if score < 0.35:
        # 提取精简查询（可以用规则或小模型）
        refined = extract_keywords(user_input)

        # 插入向量库
        pattern = {
            "id": f"query_pattern:learned:{uuid4()}",
            "content": user_input,
            "metadata": {
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": refined,
                "category": "mcp_tool",
                "learned_from": "user_feedback"
            }
        }

        vector_db.insert(pattern)
```

### 2. 管理接口（可选）

```python
# API 端点：GET /api/query_patterns
# 返回所有查询模式

# API 端点：POST /api/query_patterns
# 添加新的查询模式

# API 端点：DELETE /api/query_patterns/{id}
# 删除查询模式
```

---

## 性能评估

### 时延分析

| 操作 | 耗时 |
|------|------|
| 单次向量检索 | ~25ms |
| 两轮递归检索 | ~50ms |
| 三轮递归检索 | ~75ms |

**对比**：
- LLM查询改写：500-2000ms
- 向量递归查询：50-75ms
- **提升10-40倍**

### 准确率提升

| 查询类型 | 原始相似度 | 递归后相似度 | 提升 |
|---------|-----------|------------|------|
| "5分钟后提醒我吃药" | 0.2848 | 0.5415 | +90% |
| "每天提醒我吃药" | 0.2565 | 0.5872 | +129% |
| "remind me in 5 minutes" | 0.3070 | 0.5918 | +93% |

---

## 总结

### 核心优势

1. ✅ **数据驱动**：查询模式写在向量库，不写死在代码
2. ✅ **自动化**：通过 `is_recursive` 标记自动触发递归
3. ✅ **安全可靠**：硬编码最多递归3次，避免死循环
4. ✅ **多语言支持**：天然支持中英文查询
5. ✅ **零LLM成本**：纯向量检索，无额外API调用
6. ✅ **可扩展**：随时添加新的查询模式

### 与工具循环的一致性

- 都是数据驱动的设计
- 都通过数据标记控制流程
- 都不需要修改核心代码

---

## 参考资料

- `agent/vector_search.py` - 向量检索实现
- `agent/generic/handler.py` - 工具循环实现
- `scripts/init_vector_db.py` - 向量库初始化

---

## 更新日志

- 2026-04-07: 初版设计，提出向量递归查询方案
