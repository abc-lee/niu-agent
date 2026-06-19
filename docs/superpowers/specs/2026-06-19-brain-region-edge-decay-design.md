# 知识图谱脑区边衰减增强机制设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 重新设计知识图谱中实体→脑区边的衰减/增强/保底机制，防止实体变成孤立节点，同时按脑区优先级实现差异化遗忘曲线。

**Architecture:** 在现有 `decay_structural_edges()` 和 `_reinforce_edge_weight()` 基础上改造（方案A），判断逻辑从"边类型=包含"改为"目标节点 entity_type=brainregion"，加入半衰期模型、保底机制、优先级配置。

**Tech Stack:** Python, NetworkX (nx.Graph 无向图), LightRAG, preferences.json 配置

---

## 1. 核心概念

### 1.1 边的分类

- **脑区边**：实体↔脑区节点的边（对端节点 `entity_type == "brainregion"`）。逻辑边，表示归属关系，需要衰减/增强/保底机制。
- **知识关系边**：实体↔实体的边（如"认识"、"擅长"）。真实边，由 LLM 从内容中提取，不参与衰减。

**关键**：不能靠边类型（"包含"）区分，因为 LightRAG 自己也可能提取"包含"类型的知识关系边。必须靠对端节点 `entity_type` 判断。

**图模型**：LightRAG 使用 `nx.Graph()`（无向图），不是 `nx.DiGraph()`。边的遍历使用 `G.neighbors(node)`，度数使用 `G.degree(node)`。

### 1.2 优先级体系

| 优先级 | 半衰期 | 日衰减率 | 含义 |
|--------|--------|----------|------|
| `permanent` | 360天 | 0.5^(1/360) ≈ 0.99808 | 永久保留，正常衰减但到保底值冻结，永不删除 |
| `long` | 360天 | 0.5^(1/360) ≈ 0.99808 | 长期记忆 |
| `medium` | 180天 | 0.5^(1/180) ≈ 0.99615 | 中期记忆 |
| `short` | 90天 | 0.5^(1/90) ≈ 0.99232 | 短期记忆 |

### 1.3 脑区优先级分配

| 脑区 | 优先级 | 半衰期 |
|------|--------|--------|
| 人际关系 | `permanent` | 360天（保底冻结） |
| 组织机构 | `permanent` | 360天（保底冻结） |
| 文档库 | `permanent` | 360天（保底冻结） |
| 知识体系 | `long` | 360天 |
| 聊天历史 | `medium` | 180天 |
| 工作事务 | `medium` | 180天 |
| 生活事务 | `short` | 90天 |

### 1.4 优先级存储机制

脑区节点的 priority 存储在两个位置：

1. **preferences.json**：`brain_regions.defaults[].priority` — 配置来源，7个默认脑区在此定义
2. **NetworkX 节点属性**：`_encode_description()` 中增加 `brain_meta_priority:{priority}` 字段 — 运行时读取

衰减/增强函数从 NetworkX 节点属性中读取 priority（解析 description 中的 `brain_meta_priority` 前缀），不直接读 preferences.json。

**非默认脑区**（Leiden 社区检测自动发现的）默认 priority 为 `"medium"`。

**priority 写入时机**：
- 创建脑区时（`create_region_nodes()`）：默认脑区从 preferences.json 读取 priority；Leiden 新建脑区传入 `DEFAULT_PRIORITY ("medium")`
- 更新脑区时（`_encode_description()`）：priority 作为标准字段，所有调用点显式传递，不依赖 `extra_meta` 隐式保留

**priority 是 `_encode_description()` 的标准字段**：新增 `priority` 参数（第6个参数），所有调用 `_encode_description()` 的地方都必须传递 priority 值。这确保 Leiden 漂移更新（`_update_drifted_regions()`）、摘要更新（`update_region_summaries()`）、解散重建（`dissolve_shrunk_regions()`）和默认分配（`assign_entities_to_default_regions()`）不会丢失 priority 信息。所有调用点的 priority 获取方式：从旧 description 解析 `brain_meta_priority`，fallback 到 `DEFAULT_PRIORITY`；新建脑区从 preferences.json 读取或使用 `DEFAULT_PRIORITY`。

**priority 读取**：衰减/增强函数从 NetworkX 节点属性中读取 priority（解析 description 中的 `brain_meta_priority` 前缀），不直接读 preferences.json。如果 description 中缺少 `brain_meta_priority`，fallback 到 `DEFAULT_PRIORITY ("medium")`。

---

## 2. 衰减算法

### 2.1 执行时机

`RegionSync` 守护线程每24小时执行一次（非 cron 定时任务）。程序未启动时，下次启动后首次同步会补跑。

### 2.2 遍历策略

**从脑区节点出发遍历邻居**（与旧实现一致），而非遍历所有实体节点。原因：
- 脑区节点数量远少于实体节点（7个 vs 数千个）
- 从脑区出发即可覆盖所有脑区边
- 性能显著优于遍历所有实体

### 2.3 算法流程

```
对每个脑区节点 R (entity_type == "brainregion"):
  读取 R 的优先级 priority（从 description 中解析 brain_meta_priority）
  如果 priority 为空或不在 PRIORITY_HALFLIFE 中: priority = DEFAULT_PRIORITY ("medium")

  对 R 的每个邻居实体 E:
    如果 E.entity_type == "brainregion": 跳过（锚点边不衰减，脑区之间的导航边不属于归属关系）
    获取 R-E 边的权重 weight
    计算新权重: new_weight = weight * daily_decay(priority)

    保底检查: 统计 E 的总边数（G.degree(E)，包括所有类型）
    - 总边数 == 1（只剩这一条边）→ new_weight = max(new_weight, FLOOR_WEIGHT)
    - 总边数 >= 2 且 priority == "permanent" → new_weight = max(new_weight, FLOOR_WEIGHT)（permanent 级永不删除，保底冻结）
    - 总边数 >= 2 且 priority != "permanent" → 允许继续衰减

    如果 new_weight < FLOOR_WEIGHT (0.1) 且总边数 >= 2 且 priority != "permanent" → 删除边
    否则写回 weight = new_weight
```

### 2.4 关键参数

| 参数 | 值 | 含义 |
|------|-----|------|
| `FLOOR_WEIGHT` | 0.1 | 保底权重，也是删除阈值。permanent 级边和孤立实体的最后一条边冻结于此值；非 permanent 且总边数>=2 时低于此值删除 |
| `INITIAL_WEIGHT` | 1.0 | 边的初始权重，增强恢复目标值 |

**无 `REMOVE_THRESHOLD`**：保底值就是删除阈值。低于 0.1 的边：permanent 级冻结在 0.1（无论总边数）；非 permanent 且总边数>=2 时删除；非 permanent 且总边数==1 时冻结在 0.1。

### 2.5 保底逻辑

保底判断看实体的**总边数**（`G.degree(E)`，包括知识关系边），不是只看脑区边数：

- 总边数 == 1（只剩这一条边）→ 必须保底，权重不低于 `FLOOR_WEIGHT`，防止实体变成孤立节点
- 总边数 >= 2 且 priority == "permanent" → 保底冻结，权重不低于 `FLOOR_WEIGHT`，永不删除
- 总边数 >= 2 且 priority != "permanent" → 允许正常衰减，低于 `FLOOR_WEIGHT` 时删除

---

## 3. 增强算法

### 3.1 触发时机

实体被工具调用/查询时，在 `brain_tools.py` 的 `reinforce_on_tool_use()` 中触发。调用链：工具使用 → `reinforce_on_tool_use(tool_name)` → `_reinforce_edge_weight(region_id)`。

### 3.2 算法流程

从脑区节点出发遍历邻居（与调用链一致）：

```
对脑区节点 R 的每个邻居实体 E:
  获取 R-E 边
  如果 E.entity_type == "brainregion": 跳过（脑区之间的锚点边不增强）
  恢复权重: weight = INITIAL_WEIGHT (1.0)
  写回 weight
```

**增强范围**：`reinforce_on_tool_use()` 通过 `tool_to_region` 映射找到工具对应的脑区，只增强该脑区的边。如果实体同时属于多个脑区，只有被调用工具对应的脑区边被增强，其他脑区边继续按各自半衰期衰减。这符合设计意图——工具使用只应增强与该工具相关的脑区。

### 3.3 与旧实现的区别

- 旧：`weight += REINFORCE_DELTA (0.15)`，上限 `MAX_EDGE_WEIGHT (2.0)`——增量式
- 新：直接恢复到 `INITIAL_WEIGHT (1.0)`——"用一次就满血"

增强后，次日的衰减从 1.0 重新开始按半衰期下降，相当于"遗忘计时器重置"。

### 3.4 函数签名变更

```python
# 旧
def reinforce_on_tool_use(tool_name: str, reinforce_delta: float = REINFORCE_DELTA) -> str | None:

# 新
def reinforce_on_tool_use(tool_name: str) -> str | None:
```

删除 `reinforce_delta` 参数（不再使用增量式增强）。

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

**不做运行时兼容映射**：直接更新配置文件，旧值 `"core"`/`"category"` 不再使用。如果运行时遇到旧值，输出警告日志并使用默认优先级 `"medium"`。

### 4.2 代码中的优先级映射

在 `region_manager.py` 中新增常量：

```python
PRIORITY_HALFLIFE = {
    "permanent": 360,  # 衰减但保底冻结，永不删除
    "long": 360,
    "medium": 180,
    "short": 90,
}

FLOOR_WEIGHT = 0.1
INITIAL_WEIGHT = 1.0
DEFAULT_PRIORITY = "medium"  # 非默认脑区和旧配置的回退值
```

日衰减率在运行时计算：`daily_decay = 0.5 ** (1 / halflife_days)`

### 4.3 结构边初始权重统一

当前结构边（"包含"关系）创建时 weight = 0.5，与 `INITIAL_WEIGHT = 1.0` 不一致。统一修改所有结构边创建处：

- `region_manager.py` 第 311 行（`create_region_nodes` 新增成员边）
- `region_manager.py` 第 344 行（锚点边）
- `region_manager.py` 第 1694 行（`create_default_regions` 锚点边，当前无 weight 字段，需显式添加 `"weight": INITIAL_WEIGHT`）
- `region_manager.py` 第 355 行（新脑区成员边）
- `region_manager.py` 第 794 行（`_update_drifted_regions` 漂移更新边）
- `region_manager.py` 第 916 行（`dissolve_shrunk_regions` 重新分配边）
- `region_manager.py` 第 1821 行（`assign_entities_to_default_regions` 关键词匹配边）

将以上 0.5 全部改为 `INITIAL_WEIGHT`（1.0），与 LLM 提取的知识关系边默认权重一致。

---

## 5. 代码改动范围

### 5.1 需要修改的文件

| 文件 | 改动内容 |
|------|----------|
| `niu_api/internal/region_manager.py` | (1) 改造 `decay_structural_edges()`：判断逻辑从边类型改为目标节点 entity_type；加入优先级→半衰期映射；加入保底逻辑（G.degree==1时冻结）；新增常量 PRIORITY_HALFLIFE/FLOOR_WEIGHT/INITIAL_WEIGHT/DEFAULT_PRIORITY (2) 取消注释 `incremental_update()` 第 1524-1526 行的衰减调用 (3) `_encode_description()` 新增 `priority` 标准参数，写入 `brain_meta_priority` 字段 (4) 所有7处 `_encode_description()` 调用点传递 priority：① `create_region_nodes()` 行274 传配置值或 DEFAULT_PRIORITY ② `update_region_summaries()` 行509 从旧description解析 ③ `_update_drifted_regions()` 行779 从旧description解析 ④ `dissolve_shrunk_regions()` 行950 从旧description解析 ⑤ `create_default_regions()` 行1677 传 region_def["priority"] ⑥ `assign_entities_to_default_regions()` 行1865 从旧description解析 (5) 结构边初始权重从 0.5 改为 1.0（7处，含 create_default_regions 锚点边行1694） (6) `create_default_regions()` 第 1666 行 category 跳过逻辑改为基于新优先级 |
| `agent/brain_tools.py` | (1) 改造 `_reinforce_edge_weight()`：判断逻辑改为对端节点 entity_type；增强改为恢复到 INITIAL_WEIGHT (2) 删除旧常量 REINFORCE_DELTA/MAX_EDGE_WEIGHT (3) `reinforce_on_tool_use()` 删除 reinforce_delta 参数 (4) 取消注释第 389-391 行的增强调用 |
| `agent/injector/region_sync.py` | 取消注释 `decay_structural_edges()` 调用（Step 6，第 322-331 行） |
| `niu_api/brain_region_api.py` | 取消注释 `decay_structural_edges()` 调用（Step 8，第 316-323 行） |
| `memory/preferences.json` | 更新 priority 字段值（core/category → permanent/long/medium/short） |

### 5.2 不改动的部分

- 会话级激活衰减（`region_activation.py` 的 `decay_all()`）——仍在运行，与边权重衰减是独立机制
- LightRAG 的边权重参与检索排序——已有机制
- RegionSync 守护线程调度——24小时周期不变
- 定时任务调度——边衰减不在 cron 中，在 RegionSync 中

### 5.3 需要删除/清理的旧常量和旧代码

- `brain_tools.py` 中的 `REINFORCE_DELTA = 0.15` 和 `MAX_EDGE_WEIGHT = 2.0`
- `brain_tools.py` 中 `from niu_api.internal.region_manager import STRUCTURAL_EDGE_TYPES_LOWER` 的 import — 判断逻辑从边类型改为 entity_type 后不再使用
- `region_manager.py` 中的 `STRUCTURAL_EDGE_TYPES_LOWER` — 如果无其他调用方则删除；如有其他调用方保留但加注释说明衰减/增强不再依赖此常量

---

## 6. 风险与注意事项

1. **并发安全**：衰减在 `graph_write_lock()` 下执行，增强也在 `graph_write_lock()` 下，无冲突。
2. **旧配置兼容**：如果运行时遇到旧值 `"core"`/`"category"`，输出警告日志并使用默认优先级 `"medium"`。不做通用映射（因为同一旧值下不同脑区应有不同新值）。
3. **G.degree() 语义**：LightRAG 使用 `nx.Graph()` 无向图，`G.degree(node)` 即总边数，无需区分出入边。
4. **create_default_regions() 跳过逻辑**：第 1666 行的 `priority == "category"` 判断需更新为基于新优先级体系，例如 `priority in ("short", "medium") and not include_category`。
5. **Leiden 新建脑区**：Leiden 社区检测创建的脑区不在 preferences.json 中，使用 `DEFAULT_PRIORITY ("medium")`。`_encode_description()` 的 `priority` 参数确保 priority 信息在漂移更新和摘要更新时不丢失。
6. **衰减日志**：每次衰减运行后输出日志记录衰减/删除边数量，便于监控。
