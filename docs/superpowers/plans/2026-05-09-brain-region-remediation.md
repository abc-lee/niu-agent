# 脑区功能整改方案

> 基于 2026-05-09 实测结果 + 代码审查。10/10 测试通过，但暴露 8 个结构性问题。

## 测试结果摘要

| # | 测试 | 结果 | 关键发现 |
|---|------|------|---------|
| 1 | 脑区实体存在性 | ✅ | 8 个脑区，含 1 个旧格式 `brain:region:person:{uuid}` |
| 2 | 边权重 reinforce | ✅ | 边默认 weight=1.0，reinforce 无可见效果 |
| 3 | 边权重 decay | ✅ | decay_factor=0.5, threshold=0.1 逻辑正确 |
| 4 | 脑区激活+衰减 | ✅ | factor=0.92, threshold=0.3 逻辑正确 |
| 5 | spillover 激活 | ✅ | 代码逻辑正确，但邻居映射为空，从未实际触发 |
| 6 | brain_region_prompt 注入 | ✅ | 注入 69000 字符，含 person:{uuid} 旧格式 |
| 7 | 默认脑区创建 | ✅ | 3 个默认脑区可创建 |
| 8 | 脑区合并/解散 | ✅ | 逻辑正确 |
| 9 | RegionActivationManager API | ✅ | 所有 API 可用 |
| 10 | RegionManager API | ✅ | 8/8 方法存在，可调用 |

## 8 个结构性问题

按严重程度排序：

### P0 — 边动力学闭环断裂（核心功能不可用）

**问题**：设计目标是"活跃脑区 → 知识/工具边强化 → 不用则衰减 → 断开"，但闭环完全断裂：

1. **边默认 weight=1.0**：LightRAG 创建边时 weight=1.0，`_reinforce_edge_weight` 做 `min(1.0, 1.0+0.1)=1.0`，无任何变化
2. **_decay_structural_edges 从未运行**：只处理 `_region:` 前缀边，但当前图中不存在此类边（边创建时没有加前缀）
3. **边衰减与激活衰减不协调**：activation decay 每轮 0.92，edge decay 每次同步 0.5，时间尺度不匹配

**根因**：边创建路径（inject_custom_kg / ainsert）不使用 `_region:` 前缀，但衰减只处理此前缀。

**修复方案**：
```
方案A（推荐）：统一边权重体系
1. 所有脑区相关边（features, remembers, belongs_to 等）统一使用 weight 字段
2. 初始 weight=0.5（不是 1.0），reinforce +0.15，decay ×0.5
3. _decay_structural_edges 改为处理所有脑区相关边（不限前缀）
4. reinforce_on_tool_use 统一 delta=0.15（与 RegionActivationManager.tool_reinforce_value 对齐）

方案B：使用 _region: 前缀
1. inject_custom_kg 创建边时加 _region: 前缀
2. 现有边批量迁移加前缀
3. 风险：需要修改所有边创建路径
```

### P1 — spillover 激活不工作

**问题**：`RegionSync.run_sync()` 第6步构建邻居映射，但代码中 `neighbor_map` 为空 dict，spillover 永远不触发。

**根因**：`region_sync.py:295` 有 TODO 注释，邻居映射从未实现。

**修复方案**：
```python
# 在 RegionSync.run_sync() 第6步，基于图谱边构建邻居映射
def _build_neighbor_map(self, regions):
    """基于图谱中脑区之间的边构建邻居映射"""
    neighbor_map = {}
    for r1 in regions:
        neighbors = []
        for r2 in regions:
            if r1.name == r2.name:
                continue
            # 检查两个脑区是否有共享成员
            shared = set(r1.members) & set(r2.members)
            if shared:
                neighbors.append(r2.name)
        neighbor_map[r1.name] = neighbors
    return neighbor_map
```

### P1 — brain_region_prompt 使用 person:{uuid} 旧格式

**问题**：`brain_region_prompt.py` 静态提示中仍使用 `person:{uuid}` 格式，与 KG 开发字典明确矛盾。

**根因**：brain_region_prompt 编写早于 KG 开发字典的实测结论。

**修复方案**：
```python
# brain_region_prompt.py 静态提示中，将：
# "person:{uuid} 格式的人物实体"
# 改为：
# "人物实体使用人名作为 entity_name（如'任飞'），禁止 person:{uuid} 格式"
# "未命名人物使用 '未命名人物_{n}' 格式"
```

### P2 — incremental_update 未实现

**问题**：`RegionManager.incremental_update()` 方法体是 `pass`，Leiden 社区检测后的增量更新不工作。

**修复方案**：
```python
def incremental_update(self, old_regions, new_regions):
    """增量更新脑区：新增、合并、解散"""
    old_map = {r.name: r for r in old_regions}
    new_map = {r.name: r for r in new_regions}

    # 新增的脑区
    added = [r for name, r in new_map.items() if name not in old_map]
    if added:
        self.create_region_nodes(added)

    # 解散的脑区
    dissolved = [r for name, r in old_map.items() if name not in new_map]
    if dissolved:
        self.dissolve_shrunk_regions(dissolved)

    # 更新所有新脑区摘要
    self.update_region_summaries(list(new_map.values()))
```

### P2 — _summarize_region 是启发式

**问题**：脑区 label 用第一个实体名，description 用 top5 实体拼接，不是语义摘要。

**修复方案**：
```python
# 方案A（推荐）：使用 LLM 生成摘要
def _summarize_region(self, region_name, members):
    prompt = f"请用一句话总结以下实体所属的知识领域：{', '.join(members[:20])}"
    return llm_generate(prompt)

# 方案B：改进启发式
def _summarize_region(self, region_name, members):
    # 用最常出现的实体类型作为 label
    # 用 top3 实体名拼接作为 description
    pass
```

### P2 — leidenalg 未声明依赖

**问题**：社区检测需要 `leidenalg` 包，但未在 `requirements.txt` 中声明。

**修复方案**：在 `mcp-servers/lightrag-server/requirements.txt` 和 `agent/requirements.txt` 中添加 `leidenalg>=0.10`。

### P3 — 边权重 delta 不一致

**问题**：
- `brain_tools._reinforce_edge_weight`: delta=+0.1, max=1.0
- `RegionActivationManager.tool_reinforce_value`: 0.85

两个系统对"强化"的定义不一致。

**修复方案**：统一为 delta=+0.15, max=2.0（与设计文档 §3.3 对齐）。

### P3 — brain_region_prompt 注入量过大

**问题**：注入内容 69000 字符，占 LLM 上下文窗口的很大比例。

**修复方案**：
```python
# 静态提示精简为关键规则（<2000 字符）
# 动态提示只注入当前活跃脑区信息（不注入全部脑区）
# 总注入量控制在 <5000 字符
```

## 整改优先级

| 阶段 | 内容 | 预期效果 |
|------|------|---------|
| **阶段1** | P0 边动力学闭环修复 | 核心功能可用：边强化/衰减/断开 |
| **阶段2** | P1 spillover + person:{uuid} | 脑区联动 + 实体格式统一 |
| **阶段3** | P2 incremental_update + summarize + leidenalg | 社区检测增量更新 + 语义摘要 |
| **阶段4** | P3 delta统一 + 注入量优化 | 细节优化 |

## 阶段1 详细实施计划

### 1.1 统一边权重初始值

**文件**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

在 `inject_custom_kg` 中，所有脑区相关边的 weight 初始值改为 0.5：
```python
# inject_custom_kg 创建 relationships 时
for rel in relationships:
    if "weight" not in rel:
        rel["weight"] = 0.5  # 统一初始权重
```

### 1.2 修改 _decay_structural_edges 处理范围

**文件**: `niu_api/internal/region_manager.py`

```python
def _decay_structural_edges(self, regions):
    """衰减所有脑区相关边（不限 _region: 前缀）"""
    # 遍历所有脑区的成员边
    # 对 weight > threshold 的边: weight *= decay_factor
    # 对 weight <= threshold 的边: 断开
    pass
```

### 1.3 统一 reinforce delta

**文件**: `agent/brain_tools.py`

```python
REINFORCE_DELTA = 0.15  # 统一与设计文档对齐
MAX_EDGE_WEIGHT = 2.0   # 允许权重超过 1.0
```

### 1.4 协调衰减时间尺度

**文件**: `niu_api/internal/region_activation.py` + `niu_api/internal/region_manager.py`

- activation decay: 每轮 0.92（保持不变）
- edge decay: 每次同步 0.5（保持不变，但同步间隔改为 1 小时）
- 新增：每轮 agent loop 末尾调用 `decay_all()` + `_decay_structural_edges()`

### 1.5 现有边迁移

一次性脚本：将现有图谱中所有脑区相关边的 weight 从 1.0 改为 0.5：
```python
# scripts/migrate_edge_weights.py
g = rag.chunk_entity_relation_graph._graph
for u, v, data in g.edges(data=True):
    if data.get("weight", 1.0) == 1.0:
        data["weight"] = 0.5
```

## 不做的事

1. **不修改 LightRAG 核心代码** — 只修改我们的适配层
2. **不删除现有脑区数据** — 只修改边权重
3. **不改变脑区实体命名规范** — `brain:region:{name}` 格式保持不变
4. **不修改 RegionSync 的 8 步流程** — 只填充空实现
