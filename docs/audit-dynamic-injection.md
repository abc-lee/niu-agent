# 动态注入架构实现审核报告

> 审核日期: 2026-04-05
> 设计文档: docs/design-dynamic-injection.md
> 状态: ⚠️ 部分实现，存在架构偏离和潜在 Bug

---

## 1. 总体评估

| 设计要求 | 实现状态 | 说明 |
|---------|---------|------|
| **架构设计** | ⚠️ 简化实现 | 没有独立的 DynamicInjector 和 ResourceRegistry 类 |
| **Skills 同步** | ⚠️ 部分实现 | 只用了定时扫描，缺少 watchdog 监听 |
| **MCP Tools 注册** | ⚠️ 部分实现 | 有 API 端点，但缺少启动时自动注册逻辑 |
| **动态注入流程** | ✅ 基本实现 | 功能可用，但实现方式与设计不同 |
| **数据模型** | ✅ 完全实现 | ID 命名、Metadata 结构符合设计 |
| **向量库 filter** | ✅ 完全实现 | 支持数组 filter 值 |

---

## 2. 架构偏离分析

### 2.1 设计要求的架构

```
agent/injector/
├── __init__.py
├── models.py               # InjectableResource, InjectionResult
├── registry.py             # ResourceRegistry（资源注册与检索）
├── injector.py             # DynamicInjector（动态注入器）
└── sync.py                 # ResourceSync（watchdog 监听）
```

### 2.2 实际实现的架构

```
agent/injector/
├── __init__.py             # 只导出 SkillSync
└── sync.py                 # 只有定时扫描，没有 watchdog

agent/runner.py
└── _inject_dynamic_resources()  # 注入逻辑直接写在 Runner 中

niu_api/injector.py
└── register_mcp_tool()     # API 端点（手动注册）
```

### 2.3 影响分析

**优点**：
- 实现更简洁，代码量更少
- 功能基本可用，满足当前需求
- 减少了抽象层次，易于理解

**缺点**：
- **不符合设计文档**：代码与设计不一致，维护困难
- **扩展性差**：没有统一的注册/检索接口，后续扩展困难
- **职责不清**：Runner 承担了注入逻辑，违反单一职责原则

---

## 3. 核心功能实现对比

### 3.1 Skills 同步机制

**设计要求**（第 3.2 节）：
```
watchdog 监听（实时事件）
    ↓
防抖等待 500ms
    ↓
检测 self_writing
    ↓
同步到向量库

+ 定时扫描兜底（每分钟）
```

**实际实现**（`agent/injector/sync.py`）：
```python
class SkillSync:
    def __init__(self, scan_interval: int = 60):
        self.scan_interval = scan_interval  # 只有定时扫描
        # 没有 watchdog

    def start_background_sync(self):
        # 只启动定时扫描线程
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
```

**缺失功能**：
- ❌ watchdog 监听（实时性差）
- ❌ 防抖机制（可能导致频繁扫描）
- ❌ self_writing 检测（可能误同步）

**潜在问题**：
- 新增/修改 skill 后，最多需要等待 60 秒才能生效
- 批量修改 skills 时，可能触发多次全量扫描

---

### 3.2 MCP Tools 注册机制

**设计要求**（第 3.1 节）：
```python
# 启动时执行一次
def register_mcp_tools():
    for server_name, config in MCP_SERVERS.items():
        tools = discover_tools_from_config(server_name, config)
        for tool in tools:
            registry.register_mcp_tool(...)
```

**实际实现**（`niu_api/__main__.py`）：
```python
# 预加载 MCP 工具
mcp_tools = await list_mcp_tools()
logger.info(f"MCP tools preloaded: {len(mcp_tools)} tools")

# 初始化 runner with MCP tools
init_runner(mcp_tools)
```

**问题**：
- ⚠️ **预加载了 MCP 工具，但没有注册到向量库**
- ⚠️ 需要手动调用 `/api/inject/mcp-tool` 才能注册
- ⚠️ MCP 工具更新后，需要手动重新注册

**缺失逻辑**：
```python
# 设计要求的逻辑（缺失）
for tool in mcp_tools:
    await register_mcp_tool_to_vector_db(tool)
```

**建议**：
在 `niu_api/__main__.py` 的 `lifespan()` 中添加：
```python
# 预加载 MCP 工具后，自动注册到向量库
from niu_api.injector import register_mcp_tool
for tool in mcp_tools:
    await register_mcp_tool(RegisterMCPToolRequest(
        server_name=tool.get("server", "unknown"),
        tool_name=tool["name"],
        description=tool.get("description", ""),
        input_schema=tool.get("input_schema", {}),
    ))
```

---

### 3.3 动态注入流程

**设计要求**（第 4.2 节）：
```python
def inject(self, user_input: str) -> InjectionResult:
    # 1. 向量检索（分类型检索）
    skills = self.registry.search(query=user_input, filter={"type": "skill"}, ...)
    mcp_tools = self.registry.search(query=user_input, filter={"type": "mcp_tool"}, ...)
    knowledge = self.registry.search(query=user_input, filter={"type": "knowledge"}, ...)

    # 2. 格式化
    prompt_parts = []
    if skills:
        prompt_parts.append(self._format_skills(skills))

    # 3. 构建工具 Schema
    tools_schema = [self._build_tool_schema(t) for t in mcp_tools]

    return InjectionResult(
        prompt_extension="\n\n".join(prompt_parts),
        tools_schema=tools_schema,
        resources=skills + mcp_tools + knowledge,
    )
```

**实际实现**（`agent/runner.py`）：
```python
def _inject_dynamic_resources(self, user_input: str) -> str:
    # 搜索 Skills
    skills = self.vector_search.search(query=user_input, limit=3, min_score=0.25, filter={"type": "skill"})

    # 搜索 MCP 工具描述
    mcp_tools = self.vector_search.search(query=user_input, limit=5, min_score=0.25, filter={"type": "mcp_tool"})

    # 搜索知识
    knowledge = self.vector_search.search(query=user_input, limit=8, min_score=0.35, filter={"type": "l1"})

    # 格式化
    parts = []
    if skills:
        parts.append(format_resources_for_prompt(skills, "相关技能"))
    if mcp_tools:
        parts.append(format_resources_for_prompt(mcp_tools, "可用工具"))
    if knowledge:
        parts.append(format_resources_for_prompt(knowledge, "参考知识"))

    return "\n".join(parts)
```

**对比**：
- ✅ 基本流程符合设计
- ✅ 分类型检索、阈值过滤、数量限制都实现了
- ⚠️ 没有返回 InjectionResult，直接返回字符串
- ⚠️ MCP 工具的 Schema 注入逻辑在 `chat()` 方法中：
  ```python
  # 组装 tools_schema = 内置工具 + MCP 工具
  tools_schema = self.base_tools_schema.copy()
  if self._mcp_tools_schema:
      tools_schema.extend(self._mcp_tools_schema)
  ```
- ⚠️ 这里的 MCP 工具 Schema 是启动时预加载的，**不是动态检索的**

**潜在 Bug**：
- MCP 工具描述虽然被检索到并注入到提示词中，但 **工具 Schema 没有被动态注入**
- 用户可能看到工具描述，但无法调用（因为 Schema 不在工具列表中）

---

## 4. 潜在 Bug 分析

### Bug 1: MCP 工具 Schema 不匹配

**现象**：
- 动态注入提示词中包含 MCP 工具描述
- 但工具 Schema 是启动时预加载的全量 MCP 工具
- 没有根据用户输入动态筛选

**根因**：
```python
# runner.py 第 346-349 行
tools_schema = self.base_tools_schema.copy()
if self._mcp_tools_schema:
    tools_schema.extend(self._mcp_tools_schema)  # 全量 MCP 工具，未筛选
```

**影响**：
- 工具列表可能包含不相关的 MCP 工具，干扰 LLM 选择
- 提示词和工具列表可能不一致

**建议修复**：
```python
# 方案 1：根据检索到的 MCP 工具动态组装 Schema
mcp_tool_names = {t.metadata["name"] for t in mcp_tools}
tools_schema = self.base_tools_schema.copy()
tools_schema.extend([
    t for t in self._mcp_tools_schema
    if t["function"]["name"] in mcp_tool_names
])

# 方案 2：保持全量 MCP 工具，但降低动态注入的 mcp_tool 数量
# （当前实现，不算 Bug，但不够优化）
```

---

### Bug 2: Skills 同步缺少 watchdog

**现象**：
- 新增/修改 skill 文件后，最多需要等待 60 秒才能生效
- 实时性差，用户体验不佳

**根因**：
```python
# agent/injector/sync.py
class SkillSync:
    def __init__(self, scan_interval: int = 60):
        self.scan_interval = scan_interval  # 只有定时扫描
        # 没有 watchdog
```

**影响**：
- 用户添加 skill 后，无法立即测试效果
- 可能导致用户误以为功能失效

**建议修复**：
添加 watchdog 监听：
```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class SkillFileHandler(FileSystemEventHandler):
    def __init__(self, skill_sync):
        self.skill_sync = skill_sync
        self._debounce_timer = None

    def on_modified(self, event):
        if event.src_path.endswith('.md'):
            # 防抖：500ms 后执行
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(0.5, self.skill_sync.scan_and_sync)
            self._debounce_timer.start()

# 在 start_background_sync() 中启动 watchdog
```

---

### Bug 3: MCP 工具启动时未自动注册到向量库

**现象**：
- MCP 工具描述需要手动调用 API 注册
- 新增 MCP 服务器后，动态注入无法找到其工具描述

**根因**：
```python
# niu_api/__main__.py 第 108-123 行
# 预加载 MCP tools
mcp_tools = await list_mcp_tools()

# 初始化 runner with MCP tools
init_runner(mcp_tools)  # 只设置 Schema，没有注册到向量库
```

**影响**：
- 新增 MCP 服务器后，用户需要手动注册工具描述
- 如果忘记注册，动态注入无法检索到相关工具

**建议修复**：
```python
# 在 init_runner() 中自动注册 MCP 工具到向量库
async def register_mcp_tools_to_vector_db(mcp_tools):
    from niu_api.injector import RegisterMCPToolRequest, register_mcp_tool

    for tool in mcp_tools:
        # 从 tool 结构中提取信息
        # 注意：list_mcp_tools() 返回的结构可能与 register_mcp_tool 需要的不同
        await register_mcp_tool(RegisterMCPToolRequest(
            server_name=tool.get("server", "unknown"),  # 需要确认字段名
            tool_name=tool["name"],
            description=tool.get("description", ""),
            input_schema=tool.get("input_schema", {}),
        ))
```

**注意**：
需要确认 `list_mcp_tools()` 返回的工具结构，确保字段名称匹配。

---

### Bug 4: 向量库 filter 可能误匹配

**现象**：
`vector_search.py` 第 178-190 行的 `_matches_filter()` 使用 LIKE 查询 JSON 字符串：

```python
# 第 78-79 行
cursor = conn.execute(
    "SELECT id, content, metadata FROM documents WHERE metadata LIKE ?",
    (f'%"type": "{resource_type}"%',),
)
```

**潜在问题**：
- 如果 metadata 中包含 `"type": "xxx"` 作为字符串值（不是字段名），可能误匹配
- 例如：`{"name": "type", "value": "skill"}` 会误匹配 `%"type": "skill"%`

**建议修复**：
使用 JSON 函数（SQLite 3.38+ 支持）：
```python
cursor = conn.execute(
    "SELECT id, content, metadata FROM documents WHERE json_extract(metadata, '$.type') = ?",
    (resource_type,),
)
```

或者改为 Python 层过滤（当前实现，第 86 行）：
```python
if metadata.get("type") == resource_type:  # 精确匹配
    results.append(...)
```

**当前状态**：
- ✅ Python 层已有精确匹配（第 86 行）
- ⚠️ 但 SQL 查询仍使用 LIKE，可能返回不必要的行

---

## 5. 测试用例验证

### 5.1 设计文档的测试用例

**测试 1：资源注册测试**
```python
# 设计文档第 8.1 节
registry.register_skill(SkillResource(
    id="skill:photo-processing",
    content="照片处理流程...",
    metadata={"type": "skill", "triggers": ["照片", "人脸"]}
))

results = registry.search("帮我处理照片", filter={"type": "skill"})
assert len(results) > 0
```

**实际实现**：
- ❌ 没有 `registry.register_skill()` 接口
- ✅ 可以通过 `SkillSync._sync_skill()` 注册
- ✅ 可以通过 `vector_search.search()` 检索

**结论**：功能实现，但接口不同。

---

**测试 2：动态注入测试**
```python
# 设计文档第 8.2 节
injector = DynamicInjector(registry)
result = injector.inject("帮我处理这张照片里的人脸")

assert "photo-processing" in result.prompt_extension
assert any(t["function"]["name"] == "ingest_photo" for t in result.tools_schema)
```

**实际实现**：
```python
runner = NiuRunner(...)
injection = runner._inject_dynamic_resources("帮我处理这张照片里的人脸")

# prompt_extension 是字符串，不是 InjectionResult
assert "photo-processing" in injection  # 可能成立（如果 skill 已注册）

# tools_schema 在 chat() 方法中组装，不在 _inject_dynamic_resources() 返回值中
```

**结论**：
- ✅ 提示词注入功能实现
- ⚠️ 工具 Schema 注入逻辑分散，不符合设计

---

**测试 3：Filter 测试**
```python
# 设计文档第 8.3 节
# 单值 filter
results = registry.search(query, filter={"type": "skill"})

# 数组 filter
results = registry.search(query, filter={"type": ["skill", "mcp_tool"]})
```

**实际实现**：
```python
# vector_search.py 第 82-84 行
def search(self, query: str, limit: int = 10, min_score: float = 0.5, filter: dict = None):
    # ...

# 第 177-190 行
def _matches_filter(self, metadata: dict, filter: dict) -> bool:
    for key, value in filter.items():
        if isinstance(value, list):
            # 数组匹配：metadata[key] 在 value 列表中
            if metadata[key] not in value:
                return False
        else:
            # 单值匹配
            if metadata[key] != value:
                return False
    return True
```

**结论**：✅ 完全实现，支持单值和数组 filter。

---

## 6. 修改建议优先级

### P0（必须修复）

| 问题 | 影响 | 修改建议 |
|------|------|---------|
| MCP 工具启动时未注册到向量库 | 动态注入无法找到 MCP 工具描述 | 在 `niu_api/__main__.py` 的 `lifespan()` 中添加自动注册逻辑 |

### P1（建议修复）

| 问题 | 影响 | 修改建议 |
|------|------|---------|
| Skills 同步缺少 watchdog | 实时性差，用户等待时间长 | 添加 watchdog 监听 + 防抖机制 |
| MCP 工具 Schema 不匹配 | 提示词和工具列表不一致 | 根据检索结果动态组装工具 Schema |

### P2（可选优化）

| 问题 | 影响 | 修改建议 |
|------|------|---------|
| 架构与设计不符 | 维护困难，扩展性差 | 重构为 DynamicInjector + ResourceRegistry |
| 向量库 LIKE 查询效率低 | 性能稍差 | 使用 json_extract() 函数 |

---

## 7. 后续优化建议

1. **完善架构**：
   - 实现独立的 `DynamicInjector` 和 `ResourceRegistry` 类
   - 统一资源注册接口（`register_skill()`, `register_mcp_tool()`）

2. **增强 Skills 同步**：
   - 添加 watchdog 监听
   - 添加防抖机制
   - 检测 self_writing（避免误同步 Agent 自己写入的内容）

3. **优化 MCP 工具注册**：
   - 启动时自动注册到向量库
   - 监听 MCP 服务器变化，动态更新

4. **完善测试**：
   - 添加单元测试覆盖注入逻辑
   - 添加集成测试验证端到端功能

---

## 8. 总结

### 实现完成度

- **核心功能**: 70%
  - ✅ 向量检索和注入
  - ✅ 数据模型和 Metadata
  - ⚠️ Skills 同步（缺少 watchdog）
  - ⚠️ MCP Tools 注册（缺少自动化）

- **架构符合度**: 50%
  - ❌ 没有 DynamicInjector 和 ResourceRegistry
  - ⚠️ 注入逻辑分散在 Runner 中
  - ✅ 向量库 filter 功能完善

### 风险评估

- **高风险**: MCP 工具未自动注册，动态注入可能失效
- **中风险**: Skills 同步实时性差，用户体验不佳
- **低风险**: 架构与设计不符，维护成本稍高

### 建议

1. **立即修复** P0 问题（MCP 工具自动注册）
2. **近期优化** P1 问题（watchdog + Schema 匹配）
3. **长期重构** P2 问题（架构优化）

---

**审核人**: Claude Code
**审核日期**: 2026-04-05
