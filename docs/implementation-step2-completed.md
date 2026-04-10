# Step 2: 工具生命周期管理（已完成）

> 完成日期：2026-04-10
> 状态：✅ 已完成
> 验证：测试通过

---

## 实施内容

### 2.1 创建 ToolLifecycleManager 类

**文件**：`agent/tool_lifecycle.py`（新建）

**核心功能**：
```python
class ToolLifecycleManager:
    """管理工具在对话单元中的生命周期"""

    def __init__(self, decay_rate: int = 10, min_score: int = 50):
        """
        Args:
            decay_rate: 每轮衰减分数（默认10分/轮）
            min_score: 低于此分数移除工具（默认50分）
        """
        self.active_tools: Dict[str, int] = {}  # tool_name -> current_score
        self.decay_rate = decay_rate
        self.min_score = min_score

    def hit_tool(self, tool_name: str):
        """工具被命中，重置为100分"""

    def decay_tools(self):
        """每轮对话后衰减所有工具分数"""

    def get_active_tools(self) -> List[str]:
        """获取当前应该注入的工具列表"""

    def clear(self):
        """清空所有活跃工具"""
```

**机制**：
- **命中**：工具被检索到 → 设置分数为 100
- **衰减**：每轮对话后 -10 分
- **移除**：分数 < 50 分时自动移除
- **持久化**：确保对话单元内工具持续可用

---

### 2.2 集成到 NiuRunner

**修改文件**：`agent/runner.py`

**添加初始化**：
```python
from .tool_lifecycle import ToolLifecycleManager

class NiuRunner:
    def __init__(self, ...):
        # ... 现有代码
        self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, min_score=50)
```

---

### 2.3 在 chat() 中集成生命周期管理

**修改位置**：`agent/runner.py` 的 `chat()` 方法

**核心流程**：
```python
def chat(self, session_id, user_input, ...):
    # 1. 向量检索工具
    matched_tools = self.vector_search.search(
        query=user_input,
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )

    # 2. 更新工具生命周期（命中工具设置为100分）
    for result in matched_tools:
        tool_name = result.metadata.get('name')
        server = result.metadata.get('server')
        full_name = f"{server}/{tool_name}"
        self.tool_lifecycle.hit_tool(full_name)

    # 3. 获取所有活跃工具（包括命中的 + 之前未衰减完的）
    active_tool_names = self.tool_lifecycle.get_active_tools()

    # 4. 组装 tools_schema
    tools_schema = self.base_tools_schema.copy()

    # 固定注入基础MCP工具
    for tool_name in BASE_MCP_TOOLS:
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            tools_schema.append(schema)

    # 注入活跃工具（排除基础MCP工具，避免重复）
    for tool_name in active_tool_names:
        if tool_name in BASE_MCP_TOOLS:
            continue
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            tools_schema.append(schema)

    # ... 执行对话 ...

    # 5. 对话结束后衰减工具分数
    self.tool_lifecycle.decay_tools()

    yield full_resp.strip()
```

---

## 测试验证

### 测试脚本

**文件**：`scripts/test_tool_lifecycle.py`

### 测试结果

```
=== 测试工具生命周期管理 ===

1. 测试工具命中
   ✓ 工具命中正确

2. 测试分数衰减
   ✓ 分数衰减正确

3. 测试持续衰减
   ✓ 持续衰减正确

4. 测试工具移除（分数 < 50）
   ✓ 工具移除正确

5. 测试工具重生（重新命中）
   ✓ 工具重生正确

6. 测试清空所有工具
   ✓ 清空功能正确

=== 测试工具持久化（模拟多轮对话） ===

第1轮：用户说'入库照片'
  命中工具: photo-server/ingest_photo, 分数: 100

第2轮：用户说'是的'
  photo-server/ingest_photo 分数: 90

第3轮：用户再次提到'处理照片'
  重新命中，分数重置为: 100

第4-8轮：用户讨论其他话题
  第8轮衰减后分数: 0
  最终活跃工具: []
  ✓ 工具正确移除

=== 所有测试完成 ===
```

---

## 效果演示

### 场景1：照片处理多轮对话

| 轮次 | 用户输入 | 工具状态 | 分数 |
|------|----------|----------|------|
| 1 | "入库照片" | 命中 ingest_photo | 100 → 90 |
| 2 | "是的" | ingest_photo 持续 | 90 → 80 |
| 3 | "处理照片" | 重新命中 ingest_photo | 100 → 90 |
| 4-8 | 其他话题 | 持续衰减 | 80 → 70 → 60 → 50 → 40 → 移除 |

### 场景2：工具持久化

**优势**：
- 用户说"入库照片"，然后说"是的"确认
- `ingest_photo` 工具仍然可用，无需重新检索
- 提升对话流畅度

---

## 核心机制

### 分数计算

```
初始命中: 100 分
每轮衰减: -10 分
存活阈值: >= 50 分
生命周期: 100 → 90 → 80 → 70 → 60 → 50 → 移除（共6轮）
```

### 参数调优

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `decay_rate` | 10 | 每轮衰减分数 |
| `min_score` | 50 | 移除阈值 |

**调优建议**：
- 工具变化频繁：提高 `decay_rate` 到 15
- 工具使用持久：降低 `min_score` 到 30
- 当前参数适合大多数场景

---

## 后续步骤

**Step 3：触发机制改进**（待实施）
- 扩展触发源：user_input, llm_response, tool_result, subagent_return
- 实现上下文监控
- 在 `agent_runner_loop` 中检查是否需要新工具

**Step 4：测试验证**（待实施）
- 向量检索精度测试
- 工具生命周期测试
- 动态工具注入测试
- 多轮对话测试
- 端到端测试

---

## 相关文档

- `docs/implementation-plan-tool-injection-optimization.md` — 完整实施计划
- `docs/tool-layer-decision.md` — 工具分层决策
- `agent/tool_lifecycle.py` — 工具生命周期管理类
- `scripts/test_tool_lifecycle.py` — 测试脚本
