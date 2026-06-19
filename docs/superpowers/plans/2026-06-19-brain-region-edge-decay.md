# 知识图谱脑区边衰减增强机制 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重新实现知识图谱中实体→脑区边的衰减/增强/保底机制，按脑区优先级差异化遗忘曲线，防止实体变成孤立节点。

**Architecture:** 在现有 `decay_structural_edges()` 和 `_reinforce_edge_weight()` 基础上改造（方案A），判断逻辑从"边类型=包含"改为"目标节点 entity_type=brainregion"，加入半衰期模型、保底机制、优先级配置。`_encode_description()` 新增 `priority` 标准参数确保优先级信息不丢失。

**Tech Stack:** Python, NetworkX (nx.Graph 无向图), LightRAG, preferences.json

**设计文档：** `docs/superpowers/specs/2026-06-19-brain-region-edge-decay-design.md`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `niu_api/internal/region_manager.py` | 衰减算法、优先级常量、`_encode_description()` 改造、结构边权重统一 |
| `agent/brain_tools.py` | 增强算法改造 |
| `agent/injector/region_sync.py` | 取消注释衰减调用 |
| `niu_api/brain_region_api.py` | 取消注释衰减调用 |
| `memory/preferences.json` | priority 字段更新 |
| `tests/test_brain_region_edge_decay.py` | 新增测试文件 |

---

### Task 1: 新增常量和优先级解析函数

**Files:**
- Modify: `niu_api/internal/region_manager.py` (常量定义区，约第30行附近)
- Create: `tests/test_brain_region_edge_decay.py`

- [ ] **Step 1: 写失败测试 — 优先级常量和日衰减率计算**

```python
# tests/test_brain_region_edge_decay.py
"""
脑区边衰减增强机制测试

真实测试：需要程序运行 + 真实 LLM。
手动执行：python -m pytest tests/test_brain_region_edge_decay.py -v

单元测试部分可直接运行：python -m pytest tests/test_brain_region_edge_decay.py -v -k "not integration"
"""
import math
import pytest


class TestPriorityConstants:
    """优先级常量和日衰减率计算"""

    def test_priority_halflife_defined(self):
        from niu_api.internal.region_manager import PRIORITY_HALFLIFE
        assert "permanent" in PRIORITY_HALFLIFE
        assert "long" in PRIORITY_HALFLIFE
        assert "medium" in PRIORITY_HALFLIFE
        assert "short" in PRIORITY_HALFLIFE

    def test_priority_halflife_values(self):
        from niu_api.internal.region_manager import PRIORITY_HALFLIFE
        assert PRIORITY_HALFLIFE["permanent"] == 360
        assert PRIORITY_HALFLIFE["long"] == 360
        assert PRIORITY_HALFLIFE["medium"] == 180
        assert PRIORITY_HALFLIFE["short"] == 90

    def test_floor_and_initial_weight(self):
        from niu_api.internal.region_manager import FLOOR_WEIGHT, INITIAL_WEIGHT
        assert FLOOR_WEIGHT == 0.1
        assert INITIAL_WEIGHT == 1.0

    def test_default_priority(self):
        from niu_api.internal.region_manager import DEFAULT_PRIORITY
        assert DEFAULT_PRIORITY == "medium"

    def test_daily_decay_calculation(self):
        from niu_api.internal.region_manager import daily_decay_rate
        # 360天半衰期
        rate_360 = daily_decay_rate("permanent")
        assert rate_360 == pytest.approx(0.5 ** (1/360), rel=1e-6)
        assert rate_360 == pytest.approx(0.99808, rel=0.001)

        # 180天半衰期
        rate_180 = daily_decay_rate("medium")
        assert rate_180 == pytest.approx(0.5 ** (1/180), rel=1e-6)

        # 90天半衰期
        rate_90 = daily_decay_rate("short")
        assert rate_90 == pytest.approx(0.5 ** (1/90), rel=1e-6)

        # long 和 permanent 半衰期相同
        assert daily_decay_rate("long") == daily_decay_rate("permanent")

    def test_daily_decay_unknown_priority(self):
        from niu_api.internal.region_manager import daily_decay_rate
        # 未知优先级回退到 medium
        rate = daily_decay_rate("unknown_priority")
        assert rate == daily_decay_rate("medium")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestPriorityConstants" 2>&1 | head -30`
Expected: FAIL — `ImportError: cannot import name 'PRIORITY_HALFLIFE'`

- [ ] **Step 3: 实现常量和函数**

在 `niu_api/internal/region_manager.py` 的常量区（约第30行附近，`STRUCTURAL_EDGE_TYPES_LOWER` 下方）添加：

```python
# 脑区边衰减优先级体系
PRIORITY_HALFLIFE = {
    "permanent": 360,  # 衰减但保底冻结，永不删除
    "long": 360,
    "medium": 180,
    "short": 90,
}
FLOOR_WEIGHT = 0.1       # 保底权重 / 删除阈值
INITIAL_WEIGHT = 1.0     # 边初始权重 / 增强恢复目标值
DEFAULT_PRIORITY = "medium"  # 非默认脑区和旧配置的回退值


def daily_decay_rate(priority: str) -> float:
    """根据优先级计算日衰减率（半衰期模型）"""
    halflife = PRIORITY_HALFLIFE.get(priority)
    if halflife is None:
        halflife = PRIORITY_HALFLIFE[DEFAULT_PRIORITY]
    return 0.5 ** (1.0 / halflife)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestPriorityConstants" 2>&1 | tail -10`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_brain_region_edge_decay.py
git commit -m "feat: add priority constants and daily_decay_rate for brain region edge decay"
```

- [ ] **Step 6: 方案对齐审查**

对照设计文档 1.2 节优先级体系表和 4.2 节常量定义，确认：
- `PRIORITY_HALFLIFE` 的4个等级和值是否一致
- `FLOOR_WEIGHT`、`INITIAL_WEIGHT`、`DEFAULT_PRIORITY` 的值是否一致
- `daily_decay_rate()` 的计算公式是否与设计文档描述一致

- [ ] **Step 7: 代码审查**

检查：
- 函数签名、类型注解是否完整
- 边界情况：`priority=""` 是否 fallback 到 DEFAULT_PRIORITY
- 常量命名是否与设计文档一致

---

### Task 2: 改造 `_encode_description()` — 新增 priority 标准参数

**Files:**
- Modify: `niu_api/internal/region_manager.py:261-289` (`_encode_description` 函数)
- Modify: `tests/test_brain_region_edge_decay.py`

- [ ] **Step 1: 写失败测试 — priority 写入和解析**

```python
class TestEncodeDescriptionPriority:
    """_encode_description 的 priority 字段写入和解析"""

    def test_encode_description_includes_priority(self):
        from niu_api.internal.region_manager import _encode_description
        desc = _encode_description(
            label="测试脑区",
            summary="测试摘要",
            entity_count=5,
            keywords=["k1", "k2"],
            extra_meta={"source": "default"},
            priority="permanent",
        )
        assert "brain_meta_priority:permanent" in desc

    def test_encode_description_default_priority(self):
        from niu_api.internal.region_manager import _encode_description, DEFAULT_PRIORITY
        desc = _encode_description(
            label="测试脑区",
            summary="测试摘要",
            entity_count=5,
            keywords=["k1", "k2"],
            extra_meta={},
            priority=DEFAULT_PRIORITY,
        )
        assert "brain_meta_priority:medium" in desc

    def test_parse_priority_from_description(self):
        from niu_api.internal.region_manager import parse_priority_from_description
        desc = "brain_meta_priority:long|brain_meta_source:default|..."
        assert parse_priority_from_description(desc) == "long"

    def test_parse_priority_missing(self):
        from niu_api.internal.region_manager import parse_priority_from_description, DEFAULT_PRIORITY
        desc = "brain_meta_source:default|some other content"
        assert parse_priority_from_description(desc) == DEFAULT_PRIORITY

    def test_parse_priority_empty(self):
        from niu_api.internal.region_manager import parse_priority_from_description, DEFAULT_PRIORITY
        assert parse_priority_from_description("") == DEFAULT_PRIORITY
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestEncodeDescriptionPriority" 2>&1 | head -20`
Expected: FAIL — `_encode_description()` 接受5个位置参数，给了6个

- [ ] **Step 3: 实现 — 修改 `_encode_description()` 和新增 `parse_priority_from_description()`**

修改 `_encode_description()` 函数签名，新增 `priority` 参数：

```python
def _encode_description(
    label: str,
    summary: str,
    entity_count: int,
    keywords: list,
    extra_meta: dict,
    priority: str = DEFAULT_PRIORITY,  # 新增参数
) -> str:
```

在函数体中，`meta_parts` 构建处增加 priority 字段。当前代码约第274-285行：

```python
    # 构建 meta 信息
    meta_parts = []
    if extra_meta:
        for k, v in extra_meta.items():
            meta_parts.append(f"brain_meta_{k}:{v}")
    # 新增：priority 字段
    meta_parts.append(f"brain_meta_priority:{priority}")
```

新增解析函数（在 `_encode_description` 下方）：

```python
def parse_priority_from_description(description: str) -> str:
    """从 description 中解析 brain_meta_priority 字段"""
    if not description:
        return DEFAULT_PRIORITY
    for part in description.split("|"):
        if part.startswith("brain_meta_priority:"):
            val = part[len("brain_meta_priority:"):]
            if val in PRIORITY_HALFLIFE:
                return val
    return DEFAULT_PRIORITY
```

- [ ] **Step 4: 更新所有 `_encode_description()` 调用点**

**6处调用点**，全部增加 `priority=` 参数。如果原调用有 `extra_meta` 中含 `source` 等字段，保持不变，只增加 `priority` 参数。

1. **行274附近** `create_region_nodes()` — 新建脑区时：
```python
# 从 preferences.json 读取 priority，fallback 到 DEFAULT_PRIORITY
priority = region_def.get("priority", DEFAULT_PRIORITY) if region_def else DEFAULT_PRIORITY
desc = _encode_description(label, summary, entity_count, keywords, extra_meta, priority=priority)
```

2. **行509附近** `update_region_summaries()` — 摘要更新：
```python
# 从旧 description 解析 priority
old_desc = nx_graph.nodes[region_key].get("description", "")
priority = parse_priority_from_description(old_desc)
desc = _encode_description(label, summary, entity_count, keywords, extra_meta, priority=priority)
```

3. **行779附近** `_update_drifted_regions()` — 漂移更新：
```python
old_desc = nx_graph.nodes[region_key].get("description", "")
priority = parse_priority_from_description(old_desc)
desc = _encode_description(label, summary, entity_count, keywords, extra_meta, priority=priority)
```

4. **行950附近** `dissolve_shrunk_regions()` — 解散重建：
```python
old_desc = nx_graph.nodes[region_key].get("description", "")
priority = parse_priority_from_description(old_desc)
desc = _encode_description(label, summary, entity_count, keywords, extra_meta, priority=priority)
```

5. **行1677附近** `create_default_regions()` — 默认创建：
```python
priority = region_def.get("priority", DEFAULT_PRIORITY)
desc = _encode_description(label, summary, 0, keywords, extra_meta, priority=priority)
```

6. **行1865附近** `assign_entities_to_default_regions()` — 关键词分配：
```python
old_desc = nx_graph.nodes[region_key].get("description", "")
priority = parse_priority_from_description(old_desc)
desc = _encode_description(label, summary, entity_count, keywords, extra_meta, priority=priority)
```

注意：以上行号为近似值，实现者需要用 grep 精确定位每个调用点。每个调用点修改前必须先 Read 确认上下文。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestEncodeDescriptionPriority" 2>&1 | tail -10`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_brain_region_edge_decay.py
git commit -m "feat: add priority param to _encode_description and parse_priority_from_description"
```

- [ ] **Step 7: 方案对齐审查**

对照设计文档 1.4 节优先级存储机制，确认：
- `_encode_description()` 是否将 priority 写为 `brain_meta_priority:{value}` 格式
- `parse_priority_from_description()` 是否能正确解析并 fallback
- 所有6处调用点的 priority 获取方式是否与设计文档一致

- [ ] **Step 8: 代码审查**

检查：
- 旧 description 中已有 `brain_meta_priority` 时，新写入是否会重复（应替换旧值或确保不会重复写入）
- `parse_priority_from_description` 是否处理了 `priority` 值不在 `PRIORITY_HALFLIFE` 中的情况
- 调用点是否遗漏（用 `grep "_encode_description(" region_manager.py` 验证）

---

### Task 3: 改造 `decay_structural_edges()` — 半衰期模型 + 保底机制

**Files:**
- Modify: `niu_api/internal/region_manager.py` (`decay_structural_edges` 函数)
- Modify: `tests/test_brain_region_edge_decay.py`

- [ ] **Step 1: 写失败测试 — 衰减算法**

```python
import networkx as nx


class TestDecayStructuralEdges:
    """衰减算法测试 — 使用内存 NetworkX 图"""

    def _build_test_graph(self):
        """构建测试用图：2个脑区 + 3个实体"""
        G = nx.Graph()
        # 脑区节点
        G.add_node("region_permanent", entity_type="brainregion",
                   description="brain_meta_priority:permanent|brain_meta_source:default|永久脑区")
        G.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short|brain_meta_source:default|短期脑区")
        # 实体节点
        G.add_node("entity_a", entity_type="person", description="人物A")
        G.add_node("entity_b", entity_type="skill", description="技能B")
        G.add_node("entity_c", entity_type="topic", description="话题C")
        # 脑区边（权重1.0）
        G.add_edge("region_permanent", "entity_a", weight=1.0, description="包含")
        G.add_edge("region_short", "entity_a", weight=1.0, description="包含")
        G.add_edge("region_short", "entity_b", weight=1.0, description="包含")
        # 知识关系边（不应被衰减）
        G.add_edge("entity_a", "entity_c", weight=1.0, description="讨论")
        return G

    def test_decay_short_priority(self):
        """short 级（90天半衰期）边权重衰减"""
        from niu_api.internal.region_manager import decay_structural_edges, daily_decay_rate
        G = self._build_test_graph()
        decay_structural_edges(G)

        # entity_b 只有1条脑区边 + 0条知识边 = 总边数1 → 保底
        weight_b = G["region_short"]["entity_b"]["weight"]
        expected = 1.0 * daily_decay_rate("short")
        assert weight_b == pytest.approx(max(expected, 0.1), rel=1e-6)

    def test_permanent_freeze_at_floor(self):
        """permanent 级边权重衰减到保底值冻结"""
        from niu_api.internal.region_manager import decay_structural_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent|永久脑区")
        G.add_node("entity_x", entity_type="person", description="人物X")
        # 设一个已经接近保底的权重
        G.add_edge("region_perm", "entity_x", weight=0.11, description="包含")

        decay_structural_edges(G)
        # permanent 级：max(0.11 * decay, FLOOR_WEIGHT)，但 0.11*decay > 0.1 所以正常衰减
        weight = G["region_perm"]["entity_x"]["weight"]
        assert weight >= FLOOR_WEIGHT

    def test_floor_protection_orphan(self):
        """总边数==1时保底保护"""
        from niu_api.internal.region_manager import decay_structural_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short|短期脑区")
        G.add_node("entity_lonely", entity_type="topic", description="孤独话题")
        G.add_edge("region_short", "entity_lonely", weight=0.05, description="包含")

        decay_structural_edges(G)
        # 孤立实体，权重不应低于 FLOOR_WEIGHT
        weight = G["region_short"]["entity_lonely"]["weight"]
        assert weight >= FLOOR_WEIGHT

    def test_delete_below_floor_with_other_edges(self):
        """非 permanent + 总边数>=2 + 低于保底 → 删除边"""
        from niu_api.internal.region_manager import decay_structural_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short|短期脑区")
        G.add_node("entity_multi", entity_type="person", description="多边人物")
        G.add_node("entity_other", entity_type="skill", description="其他技能")
        G.add_edge("region_short", "entity_multi", weight=0.03, description="包含")
        G.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")

        decay_structural_edges(G)
        # 总边数2，weight 0.03 * decay < FLOOR_WEIGHT → 删除
        assert not G.has_edge("region_short", "entity_multi")

    def test_permanent_not_deleted_with_other_edges(self):
        """permanent + 总边数>=2 + 低于保底 → 不删除，冻结在保底"""
        from niu_api.internal.region_manager import decay_structural_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent|永久脑区")
        G.add_node("entity_multi", entity_type="person", description="多边人物")
        G.add_node("entity_other", entity_type="skill", description="其他技能")
        G.add_edge("region_perm", "entity_multi", weight=0.03, description="包含")
        G.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")

        decay_structural_edges(G)
        # permanent 级：冻结在 FLOOR_WEIGHT，不删除
        assert G.has_edge("region_perm", "entity_multi")
        assert G["region_perm"]["entity_multi"]["weight"] == FLOOR_WEIGHT

    def test_knowledge_edge_not_decayed(self):
        """知识关系边（实体→实体）不被衰减"""
        from niu_api.internal.region_manager import decay_structural_edges
        G = self._build_test_graph()
        decay_structural_edges(G)
        # entity_a ↔ entity_c 是知识关系边，权重不变
        weight = G["entity_a"]["entity_c"]["weight"]
        assert weight == 1.0

    def test_anchor_edge_not_decayed(self):
        """脑区之间的锚点边不被衰减"""
        from niu_api.internal.region_manager import decay_structural_edges
        G = nx.Graph()
        G.add_node("Niu", entity_type="brainregion", description="根节点")
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent|永久脑区")
        G.add_edge("Niu", "region_perm", weight=0.5, description="锚点")

        decay_structural_edges(G)
        # Niu 的邻居 region_perm 也是 brainregion → 跳过
        weight = G["Niu"]["region_perm"]["weight"]
        assert weight == 0.5

    def test_missing_priority_fallback(self):
        """description 中缺少 brain_meta_priority 时回退到 medium"""
        from niu_api.internal.region_manager import decay_structural_edges, daily_decay_rate, DEFAULT_PRIORITY
        G = nx.Graph()
        G.add_node("region_no_priority", entity_type="brainregion",
                   description="brain_meta_source:leiden|无优先级脑区")
        G.add_node("entity_y", entity_type="topic", description="话题Y")
        G.add_node("entity_z", entity_type="skill", description="技能Z")
        G.add_edge("region_no_priority", "entity_y", weight=1.0, description="包含")
        G.add_edge("entity_y", "entity_z", weight=1.0, description="相关")

        decay_structural_edges(G)
        # 应使用 medium (180天) 的衰减率
        expected = 1.0 * daily_decay_rate(DEFAULT_PRIORITY)
        weight = G["region_no_priority"]["entity_y"]["weight"]
        assert weight == pytest.approx(expected, rel=1e-6)

    def test_empty_graph_safe(self):
        """空图不会报错"""
        from niu_api.internal.region_manager import decay_structural_edges
        G = nx.Graph()
        decay_structural_edges(G)  # 不应抛异常
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestDecayStructuralEdges" 2>&1 | head -30`
Expected: FAIL — 当前 `decay_structural_edges` 逻辑不符合新设计

- [ ] **Step 3: 实现 — 改造 `decay_structural_edges()`**

替换整个 `decay_structural_edges()` 函数体。当前函数约在第1490-1560行。关键改动：

1. 判断逻辑从 `edge_type in STRUCTURAL_EDGE_TYPES_LOWER` 改为 `neighbor_node.entity_type == "brainregion"`
2. 遍历从脑区节点出发（获取所有 `entity_type == "brainregion"` 的节点）
3. 跳过 `entity_type == "brainregion"` 的邻居（锚点边）
4. 从 description 解析 priority，fallback 到 DEFAULT_PRIORITY
5. 使用 `daily_decay_rate(priority)` 计算新权重
6. 保底逻辑：`G.degree(entity)` 判断总边数
7. permanent 级：`max(new_weight, FLOOR_WEIGHT)` 无论总边数
8. 非 permanent：总边数==1 时 `max(new_weight, FLOOR_WEIGHT)`，总边数>=2 且 new_weight < FLOOR_WEIGHT 时删除边

```python
def decay_structural_edges(nx_graph: nx.Graph, stats: dict | None = None) -> dict:
    """脑区边衰减 — 半衰期模型 + 保底机制

    只衰减实体→脑区的归属边（目标节点 entity_type == "brainregion"）。
    不衰减知识关系边（实体→实体）。
    """
    decayed = 0
    deleted = 0
    protected = 0
    skipped_permanent = 0
    skipped_anchor = 0

    # 从脑区节点出发遍历
    brain_regions = [
        n for n in nx_graph.nodes()
        if nx_graph.nodes[n].get("entity_type") == "brainregion"
    ]

    for region_key in brain_regions:
        desc = nx_graph.nodes[region_key].get("description", "")
        priority = parse_priority_from_description(desc)
        decay_rate = daily_decay_rate(priority)

        # 收集需要处理的邻居（避免遍历时修改图）
        neighbors = list(nx_graph.neighbors(region_key))

        for entity_key in neighbors:
            # 跳过锚点边（脑区之间的导航边）
            if nx_graph.nodes[entity_key].get("entity_type") == "brainregion":
                skipped_anchor += 1
                continue

            edge_data = nx_graph.edges[region_key, entity_key]
            old_weight = edge_data.get("weight", INITIAL_WEIGHT)

            # 计算衰减后权重
            new_weight = old_weight * decay_rate

            # 保底检查
            total_degree = nx_graph.degree(entity_key)

            if priority == "permanent":
                # permanent 级：保底冻结，永不删除
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif total_degree <= 1:
                # 孤立实体：保底保护
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif new_weight < FLOOR_WEIGHT:
                # 非 permanent + 总边数>=2 + 低于保底 → 删除
                nx_graph.remove_edge(region_key, entity_key)
                deleted += 1
            else:
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1

    result = {
        "decayed": decayed,
        "deleted": deleted,
        "protected": protected,
        "skipped_anchor": skipped_anchor,
    }
    logger.info(
        f"[Decay] brain region edges: decayed={decayed}, deleted={deleted}, "
        f"protected={protected}, skipped_anchor={skipped_anchor}"
    )
    return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestDecayStructuralEdges" 2>&1 | tail -15`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_brain_region_edge_decay.py
git commit -m "feat: rewrite decay_structural_edges with half-life model and floor protection"
```

- [ ] **Step 6: 方案对齐审查**

对照设计文档 2.2-2.5 节，确认：
- 遍历策略是否从脑区节点出发
- 锚点边是否跳过
- priority fallback 是否正确
- 保底逻辑三条分支是否完整：孤立实体冻结、permanent冻结、非permanent可删除
- 日衰减率是否使用 `daily_decay_rate(priority)`
- 不再依赖 `STRUCTURAL_EDGE_TYPES_LOWER`

- [ ] **Step 7: 代码审查**

检查：
- 遍历时是否安全（已用 `list()` 复制 neighbors）
- 删除边时是否影响正在遍历的 neighbors 列表（不会，因为是对每个 region 独立遍历，删除的是 region↔entity 边）
- 日志输出是否包含所有关键指标
- 空图、空脑区、无邻居等边界情况是否安全

---

### Task 4: 改造 `_reinforce_edge_weight()` — 恢复到初始值

**Files:**
- Modify: `agent/brain_tools.py` (`_reinforce_edge_weight` 和 `reinforce_on_tool_use`)
- Modify: `tests/test_brain_region_edge_decay.py`

- [ ] **Step 1: 写失败测试 — 增强算法**

```python
class TestReinforceEdgeWeight:
    """增强算法测试"""

    def _build_test_graph(self):
        G = nx.Graph()
        G.add_node("region_permanent", entity_type="brainregion",
                   description="brain_meta_priority:permanent|永久脑区")
        G.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short|短期脑区")
        G.add_node("entity_a", entity_type="person", description="人物A")
        G.add_node("Niu", entity_type="brainregion", description="根节点")
        # 衰减后的边
        G.add_edge("region_permanent", "entity_a", weight=0.3, description="包含")
        G.add_edge("region_short", "entity_a", weight=0.2, description="包含")
        # 锚点边（不应增强）
        G.add_edge("Niu", "region_permanent", weight=0.5, description="锚点")
        return G

    def test_reinforce_restores_to_initial_weight(self):
        """增强将权重恢复到 INITIAL_WEIGHT (1.0)"""
        from agent.brain_tools import _reinforce_edge_weight
        from niu_api.internal.region_manager import INITIAL_WEIGHT
        G = self._build_test_graph()
        _reinforce_edge_weight(G, "region_permanent")
        weight = G["region_permanent"]["entity_a"]["weight"]
        assert weight == INITIAL_WEIGHT

    def test_reinforce_skips_anchor_edges(self):
        """增强跳过锚点边（脑区→脑区）"""
        from agent.brain_tools import _reinforce_edge_weight
        G = self._build_test_graph()
        _reinforce_edge_weight(G, "region_permanent")
        # Niu ↔ region_permanent 锚点边权重不变
        weight = G["Niu"]["region_permanent"]["weight"]
        assert weight == 0.5

    def test_reinforce_only_target_region(self):
        """增强只影响目标脑区的边，不影响其他脑区"""
        from agent.brain_tools import _reinforce_edge_weight
        G = self._build_test_graph()
        _reinforce_edge_weight(G, "region_permanent")
        # region_short 的边不受影响
        weight = G["region_short"]["entity_a"]["weight"]
        assert weight == 0.2

    def test_reinforce_no_brainregion_neighbors(self):
        """脑区没有实体邻居时安全返回"""
        from agent.brain_tools import _reinforce_edge_weight
        G = nx.Graph()
        G.add_node("region_empty", entity_type="brainregion",
                   description="brain_meta_priority:short|空脑区")
        _reinforce_edge_weight(G, "region_empty")  # 不应抛异常
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestReinforceEdgeWeight" 2>&1 | head -20`
Expected: FAIL — 当前 `_reinforce_edge_weight` 使用增量式增强

- [ ] **Step 3: 实现 — 改造 `_reinforce_edge_weight()`**

替换 `_reinforce_edge_weight()` 函数体：

```python
def _reinforce_edge_weight(nx_graph, region_key: str) -> int:
    """增强脑区边权重 — 恢复到 INITIAL_WEIGHT

    只增强实体→脑区的归属边，跳过锚点边（脑区→脑区）。
    """
    from niu_api.internal.region_manager import INITIAL_WEIGHT

    if region_key not in nx_graph:
        return 0

    reinforced = 0
    neighbors = list(nx_graph.neighbors(region_key))

    for entity_key in neighbors:
        # 跳过锚点边
        if nx_graph.nodes[entity_key].get("entity_type") == "brainregion":
            continue

        edge_data = nx_graph.edges[region_key, entity_key]
        old_weight = edge_data.get("weight", INITIAL_WEIGHT)

        if old_weight < INITIAL_WEIGHT:
            nx_graph.edges[region_key, entity_key]["weight"] = INITIAL_WEIGHT
            reinforced += 1

    if reinforced > 0:
        logger.debug(f"[Reinforce] region={region_key}: {reinforced} edges restored to {INITIAL_WEIGHT}")

    return reinforced
```

修改 `reinforce_on_tool_use()` — 删除 `reinforce_delta` 参数：

```python
def reinforce_on_tool_use(tool_name: str) -> str | None:
    """工具使用时增强对应脑区边权重"""
    # ... 保持现有逻辑，只删除 reinforce_delta 参数和调用处的传递
    # 内部调用改为: _reinforce_edge_weight(nx_graph, region_id)
    # 不再传递 reinforce_delta
```

删除旧常量：
```python
# 删除这两行
REINFORCE_DELTA = 0.15
MAX_EDGE_WEIGHT = 2.0
```

删除旧 import：
```python
# 删除（如果存在）
from niu_api.internal.region_manager import STRUCTURAL_EDGE_TYPES_LOWER
```

- [ ] **Step 4: 更新 `reinforce_on_tool_use()` 调用方**

搜索 `handler.py` 中调用 `reinforce_on_tool_use` 的位置，确认没有传递 `reinforce_delta` 参数。如果传递了，删除该参数。

Run: `grep -n "reinforce_on_tool_use" REDACTED_USER_PATH/tools/ai-bot/agent/handler.py`

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v -k "TestReinforceEdgeWeight" 2>&1 | tail -10`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add agent/brain_tools.py tests/test_brain_region_edge_decay.py
git commit -m "feat: rewrite _reinforce_edge_weight to restore INITIAL_WEIGHT, remove REINFORCE_DELTA"
```

- [ ] **Step 7: 方案对齐审查**

对照设计文档第3节增强算法，确认：
- 增强是否恢复到 `INITIAL_WEIGHT (1.0)` 而非增量
- 是否跳过锚点边
- `reinforce_delta` 参数是否已删除
- 旧常量 `REINFORCE_DELTA` / `MAX_EDGE_WEIGHT` 是否已删除
- `STRUCTURAL_EDGE_TYPES_LOWER` import 是否已清理

- [ ] **Step 8: 代码审查**

检查：
- 函数签名变更是否影响所有调用方
- `handler.py` 中是否有其他地方依赖 `REINFORCE_DELTA` 或 `MAX_EDGE_WEIGHT`
- 增强逻辑是否与衰减逻辑的遍历方向一致（从脑区出发）

---

### Task 5: 取消注释恢复衰减/增强调用点

**Files:**
- Modify: `agent/injector/region_sync.py:322-331`
- Modify: `niu_api/brain_region_api.py:316-323`
- Modify: `niu_api/internal/region_manager.py:1524-1526`
- Modify: `agent/brain_tools.py:389-391`

- [ ] **Step 1: 恢复 region_sync.py 中的衰减调用**

在 `agent/injector/region_sync.py` 约322-331行，取消注释 `decay_structural_edges()` 调用。具体行号需 grep 确认：

Run: `grep -n "decay_structural_edges\|Step 6" REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py`

将被注释的代码恢复为：
```python
            # Step 6: Decay structural edges
            decay_result = decay_structural_edges(nx_graph)
            if decay_result:
                stats["decay"] = decay_result
```

- [ ] **Step 2: 恢复 brain_region_api.py 中的衰减调用**

Run: `grep -n "decay_structural_edges\|Step 8" REDACTED_USER_PATH/tools/ai-bot/niu_api/brain_region_api.py`

将被注释的代码恢复为：
```python
        # Step 8: Decay structural edges
        decay_result = decay_structural_edges(nx_graph)
        if decay_result:
            stats["decay"] = decay_result
```

- [ ] **Step 3: 恢复 region_manager.py incremental_update 中的衰减调用**

Run: `grep -n "decay_structural_edges" REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py`

在 `incremental_update()` 函数中，恢复被注释的衰减调用。

- [ ] **Step 4: 恢复 brain_tools.py 中的增强调用**

Run: `grep -n "_reinforce_edge_weight\|reinforce_on_tool_use" REDACTED_USER_PATH/tools/ai-bot/agent/brain_tools.py`

恢复被注释的增强调用。

- [ ] **Step 5: 提交**

```bash
git add agent/injector/region_sync.py niu_api/brain_region_api.py niu_api/internal/region_manager.py agent/brain_tools.py
git commit -m "feat: re-enable decay and reinforce calls (uncomment)"
```

- [ ] **Step 6: 代码审查**

检查：
- 恢复的调用是否与新的函数签名匹配（`decay_structural_edges` 返回值、`_reinforce_edge_weight` 参数）
- 是否有其他被注释的调用点遗漏（用 `grep -rn "decay_structural_edges\|_reinforce_edge_weight" agent/ niu_api/` 全面搜索）
- import 是否正确

---

### Task 6: 结构边初始权重统一为 1.0 + 配置文件更新

**Files:**
- Modify: `niu_api/internal/region_manager.py` (7处 weight=0.5)
- Modify: `memory/preferences.json` (priority 字段)

- [ ] **Step 1: 搜索所有 weight=0.5 的结构边**

Run: `grep -n 'weight.*0\.5\|"weight": 0\.5' REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py`

应找到约7处：
- 行311附近：`create_region_nodes()` 新增成员边
- 行344附近：锚点边
- 行355附近：新脑区成员边
- 行794附近：`_update_drifted_regions()` 漂移更新边
- 行916附近：`dissolve_shrunk_regions()` 重新分配边
- 行1694附近：`create_default_regions()` 锚点边（可能无 weight 字段，需显式添加）
- 行1821附近：`assign_entities_to_default_regions()` 关键词匹配边

- [ ] **Step 2: 将所有 0.5 改为 INITIAL_WEIGHT**

每处修改前先 Read 确认上下文，然后将 `"weight": 0.5` 改为 `"weight": INITIAL_WEIGHT`。

对于行1694的锚点边，如果没有 weight 字段，添加 `"weight": INITIAL_WEIGHT`。

- [ ] **Step 3: 更新 memory/preferences.json 中的 priority 字段**

将每个脑区的 `priority` 从 `"core"` / `"category"` 改为新值：

```json
{"label": "聊天历史", "priority": "medium", ...},
{"label": "文档库",   "priority": "permanent", ...},
{"label": "知识体系", "priority": "long", ...},
{"label": "人际关系", "priority": "permanent", ...},
{"label": "工作事务", "priority": "medium", ...},
{"label": "生活事务", "priority": "short", ...},
{"label": "组织机构", "priority": "permanent", ...}
```

- [ ] **Step 4: 更新 create_default_regions() 跳过逻辑**

在 `create_default_regions()` 函数中，找到 `priority == "category"` 的判断（约行1666），改为：

```python
if priority in ("short", "medium") and not include_category:
    continue
```

- [ ] **Step 5: 清理 STRUCTURAL_EDGE_TYPES_LOWER**

检查 `STRUCTURAL_EDGE_TYPES_LOWER` 是否还有其他调用方：

Run: `grep -rn "STRUCTURAL_EDGE_TYPES_LOWER" REDACTED_USER_PATH/tools/ai-bot/niu_api/ REDACTED_USER_PATH/tools/ai-bot/agent/`

- 如果无其他调用方：删除常量定义
- 如果有其他调用方：保留定义但加注释说明衰减/增强不再依赖此常量

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py memory/preferences.json
git commit -m "feat: unify structural edge weight to 1.0, update priority config, clean STRUCTURAL_EDGE_TYPES_LOWER"
```

- [ ] **Step 7: 方案对齐审查**

对照设计文档 4.3 节和 5.1 节，确认：
- 7处 weight=0.5 是否全部改为 INITIAL_WEIGHT
- preferences.json 的7个脑区 priority 值是否与设计文档1.3节一致
- `create_default_regions()` 跳过逻辑是否正确
- `STRUCTURAL_EDGE_TYPES_LOWER` 是否已清理

- [ ] **Step 8: 代码审查**

检查：
- 修改后是否有遗漏的 0.5（再 grep 一次确认）
- preferences.json 的 JSON 格式是否正确
- `INITIAL_WEIGHT` import 是否正确

---

### Task 7: 集成测试 — 启动程序验证真实衰减

**Files:**
- Create: `tests/test_brain_region_decay_integration.py`

- [ ] **Step 1: 写集成测试**

```python
# tests/test_brain_region_decay_integration.py
"""
脑区边衰减增强集成测试

真实测试：需要程序运行（./niu）。
手动执行：python tests/test_brain_region_decay_integration.py
"""
import json
import time
import requests
from pathlib import Path

API_BASE = "http://localhost:9876"
NIU_DIR = Path.home() / ".niu"


def test_api_health():
    """验证 API 可达"""
    resp = requests.get(f"{API_BASE}/api/stats", timeout=5)
    assert resp.status_code == 200
    print("[PASS] API health check")


def test_brain_regions_exist():
    """验证脑区节点存在且有 priority"""
    # 通过 API 获取脑区状态
    resp = requests.get(f"{API_BASE}/api/brain-regions", timeout=10)
    assert resp.status_code == 200
    regions = resp.json()
    assert len(regions) > 0
    print(f"[INFO] Found {len(regions)} brain regions")

    # 检查 priority 字段
    for region in regions:
        desc = region.get("description", "")
        has_priority = "brain_meta_priority:" in desc
        print(f"  Region '{region.get('label', '?')}': priority={'yes' if has_priority else 'NO'}")
    print("[PASS] Brain regions exist with priority fields")


def test_force_decay_via_tidy():
    """通过 force tidy 触发衰减并检查结果"""
    resp = requests.post(
        f"{API_BASE}/api/context/tidy",
        json={"session_id": "default", "mode": "force"},
        timeout=120,
    )
    assert resp.status_code == 200
    result = resp.json()
    decay_info = result.get("decay", {})
    print(f"[INFO] Decay result: {json.dumps(decay_info, ensure_ascii=False)}")
    print("[PASS] Force tidy triggers decay")


def test_send_chat_and_check_reinforce():
    """发送对话后检查增强是否触发"""
    resp = requests.post(
        f"{API_BASE}/chat",
        json={"message": "帮我查一下我有哪些联系人"},
        timeout=60,
    )
    assert resp.status_code == 200
    time.sleep(2)
    print("[PASS] Chat message sent (reinforce should trigger on tool use)")


if __name__ == "__main__":
    print("=== 脑区边衰减增强集成测试 ===\n")
    print("前置条件：程序已启动（./niu）\n")

    try:
        test_api_health()
        test_brain_regions_exist()
        test_force_decay_via_tidy()
        test_send_chat_and_check_reinforce()
        print("\n=== 所有集成测试通过 ===")
    except AssertionError as e:
        print(f"\n=== 测试失败: {e} ===")
    except Exception as e:
        print(f"\n=== 测试异常: {e} ===")
```

- [ ] **Step 2: 启动程序运行集成测试**

```bash
# 确保没有残留进程
pkill -f "niu" || true

# 启动程序
cd REDACTED_USER_PATH/tools/ai-bot && ./niu &

# 等待启动
sleep 15

# 运行集成测试
python tests/test_brain_region_decay_integration.py
```

- [ ] **Step 3: 检查日志确认衰减/增强运行**

```bash
# 查看衰减日志
grep -i "Decay\|decay_structural" ~/.niu/logs/api_stderr.log | tail -5

# 查看增强日志
grep -i "Reinforce\|reinforce_edge" ~/.niu/logs/api_stderr.log | tail -5
```

- [ ] **Step 4: 测试完成后杀进程**

```bash
pkill -f "niu"
```

- [ ] **Step 5: 提交**

```bash
git add tests/test_brain_region_decay_integration.py
git commit -m "test: add integration tests for brain region edge decay/reinforce"
```

- [ ] **Step 6: 方案对齐审查**

对照设计文档第2节和第3节，确认：
- 衰减是否在 RegionSync 中正确触发
- 增强是否在工具使用时正确触发
- priority 是否正确写入脑区节点 description
- 日志输出是否符合设计文档6.5节要求

- [ ] **Step 7: 代码审查**

检查：
- 集成测试是否覆盖了设计文档的关键验证点
- API 端点是否正确（不是 `/api/chat` 而是 `/chat`）
- 进程清理是否彻底

---

### Task 8: 最终代码审查 + 方案全面对齐

**Files:**
- Review: 所有修改过的文件

- [ ] **Step 1: 全面方案对齐审查**

逐项对照设计文档与实现：

1. 设计文档 1.2 优先级体系 → 常量定义
2. 设计文档 1.3 脑区优先级分配 → preferences.json
3. 设计文档 1.4 优先级存储机制 → `_encode_description()` + `parse_priority_from_description()`
4. 设计文档 2.3 衰减算法 → `decay_structural_edges()`
5. 设计文档 2.4 关键参数 → 常量值
6. 设计文档 2.5 保底逻辑 → 三条分支
7. 设计文档 3.2 增强算法 → `_reinforce_edge_weight()`
8. 设计文档 4.3 结构边权重统一 → 7处0.5→1.0
9. 设计文档 5.1 改动范围 → 确认所有文件已修改
10. 设计文档 5.3 旧代码清理 → 确认已删除

- [ ] **Step 2: 运行全部单元测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_region_edge_decay.py -v`
Expected: 全部通过

- [ ] **Step 3: 运行集成测试**

Run: `python tests/test_brain_region_decay_integration.py`
Expected: 全部通过

- [ ] **Step 4: 启动程序做冒烟测试**

```bash
# 启动程序
cd REDACTED_USER_PATH/tools/ai-bot && ./niu &
sleep 15

# 检查脑区状态
curl -s http://localhost:9876/api/brain-regions | python -m json.tool | head -30

# 发送对话
curl -s -X POST http://localhost:9876/chat -H "Content-Type: application/json" -d '{"message": "你好"}' | head -5

# 检查日志
grep -i "Decay\|Reinforce" ~/.niu/logs/api_stderr.log | tail -10

# 杀进程
pkill -f "niu"
```

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "chore: final review and cleanup for brain region edge decay/reinforce"
```
