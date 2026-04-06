# 动态注入架构设计

> 版本: 1.0
> 日期: 2026-04-04
> 状态: 设计完成，待实施

## 1. 概述

### 1.1 目标

将 Skills、MCP Tools、Knowledge 统一存储到向量库，根据用户输入语义匹配后动态注入到主 Agent 的系统提示词和工具 Schema 中。

### 1.2 设计原则

- **SubAgent 简化**：写死配置，启动时一次性注入，不搞复杂继承
- **主 Agent 动态**：根据用户输入语义匹配相关资源
- **向量库统一**：所有资源存入同一向量库，通过标签区分类型
- **增量同步**：Skills 用 watchdog 监听，MCP Tools 启动时注册

### 1.3 架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           用户输入                                       │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      DynamicInjector.inject()                           │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              ResourceRegistry.search(user_input)                │   │
│  │                                                                 │   │
│  │  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐         │   │
│  │  │ Skills 向量库 │ │ MCP Tools 库  │ │ Knowledge 库  │         │   │
│  │  │ (watchdog同步)│ │ (启动时注册)  │ │ (文档入库)    │         │   │
│  │  └───────────────┘ └───────────────┘ └───────────────┘         │   │
│  │                                                                 │   │
│  │  语义匹配 → 按分数排序 → 分组                                   │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                 │                                       │
│                                 ▼                                       │
│  注入结果:                                                              │
│  - prompt_extension: "### [相关技能]\n...### [参考知识]\n..."          │
│  - tools_schema: [相关 MCP 工具定义]                                   │
│                                                                         │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      GenericAgentRunner.chat()                          │
│                                                                         │
│  system_prompt = base_prompt + injection.prompt_extension              │
│  tools_schema = builtin_tools + injection.tools                        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 数据模型

### 2.1 向量库 Schema

复用现有 `documents` 表：

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,           -- 资源ID (见命名规范)
    content TEXT NOT NULL,         -- 内容/描述 (用于向量化)
    embedding BLOB,                -- 向量
    metadata TEXT                  -- JSON 元数据
);
```

### 2.2 Metadata 结构

```json
{
    "type": "l1|l2|skill|mcp_tool|builtin_tool",
    "name": "photo-processing",
    "description": "照片处理技能",
    "source": "memory/skills/photo.md",
    "priority": 60,
    "tags": ["photo", "face", "image"],

    "_type_specific_fields_": {
        "skill": {
            "triggers": ["照片", "图片"]
        },
        "mcp_tool": {
            "server": "photo-server",
            "tool_name": "ingest_photo",
            "input_schema": {}
        },
        "builtin_tool": {
            "category": "file|code|web"
        }
    }
}
```

**说明**：
- L1/L2 知识直接使用 `"type": "l1"` 或 `"type": "l2"`（单字段，简化设计）
- Skills 使用 `"type": "skill"`
- MCP 工具使用 `"type": "mcp_tool"`

### 2.3 ID 命名规范

```
knowledge:l1:{名称}          # L1 知识
knowledge:l2:{名称}          # L2 知识
skill:{技能名}               # 技能 SOP
mcp_tool:{server}:{tool}     # MCP 工具
builtin_tool:{工具名}        # 内置工具
```

### 2.4 标签体系

| type 值 | 说明 | 必需字段 | 可选字段 |
|---------|------|---------|---------|
| `l1` | L1 知识摘要 | - | - |
| `l2` | L2 知识原文 | - | - |
| `skill` | 技能 SOP | `triggers` | - |
| `mcp_tool` | MCP 工具 | `server`, `tool_name`, `input_schema` | - |
| `builtin_tool` | 内置工具 | `category` | - |

---

## 3. 资源同步机制

### 3.1 MCP Tools - 启动时注册

```python
# 启动时执行一次
def register_mcp_tools():
    for server_name, config in MCP_SERVERS.items():
        tools = discover_tools_from_config(server_name, config)
        for tool in tools:
            registry.register_mcp_tool(MCPToolResource(
                id=f"mcp_tool:{server_name}:{tool['name']}",
                content=f"{tool['name']}: {tool['description']}",
                metadata={
                    "type": "mcp_tool",
                    "name": tool['name'],
                    "description": tool['description'],
                    "server": server_name,
                    "tool_name": tool['name'],
                    "input_schema": tool.get('inputSchema', {}),
                    "tags": extract_tags(tool),
                }
            ))
```

### 3.2 Skills - watchdog 监听

```
┌─────────────────────────────────────────────────────────────┐
│                    ResourceSync 服务                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────┐    ┌─────────────────┐                │
│  │ watchdog 监听   │    │ 定时扫描兜底    │                │
│  │ (实时事件)      │    │ (每分钟)        │                │
│  └────────┬────────┘    └────────┬────────┘                │
│           │                      │                          │
│           └──────────┬───────────┘                          │
│                      ▼                                      │
│           ┌─────────────────────┐                          │
│           │ 防抖等待 500ms      │                          │
│           └──────────┬──────────┘                          │
│                      ▼                                      │
│           ┌─────────────────────┐                          │
│           │ 检测 self_writing   │                          │
│           └──────────┬──────────┘                          │
│                      ▼                                      │
│           ┌─────────────────────┐                          │
│           │ 同步到向量库        │                          │
│           └─────────────────────┘                          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 Knowledge - 入库时写入

现有文档入库流程自动写入，只需确保 metadata 包含正确标签。

---

## 4. 动态注入流程

### 4.1 注入器接口

```python
class DynamicInjector:
    """动态注入器"""
    
    def inject(self, user_input: str) -> InjectionResult:
        """
        根据用户输入动态组装注入内容
        
        Returns:
            InjectionResult(
                prompt_extension=str,   # 系统提示词扩展
                tools_schema=list,      # 工具 Schema 扩展
                resources=list,         # 匹配到的资源
            )
        """
        pass
```

### 4.2 检索策略

```python
def inject(self, user_input: str) -> InjectionResult:
    # 1. 向量检索（分类型检索）
    skills = self.registry.search(
        query=user_input,
        filter={"type": "skill"},
        limit=3,
        min_score=0.5
    )

    mcp_tools = self.registry.search(
        query=user_input,
        filter={"type": "mcp_tool"},
        limit=5,
        min_score=0.5
    )

    # L1/L2 知识统一检索
    knowledge = self.registry.search(
        query=user_input,
        filter={"type": ["l1", "l2"]},  # 数组 filter，同时搜索 L1 和 L2
        limit=8,
        min_score=0.5
    )

    # 2. 格式化
    prompt_parts = []
    if skills:
        prompt_parts.append(self._format_skills(skills))
    if knowledge:
        prompt_parts.append(self._format_knowledge(knowledge))

    # 3. 构建工具 Schema
    tools_schema = [self._build_tool_schema(t) for t in mcp_tools]

    return InjectionResult(
        prompt_extension="\n\n".join(prompt_parts),
        tools_schema=tools_schema,
        resources=skills + mcp_tools + knowledge,
    )
```

### 4.3 格式化模板

**Skills 格式化**:
```
### [相关技能]

**photo-processing**: 照片处理流程
- 人脸识别
- 人物命名
- 人物合并

详细内容见: memory/skills/photo-processing.md
```

**Knowledge 格式化**:
```
### [参考知识]

1. 人脸识别使用 InsightFace buffalo_l 模型... (分数: 85%)
2. 项目主目录为 E:/tools/ai-bot... (分数: 72%)
```

**MCP Tools 格式化**:
```json
{
    "type": "function",
    "function": {
        "name": "ingest_photo",
        "description": "照片入库与人脸识别",
        "parameters": {...}
    }
}
```

---

## 5. 代码结构

```
agent/
├── injector/                    # 新增模块
│   ├── __init__.py             # 导出
│   ├── models.py               # 数据模型 (InjectableResource)
│   ├── registry.py             # ResourceRegistry
│   ├── injector.py             # DynamicInjector
│   └── sync.py                 # ResourceSync (watchdog)
│
├── vector_search.py            # 修改: 支持数组 filter
├── runner.py                   # 修改: 集成 DynamicInjector
└── ...
```

---

## 6. 修改清单

### 6.1 新增文件

| 文件 | 说明 |
|------|------|
| `agent/injector/__init__.py` | 模块导出 |
| `agent/injector/models.py` | 数据模型定义 |
| `agent/injector/registry.py` | 资源注册与检索 |
| `agent/injector/injector.py` | 动态注入器 |
| `agent/injector/sync.py` | 资源同步服务 |

### 6.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `agent/vector_search.py` | 支持数组 filter 值 |
| `agent/runner.py` | 集成 DynamicInjector |
| `mcp-servers/vector-store/src/.../__init__.py` | 支持数组 filter 值 |

---

## 7. 实施步骤

| 步骤 | 任务 | 预估时间 |
|------|------|---------|
| 1 | 修改 vector_search.py 支持数组 filter | 0.5h |
| 2 | 创建 injector/models.py 数据模型 | 0.5h |
| 3 | 实现 injector/registry.py 资源注册与检索 | 1h |
| 4 | 实现 injector/injector.py 动态注入 | 1h |
| 5 | 实现 injector/sync.py 资源同步 | 1h |
| 6 | 修改 runner.py 集成注入器 | 0.5h |
| 7 | MCP Tools 启动时注册 | 0.5h |
| 8 | 测试与调试 | 1h |

**总计**: 约 6 小时

---

## 8. 测试用例

### 8.1 资源注册测试

```python
# 注册 Skill
registry.register_skill(SkillResource(
    id="skill:photo-processing",
    content="照片处理流程...",
    metadata={"type": "skill", "triggers": ["照片", "人脸"]}
))

# 检索
results = registry.search("帮我处理照片", filter={"type": "skill"})
assert len(results) > 0
assert results[0].metadata["name"] == "photo-processing"
```

### 8.2 动态注入测试

```python
injector = DynamicInjector(registry)
result = injector.inject("帮我处理这张照片里的人脸")

assert "photo-processing" in result.prompt_extension
assert any(t["function"]["name"] == "ingest_photo" for t in result.tools_schema)
```

### 8.3 Filter 测试

```python
# 单值 filter
results = registry.search(query, filter={"type": "skill"})

# 数组 filter（同时搜索多种类型）
results = registry.search(query, filter={"type": ["l1", "l2"]})

# 单值 filter（L1 知识）
results = registry.search(query, filter={"type": "l1"})
```

---

## 9. 后续优化

1. **优先级动态调整**: 工具被成功调用 → priority += 5
2. **使用统计**: 记录资源使用频率，优化检索排序
3. **缓存机制**: 热门资源缓存，减少向量计算
4. **增量更新**: 只同步变化的文件，不全量扫描

---

## 附录: 现有代码分析

### A.1 vector_search.py 现有接口

```python
class VectorSearchAdapter:
    def search(self, query, limit=10, min_score=0.5, filter=None) -> list[SearchResult]
    def format_for_prompt(self, results) -> str

def search_knowledge(query, limit=10, min_score=0.5) -> str
```

### A.2 runner.py 现有注入点

```python
def get_system_prompt(user_input: str = None) -> str:
    # 1. 读取 sys_prompt.txt
    # 2. 追加 niu.md
    # 3. 添加日期
    # 4. 添加记忆上下文
    # 5. 向量检索注入  ← 扩展点
```

### A.3 GenericAgent 工具 Schema 加载

```python
def load_tools_schema() -> List[Dict]:
    # 从 JSON 文件加载  ← 扩展点：动态组装
```
