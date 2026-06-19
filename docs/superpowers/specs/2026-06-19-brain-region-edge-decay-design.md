# 知识图谱脑区边衰减增强机制设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 重新设计知识图谱中实体→脑区边的衰减/增强/保底机制，防止实体变成孤立节点，同时按脑区优先级实现差异化遗忘曲线。

**Architecture:** 在现有 `decay_structural_edges()` 和 `_reinforce_edge_weight()` 基础上改造（方案A），判断逻辑从"边类型=包含"改为"目标节点 entity_type=brainregion"，加入半衰期模型、保底机制、优先级配置。

**Tech Stack:** Python, NetworkX, LightRAG, preferences.json 配置

---

## 1. 核心概念

### 1.1 边的分类

- **脑区边**：实体→脑区节点的边（目标节点 `entity_type == "brainregion"`）。逻辑边，表示归属关系，需要衰减/增强/保底机制。
- **知识关系边**：实体→实体的边（如"认识"、"擅长"）。真实边，由 LLM 从内容中提取，不参与衰减。

**关键**：不能靠边类型（"包含"）区分，因为 LightRAG 自己也可能提取"包含"类型的知识关系边。必须靠目标节点 `entity_type` 判断。

### 1.2 优先级体系

| 优先级 | 半衰期 | 日衰减率 | 含义 |
|--------|--------|----------|------|
| `permanent` | 无衰减 | 1.0 | 永久保留，权重只衰减到保底值 |
| `long` | 360天 | 0.5^(1/360) ≈ 0.99808 | 长期记忆 |
| `medium` | 180天 | 0.5^(1/180) ≈ 0.99615 | 中期记忆 |
| `short` | 90天 | 0.5^(1/90) ≈ 0.99232 | 短期记忆 |

### 1.3 脑区优先级分配

| 脑区 | 优先级 | 半衰期 |
|------|--------|--------|
| 人际关系 | `permanent` | 无衰减 |
| 组织机构 | `permanent` | 无衰减 |
| 文档库 | `permanent` | 无衰减 |
| 知识体系 | `long` | 360天 |
| 聊天历史 | `medium` | 180天 |
| 工作事务 | `medium` | 180天 |
| 生活事务 | `short` | 90天 |

---

## 2. 衰减算法

### 2.1 执行时机

`RegionSync` 守护线程每24小时执行一次（非 cron 定时任务）。程序未启动时，下次启动后首次同步会补跑。

### 2.2 算法流程

```
对图中每个实体节点 E:
  对 E 的每条出边/入边 B:
    1. 获取 B 的目标节点 T
    2. 如果 T.entity_type != "brainregion": 跳过（非脑区边不衰减）
    3. 读取 B 的权重 weight
    4. 读取 T 所属脑区的优先级 priority
    5. 如果 priority == "permanent": 跳过（永久级不衰减）
    6. 计算新权重: new_weight = weight * daily_decay(priority)
    7. 保底检查: 统计 E 的总边数（所有类型，出边+入边）
       - 总边数 == 1（只剩这条脑区边）→ new_weight = max(new_weight, FLOOR_WEIGHT)
       - 总边数 >= 2 → 允许继续衰减
    8. 如果 new_weight < FLOOR_WEIGHT (0.1) 且总边数 >= 2 → 删除边
    9. 否则写回 weight = new_weight
```

### 2.3 关键参数

| 参数 | 值 | 含义 |
|------|-----|------|
| `FLOOR_WEIGHT` | 0.1 | 保底权重，也是删除阈值。最后一条脑区边的下限；非最后一条边低于此值时删除 |
| `INITIAL_WEIGHT` | 1.0 | 边的初始权重，增强恢复目标值 |

**无 `REMOVE_THRESHOLD`**：保底值就是删除阈值，低于 0.1 的边要么删除（总边数>=2），要么冻结在 0.1（总边数==1）。

### 2.4 保底逻辑

保底判断看实体的**总边数**（包括知识关系边），不是只看脑区边数：

- 总边数 == 1（只剩这条脑区边）→ 必须保底，权重不低于 `FLOOR_WEIGHT`，防止实体变成孤立节点
- 总边数 >= 2（还有其他边）→ 脑区边可以正常衰减，低于 `FLOOR_WEIGHT` 时删除

---

## 3. 增强算法

### 3.1 触发时机

实体被工具调用/查询时，在 `brain_tools.py` 的 `reinforce_on_tool_use()` 中触发。

### 3.2 算法流程

```
对实体 E 的每条出边/入边 B:
  1. 获取 B 的目标节点 T
  2. 如果 T.entity_type != "brainregion": 跳过
  3. 恢复权重: weight = INITIAL_WEIGHT (1.0)
  4. 写回 weight
```

### 3.3 与旧实现的区别

- 旧：`weight += REINFORCE_DELTA (0.15)`，上限 `MAX_EDGE_WEIGHT (2.0)`——增量式
- 新：直接恢复到 `INITIAL_WEIGHT (1.0)`——"用一次就满血"

增强后，次日的衰减从 1.0 重新开始按半衰期下降，相当于"遗忘计时器重置"。

---

## 4. 配置变更

### 4.1 preferences.json

两个位置需要同步更新：
- `memory/preferences.json`（仓库备份，正式文档）
- `~/.niu/preferences.json`（运行时，程序自动从 memory/ 拷贝）

每个脑区的 `priority` 字段从 `"core"`/`"category"` 改为新等级：

```json
"brain_regions": {
  "defaults": [
    {"label": "聊天历史", "priority": "medium", ...},
    {"label": "文档库",   "priority": "permanent", ...},
    {"label": "知识体系", "priority": "long", ...},
    {"label": "人际关系", "priority": "permanent", ...},
    {"label": "工作事务", "priority": "medium", ...},
    {"label": "生活事务", "priority": "short", ...},
    {"label": "组织机构", "priority": "permanent", ...}
  ]
}
```

### 4.2 代码中的优先级映射

在 `region_manager.py` 中新增常量：

```python
PRIORITY_HALFLIFE = {
    "permanent": None,   # 不衰减
    "long": 360,
    "medium": 180,
    "short": 90,
}

FLOOR_WEIGHT = 0.1
INITIAL_WEIGHT = 1.0
```

日衰减率在运行时计算：`daily_decay = 0.5 ** (1 / halflife_days)`

---

## 5. 代码改动范围

### 5.1 需要修改的文件

| 文件 | 改动内容 |
|------|----------|
| `niu_api/internal/region_manager.py` | 改造 `decay_structural_edges()`：判断逻辑从边类型改为目标节点 entity_type；加入优先级→半衰期映射；加入保底逻辑（总边数==1时冻结）；新增常量；取消注释恢复调用 |
| `agent/brain_tools.py` | 改造 `_reinforce_edge_weight()`：判断逻辑改为目标节点 entity_type；增强改为恢复到 INITIAL_WEIGHT；删除旧常量 REINFORCE_DELTA/MAX_EDGE_WEIGHT；取消注释恢复调用 |
| `agent/injector/region_sync.py` | 取消注释 `decay_structural_edges()` 调用（Step 6） |
| `niu_api/brain_region_api.py` | 取消注释 `decay_structural_edges()` 调用（Step 8） |
| `memory/preferences.json` | 更新 priority 字段值 |

### 5.2 不改动的部分

- 会话级激活衰减（`region_activation.py` 的 `decay_all()`）——仍在运行，与边权重衰减是独立机制
- LightRAG 的边权重参与检索排序——已有机制
- RegionSync 守护线程调度——24小时周期不变
- 定时任务调度——边衰减不在 cron 中，在 RegionSync 中

### 5.3 需要删除的旧常量

- `brain_tools.py` 中的 `REINFORCE_DELTA = 0.15` 和 `MAX_EDGE_WEIGHT = 2.0`

---

## 6. 风险与注意事项

1. **首次运行**：恢复衰减后，已有边的权重可能已经很久没衰减过。建议首次运行时做一次完整衰减，但不要一次性删除大量边——可以加日志记录衰减/删除数量，人工确认后再放开。
2. **保底边数统计**：统计总边数时需要同时统计出边和入边，使用 `G.degree(node)` 或 `len(G.predecessors(node)) + len(G.successors(node))`。
3. **并发安全**：衰减在 `graph_write_lock()` 下执行，与增强可能并发。增强也在 `graph_write_lock()` 下，无冲突。
4. **priority 字段兼容**：旧配置中 priority 为 `"core"`/`"category"`，代码需要兼容处理——`"core"` 映射为 `"medium"`，`"category"` 映射为 `"short"`，并输出警告日志建议更新配置。
