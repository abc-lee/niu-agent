# L1 摘要层规范

> 版本：v3.0
> 日期：2026-04-12
> 更新：丰富内容格式规范，明确指针字段定义

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

## L1 Content 格式规范

### 管道分隔格式（推荐）

**格式**：
```
{标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
```

**字段说明**：

| 字段 | 说明 | 示例 |
|------|------|------|
| 标题 | 简短标题 | `Browser automation` |
| 关键词 | 逗号分隔的关键词 | `browser,form filling,web operation` |
| 摘要 | 详细描述 | `Use browser_navigate + code_run to...` |
| 实体 | 相关实体/工具 | `browser_navigate,Playwright,BrowserManager` |
| 类型 | 内容类型 | `skill`, `memory`, `document` |
| 指针 | L2内容位置 | `memory/skills/browser-automation.md` |

**示例**：
```
Browser automation|browser,form filling,web operation|Use browser_navigate + code_run to execute Playwright code for browser automation|browser_navigate,Playwright,BrowserManager,code_run|skill|memory/skills/browser-automation.md
```

### 指针字段类型

**指针指向 L2 完整内容的位置**，可以是：

| 指针类型 | 格式 | 示例 |
|---------|------|------|
| 文件路径 | 相对或绝对路径 | `memory/skills/browser-automation.md` |
| L2 记录ID | 向量库记录ID | `mem-550e8400-e29b-41d4-a716-446655440000:l2` |
| URL | 网页链接 | `https://docs.example.com/guide` |
| 数据库ID | 外部数据库ID | `db://knowledge/12345` |

**重要性**：
- ✅ Agent 通过指针读取完整内容
- ✅ 实现"先看摘要，按需加载全文"的动态机制
- ✅ 避免向量库存储大量重复内容

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

#### `category: "memory"` — 记忆

| 字段 | 说明 |
|------|------|
| `memory_type` | 记忆类型 |
| `l2_pointer` | L2记录ID（指针） |
| `importance` | 重要性分数 |
| `created_at` | 创建时间 |

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
5. ✅ content 使用管道格式（推荐）
6. ✅ 最后一个字段是指针（如有 L2 内容）

---

## 代码实现示例

### Skills 同步（agent/injector/sync.py）

```python
def _extract_description(self, content: str) -> str:
    """提取 L1 摘要（强制英文）"""

    # 优先级 1: 提取 L1 摘要（管道格式）
    match_l1 = re.search(r"\*\*[lL]1 摘要\*\*[：:]\s*(.+)", content)
    if match_l1:
        description = match_l1.group(1).strip()
        if description:
            return description

    # 优先级 2: 标题（降级）
    # ...

    # 如果没有 L1 摘要，拒绝同步
    return ""
```

### Memory Server（mcp-servers/memory-server/）

```python
def _generate_l1_summary(self, content: str, memory_type: str, title: str = None, l2_pointer: str = None) -> str:
    """生成 L1 摘要（管道格式，最后一个字段是指针）"""

    # ... 提取字段逻辑

    # 最后一个字段：L2 指针
    pointer = l2_pointer or "l2"

    return f"{title_str}|{keywords_str}|{summary_str}|{entities_str}|{memory_type}|{pointer}"
```

---

## 向量库记录结构示例

### Skill 记录

```python
{
    "id": "skill:browser-automation",
    "content": "Browser automation|browser,form filling,web operation|Use browser_navigate + code_run to execute Playwright code for browser automation|browser_navigate,Playwright,BrowserManager,code_run|skill|memory/skills/browser-automation.md",
    "embedding": <L2归一化后的向量>,
    "metadata": {
        "level": "l1",
        "category": "skill",
        "language": "en",
        "name": "browser-automation",
        "description": "Browser automation|browser,form filling...",
        "source": "E:\\tools\\ai-bot\\memory\\skills\\browser-automation.md",
        "priority": 50,
        "tags": ["browser", "automation", "playwright"],
        "triggers": ["浏览器", "网页", "填表"]
    }
}
```

### Memory 记录

```python
{
    "id": "mem-550e8400-e29b-41d4-a716-446655440000:l1",
    "content": "User preference|python,testing|User prefers using pytest for testing with 80% coverage|pytest,coverage,testing|preference|mem-550e8400-e29b-41d4-a716-446655440000:l2",
    "embedding": <L2归一化后的向量>,
    "metadata": {
        "level": "l1",
        "category": "memory",
        "language": "en",
        "memory_type": "preference",
        "l2_pointer": "mem-550e8400-e29b-41d4-a716-446655440000:l2",
        "importance": 0.8,
        "created_at": "2026-04-12T10:30:00"
    }
}
```

---

## 更新日志

- 2026-04-07: v2.0 新增L2归一化要求、L1内容强制英文
- 2026-04-09: v3.0 统一metadata结构，移除normalized标记，精简规范
- 2026-04-12: v3.0 丰富内容格式规范，明确指针字段定义，添加代码示例
