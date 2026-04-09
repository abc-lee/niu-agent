# L1 摘要层规范

> 版本：v3.0
> 日期：2026-04-09
> 更新：统一metadata结构，移除normalized标记，简化规范

---

## 核心要求

### 1. L1 内容强制英文

**原因**：英文变化少，语义准确，向量检索匹配率高

| 对比项 | 中文查询 | 英文查询 | 提升 |
|--------|---------|---------|------|
| "5分钟后提醒我吃药" | 0.2848 | 0.8276 | **+191%** |

**要求**：
- 所有 L1 content 必须用英文
- 工具描述、Skills摘要、查询模式、系统文档 全部英文

### 2. L2 归一化（标准行为）

**所有入库的 embedding 向量必须做 L2 归一化**，无需标记。

```python
import numpy as np

# 入库时 L2 归一化
vec = np.array(embedding, dtype=np.float32)
norm = np.linalg.norm(vec)
if norm > 0:
    vec = vec / norm
embedding_blob = vec.tobytes()

# 检索时简化计算
score = np.dot(query_vec, doc_vec)  # 归一化后直接点积
```

**好处**：计算优化 + 数值稳定

---

## 统一 Metadata 结构

### 基础字段（所有 L1 记录必须有）

| 字段 | 值 | 说明 |
|------|----|------|
| `level` | `"l1"` | 层级标识（必须小写） |
| `category` | `"mcp_tool" / "skill" / "document" / "query_pattern"` | 内容分类 |
| `language` | `"en"` | 内容语言（统一英文） |

### 类型扩展字段（按 category 扩展）

#### `category: "mcp_tool"` — MCP工具

| 字段 | 说明 |
|------|------|
| `name` | 工具名（如 `schedule_task`） |
| `server` | 服务器名（如 `scheduler-server`） |
| `description` | 工具描述（英文） |
| `input_schema` | 参数schema |

#### `category: "query_pattern"` — 查询模式

| 字段 | 说明 |
|------|------|
| `type` | `"query_pattern"` |
| `is_recursive` | `True` — 触发递归查询 |
| `refined_query` | 第二轮检索用的精简查询（英文） |
| `category` | 目标类别（如 `"mcp_tool"`） |
| `description` | 模式描述（英文） |

#### `category: "skill"` — Skills

| 字段 | 说明 |
|------|------|
| `name` | Skill名称 |
| `description` | 描述（英文） |
| `source` | 来源文件路径 |
| `priority` | 优先级（默认50） |
| `tags` | 标签列表 |
| `triggers` | 触发条件 |

#### `category: "document"` — 文档

| 字段 | 说明 |
|------|------|
| `resource_type` | 资源类型（如 `"system_manual"`） |
| `section` | 章节 |
| `title` | 标题（英文） |

---

## Content 格式

### MCP工具格式

```
{tool_name}: {description}
```

**示例**：
```
schedule_task: Create scheduled tasks, reminders, and alarms. Supports one-time and recurring reminders. Use when user says 'remind me', 'alarm', 'set reminder'.
```

### 查询模式格式

```
{user_query_pattern}
```

**示例**：
```
remind me in X minutes
```

---

## 向量库记录结构

```python
{
    "id": "mcp_tool:scheduler-server:schedule_task",
    "content": "schedule_task: Create scheduled tasks...",
    "embedding": <L2归一化后的向量>,
    "metadata": {
        "level": "l1",
        "category": "mcp_tool",
        "language": "en",
        "name": "schedule_task",
        "server": "scheduler-server",
        "description": "Create scheduled tasks...",
        "input_schema": {...}
    }
}
```

---

## 递归查询模式记录

```python
{
    "id": "query_pattern:reminder_time",
    "content": "remind me in X minutes",
    "embedding": <L2归一化后的向量>,
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

---

## 实施要求

### 向量库初始化脚本（init_vector_db.py）

1. **MCP工具注册**：注册到向量库，`category: "mcp_tool"`
2. **查询模式注册**：注册到向量库，`category: "query_pattern"`
3. **Skills同步**：通过 `sync_skills()` 同步，`category: "skill"`
4. **系统文档注入**：通过 `inject_system_manual()` 注入，`category: "document"`

### 所有记录必须满足

1. ✅ content 英文
2. ✅ embedding L2归一化
3. ✅ metadata 包含基础字段（level, category, language）
4. ✅ metadata 包含类型扩展字段

---

## 更新日志

- 2026-04-07: v2.0 新增L2归一化要求、L1内容强制英文
- 2026-04-09: v3.0 统一metadata结构，移除normalized标记，精简规范
