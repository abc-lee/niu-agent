# MCP工具动态注入架构优化实施计划

> 创建日期：2026-04-10
> 状态：待实施
> 预计工作量：4-6小时

---

## 背景

当前架构问题：
1. **工具注入过多**：77个工具全部注入给主Agent，LLM需要理解过多工具
2. **架构矛盾**：主Agent被指示"不要直接调用MCP工具"，但工具列表中包含所有MCP工具
3. **动态注入不完整**：提示词可以动态注入，但工具列表是静态的
4. **触发机制缺陷**：只在`chat()`入口根据`user_input`注入一次，无法响应LLM生成内容、子Agent返回等上下文变化

目标：
- 主Agent基础工具减少到22个（减少71%）
- 实现工具动态注入，根据上下文按需加载
- 实现工具生命周期管理，保证对话单元内工具持续可用

---

## Step 0: 数据准备 - 向量库索引（前置条件）

**任务ID**: #5
**依赖**: 无
**预计时间**: 60-90分钟

### 目标

为向量递归检索准备必要的数据：
- MCP工具描述索引
- 查询模式索引
- Skills索引

### 当前状态

向量库中只有：
- 16条记录（L1会话摘要、事件提醒）
- **缺失**：MCP工具描述（mcp_tool）、查询模式（query_pattern）、Skills

### 子任务

#### 0.1 确认工具分层

**需要决策的问题**：

1. **memory-server归属**
   - 当前决策：主Agent基础工具
   - 6个工具全部保留

2. **config-manager工具**
   - 当前决策：删除（20个工具全部移除）
   - 替代方案：bash + file操作 + 系统管理手册（Skills）

3. **vector-store工具**
   - 主Agent需要：add_document, search_documents, get_document, delete_document, list_documents
   - event-manager/context-manager共用：相同工具
   - 冲突解决：主Agent和子Agent可以共享工具

#### 0.2 编写索引脚本

**文件**：`scripts/index_mcp_tools.py`

```python
"""
索引MCP工具描述到向量库

遵循规范：
- level: "l1"
- category: "mcp_tool"
- language: "en" (统一英文)
- is_recursive: False
"""
```

**关键点**：
- 工具描述标准化（英文）
- 包含：name, description, input_schema
- 遵循 `spec-L1-summary.md` 的 metadata 结构

**工具列表**（主Agent基础工具）：
```python
BASE_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    "memory-server/update_memory",
    "memory-server/get_memory_stats",
    "memory-server/cleanup_memories",
    "memory-server/link_memories",

    # vector-store (5个)
    "vector-store/add_document",
    "vector-store/search_documents",
    "vector-store/get_document",
    "vector-store/delete_document",
    "vector-store/list_documents",
]
```

**输出格式**：
```python
{
    "id": "mcp_tool:memory-server:remember",
    "content": "remember: Save long-term memory with auto-generated L0/L1/L2 layers...",
    "metadata": {
        "level": "l1",
        "category": "mcp_tool",
        "language": "en",
        "name": "remember",
        "server": "memory-server",
        "description": "Save long-term memory...",
        "input_schema": {...}
    }
}
```

#### 0.3 编写查询模式索引脚本

**文件**：`scripts/index_query_patterns.py`

**参考**：`docs/design-vector-recursive-query.md` 第171-285行

**关键查询模式**（基于用户习惯用语）：
```python
QUERY_PATTERNS = [
    # 记忆管理类
    {
        "id": "query_pattern:recall_memory",
        "content": "recall previous memories",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall",
            "target_category": "mcp_tool"
        }
    },

    # 文档检索类
    {
        "id": "query_pattern:search_documents",
        "content": "search for documents",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search",
            "target_category": "mcp_tool"
        }
    },

    # ... 更多模式
]
```

#### 0.4 编写Skills索引脚本

**文件**：`scripts/index_skills.py`

**内容**：
- 系统管理手册（配置文件结构、修改方法）
- 常用操作指南（照片入库、文档处理、事件管理）
- Skills目录下的所有.md文件

**输出格式**：
```python
{
    "id": "skill:system_management",
    "content": "System Management Manual: Configuration files are stored in...",
    "metadata": {
        "level": "l1",
        "category": "skill",
        "language": "en",
        "title": "System Management Manual",
        "tags": ["config", "management", "system"]
    }
}
```

#### 0.5 运行索引脚本并验证

```bash
python scripts/index_mcp_tools.py
python scripts/index_query_patterns.py
python scripts/index_skills.py

# 验证
python scripts/verify_vector_db.py
```

**验证内容**：
- MCP工具数量：至少11个
- 查询模式数量：至少20个
- Skills数量：至少10个
- 向量检索精度测试

### 完成标准

- [ ] 向量库中存在 `category: mcp_tool` 的记录
- [ ] 向量库中存在 `category: query_pattern` 的记录
- [ ] 向量库中存在 `category: skill` 的记录
- [ ] 向量检索测试通过（准确率 > 70%）
- [ ] 递归查询测试通过（相似度提升 > 50%）

---

## Step 1: 架构调整 - 动态工具注入

**任务ID**: #4
**依赖**: Step 0完成
**预计时间**: 90-120分钟

### 目标

实现主Agent基础工具固定注入 + 动态工具按需加载

### 子任务

#### 1.1 确定主Agent基础工具列表

**修改文件**：`agent/runner.py`

```python
def get_tools_schema() -> list:
    """获取主Agent基础工具Schema（不包含MCP工具）"""
    base_tools = [
        # 内置工具（11个）
        "code_run",
        "file_read", "file_patch", "file_write",
        "web_scan", "web_execute_js",
        "update_working_checkpoint", "start_long_term_update",
        "chat-with-file-processor",
        "chat-with-event-manager",
        "chat-with-context-manager",
    ]

    # 返回内置工具schema
    return [t for t in load_base_tools_schema() if t["function"]["name"] in base_tools]
```

**主Agent基础MCP工具**（固定注入）：
```python
BASE_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    "memory-server/update_memory",
    "memory-server/get_memory_stats",
    "memory-server/cleanup_memories",
    "memory-server/link_memories",

    # vector-store (5个)
    "vector-store/add_document",
    "vector-store/search_documents",
    "vector-store/get_document",
    "vector-store/delete_document",
    "vector-store/list_documents",
]
```

#### 1.2 实现动态工具检索

**修改文件**：`agent/runner.py`

```python
def _get_dynamic_tools(self, context: str) -> list:
    """
    根据上下文动态检索工具

    Args:
        context: 用户输入/LLM响应/工具结果/子Agent返回

    Returns:
        工具schema列表
    """
    # 向量递归检索
    results = self.vector_search.search(
        query=context,
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )

    # 获取工具schema
    tools = []
    for result in results:
        tool_name = result.metadata.get('name')
        server = result.metadata.get('server')
        full_name = f"{server}/{tool_name}"

        # 从ToolRegistry获取schema
        schema = self._get_tool_schema_by_name(full_name)
        if schema:
            tools.append(schema)

    return tools
```

#### 1.3 修改工具注入逻辑

**修改文件**：`agent/runner.py` 第370-373行

**旧代码**：
```python
tools_schema = self.base_tools_schema.copy()
if self._mcp_tools_schema:
    tools_schema.extend(self._mcp_tools_schema)  # 全部66个
```

**新代码**：
```python
# 基础工具 = 内置 + 基础MCP工具
tools_schema = self.base_tools_schema.copy()

# 固定注入基础MCP工具
for tool_name in BASE_MCP_TOOLS:
    schema = self._get_tool_schema_by_name(tool_name)
    if schema:
        tools_schema.append(schema)

# 动态注入其他工具（待Step 2实现）
# dynamic_tools = self._get_dynamic_tools(user_input)
# tools_schema.extend(dynamic_tools)
```

#### 1.4 取消config-manager工具

**原因**：
- 配置文件结构写入系统管理手册（Skills）
- 主Agent用 `bash + file_read/file_write` 修改配置
- 不需要专门的配置管理工具

**操作**：
- 从 `REQUIRED_SERVERS` 中移除 `config-manager`
- 或保留服务器但不注入工具到主Agent

### 完成标准

- [ ] 主Agent基础工具数量 = 22个（11内置 + 11 MCP）
- [ ] 向量检索能返回正确的MCP工具
- [ ] 递归查询测试通过
- [ ] 现有功能不受影响（子Agent仍能正常工作）

---

## Step 2: 工具生命周期管理

**任务ID**: #2
**依赖**: Step 1完成
**预计时间**: 60-90分钟

### 目标

实现工具命中状态管理，保证对话单元内工具持续可用

### 子任务

#### 2.1 实现ToolLifecycleManager类

**文件**：`agent/tool_lifecycle.py`（新建）

```python
from typing import Dict, List

class ToolLifecycleManager:
    """管理工具在对话单元中的生命周期"""

    def __init__(self, decay_rate: int = 10, min_score: int = 50):
        """
        Args:
            decay_rate: 每轮衰减分数
            min_score: 低于此分数移除工具
        """
        self.active_tools: Dict[str, int] = {}  # tool_name -> current_score
        self.decay_rate = decay_rate
        self.min_score = min_score

    def hit_tool(self, tool_name: str):
        """工具被命中，重置为100分"""
        self.active_tools[tool_name] = 100

    def decay_tools(self):
        """每轮对话后衰减所有工具分数"""
        to_remove = []
        for tool_name, score in self.active_tools.items():
            self.active_tools[tool_name] = score - self.decay_rate
            if self.active_tools[tool_name] < self.min_score:
                to_remove.append(tool_name)

        for tool_name in to_remove:
            del self.active_tools[tool_name]

    def get_active_tools(self) -> List[str]:
        """获取当前应该注入的工具列表"""
        return list(self.active_tools.keys())

    def clear(self):
        """清空所有活跃工具"""
        self.active_tools.clear()
```

#### 2.2 集成到NiuRunner

**修改文件**：`agent/runner.py`

```python
from .tool_lifecycle import ToolLifecycleManager

class NiuRunner:
    def __init__(self, ...):
        # ... 现有代码
        self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, min_score=50)
```

#### 2.3 在chat()中集成生命周期管理

**修改文件**：`agent/runner.py` 第344-392行

```python
def chat(self, session_id, user_input, ...):
    # 1. 向量检索工具
    matched_tools = self.vector_search.search(
        query=user_input,
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )

    # 2. 更新工具生命周期
    for result in matched_tools:
        tool_name = result.metadata.get('name')
        server = result.metadata.get('server')
        full_name = f"{server}/{tool_name}"
        self.tool_lifecycle.hit_tool(full_name)

    # 3. 获取所有活跃工具（包括命中的+之前未衰减完的）
    active_tool_names = self.tool_lifecycle.get_active_tools()

    # 4. 组装工具列表
    tools_schema = self.base_tools_schema.copy()
    for tool_name in active_tool_names:
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            tools_schema.append(schema)

    # 5. 调用LLM
    gen = agent_runner_loop(...)

    # 6. 对话结束后衰减工具分数
    self.tool_lifecycle.decay_tools()

    return gen
```

### 完成标准

- [ ] ToolLifecycleManager类实现完成
- [ ] 工具命中后分数为100
- [ ] 每轮衰减10分
- [ ] 低于50分自动移除
- [ ] 测试用例通过

---

## Step 3: 触发机制改进

**任务ID**: #3
**依赖**: Step 2完成
**预计时间**: 90-120分钟

### 目标

扩展工具加载触发源，实现多轮对话中动态工具加载

### 当前问题

**触发源单一**：
```python
# 只在chat()入口根据user_input触发
injection = self._inject_dynamic_resources(user_input)
```

**缺失的触发场景**：
- LLM响应："你是否需要我检索一下原有的记忆？"
- 用户回答："是的"
- 子Agent返回：关键内容
- 工具调用结果：需要进一步处理的场景

### 子任务

#### 3.1 分析agent_runner_loop结构

**文件**：`agent/generic/agent_loop.py`

**关键循环**：
```python
for turn in range(max_turns):
    # LLM调用
    response = client.chat(...)

    # 工具调用
    for tool_call in tool_calls:
        result = handler.dispatch(tool_name, args, response)
        # 问题：此时tools_schema已固定，无法添加新工具
```

#### 3.2 实现上下文监控

**修改文件**：`agent/generic/agent_loop.py`

**方案A：每轮检查**（推荐）

```python
def agent_runner_loop(...):
    for turn in range(max_turns):
        # 1. 检查是否需要注入新工具
        context = get_current_context(messages, last_response)
        if should_inject_tools(context):
            new_tools = search_tools_by_context(context)
            tools_schema = update_tools_schema(tools_schema, new_tools)

        # 2. LLM调用
        response = client.chat(system_prompt, tools_schema, messages)

        # 3. 工具调用
        for tool_call in tool_calls:
            result = handler.dispatch(tool_name, args, response)

            # 4. 检查工具结果是否需要新工具
            if should_inject_tools(result):
                new_tools = search_tools_by_context(result)
                tools_schema = update_tools_schema(tools_schema, new_tools)
```

**方案B：回调机制**

```python
def agent_runner_loop(...):
    for turn in range(max_turns):
        response = client.chat(...)

        for tool_call in tool_calls:
            result = handler.dispatch(tool_name, args, response)

            # 回调检查是否需要新工具
            if hasattr(handler, 'on_tool_result'):
                new_tools = handler.on_tool_result(result)
                tools_schema.extend(new_tools)
```

#### 3.3 实现should_inject_tools函数

```python
def should_inject_tools(context: str) -> bool:
    """
    判断是否需要注入新工具

    触发条件：
    1. LLM响应中包含"检索记忆"、"搜索文档"等关键词
    2. 工具结果需要进一步处理
    3. 子Agent返回关键内容
    """
    # 向量检索
    results = vector_search.search(
        query=context,
        limit=1,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )

    return len(results) > 0
```

#### 3.4 扩展触发源

**触发源列表**：
```python
TRIGGER_SOURCES = [
    "user_input",        # 用户输入
    "llm_response",      # LLM响应
    "tool_result",       # 工具调用结果
    "subagent_return",   # 子Agent返回
]
```

**实现**：
```python
def extract_context_from_messages(messages: list) -> str:
    """从消息列表提取上下文"""
    # 提取最近N条消息
    recent_messages = messages[-5:]

    # 拼接内容
    context = "\n".join([
        msg.get("content", "")
        for msg in recent_messages
        if msg.get("content")
    ])

    return context
```

### 完成标准

- [ ] agent_runner_loop内部实现上下文监控
- [ ] 每轮LLM调用前检查是否需要新工具
- [ ] 触发源扩展到4种
- [ ] 测试用例：用户说"是的"后能正确注入工具

---

## Step 4: 测试验证

**任务ID**: #6
**依赖**: Step 3完成
**预计时间**: 60-90分钟

### 目标

验证整个架构优化方案的可行性和效果

### 测试用例

#### 4.1 向量检索精度测试

```python
def test_vector_search_precision():
    """测试向量检索精度"""
    test_cases = [
        {
            "query": "入库这张照片",
            "expected": "photo-server/ingest_photo",
            "min_score": 0.5
        },
        {
            "query": "检索之前的记忆",
            "expected": "memory-server/recall",
            "min_score": 0.5
        },
        {
            "query": "搜索关于Python的文档",
            "expected": "vector-store/search_documents",
            "min_score": 0.5
        }
    ]

    for case in test_cases:
        results = vector_search.search(
            query=case["query"],
            limit=3,
            min_score=case["min_score"],
            filter={'category': 'mcp_tool'}
        )

        assert len(results) > 0, f"No results for: {case['query']}"
        assert case["expected"] in [r.metadata.get('name') for r in results]
```

#### 4.2 工具生命周期测试

```python
def test_tool_lifecycle():
    """测试工具生命周期管理"""
    manager = ToolLifecycleManager(decay_rate=10, min_score=50)

    # 1. 工具命中
    manager.hit_tool("memory-server/recall")
    assert manager.get_active_tools() == ["memory-server/recall"]
    assert manager.active_tools["memory-server/recall"] == 100

    # 2. 衰减
    manager.decay_tools()
    assert manager.active_tools["memory-server/recall"] == 90

    # 3. 持续衰减到移除
    for _ in range(5):
        manager.decay_tools()

    assert "memory-server/recall" not in manager.active_tools
```

#### 4.3 动态工具注入测试

```python
def test_dynamic_tool_injection():
    """测试动态工具注入"""
    runner = NiuRunner(...)

    # 1. 基础工具数量
    base_count = len(runner.base_tools_schema)
    assert base_count == 22, f"Expected 22 base tools, got {base_count}"

    # 2. 动态注入
    gen = runner.chat("test-session", "检索之前的记忆")

    # 3. 验证工具列表
    # (需要在agent_runner_loop中添加日志或断言)
```

#### 4.4 多轮对话测试

```python
def test_multi_turn_conversation():
    """测试多轮对话中工具保持"""
    runner = NiuRunner(...)

    # 第1轮
    gen = runner.chat("test-session", "帮我想想之前有没有类似的经验")
    response1 = "".join(list(gen))

    # 第2轮
    gen = runner.chat("test-session", "是的，检索一下")
    response2 = "".join(list(gen))

    # 验证：第2轮应该仍然有recall工具
    assert "memory-server/recall" in runner.tool_lifecycle.get_active_tools()
```

#### 4.5 端到端测试

**场景1：照片入库**
```
用户：入库这张照片 E:/test.jpg
→ 命中：photo-server/ingest_photo
→ 注入工具：[base + ingest_photo]
→ LLM：调用ingest_photo
→ 成功

用户：这个人是谁？
→ 未命中（工具已存在）
→ 工具列表：[base + ingest_photo]（分数90）
→ LLM：使用search_persons或get_unnamed_persons
→ 等等，这些工具不在列表中！
```

**发现问题**：ingest_photo执行后，可能需要search_persons等工具

**解决方案**：
1. photo-server所有工具绑定在一起
2. 或在工具结果中检查是否需要注入新工具

### 性能指标

| 指标 | 目标 | 测试方法 |
|------|------|---------|
| 向量检索精度 | > 70% | 100个测试用例 |
| 工具注入延迟 | < 50ms | 两轮递归检索 |
| 工具列表大小 | ≤ 30个 | 统计平均工具数 |
| 对话成功率 | > 95% | 端到端测试 |

### 完成标准

- [ ] 所有单元测试通过
- [ ] 集成测试通过
- [ ] 端到端测试通过
- [ ] 性能指标达标
- [ ] 无回归问题（现有功能不受影响）

---

## 风险与应对

### 风险1：向量检索精度不足

**症状**：用户说"检索记忆"但无法命中recall工具

**应对**：
- 增加查询模式（query_pattern）
- 降低阈值（min_score）
- 人工审查工具描述质量

### 风险2：工具加载延迟过高

**症状**：每轮对话增加100-200ms延迟

**应对**：
- 限制递归次数（max_recursion=3）
- 限制工具数量（limit=3）
- 缓存热门工具

### 风险3：工具生命周期管理失效

**症状**：工具过早移除或永不移除

**应对**：
- 调整衰减参数（decay_rate, min_score）
- 添加最大保留轮次限制
- 记录日志用于调试

### 风险4：回归问题

**症状**：现有功能异常

**应对**：
- 先不删除config-manager，只取消注入
- 保留ToolRegistry中的所有工具
- 分阶段实施，每步验证

---

## 实施时间表

| 阶段 | 任务 | 预计时间 | 完成标志 |
|------|------|---------|---------|
| 第1天 | Step 0: 数据准备 | 90分钟 | 向量库索引完成 |
| 第2天 | Step 1: 架构调整 | 120分钟 | 工具数量减少到22个 |
| 第3天 | Step 2: 生命周期管理 | 90分钟 | ToolLifecycleManager实现 |
| 第4天 | Step 3: 触发机制改进 | 120分钟 | 多触发源实现 |
| 第5天 | Step 4: 测试验证 | 90分钟 | 所有测试通过 |

**总计**：5天，约8小时

---

## 后续优化方向

### 短期（1-2周）

1. **工具描述质量优化**
   - 人工审查所有工具描述
   - 增加同义词、示例
   - 优化英文表达

2. **查询模式库扩展**
   - 收集用户实际用语
   - 增加覆盖率到80%+

3. **性能优化**
   - 热门工具预加载
   - 向量缓存
   - 并行检索

### 中期（1-2月）

1. **自适应参数调整**
   - 动态调整衰减率
   - 根据使用频率优化阈值

2. **工具使用分析**
   - 统计工具命中率
   - 识别冗余工具
   - 优化工具组合

3. **子Agent工具隔离**
   - 彻底移除子Agent专用工具
   - 实现严格的工具权限控制

### 长期（3-6月）

1. **CLI+Skills方案探索**
   - 评估MCP工具转CLI的可行性
   - 实现自我进化机制

2. **架构重构**
   - 简化MCP架构
   - 统一工具调用接口

---

## 参考文档

- `docs/design-vector-recursive-query.md` - 向量递归查询设计
- `docs/spec-L1-summary.md` - L1摘要规范
- `config/agents/file-processor.md` - 文件处理子Agent配置
- `config/agents/event-manager.md` - 事件管理子Agent配置
- `config/agents/context-manager.md` - 上下文管理子Agent配置

---

## 变更日志

- 2026-04-10: 初版，完整实施计划
