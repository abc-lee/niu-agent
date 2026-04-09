# 向量递归查询设计方案

> 版本：v2.0
> 日期：2026-04-09
> 更新：统一使用英文
> 参考：`spec-L1-summary.md`（统一metadata规范）

---

## 设计背景

### 问题

用户输入复杂查询时，向量检索分数低：

```
用户输入："remind me in 5 minutes to take medicine"
工具描述："schedule_task: Create scheduled tasks, reminders, and alarms..."

相似度：0.2848（28分）
```

### 原因分析

1. **语义稀释**：长句包含时间、内容、背景多个维度，核心语义被稀释
2. **表达差异**：用户表达多样，工具描述相对固定
3. **统一英文**：所有L1内容统一使用英文，检索时无需跨语言

---

## 解决方案：向量递归查询

### 核心思想

两阶段向量检索：
```
用户输入："remind me in 5 minutes to take medicine"
  ↓ 第一轮检索
查询模式库（query_pattern）
  匹配到："remind me in X minutes"
  返回：refined_query = "schedule task"
  ↓ 第二轮检索
工具描述库（mcp_tool）
  匹配到：schedule_task
  相似度：0.54（提升90%）
```

---

## 数据结构

### 查询模式（query_pattern）

```python
{
    "id": "query_pattern:reminder_time",
    "content": "remind me in X minutes",
    "embedding": <L2归一化向量>,
    "metadata": {
        "level": "l1",
        "category": "query_pattern",
        "language": "en",
        "type": "query_pattern",
        "is_recursive": True,
        "refined_query": "schedule task",
        "category": "mcp_tool",
        "description": "Remind user after X minutes"
    }
}
```

### 目标工具（mcp_tool）

```python
{
    "id": "mcp_tool:scheduler-server:schedule_task",
    "content": "schedule_task: Create scheduled tasks, reminders, and alarms...",
    "embedding": <L2归一化向量>,
    "metadata": {
        "level": "l1",
        "category": "mcp_tool",
        "language": "en",
        "name": "schedule_task",
        "server": "scheduler-server",
        "description": "Create scheduled tasks, reminders, and alarms...",
        "input_schema": {...}
    }
}
```

详细字段规范见 `spec-L1-summary.md`。

---

## 检索流程

### 算法流程

```
用户输入
  ↓
第一轮检索
  ↓
检查 is_recursive 标记
  ├─ True → 提取 refined_query → 第二轮检索
  └─ False → 直接返回结果
  ↓
第二轮检索结果排除 query_pattern
  ↓
返回最终结果
```

### 代码实现（agent/vector_search.py）

```python
def search(self, query: str, limit: int = 10, min_score: float = 0.5,
           filter: dict = None, max_recursion: int = 3) -> list[SearchResult]:
    """
    向量检索（支持递归查询）
    """
    # 安全限制：最多递归3次
    if max_recursion <= 0:
        return []

    # 第一轮检索
    results = self._search_once(query, limit, min_score, filter)

    # 检查递归标记
    for result in results:
        if result.metadata.get("is_recursive") == True:
            refined = result.metadata.get("refined_query")
            if not refined:
                continue

            print(f"[Recursive Query] {query} → {refined}")

            # 第二轮检索
            results = self._search_once(
                query=refined,
                limit=limit,
                min_score=min_score,
                filter=None,
                level=level
            )
            break  # 只递归一轮

    return results
```

---

## 安全机制

### 递归次数限制（硬编码）

```python
max_recursion: int = 3  # 最多递归3次
```

**原因**：
- 避免数据错误导致的死循环
- 限制查询时延（每轮~25ms，3轮最多75ms）

---

## 初始查询模式库

> 统一使用英文，详见 `spec-L1-summary.md`

```python
QUERY_PATTERNS = [
    # 提醒类
    {
        "id": "query_pattern:reminder_time",
        "content": "remind me in X minutes",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "schedule task",
            "category": "mcp_tool",
            "description": "Remind user after X minutes"
        }
    },
    {
        "id": "query_pattern:reminder_short",
        "content": "remind me later",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "schedule task",
            "category": "mcp_tool",
            "description": "Remind user shortly"
        }
    },
    {
        "id": "query_pattern:reminder_daily",
        "content": "remind me every day",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "recurring task",
            "category": "mcp_tool",
            "description": "Daily recurring reminder"
        }
    },
    {
        "id": "query_pattern:reminder_workday",
        "content": "remind me on workdays",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "workday reminder recurring task",
            "category": "mcp_tool",
            "description": "Workday recurring reminder"
        }
    },
    {
        "id": "query_pattern:reminder_en_time",
        "content": "set a reminder",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "schedule task",
            "category": "mcp_tool"
        }
    },
    {
        "id": "query_pattern:reminder_en_alarm",
        "content": "set alarm",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "schedule alarm",
            "category": "mcp_tool"
        }
    },

    # 文档处理类
    {
        "id": "query_pattern:document_ingest",
        "content": "ingest this document",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document ingestion",
            "category": "mcp_tool",
            "description": "Ingest document to knowledge base"
        }
    },
    {
        "id": "query_pattern:photo_ingest",
        "content": "process photos",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "photo ingestion",
            "category": "mcp_tool",
            "description": "Ingest photos to gallery"
        }
    },
]
```

---

## 性能评估

| 操作 | 耗时 |
|------|------|
| 单次向量检索 | ~25ms |
| 两轮递归检索 | ~50ms |

**对比 LLM查询改写**：500-2000ms，递归查询提升 **10-40倍**

### 准确率提升

| 查询类型 | 原始相似度 | 递归后相似度 | 提升 |
|---------|-----------|------------|------|
| "remind me in 5 minutes to take medicine" | 0.2848 | 0.5415 | +90% |
| "remind me every day to exercise" | 0.2565 | 0.5872 | +129% |
| "set reminder for tomorrow" | 0.3070 | 0.5918 | +93% |

---

## 总结

1. ✅ **数据驱动**：查询模式写在向量库，不写死在代码
2. ✅ **自动化**：通过 `is_recursive` 标记自动触发递归
3. ✅ **安全可靠**：硬编码最多递归3次，避免死循环
4. ✅ **统一规范**：遵循 `spec-L1-summary.md` 的 metadata 结构

---

## 更新日志

- 2026-04-07: 初版设计，提出向量递归查询方案
- 2026-04-09: v2.0 统一使用英文，统一metadata结构
