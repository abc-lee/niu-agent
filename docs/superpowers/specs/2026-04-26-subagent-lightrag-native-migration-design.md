# 子Agent LightRAG 原生迁移设计

> **⚠️ 历史文档**：本文档中使用 `brain:Niu`、`brain:region:xxx`、`brain:concept:xxx`、`brain:event:xxx`、`brain:person:xxx`、`brain:session:xxx`、`event:xxx`、`skill:xxx`、`person:xxx` 等冒号前缀实体名的描述已过时。当前系统要求所有实体名必须使用自然语言（如 `Niu`、`编程开发脑区`、`Python`、`海滩日落事件`），禁止冒号前缀格式。详见 `docs/kg-dev-dictionary.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将5个子Agent从旧的向量库管理模式迁移到 LightRAG 原生架构，不是硬套旧逻辑，而是按 LightRAG 的图检索理念和脑区激活方案重新设计每个子Agent的职责和工具使用方式。

**Architecture:** 每个子Agent按其核心职责重新定位：context-manager 精简为压缩器（L0压缩+非破坏性整理+强制压缩），dream-evolver 整合脑区激活方案成为知识写入唯一入口，entity-extractor 改用 LightRAG 原生工具做精确控制，event-manager 独立为结构化存储+LightRAG 双轨，kg-enricher 删除。

**Tech Stack:** LightRAG (知识图谱), session-manager (消息管理), JSON (事件结构化存储)

---

## 核心设计决策

### 1. L1/L2 分层取消

**决策：取消。**

LightRAG 的 entity.description 天然充当 L1（精炼摘要），chunk 原文天然充当 L2（完整内容）。entity.source_id 指向 chunk ID 列表，chunk.file_path 指向原始文件，形成完整的多指针链路：

```
entity.source_id → chunk_id → chunk.content (原文)
entity.file_path → 原始文件路径
chunk.full_doc_id → 完整文档
```

这比 context-manager 的 L1→L2 单指针更强。无需人工维护指针字段。

### 2. kg-enricher 删除

**决策：删除。**

kg-enricher 原来是从向量库同步数据到知识图谱的"同步器"。向量库已废弃，这个角色不再存在。其功能由 dream-evolver 在写入 LightRAG 时直接完成。

### 3. event-manager 独立化

**决策：独立结构化存储 + LightRAG 双轨。**

- **独立结构化存储**（JSON 文件）负责精确的事件 CRUD（创建、按状态/时间查询、修改状态、删除）
- **事件写入时同时送一份给 LightRAG**（通过 `lightrag_insert`），让图检索知道这些事件
- **大模型通过图检索**可以语义关联到事件（"下周有什么安排？" → LightRAG 返回相关实体）
- **实际的事件处理**（提醒、状态变更）由独立程序完成
- **L1 工作记忆功能取消**，由 dream-evolver 承担

### 4. context-manager 精简为压缩器（含非破坏性整理）

**决策：保留，职责精简。**

**三种工作模式**：

| 模式 | 触发条件 | 执行内容 | 破坏性 |
|------|---------|---------|--------|
| 睡眠整理（非破坏性） | 5分钟空闲，上下文 <50% | 合并冗余消息、精简工具输出、压缩简单确认回复 | 低（保留核心内容） |
| 睡眠整理（半破坏性） | 5分钟空闲，上下文 ≥50% | dream-evolver先→context-manager后，L0压缩+删除 | 中 |
| 强制压缩 | 上下文 >80% | dream-evolver先（同步等待完成）→context-manager后（紧急压缩） | 高 |

**关键设计：双游标机制（UUID 基准，防重复整理）**

context-manager 和 dream-evolver 各有独立游标，基于消息 UUID（而非 idx），确保删除消息后游标不失效。

| 游标 | 用途 | 存储位置 | 基准 | 写入方 |
|------|------|---------|------|--------|
| `last_dream_evolve_id` | dream-evolver：只处理此 ID 之后的新消息 | `~/.niu/last_dream_evolve.json` | 消息 UUID | dream-evolver |
| `last_compress_id` | context-manager：只处理此 ID 之前、且 ≤ dream 游标的消息 | `~/.niu/last_compress.json` | 消息 UUID | context-manager |

**为什么用 UUID 而非 idx**：
- 当前实现用 idx（消息序号），但 context-manager 删除消息后 idx 重新编号，导致 dream-evolver 游标指向错误位置
- UUID 是消息的持久标识，不受删除影响

**协作规则**：
1. dream-evolver 处理完后写入 `last_dream_evolve_id`（最后处理的消息 UUID）
2. context-manager 读取两个游标，**只处理 `last_compress_id < msg.id ≤ last_dream_evolve_id` 范围内的消息**
3. context-manager 处理完后写入 `last_compress_id`（最后压缩的消息 UUID）
4. 超过 dream 游标范围的消息（dream-evolver 未处理的知识），context-manager **不得删除**
5. 低于 compress 游标的消息（已压缩整理过的），context-manager **不得重复处理**

**双游标工作流**：
```
消息 UUID: m01, m02, m03, ..., m50, m51, ..., m60

dream-evolver 处理 m01-m50 → 更新 last_dream_evolve_id=m50
    ↓
context-manager 读取：
  last_compress_id=m20（上次压缩到这里）
  last_dream_evolve_id=m50（dream-evolver 已处理到这里）
  → 只处理 m21-m50（两个游标之间的消息）
    ↓
context-manager 处理完 → 更新 last_compress_id=m50
    ↓
m51-m60（dream-evolver 未处理）→ context-manager 不动
```

**force 模式下的游标行为**：
- dream-evolver：绕过游标，全量处理所有消息，完成后更新 `last_dream_evolve_id` 为最后一条消息的 UUID
- context-manager：同样绕过 `last_compress_id`，但仍然受 `last_dream_evolve_id` 约束（只处理 dream-evolver 已处理范围内的消息）

**强制压缩的关键约束**：主Agent必须**同步等待** dream-evolver 完成后，才能执行压缩删除。不能异步，否则知识可能丢失。在 `compat.py` 的 force 模式实现中，必须用 `await asyncio.to_thread(run_dream_evolver)` 同步等待，然后再启动 context-manager。

---

### 5. dream-evolver 整合脑区激活方案（知识写入唯一入口）

**决策：按 06-brain-region-activation.md 完整实现，成为知识写入唯一入口。**

详见下方 dream-evolver 详细设计。

### 6. entity-extractor 保留并改用 LightRAG 原生工具

**决策：保留，改用 LightRAG 原生工具。**

- 核心价值：精确控制 entity_type 和 description 格式（如 brain_meta_* 标签），这是 LightRAG ainsert() 自动提取做不到的
- 去重和重新提取功能已被 LightRAG 的 `_merge_nodes_then_upsert` 覆盖，不再需要
- 工具从 vector-store 改为 lightrag-server 原生工具

---

## 各子Agent详细设计

### A. context-manager（压缩器：非破坏性整理 + L0压缩 + 强制压缩）

**mcpServers**: `session-manager`（移除 lightrag-server）

**三种工作模式详细设计**：

#### 模式一：睡眠整理（非破坏性，上下文 <50%）

**触发**：5分钟空闲，上下文使用率 <50%
**目标**：轻度整理，减少冗余，不丢失信息
**操作**：
1. 合并连续的简单确认回复（"好的"、"明白了"、"谢谢"）为一条摘要
2. 精简大工具输出（保留关键结果，删除中间过程）
3. 压缩冗余的系统消息和重复内容
4. **不删除核心对话内容**，只做合并和精简
5. **只在双游标范围内操作**（`last_compress_id < msg.id ≤ last_dream_evolve_id`）

**实现**：用 `update_message` 改写冗余消息为精简版，用 `delete_messages` 删除被合并的消息

#### 模式二：睡眠整理（半破坏性，上下文 ≥50%）

**触发**：5分钟空闲，上下文使用率 ≥50%
**流程**：
1. **dream-evolver 先执行**（同步等待完成）— 提取知识写入 LightRAG，更新游标
2. **context-manager 后执行** — L0压缩 + 删除，**只在游标范围内操作**

**L0 压缩的具体步骤**：
1. 读取双游标（`last_compress_id` 和 `last_dream_evolve_id`）
2. 识别双游标范围内的会话单元（一个完整话题/任务）
3. 对单元内的消息：
   - 保留idx最小的一条消息
   - 用 `update_message` 将其content改写为L0摘要（一句话，~100 tokens）
   - 用 `delete_messages` 删除单元中其余消息
4. **禁止使用 `add_message`**（会导致对话顺序错乱）
5. **双游标范围外的消息不动**（低于 compress 游标的不重复处理，高于 dream 游标的不动未保存知识）

#### 模式三：强制压缩（上下文 >80%）

**触发**：上下文使用率超过80%
**流程**：
1. **dream-evolver 先执行（同步等待完成）** — 全量提取知识写入 LightRAG，更新游标
2. **context-manager 后执行** — 紧急压缩，**只在游标范围内操作**

**紧急压缩的具体步骤**：
1. 读取双游标（`last_compress_id` 和 `last_dream_evolve_id`）
2. 按删除优先级排序双游标范围内的消息：
   - 优先删除：早期的大工具输出（idx小、tokens多）
   - 其次删除：简单确认回复
   - 最后删除：早期的L0摘要（可合并）
3. 累计tokens直到达到目标（从 current 减到 current * 0.5）
4. 对要删除的内容：直接 `delete_messages`（知识已由 dream-evolver 保存）
5. **不再调用 `add_document`**（向量库已废弃，知识保存由 dream-evolver 承担）
6. **双游标范围外的消息不动**

**关键约束**：主Agent必须同步等待 dream-evolver 完成，不能异步。

**工具变化**：
- 保留：`get_messages`, `delete_messages`, `update_message`（session-manager）
- 删除：`add_document`, `search_documents`, `get_document`, `delete_document`, `list_documents`（lightrag-server）

---

### B. dream-evolver（知识写入唯一入口，整合脑区激活方案）

**mcpServers**: `lightrag-server`, `session-manager`（不变）

**设计依据**：06-brain-region-activation.md 的脑区激活方案

#### B1. 脑区机制概述

**脑区主节点**（已实现）：
- 根节点 `brain:Niu`（用户画像），在 `RegionManager._ensure_root()` 中自动生成
- 子脑区主节点如 `brain:Python`、`brain:项目管理`，由 Leiden 聚类算法自动发现
- 主节点存储为 LightRAG entity，entity_type="brain_region"

**脑区隔离机制**（已实现框架，关键缺失待补）：
- Leiden 聚类算法分析邻居图，发现高内聚社区
- 每个社区生成一个脑区主节点（`_summarize_region()`）
- 脑区主节点的 description 包含：区域标签、核心实体列表、连接权重
- 当前缺失：`incremental_update()` 未实现、runner.py 未集成 BrainContextInjector、邻居图为空

**脑区激活与衰减**（已实现）：
- `_activate_region(region_name, strength=1.0)` — 激活脑区，提升注入优先级
- `_decay_all_regions()` — 每轮衰减，未激活区域逐渐降低
- 激活信号来源：用户查询语义匹配、工具调用关联、显式触发

**当前实现状态**（来自代码分析）：

| 组件 | 状态 | 位置 |
|------|------|------|
| RegionManager | 已实现 | `agent/injector/brain_region.py` |
| Leiden 聚类 | 已实现 | `brain_region.py: _detect_regions()` |
| 主节点生成 | 已实现 | `brain_region.py: _ensure_root()`, `_create_region_node()` |
| 激活/衰减 | 已实现 | `brain_region.py: _activate_region()`, `_decay_all_regions()` |
| 增量更新 | **未实现** | `brain_region.py: incremental_update()` 为空 |
| BrainContextInjector 集成 | **未实现** | runner.py 未调用 |
| 邻居图构建 | **未实现** | `_build_adjacency()` 返回空图 |
| 区域摘要用 LLM | **未实现** | `_summarize_region()` 用启发式标签 |

#### B2. Dream Evolver 的3项核心任务

**6项→3项合并**（来自06文档第6节）：

| 原任务 | 新任务 | 说明 |
|--------|--------|------|
| 1.经验提取 + 2.知识沉淀 | **经验提取与知识沉淀** | 从对话中提取事实、概念、技能，写入语义记忆管道 |
| 3.关系构建 | **关系构建与强化** | 建立实体间关系，强化已有连接，连接优先原则 |
| 4.画像更新 + 5.偏好学习 + 6.情感标记 | **画像更新与偏好学习** | 更新用户画像实体，记录偏好和情感倾向 |

#### B3. 连接优先原则

**核心规则**：每条新实体必须至少建1条边，孤岛记忆无用。

**实现方式**：
1. 新实体写入时，必须指定至少一个连接目标
2. 如果无法确定连接目标，连接到当前 Session 节点作为兜底
3. Session 节点格式：`brain:session:{date}`（如 `brain:session:2026-04-26`）
4. Session 节点确保时间链不断链

**Session 节点兜底机制**（当前未实现，需新增）：
- 每次整理开始时，检查当天 Session 节点是否存在
- 不存在则创建：`lightrag_insert_entity(name="brain:session:2026-04-26", entity_type="session", description="2026年4月26日的对话会话")`
- 无法确定连接目标的新实体，连接到当天 Session 节点：`lightrag_insert_relation(src_id="brain:session:2026-04-26", tgt_id=new_entity, relation="contains")`

#### B4. 分级写入

**level → weight/decay_rate 映射**（来自06文档，当前未实现）：

| level | 含义 | weight | decay_rate | 示例 |
|-------|------|--------|------------|------|
| L0 | 即时印象 | 0.3 | 0.9 | 临时观察、表面信息 |
| L1 | 精炼摘要 | 0.7 | 0.5 | 经验证的事实、概念 |
| L2 | 完整内容 | 1.0 | 0.1 | 核心知识、重要经验 |

**实现方式**：在 entity 的 description 中编码为 `brain_meta_weight=0.7;brain_meta_decay_rate=0.5;` 前缀。monkey-patch 机制确保 `_merge_nodes_then_upsert` 保留这些属性。

#### B5. 边命名规范与断开机制

**LightRAG 边的数据模型**：
- 边存储为 `source_id → {target_id: {keywords: str, weight: float, ...}}` 格式
- `keywords` 字段就是边的名称/类型
- `weight` 字段天然存在，默认 1.0，合并时取 max
- monkey-patch 保留边的 `brain_meta_*` 属性

**边命名规范**（用下划线前缀区分边类型）：

| 边类型 | keywords 格式 | 含义 | 示例 |
|--------|-------------|------|------|
| 脑区包含 | `_region:contains` | 脑区主节点包含子实体 | `brain:Python → brain:Django` |
| 实体属于脑区 | `_region:belongs` | 实体属于某个脑区 | `brain:Django → brain:Python` |
| Session兜底 | `_session:contains` | Session包含临时实体 | `brain:session:2026-04-26 → brain:临时观察` |
| 语义关系 | 无前缀 | 真实语义关系 | `brain:Niu → brain:Python` (skilled_in) |
| 时间链 | 无前缀 | 时间顺序/因果 | `brain:事件A → brain:事件B` (followed_by) |

**前缀的用途**：
- 查询时可按前缀过滤：`_region:` 前缀的边是结构边，`_session:` 前缀的边是兜底边
- 断开机制只针对结构边和兜底边，不影响语义边
- 语义边（无前缀）永远不会被自动断开

**断开与加分机制**：

边 weight 的衰减和加分形成动态平衡——不用的边逐步断开，常用的边逐步增强。

1. **衰减（断开）**：

   脑区隔离过程中，节点会迁移到新脑区，旧边需要断开。具体规则：

   - **脑区迁移时的边处理**：
     - 节点从脑区A迁移到脑区B
     - 旧边 `_region:belongs`（节点→脑区A）的 weight 乘以衰减因子（如 0.5）
     - 新边 `_region:belongs`（节点→脑区B）的 weight 设为 1.0
     - 旧边 weight 低于断开阈值（如 0.1）时，自动删除该边

   - **Session兜底边的断开**：
     - 临时实体被连接到更有意义的脑区后，`_session:contains` 边的 weight 衰减
     - 低于阈值时自动断开，实体从 Session 兜底升级为正式脑区成员

   - **衰减算法**：
     ```
     每次脑区隔离触发时：
     for edge in node.outgoing_edges:
         if edge.keywords.startswith("_region:") or edge.keywords.startswith("_session:"):
             edge.weight *= 0.5  # 衰减因子
             if edge.weight < 0.1:  # 断开阈值
                 delete_edge(edge)
     ```

   - **语义边不参与断开**：
     - `skilled_in`、`prefers`、`followed_by` 等语义边永远不会被自动断开
     - 只有 `_region:` 和 `_session:` 前缀的结构边才参与衰减断开

2. **加分（强化）**：

   工具使用触发脑区激活时，对应的结构边 weight 应被强化。当前 `reinforce_by_tool_use()` 只加了脑区激活度（内存层），没有反馈到边的 weight（持久化层）。

   - **触发时机**：`reinforce_by_tool_use(tool_name)` 在 handler.py 的3处工具调用分支中已实现
   - **加分规则**：`edge.weight = min(1.0, edge.weight + reinforce_delta)`，`reinforce_delta` 默认 0.1
   - **加分范围**：只对 `_region:belongs` 和 `_region:contains` 前缀的结构边加分，语义边不参与（语义边 weight 保持语义权重，不应被工具使用频率干扰）
   - **持久化**：加分写入 LightRAG 图，下次启动仍然有效

   **动态平衡示例**：
   ```
   不用的边：weight 1.0 → 0.5 → 0.25 → 0.125 → 断开（4次隔离后）
   常用的边：weight 0.5 → 0.6 → 0.7 → 0.8 → 0.9 → 1.0（每次工具使用+0.1）
   偶尔用的边：衰减0.5 → 加分+0.1 → 0.6 → 衰减0.3 → 加分+0.1 → 0.4（交替维持中等）
   ```

3. **实现方式**：
   - 衰减和断开：在 `RegionManager.incremental_update()` 中实现
   - 加分：在 `reinforce_by_tool_use()` 中扩展，除了激活脑区（层2），还强化对应结构边 weight（层1）
   - 用 `lightrag_insert_relation` 更新边的 weight（覆盖旧值）
   - 用 LightRAG 的 `knowledge_graph` 直接操作删除低于阈值的边

| 关系类型 | 含义 | 方向 | 示例 |
|---------|------|------|------|
| `followed_by` | 时间顺序 | A→B | 事件A之后发生了事件B |
| `corrected_by` | 纠正 | A→B | 错误A被纠正为B |
| `led_to` | 因果 | A→B | 决策A导致了结果B |
| `resolved_by` | 解决 | A→B | 问题A被方案B解决 |

**当前状态**：`followed_by` 和 `corrected_by` 已在 dream-evolver.md 中定义，`led_to` 和 `resolved_by` 需新增。

#### B6. 脑区与 dream-evolver 的配合

**dream-evolver 的脑区职责**：

1. **写入时关联脑区**：新实体写入时，根据语义自动关联到已有脑区主节点
   - 如果实体与 `brain:Python` 脑区语义相关，建立 `lightrag_insert_relation(src_id="brain:Python", tgt_id=new_entity, relation="contains")`
   - 如果无法确定脑区，连接到根节点 `brain:Niu`

2. **触发脑区隔离**：当实体数量增长到阈值时，dream-evolver 调用 `RegionManager.incremental_update()` 触发 Leiden 聚类
   - 聚类发现新的高内聚社区 → 自动生成新的脑区主节点
   - 新脑区主节点通过 `lightrag_insert_entity` 写入 LightRAG
   - 脑区内的实体通过 `lightrag_insert_relation` 连接到脑区主节点

3. **脑区主节点维护**：dream-evolver 负责更新脑区主节点的 description（核心实体列表、连接权重变化）

**脑区隔离的算法**（来自06文档，已实现框架）：
1. 构建邻居图（`_build_adjacency()`）— 当前返回空图，需修复
2. 运行 Leiden 聚类（`_detect_regions()`）— 已实现
3. 生成脑区主节点（`_create_region_node()`）— 已实现
4. 连接实体到脑区主节点 — 需实现
5. 更新脑区摘要（`_summarize_region()`）— 需改用 LLM 生成语义标签

#### B7. 工具映射

| 旧工具 | 新工具 | 参数变化 |
|--------|--------|---------|
| `add_document` | `lightrag_insert` | content + doc_id + file_path |
| `ainsert` | `lightrag_insert` | content + doc_id + file_path |
| `inject_entity(name, entity_type, description)` | `lightrag_insert_entity(name, entity_type, description)` | 参数名不变，加 source_id/file_path 可选 |
| `inject_relation(source_entity_name, target_entity_name, keywords)` | `lightrag_insert_relation(src_id, tgt_id, relation)` | 参数名变化：source_entity_name→src_id, target_entity_name→tgt_id, keywords→relation |

**关键参数变化示例**：
```
旧：inject_relation("brain:Niu" → entity, keywords="skilled_in")
新：lightrag_insert_relation(src_id="brain:Niu", tgt_id=entity, relation="skilled_in")
```

**brain_meta_* 标签**：保留在 description 中，LightRAG 支持。monkey-patch 机制确保 `_merge_nodes_then_upsert` 保留自定义属性。

#### B8. 强制压缩场景

- force=True 时：dream-evolver 全量处理（绕过增量游标），确保所有未保存知识写入 LightRAG
- **主Agent必须同步等待 dream-evolver 完成**，不能异步
- 处理完成后 context-manager 才执行压缩删除

#### B9. 提示词核心逻辑框架

```
1. 读取增量消息（基于 last_dream_evolve_id 游标，force模式绕过游标）
2. 确保当天 Session 节点存在（兜底机制）
3. 对每条消息执行3项核心任务：
   a. 经验提取与知识沉淀
      - 提取事实/概念/技能 → lightrag_insert_entity(type=concept/skill/fact)
      - 编码分级信息 → description前缀 brain_meta_weight=X;brain_meta_decay_rate=Y;
      - 建立与已有实体/脑区的连接 → lightrag_insert_relation
      - 连接优先：每条新实体至少建1条边，否则连接到Session节点
   b. 关系构建与强化
      - 发现隐含关系 → lightrag_insert_relation
      - 四种时间链关系：followed_by/corrected_by/led_to/resolved_by
      - 连接优先：每条新实体至少建1条边
   c. 画像更新与偏好学习
      - 更新 brain:Niu 实体的 description → lightrag_insert_entity
      - 记录偏好/情感 → lightrag_insert_relation(brain:Niu→entity, prefers/feels)
4. 检查是否需要触发脑区隔离（实体数量增长到阈值）
   - 调用 RegionManager.incremental_update()
   - 新脑区主节点 → lightrag_insert_entity + lightrag_insert_relation
5. 更新游标（last_dream_evolve_id）
6. force 模式下：全量处理，不使用游标
```

---

### C. entity-extractor（改用 LightRAG 原生工具）

**mcpServers**: `lightrag-server`（不变）

**工具映射**：
| 旧工具 | 新工具 | 用途 |
|--------|--------|------|
| `search_documents` | `lightrag_search_entities` | 查询已有实体（按 entity_type 过滤） |
| `get_document` | `lightrag_query` + `lightrag_list_entities(list_type="documents")` | 获取文档信息 |
| `list_documents` | `lightrag_list_entities(list_type="documents")` | 列出文档 |
| `inject_entity` | `lightrag_insert_entity` | 精确注入实体 |
| `inject_relation` | `lightrag_insert_relation` | 精确注入关系 |

**职责重新定位**：
- 从"从向量库搜索文档 → 提取实体 → 注入知识图谱"改为"从 LightRAG 查询已有实体 → 发现缺失 → 补充注入"
- 去重由 LightRAG `_merge_nodes_then_upsert` 自动处理，不再需要手动去重逻辑
- "本子 Agent 未挂载 kg-server MCP 工具"说明删除（lightrag-server 已挂载）

---

### D. event-manager（独立化 + LightRAG 双轨）

**mcpServers**: 移除 `lightrag-server`，改为独立程序 + 可选 lightrag-server

**架构**：
```
事件写入 → 1) JSON 文件（精确 CRUD）→ 2) lightrag_insert（语义可发现）
事件查询 → JSON 文件（按状态/时间精确过滤）
事件删除 → 1) JSON 文件删除 → 2) lightrag_delete_document（如果存在对应文档）
大模型检索 → lightrag_query（"下周有什么安排？" → 返回相关事件实体）
```

**结构化存储格式**（JSON 文件，如 `~/.niu/events.json`）：
```json
{
  "events": [
    {
      "id": "evt_001",
      "type": "meeting",
      "title": "项目评审会",
      "status": "pending",
      "event_time": "2026-03-31T15:00:00",
      "recurrence": null,
      "content": "与产品团队进行Q1项目评审",
      "lightrag_doc_id": "doc_evt_001",
      "created_at": "2026-03-25T10:00:00",
      "updated_at": "2026-03-25T10:00:00"
    }
  ]
}
```

**LightRAG 同步**：
- 事件创建时：`lightrag_insert(content="[Event: meeting] 项目评审会 | status:pending | time:2026-03-31T15:00:00", doc_id="doc_evt_001")`
- 事件删除时：`lightrag_delete_document(doc_id="doc_evt_001")`（级联删除关联实体和关系）
- 事件状态变更时：delete + re-insert（LightRAG 无 metadata 更新 API）

**移除的 vector-store 工具**：add_document, search_documents, get_document, delete_document, list_documents, count_documents

**L1 工作记忆功能**：取消，由 dream-evolver 承担

---

### E. kg-enricher（删除）

**操作**：
1. 从 `config/agents/niu.md` 的 sub agents 列表中移除
2. 删除 `config/agents/kg-enricher.md`
3. 清理 `niu.md` 中 `chat-with-kg-enricher` 工具引用

---

## lightrag-server 工具变化

### 需新增 2 个工具

| 工具 | 对应旧工具 | LightRAG 底层 API | 用途 |
|------|-----------|-------------------|------|
| `lightrag_get_document` | `get_document` | `rag.full_docs.get_by_id(doc_id)` | 获取完整文档内容 |
| `lightrag_delete_document` | `delete_document` | `rag.adelete_by_doc_id(doc_id)` | 级联删除文档 + chunks + entities + relationships |

**实现要点**：
- `lightrag_get_document`：访问 `rag.full_docs` 内部存储获取完整内容，同时从 `rag.doc_status` 获取处理状态
- `lightrag_delete_document`：调用 `rag.adelete_by_doc_id()`，返回 `DeletionResult`
- 两个工具都不修改 LightRAG 源码，只在 MCP 层封装已有 API

### handler.py _TOOL_ALIASES 修复

| 旧映射 | 问题 | 修复 |
|--------|------|------|
| `"vector-store/get_document": "lightrag-server/lightrag_document_status"` | 语义错误：document_status 只返回计数，不返回内容 | → `"lightrag-server/lightrag_get_document"` |
| `"vector-store/update_metadata": "lightrag-server/lightrag_document_status"` | 无有效替代：LightRAG 无 metadata 更新 API | 删除此映射 |
| `"vector-store/delete_document": "lightrag-server/lightrag_delete_entity"` | 语义错误：delete_entity 只删实体，不级联删文档 | → `"lightrag-server/lightrag_delete_document"` |

### DEPRECATED_ALIASES 更新

`lightrag-server/__init__.py` 中的 `DEPRECATED_ALIASES` 需更新：

| 旧映射 | 修复 |
|--------|------|
| `"get_document": "lightrag_document_status"` | → `"lightrag_get_document"` |
| `"delete_document": "lightrag_delete_entity"` | → `"lightrag_delete_document"` |
| `"update_metadata": "lightrag_document_status"` | 删除（无对应工具） |

---

## 实施顺序

1. lightrag-server 新增 `lightrag_get_document` + `lightrag_delete_document`
2. handler.py `_TOOL_ALIASES` 修复
3. DEPRECATED_ALIASES 更新
4. dream-evolver.md 重写（整合脑区激活方案 + 工具映射 + 连接优先 + 分级写入 + Session兜底 + 时间链4种关系）
5. entity-extractor.md 更新（工具名替换 + 职责重新定位）
6. context-manager.md 更新（三种工作模式 + 移除 lightrag-server + 知识保存由 dream-evolver 承担）
7. event-manager.md 更新（独立化 + 双轨架构）
8. kg-enricher.md 删除 + niu.md 清理
9. compat.py force 模式实现（同步等待 dream-evolver → context-manager）
10. RegionManager 缺失修复（incremental_update、邻居图构建、LLM摘要、runner.py集成）

---

## 不在本次范围

- event-manager 结构化存储的代码实现 — 需要独立的 MCP 服务器或工具
- LightRAG entity_extraction_prompt 中添加领域指令 — 可作为后续优化
- 照片 KG 去重一次性清理 — 可用 `lightrag_merge_entities` 手动执行
- brain_meta 扁平化的代码实现 — 需要扩展 lightrag_insert_entity 的参数
