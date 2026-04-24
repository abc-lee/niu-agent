# Brain Region Activation — 脑区激活架构设计

> 最后更新：2026-04-24
> 状态：📋 实施方案细化完成，待实施
> 依赖：03-memory-brain-graph.md（脑图基础）、01-data-injection-retrieval.md（注入/检索）
> 前置：Phase -1 LightRAG merge 保留自定义属性（✅ 已通过 monkey-patch 完成）

## 1. Executive Summary

本文档在 Phase 03 脑图基础上，增加**脑区层**，解决 agent 在对话过程中工作记忆丢失的问题。

**核心问题**：当前 agent 用最近3条对话+3条工具名做向量查询，激活相关知识。但聊几句闲话后，激活的知识就会从上下文中消失，agent "变笨了"。

**解决方案**：在实体/关系层之上增加脑区层。脑区是知识图谱中自动聚类形成的社区，每个脑区有一个整体激活度。被激活的脑区缓慢衰减（而非立即消失），其知识和技能持续注入系统提示词，确保工作记忆不会因话题短暂偏移而丢失。

**三层架构**：

```
层1: 实体/关系层（Phase 03 已设计）
     brain:Niu, brain:person:LiLei, 记忆关系, Ebbinghaus权重衰减

层2: 脑区层（本文档新增）
     Leiden社区检测, 区域代表节点, 脑区激活/衰减

层3: 注入层（增强Phase 03）
     激活度加权检索, 分层注入系统提示词, 工作记忆持续保持
```

### 讨论确认的关键决策

| # | 决策 | 理由 |
|---|------|------|
| R1 | 脑区 = 图社区聚类，非物理隔离 | 脑区内节点天然与外部有连接，查询时自然穿透边界 |
| R2 | 激活 = 搜索得分加分/注意力权重 | 不是关上门，是给更亮的灯更多注意力 |
| R3 | 脑区整体激活，非逐实体 | 逐实体追踪激活度太复杂，整片区域亮起更符合人脑隐喻 |
| R4 | 激活度作为查询加分项 | 被激活脑区的实体在向量/图搜索中获得额外得分 |
| R5 | 衰减缓慢，闲聊不熄灭 | decay_factor ≈ 0.92/轮，20轮闲聊后仍有0.19激活度 |
| R6 | 单图共享LightRAG实例 | 脑区和文档知识在同一个图中，交叉点亮 |
| R7 | Leiden算法做社区检测 | 比Louvain更稳定，支持增量更新(is_membership_fixed) |
| R8 | 组合封装LightRAG，不改源码 | LightRAG标记@final不可继承，上层组合扩展 |

---

## 2. 脑区形成 — Leiden 社区检测

### 2.1 为什么需要脑区

Phase 03 的脑图以 `brain:Niu` 为中心，所有记忆关系从它出发。随着知识增长，这些关系会自然聚类：

```
brain:Niu ──skilled_in──→ Python ──associated_with──→ NumPy
     │                        │
  prefers                  USED_FOR
     │                        │
  Dark_Mode              Data_Analysis
     │
  participated_in
     │
  AI_Bot ──USED_FOR──→ Web_Development
```

图中 Python/NumPy/Data_Analysis 自然聚成"编程开发"脑区，AI_Bot/Web_Development 聚成"项目管理"脑区。这些聚类是客观存在的，社区检测只是让它们显性化。

### 2.2 算法选择：Leiden

**对比**：

| 算法 | 稳定性 | 增量更新 | 社区质量 | 速度 |
|------|--------|---------|---------|------|
| Louvain | 不稳定（随机性） | 不支持 | 可能内部不连通 | 快 |
| **Leiden** | **稳定** | **支持(is_membership_fixed)** | **保证内部连通** | 快 |
| Label Propagation | 非常不稳定 | 天然支持 | 无质量保证 | 最快 |

**Leiden 的关键优势 — 增量更新**：

`leidenalg` 库的 `is_membership_fixed` 参数允许冻结已有节点的社区归属，只优化新增/变更节点。这意味着：
- 已形成的脑区不会因少量新知识而大幅重组
- 增量更新的计算量与变更节点成正比，而非全图规模
- 新节点自然归入最相连的脑区

**技术栈**：
- `leidenalg` + `igraph`（C底层，速度快）
- 从 NetworkX 图转换：`ig.Graph.from_networkx(G)`
- Neo4j 后端：GDS Leiden/Louvain + `seedProperty` 热启动

### 2.3 脑区形成流程

```
阶段1: 冷启动（图节点 < 50）
  → 不做社区检测
  → 所有知识属于"默认脑区"
  → 只维护 brain:Niu 的直接关系

阶段2: 首次聚类（节点 ≥ 50）
  → 运行完整 Leiden 算法
  → 每个社区 = 一个脑区
  → 为每个脑区选最高度节点作为"代表"
  → 创建虚拟中枢节点连接所有脑区代表

阶段3: 增量更新（新知识入库后）
  → 冻结已有节点的社区归属
  → 新节点分配到最相连邻居的社区
  → 解冻新节点及其1-2跳邻居
  → 运行 Leiden 增量优化
  → 持久化社区归属（存为节点属性）
```

### 2.4 脑区主节点（核心设计）

每个脑区创建一个**虚拟主节点** `brain:region:{name}`，它承担三重角色：

**角色1：脑区语义指针**

主节点的 `description` 是由 LLM 对脑区内一级节点的摘要。LightRAG 对 description 做 embedding 后，这个向量**就是整个脑区的语义指针**——无需额外计算质心向量，完全复用 LightRAG 的 embedding 流程。

```
brain:region:编程开发
  description: "Python编程(专家级)、NumPy数据处理、Web开发技术栈、
               数据分析方法、AI/ML项目经验"

查询 "帮我分析数据" → embedding → entities_vdb 余弦匹配
  → 命中 brain:region:编程开发（摘要包含"数据分析"）
  → 脑区被激活 → 图遍历展开内部知识
```

**角色2：脑区搜索入口**

主节点与脑区内所有一级实体建立 `belongs_to` 关系。激活脑区后，从主节点出发图遍历即可展开整个脑区的知识，作为种子注入 LightRAG 查询流程。

**角色3：脑区元数据容器**

主节点存储脑区级元数据（扁平化键值，GraphML 兼容）。

```python
# 脑区主节点数据结构
{
    "entity_name": "brain:region:编程开发",
    "entity_type": "BrainRegion",
    "description": "Python编程(专家级)、NumPy数据处理、Web开发技术栈、数据分析方法、AI/ML项目经验",
    "source_id": "brain",
    "brain_meta_region_id": "community_3",
    "brain_meta_size": "6",              # 脑区内实体数
    "brain_meta_representative": "Python", # 度最高的实体
    "brain_meta_created_at": "1745366400",
    "brain_meta_updated_at": "1745366400",
}
```

**实现流程**：

```python
async def create_region_master_nodes(
    graph, community_partition, rag
):
    """
    为每个脑区创建主节点 + 摘要 + 一级关系。
    """
    for community_id, nodes in community_partition.items():
        # 1. 收集脑区内一级实体的名称和描述
        entity_summaries = []
        for node_name in nodes:
            node_data = await graph.get_node(node_name)
            if node_data and not node_name.startswith("brain:region:"):
                entity_summaries.append(
                    f"{node_name}({node_data.get('entity_type','')}): "
                    f"{node_data.get('description','')[:100]}"
                )

        # 2. LLM 生成脑区摘要
        region_summary = await llm_summarize_region(entity_summaries)
        region_name = await llm_name_region(region_summary)

        # 3. 创建主节点（ainsert_custom_kg 自动 embedding）
        master_entity = {
            "entity_name": f"brain:region:{region_name}",
            "entity_type": "BrainRegion",
            "description": region_summary,
            "source_id": "brain",
        }
        master_relations = []

        # 4. 主节点 → brain:Niu 连接（全局入口）
        master_relations.append({
            "src_id": "brain:Niu",
            "tgt_id": f"brain:region:{region_name}",
            "description": f"脑区: {region_name}",
            "keywords": "brain_region_anchor",
            "weight": 1.0,
        })

        # 5. 主节点 → 脑区内一级实体连接
        for node_name in nodes:
            if not node_name.startswith("brain:region:"):
                master_relations.append({
                    "src_id": f"brain:region:{region_name}",
                    "tgt_id": node_name,
                    "description": f"属于{region_name}脑区",
                    "keywords": "belongs_to",
                    "weight": 0.8,
                })

        await rag.ainsert_custom_kg({
            "entities": [master_entity],
            "relationships": master_relations,
        })
```

**与"代表节点"方案的区别**：

| | 旧方案：代表节点 | 新方案：主节点 |
|---|---|---|
| 语义表示 | 无，代表节点只是度最高的普通实体 | 有，description 是脑区摘要，embedding = 脑区语义指针 |
| 搜索入口 | 代表节点本身有自己的语义，不纯粹 | 主节点专为此设计，搜索时精确匹配脑区 |
| 脑区元数据 | 无处存放 | 主节点扁平化键值存储 |
| 查询时激活 | 需后置加权或额外逻辑 | 自然命中 entities_vdb → 自动激活 |
| 额外计算 | 无 | 一次 LLM 摘要调用（脑区更新时） |
| 额外存储 | 无 | 一个实体 + N条关系（belongs_to） |

**核心优势：零侵入激活**。主节点存在于 entities_vdb 中，用户查询时 LightRAG 的正常向量搜索就能命中它，不需要修改 LightRAG 的查询流程。命中主节点后，图遍历沿 `belongs_to` 关系展开脑区内部知识——这就是"点亮"。

### 2.5 触发时机

社区检测**不在**每次插入时运行，避免性能问题：

| 触发条件 | 说明 |
|---------|------|
| 批量插入完成后 | `index_done_callback` 后，对受影响区域增量更新 |
| 新节点超过阈值 | 图规模增长超过5%时，触发一次增量更新 |
| 定时任务 | 每日凌晨3:00，检查是否需要重新聚类 |
| 手动触发 | `brain_consolidate` 工具支持手动触发 |

### 2.6 Resolution 参数

Leiden 的 `resolution_parameter` 控制脑区粒度：

| 值 | 效果 | 适用场景 |
|----|------|---------|
| 0.5 | 少量大脑区（如3-5个） | 知识量较少时 |
| 1.0 | 中等脑区（如8-15个） | 默认值 |
| 2.0 | 多量小脑区（如20+个） | 知识量很大、领域细分时 |

初期使用默认值 1.0，根据实际使用效果调整。

---

## 3. 脑区激活器

### 3.1 双层衰减模型

Phase 03 已有关系级权重衰减（Ebbinghaus遗忘曲线），脑区层新增会话级激活衰减：

```
层1 - 关系级 weight（Phase 03）
  → 长期记忆强度，持久化到图
  → Ebbinghaus衰减：weight *= (1 - decay_rate × days)
  → 反映"这条记忆我有多确定"

层2 - 脑区级 activation（新增）
  → 短期工作记忆，会话级，不持久化
  → 对话衰减：activation *= decay_factor（每轮对话）
  → 反映"这个领域我现在多关注"
```

两层独立运作，查询时互相配合。

### 3.2 激活度数据结构

```python
@dataclass
class BrainRegion:
    """脑区激活状态"""
    region_id: str              # Leiden社区ID
    label: str                  # 脑区名称（用代表节点名或LLM生成）
    representative: str         # 代表节点
    nodes: set[str]             # 脑区内所有实体
    size: int                   # 实体数量
    activation: float           # 当前激活度 0.0-1.0
    last_activated_at: float    # 上次激活时间戳
    activation_count: int       # 本会话被激活次数


@dataclass
class RegionActivationManager:
    """管理所有脑区的激活状态，会话生命周期"""
    regions: dict[str, BrainRegion]        # region_id -> BrainRegion
    decay_factor: float = 0.92             # 每轮衰减因子
    activation_boost: float = 1.0          # 激活时设置的值
    min_activation: float = 0.05           # 低于此值视为暗淡
    activation_threshold: float = 0.3      # 注入上下文的最低激活度
```

### 3.3 激活规则

```python
class RegionActivationManager:

    def activate_region(self, region_id: str):
        """点亮一个脑区"""
        if region_id in self.regions:
            region = self.regions[region_id]
            region.activation = self.activation_boost  # 设为1.0
            region.last_activated_at = time.time()
            region.activation_count += 1

    def decay_all(self):
        """每轮对话后，所有脑区衰减一轮"""
        for region in self.regions.values():
            region.activation *= self.decay_factor

    def find_activated_regions(self, query_entities: list[str]) -> set[str]:
        """根据查询命中的实体，找到需要激活的脑区"""
        activated = set()
        for entity in query_entities:
            for rid, region in self.regions.items():
                if entity in region.nodes:
                    self.activate_region(rid)
                    activated.add(rid)
        return activated

    def get_active_regions(self) -> list[BrainRegion]:
        """获取所有激活度超过阈值的脑区，按激活度降序"""
        return sorted(
            [r for r in self.regions.values() if r.activation > self.activation_threshold],
            key=lambda r: r.activation,
            reverse=True
        )
```

### 3.4 衰减速度分析

`decay_factor = 0.92` 时：

| 闲聊轮数 | 激活度 | 状态 |
|---------|--------|------|
| 0轮（刚激活） | 1.00 | 全亮 |
| 5轮 | 0.66 | 亮 |
| 10轮 | 0.43 | 中等 |
| 15轮 | 0.28 | 偏暗但仍在注入 |
| 20轮 | 0.19 | 暗，接近阈值 |
| 25轮 | 0.12 | 很暗 |
| 30轮 | 0.08 | 低于阈值，停止注入 |

**关键特性**：即使聊了15轮闲话，"编程开发"脑区仍有0.28的激活度，相关知识仍会以摘要形式出现在系统提示词中。而一旦用户再次提到 Python，激活度立刻跳回1.0。

### 3.5 激活触发来源

| 来源 | 触发方式 | 说明 |
|------|---------|------|
| **向量自然命中** | LightRAG 向量搜索命中 `brain:region:*` 主节点 | **主要触发源**，主节点 description 的 embedding 与查询语义匹配时自动命中 |
| 用户消息 | 提取关键词→图搜索命中实体→激活所属脑区 | 补充触发源，覆盖主节点未命中的情况 |
| 工具调用 | 工具名→查找工具所属脑区→激活 | Phase 04 的USED_FOR关系 |
| 主动回忆 | agent 调用 brain_recall→激活相关脑区 | Agent主动回忆时 |
| 连带激活 | 被激活脑区的一跳邻居脑区获得0.3×激活度 | 脑区间的关联触发 |

**主节点的零侵入激活**是最优雅的路径：查询"帮我分析数据"→ entities_vdb 余弦匹配→命中 `brain:region:编程开发` 主节点→脑区激活→图遍历沿 `belongs_to` 展开内部知识。无需修改 LightRAG 查询流程。

**连带激活**是关键——用户提到"Python"，"编程开发"脑区全亮，"项目管理"脑区（因为AI_Bot项目用Python）也获得0.3的激活度，形成联想式回忆。

---

## 4. 激活加权的上下文注入

### 4.1 与 Phase 03 注入的衔接

Phase 03 设计的 `extract_brain_memories()` 从 `brain:Niu` 的关系提取记忆。现在升级为按脑区激活度分层注入：

```python
async def inject_brain_context(
    query_context: str,
    rag: LightRAG,
    activation_manager: RegionActivationManager,
) -> str:
    """
    基于脑区激活度的上下文注入。

    替代 Phase 03 的 extract_brain_memories()，
    增加脑区级激活度控制注入内容和深度。
    """
    # Step 1: 用查询上下文激活相关脑区
    # query_context = 最近3条消息 + 最近3条工具名（现有逻辑）
    from lightrag import QueryParam
    query_result = await rag.aquery(
        query_context,
        param=QueryParam(mode="mix", only_need_context=True, top_k=10),
    )
    # 从查询结果中提取命中的实体（含脑区主节点）
    hit_entities = extract_entities_from_result(query_result)
    activation_manager.find_activated_regions(hit_entities)

    # Step 1.5: 如果查询命中了脑区主节点，把主节点作为种子注入
    # 种子注入：激活脑区的代表节点加入种子集，图遍历从激活区展开
    region_masters = [e for e in hit_entities if e.startswith("brain:region:")]
    if region_masters:
        # 二次查询：从脑区主节点出发的图遍历，获取脑区内部知识
        for master in region_masters:
            region_result = await rag.aquery(
                master,  # 用主节点名作为查询
                param=QueryParam(mode="local", only_need_context=True, top_k=5),
            )
            # 合并到注入内容

    # Step 2: 衰减所有脑区（本轮对话的衰减）
    activation_manager.decay_all()

    # Step 3: 按激活度分层注入
    active_regions = activation_manager.get_active_regions()

    parts = []
    for region in active_regions:
        if region.activation > 0.7:
            # 高激活：注入详细知识
            parts.append(format_detailed_region(region, rag))
        elif region.activation > 0.3:
            # 中激活：注入摘要
            parts.append(format_summary_region(region, rag))
        else:
            # 低激活但超阈值：只注入脑区名
            parts.append(format_label_region(region))

    return "\n".join(parts)
```

### 4.2 脑区状态灯 + 全景地图注入

每轮对话的上下文注入，首先要注入一张**脑区全景地图**，让主 Agent 清楚地知道自己的"大脑"现在是什么状态。

**三状态灯**：

| 状态灯 | 激活度 | 图标 | 含义 |
|--------|--------|------|------|
| 点亮 | > 0.7 | 🟢 | 当前活跃脑区，完整知识已注入 |
| 即将熄灭 | 0.1 - 0.7 | 🟡 | 近期活跃但正在衰减，摘要级注入 |
| 熄灭 | < 0.1 | ⚫ | 不活跃脑区，仅名称可见 |

**注入格式**：

```
## 脑区状态 (6个脑区)
🟢 编程开发 — Python/NumPy/Web技术栈，你擅长编程 (6实体)
🟢 项目管理 — AI_Bot项目，你是主开发者 (4实体)
🟡 日常偏好 — 你偏好暗色主题，远程办公 (3实体)
🟡 客户沟通 — 近期与客户讨论需求变更 (5实体)
⚫ 财务知识 — 报销流程、预算审批 (2实体)
⚫ 摄影爱好 — 照片整理、人脸识别 (3实体)
```

这张地图**始终注入**系统提示词，消耗极少 token（每脑区约 15-20 tokens），但让主 Agent 对自己的知识全景有清晰认知。

**详细知识按激活度分层注入**：

点亮（> 0.7）— 注入该脑区的实体、关系和关联文档片段：

```
### [编程开发] (活跃)
实体: Python(expert), NumPy, Data_Analysis, Web_Development
关系:
- 你擅长Python(expert级别)，从2019年开始用于AI/ML
- Python与NumPy通过数据科学生态关联
- Web_Development是AI_Bot项目的技术栈
知识: [相关文档片段，最多3条]
```

即将熄灭（0.1 - 0.7）— 注入摘要：

```
### [项目管理] (近期)
你在参与AI_Bot项目，是主开发者。项目使用Python/Web技术栈。
```

熄灭（< 0.1）— 不注入详细内容，仅在全景地图中可见名称。

### 4.3 主 Agent 脑区控制工具

主 Agent 作为"当事人"，比任何算法都更清楚当前工作需要哪些脑区。提供两个工具让它主动控制：

#### `brain_region_activate` — 点亮脑区

```python
"brain_region_activate": {
    "description": "主动点亮一个或多个脑区，使其知识立即注入上下文。当你判断接下来的工作需要某个领域的知识时使用。",
    "parameters": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要点亮的脑区名称列表，如 ['编程开发', '项目管理']"
            },
            "reason": {
                "type": "string",
                "description": "为什么要点亮这些脑区（用于记忆记录）"
            }
        },
        "required": ["regions"]
    }
}
```

**使用场景**：
- Agent 判断"接下来要处理PDF相关任务"→ 点亮"编程开发"脑区
- 用户说"帮我看看那个项目"→ Agent 点亮"项目管理"脑区
- Agent 要切换工作上下文时主动点亮目标脑区

**效果**：被点亮的脑区 activation = 1.0，等效于被查询自动激活。

#### `brain_region_dim` — 关闭脑区

```python
"brain_region_dim": {
    "description": "主动关闭一个或多个脑区，停止注入其详细知识。当你确认某领域知识不再需要时使用，可节省上下文空间。",
    "parameters": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关闭的脑区名称列表"
            },
            "reason": {
                "type": "string",
                "description": "为什么要关闭这些脑区（可选）"
            }
        },
        "required": ["regions"]
    }
}
```

**使用场景**：
- Agent 从"编程开发"切换到"客户沟通"→ 关闭"编程开发"释放上下文
- 上下文空间紧张时，主动关闭不活跃脑区
- 用户说"这个话题结束了"→ Agent 关闭相关脑区

**效果**：被关闭的脑区 activation = 0.0（不是衰减到0，是立即熄灭），该脑区的详细知识立即从系统提示词中移除，但仍在全景地图中可见（状态变为⚫）。

#### 自动 vs 手动的协同

```
自动激活（被动）                    手动控制（主动）
─────────────────                  ──────────────
查询命中实体 → 脑区自动点亮          brain_region_activate → 立即点亮
每轮衰减 → 自然变暗                 brain_region_dim → 立即熄灭
连带激活 → 邻居脑区微亮              主Agent可以点亮任意脑区

协同原则：
1. 自动激活保证"不会遗漏"——即使用户没明确说，系统也能感知工作上下文
2. 手动控制保证"精准聚焦"——Agent比算法更清楚当前需要什么
3. 手动操作优先级高于自动——手动关闭的脑区不会被自动重新点亮（当轮内）
4. 下一轮对话时手动效果自然消退，回归自动模式（避免误操作锁死）
```

### 4.3 激活度加权查询

除了分层注入，激活度还影响 LightRAG 的搜索得分：

```python
def apply_activation_weight(
    query_results: list[dict],
    activation_manager: RegionActivationManager,
    boost_factor: float = 0.3,
) -> list[dict]:
    """
    被激活脑区的实体在搜索结果中获得额外得分。

    最终得分 = LightRAG原始得分 + 脑区激活度 × boost_factor

    这使得激活脑区的实体在检索结果中排名更高，
    即使它们的向量相似度不是最高的。
    """
    for result in query_results:
        entity_name = result.get("entity_name", "")
        # 查找实体所属脑区
        for region in activation_manager.regions.values():
            if entity_name in region.nodes:
                result["score"] += region.activation * boost_factor
                break
    return sorted(query_results, key=lambda r: r["score"], reverse=True)
```

### 4.4 上下文预算控制

系统提示词空间有限，需要控制注入量：

```python
# 上下文预算分配
CONTEXT_BUDGET = {
    "total_tokens": 4000,       # 脑图注入的总token预算
    "high_activation": 2000,    # 高激活脑区分配
    "mid_activation": 1200,     # 中激活脑区分配
    "low_activation": 400,      # 低激活脑区分配
    "skills": 400,              # 技能注入预算
}

# 注入时按预算截断
def format_detailed_region(region, rag, budget=2000):
    """格式化高激活脑区，控制在预算内"""
    # 优先注入：代表节点描述 > 高权重关系 > 关联文档
    # 超出预算时截断低优先级内容
    ...
```

---

## 5. 双记忆系统 — 语义记忆与情景记忆

### 5.1 两类记忆的本质区别

人脑有两种根本不同的记忆系统，脑图必须区分对待：

| | 语义记忆（知识类） | 情景记忆（事件类） |
|---|---|---|
| 本质 | "我知道什么" | "我经历了什么" |
| 结构 | 按关联强度建边，无序 | 按时间链建边，有序 |
| 衰减 | 关联越强越持久，孤立节点衰减 | 近期事件清晰，远期模糊 |
| 检索 | 联想式：从A想到B | 时序式：从A想起前后发生了什么 |
| 示例 | Python ──associated_with──→ NumPy | 做PPT ──followed_by──→ 选模板B |
| 排序逻辑 | 权重/相似度 | 时间先后 |

**关键原则**：知识类节点按权重建边，事件类节点**必须保留时间链**。

### 5.2 情景记忆的时间链

事件/经验之间用 `followed_by` 关系串联，形成有向链：

```
brain:event:做PPT ──followed_by──→ brain:event:选模板A
                                          │
                                     corrected_by
                                          │
                                          ▼
                                  brain:event:选模板B
```

比单纯的时间戳更强：
- 时间戳只能排序，不能表达"选模板B是因为纠正了A"
- `followed_by` / `corrected_by` 关系表达**因果和演化**，不只是先后
- 图遍历时，agent 能顺着链看到完整的故事线

### 5.3 时间链的关系类型

| 关系类型 | 含义 | 示例 |
|---------|------|------|
| `followed_by` | 时序先后（A之后发生了B） | 做PPT → 选模板 |
| `corrected_by` | 纠正（B纠正了A的决策） | 选模板A → 选模板B |
| `led_to` | 因果（A导致了B） | 遇到错误 → 切换方案 |
| `resolved_by` | 解决（B解决了A的问题） | 问题X → 解决方案Y |

### 5.4 时间链与脑区的关系

时间链不破坏脑区结构，而是在脑区内部增加一层时序：

```
[项目管理] 脑区
  ├── brain:event:开始项目 ──followed_by──→ brain:event:选技术栈
  │                                            │
  │                                       led_to
  │                                            │
  │                                            ▼
  ├── brain:concept:Python ──used_in──→ brain:event:选技术栈
  │
  └── brain:concept:Web_Development ──used_in──→ brain:event:选技术栈

检索时：从 Python 出发 → 图遍历到"选技术栈"事件
        → 沿 followed_by 链 → 看到完整的决策过程
```

### 5.5 Session 节点

同一会话的事件挂在同一个 Session 节点下，Session 内按时间链排列：

```python
# Session 节点（brain_meta 扁平化为独立键值，GraphML兼容）
{
    "entity_name": "brain:session:2026-04-23_14-30",
    "entity_type": "Session",
    "description": "用户讨论PPT制作和技术选型",
    "source_id": "brain",
    "brain_meta_created_at": "2026-04-23T14:30:00Z",
    "brain_meta_duration_minutes": 45,
    "brain_meta_message_count": 23,
}

# Session 与事件的关系
brain:session:2026-04-23_14-30 ──contains──→ brain:event:做PPT
brain:session:2026-04-23_14-30 ──contains──→ brain:event:选模板A
brain:session:2026-04-23_14-30 ──contains──→ brain:event:选模板B
```

跨 Session 的关键事件再用 `followed_by` 连接，形成长期的故事线。

---

## 6. Dream Evolver 升级 — 内容提取与脑图写入

### 6.1 核心原则：宁多勿少，靠遗忘曲线自然筛选

初期不确定重要性时多记，不危险，因为：

- **多记但全连接** → 新记忆自动与已有实体建边 → 不是孤岛 → 不会成为垃圾
- **权重初始设低** → L0 记忆 weight=0.3，decay_rate=0.05 → 45天自然衰减到0.1
- **重要的被 reinforce** → 被检索到时 +0.1 → 反复出现的记忆权重升高 → 存活
- **不重要的自生自灭** → 45天没人想起 → 被遗忘清理掉
- **连接本身就是重要性的信号** → 有连接的记忆容易被检索到 → reinforce → 存活

**不需要"重要性判断算法"，连接+衰减就是最好的过滤器。**

### 6.2 连接优先：每次提取必须建关系

现有 Dream Evolver 的问题是"提取了但不连接"。升级后，每个提取的记忆**必须**通过关系与已有实体连接：

```
错误经验: "用错工具处理PDF"
  → 创建 brain:event:Wrong_Tool_PDF
  → brain:Niu --remembers--> brain:event:Wrong_Tool_PDF
     "犯过错：用X工具处理PDF，应该用Y工具"
  → brain:event:Wrong_Tool_PDF --associated_with--> Skill:PDF_Processing
     "与PDF处理技能相关"
  → brain:event:Wrong_Tool_PDF --associated_with--> brain:concept:PDF
     "与PDF概念关联"
```

用户下次提到"PDF"，图遍历自然会经过这个错误经验，agent 就能避免再犯。

### 6.3 人物名称自动连接

人物名称不需要 Dream Evolver 额外处理。系统已有机制保证连接：
- 通讯录 → 人名实体 + 关系
- 照片人脸识别 → 人名实体 + 合影关系
- 对话中提及人名 → LightRAG ainsert() 自动提取实体 → 与已有人物实体合并

Dream Evolver 提取到人名时，`ainsert_custom_kg()` 创建的实体会被 LightRAG 的 merge 逻辑自动与已有实体合并。

### 6.4 现有6项 → 新3项

| 旧工作项 | 新工作项 | 变化 |
|---------|---------|------|
| 错误经验 | → **经验提取**（含成功+失败） | 合并，用 relation_type 区分 error/success |
| 成功经验 | ↗ | |
| 工具方言 | → **关系构建** | USED_FOR/OFTEN_WITH 关系替代 query_pattern 递归检索 |
| 用户状态 | → **画像更新**（含状态+画像） | 合并，都是 brain:Niu 的属性/偏好关系 |
| 用户画像 | ↗ | |
| KG实体/关系 | → **关系构建** | ainsert_custom_kg 统一写入 |

### 6.5 新3项详细设计

#### 工作项A：经验提取（替代旧1+2）

从对话中提取成功/失败经验，写入脑图并建立连接：

```
提取规则：
1. 用户说"不对/错了/改一下" → 错误经验，relation_type="error_experience"
2. 用户说"好的/对了/完美" → 成功经验，relation_type="success_experience"
3. 工具调用失败后重试成功 → 错误+成功配对

写入格式（ainsert_custom_kg）：
  实体: brain:event:{event_name}
    entity_type: Event
    description: 经验描述
    source_id: "brain"
    brain_meta_origin_level: "L0" 或 "L1"      # 扁平化，GraphML兼容
    brain_meta_session_id: "当前会话ID"
    brain_meta_experience_type: "error" 或 "success"

  关系（必须建立，relation_type编码到keywords中）:
    brain:Niu ──remembers──→ brain:event:{event_name}
      weight: 0.3(L0) 或 0.8(L1)
      description: 经验摘要
      keywords: "error_experience,{事件关键词}"  # relation_type编码在keywords首位

    brain:event:{event_name} ──associated_with──→ 相关实体（Skill/Concept/Person）
      weight: 0.5
      description: 关联原因

  时间链（情景记忆必须，relation_type编码到keywords）:
    前一个事件 ──followed_by──→ brain:event:{event_name}
      keywords: "followed_by"
    或
    前一个事件 ──corrected_by──→ brain:event:{event_name}
      keywords: "corrected_by"（如果是纠正）
```

#### 工作项B：关系构建（替代旧3+6）

从对话中提取实体关系，写入脑图：

```
提取规则：
1. 提到人名 → brain:person:{name} 实体 + associated_with 关系
2. 提到工具调用 → USED_FOR / OFTEN_WITH 关系
3. 提到概念 → brain:concept:{name} 实体 + associated_with 关系
4. 共现实体 → 实体间 associated_with 关系

写入格式：
  实体和关系直接通过 ainsert_custom_kg() 写入
  关系类型根据内容选择:
    - USED_FOR: 工具→任务 (Skill:PDF_Tool ──USED_FOR──→ brain:concept:Report_Writing)
    - OFTEN_WITH: 工具→工具 (Skill:PDF_Tool ──OFTEN_WITH──→ Skill:Email_Sender)
    - associated_with: 实体→实体 (brain:person:LiLei ──associated_with──→ brain:concept:Python)
```

#### 工作项C：画像更新（替代旧4+5）

更新 brain:Niu 的属性和偏好关系：

```
提取规则：
1. 用户表达偏好 → brain:Niu ──prefers──→ 偏好实体
2. 用户展示技能 → brain:Niu ──skilled_in──→ 技能实体
3. 用户情绪/状态 → 更新 brain:Niu 的 description（累积式）
4. 用户习惯 → brain:Niu ──prefers──→ 习惯实体

写入格式：
  与 Phase 03 的画像更新逻辑一致
  累积式：先读取 brain:Niu 现有关系，合并新信息，更新
```

### 6.6 提取分级

| 提取内容 | 初始级别 | Weight | Decay Rate | 理由 |
|---------|---------|--------|------------|------|
| 闲聊中提到的名字/概念 | L0 | 0.3 | 0.05/天 | 可能不重要，快速衰减 |
| 用户明确表达的经验/偏好 | L1 | 0.7 | 0.01/天 | 用户主动说的，保留久一些 |
| 错误/成功的关键经验 | L1 | 0.8 | 0.01/天 | 工作相关，重要 |
| 用户强调"记住这个" | L2 | 0.9 | 0.002/天 | 长期记忆 |
| 多次 reinforce 后的 L1 | L2 | 0.9 | 0.002/天 | 巩固升级 |

判断标准简单明确，在 Dream Evolver 提示词中写清楚规则即可，不需要复杂算法。

### 6.7 Dream Evolver 提示词核心指令（草案）

```
你是梦境进化Agent，负责从对话中提取记忆并写入脑图。

核心原则：
1. 连接优先 — 每条记忆必须与已有实体建立关系，孤岛记忆是无用的
2. 宁多勿少 — 不确定重要性时就记，遗忘曲线会自然筛选
3. 时间链不可断 — 事件之间必须用 followed_by/corrected_by 串联

三类工作（按顺序执行）：

A. 经验提取
   - 用户说"不对/错了/改" → 错误经验，keywords首词=error_experience
   - 用户说"好的/对了/完美" → 成功经验，keywords首词=success_experience
   - 每条经验必须 associated_with 至少一个已有实体
   - 事件之间用 followed_by 串联（keywords="followed_by"），纠正用 corrected_by（keywords="corrected_by"）

B. 关系构建
   - 提到人名 → 创建 person 实体 + associated_with 关系（keywords="associated_with,...")
   - 工具调用 → 创建 USED_FOR / OFTEN_WITH 关系（编码到keywords）
   - 共现概念 → 创建 associated_with 关系

C. 画像更新
   - 新偏好 → brain:Niu --prefers--> 实体（keywords="prefers,...")
   - 新技能 → brain:Niu --skilled_in--> 实体（keywords="skilled_in,...")
   - 累积式更新，不覆盖已有信息

分级规则：
- 闲聊提及 → L0 (weight=0.3, decay=0.05)
- 用户明确表达 → L1 (weight=0.7, decay=0.01)
- 关键经验 → L1 (weight=0.8, decay=0.01)
- 用户强调"记住" → L2 (weight=0.9, decay=0.002)
```

---

## 7. 与现有系统的集成

### 7.1 集成点总览

```
BrainRegionSystem (新增)
  │
  ├── 依赖 Phase 03 脑图
  │   ├── brain:Niu 主实体
  │   ├── brain: 实体/关系 schema
  │   ├── L0/L1/L2 记忆分级
  │   └── 关系级权重衰减
  │
  ├── 依赖 Phase 01 注入/检索
  │   ├── LightRAGAdapter.query()
  │   ├── LightRAGIngester.inject_custom_kg()
  │   └── _inject_dynamic_resources() 改造
  │
  ├── 依赖 Phase 04 MCP工具
  │   ├── USED_FOR 关系（工具→脑区关联）
  │   └── brain_recall 工具升级
  │
  └── 新增组件
      ├── CommunityDetector (Leiden社区检测)
      ├── RegionActivationManager (激活/衰减)
      └── BrainContextInjector (分层注入)
```

### 7.2 brain-server 模块结构升级

Phase 03 设计的 brain-server 结构：

```
mcp-servers/brain-server/
├── src/
│   └── niu_brain_server/
│       ├── __init__.py          # MCP tool definitions
│       ├── schema.py            # Entity/relation schema
│       ├── extractor.py         # LLM提取
│       ├── recall.py            # 检索策略
│       ├── consolidation.py     # L0->L1->L2 + decay
│       └── migration.py         # 数据迁移
```

新增脑区组件：

```
mcp-servers/brain-server/
├── src/
│   └── niu_brain_server/
│       ├── __init__.py
│       ├── schema.py
│       ├── extractor.py
│       ├── recall.py
│       ├── consolidation.py
│       ├── migration.py
│       ├── region_detector.py   # [新增] Leiden社区检测 + 增量更新
│       ├── region_activation.py # [新增] 脑区激活/衰减管理
│       └── region_injector.py   # [新增] 激活度加权上下文注入
```

### 7.3 MCP 工具升级

Phase 03 设计的 `brain_recall` 工具升级：

```python
"brain_recall": {
    "description": "Recall memories from the brain graph. Now supports region-aware activation.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The recall query"
            },
            "mode": {
                "type": "string",
                "enum": ["hybrid", "region_aware"],
                "description": "Recall mode. 'region_aware' uses brain region activation for weighted retrieval. Default: region_aware"
            },
            "activate_only": {
                "type": "boolean",
                "description": "If true, only activate regions without returning results. Useful for pre-loading context. Default: false"
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum results. Default: 10"
            }
        },
        "required": ["query"]
    }
}
```

新增工具：

```python
"brain_region_status": {
    "description": "Show current brain region activation states. Shows which regions are lit up and their activation levels.",
    "parameters": {
        "type": "object",
        "properties": {
            "include_dark": {
                "type": "boolean",
                "description": "Include regions below activation threshold. Default: false"
            }
        }
    }
}
```

### 7.4 定时任务扩展

在 Phase 03 的定时任务基础上增加：

| 任务 | 周期 | 说明 |
|------|------|------|
| `brain_decay` | 每日03:00 | 关系级权重衰减（Phase 03） |
| `brain_consolidate_l0_to_l1` | 每日04:00 | L0→L1记忆巩固（Phase 03） |
| `brain_consolidate_l1_to_l2` | 每周日05:00 | L1→L2记忆巩固（Phase 03） |
| `brain_deduplicate` | 每周日06:00 | 实体去重（Phase 03） |
| **`brain_region_update`** | **每日02:00** | **增量更新脑区社区归属（新增）** |

---

## 8. 配置

在 Phase 03 的 `brain_graph` 配置基础上增加：

```json
{
  "brain_graph": {
    "enabled": true,
    "lightrag_working_dir": "~/.niu/brain_graph",
    "recall_mode": "hybrid",
    "default_decay_rate_l0": 0.05,
    "default_decay_rate_l1": 0.01,
    "default_decay_rate_l2": 0.002,
    "min_weight": 0.1,
    "reinforcement_boost": 0.1,
    "max_recall_depth": 2,
    "consolidation_min_access_l0_to_l1": 3,
    "consolidation_min_access_l1_to_l2": 10,
    "dedup_similarity_threshold": 0.9,

    "region": {
      "enabled": true,
      "algorithm": "leiden",
      "resolution": 1.0,
      "min_graph_size": 50,
      "incremental_update": true,
      "neighbor_unfreeze_depth": 2,

      "decay_factor": 0.92,
      "activation_boost": 1.0,
      "activation_threshold": 0.3,
      "min_activation": 0.05,
      "spillover_activation": 0.3,
      "spillover_depth": 1,

      "context_budget_tokens": 4000,
      "high_activation_budget": 2000,
      "mid_activation_budget": 1200,
      "low_activation_budget": 400,
      "skills_budget": 400,

      "query_boost_factor": 0.3,
      "update_threshold_pct": 5
    }
  }
}
```

---

## 9. LightRAG 最小 Fork 方案（方案B）

### 9.1 可行性审核结论

对设计文档进行了代码级可行性审核，发现 3 个需要修正的问题：

| # | 问题 | 严重性 | 修正方案 |
|---|------|--------|---------|
| 1 | `_merge_nodes_then_upsert` 重建 `node_data` 时只保留7个硬编码字段，自定义属性丢失 | 高 | Fork后修复2处代码，保留已有属性 |
| 2 | `_merge_edges_then_upsert` 同上 | 高 | 同上 |
| 3 | GraphML 不支持嵌套 dict，`brain_meta` 嵌套对象无法持久化 | 高 | `brain_meta` 扁平化为字符串键值 |

### 9.2 Fork 修改点

**修改1：`operate.py` `_merge_nodes_then_upsert`（约第1906行）**

```python
# 修改前（从零构建，丢弃已有属性）：
node_data = dict(
    entity_id=entity_name,
    entity_type=entity_type,
    description=description,
    source_id=source_id,
    file_path=file_path,
    created_at=int(time.time()),
    truncate=truncation_info,
)

# 修改后（基于已有属性更新，保留自定义字段）：
node_data = dict(already_node) if already_node else {}
node_data.update(
    entity_id=entity_name,
    entity_type=entity_type,
    description=description,
    source_id=source_id,
    file_path=file_path,
    created_at=int(time.time()),
    truncate=truncation_info,
)
```

**修改2：`operate.py` `_merge_edges_then_upsert`（约第2435行）**

```python
# 修改前：
await knowledge_graph_inst.upsert_edge(
    src_id, tgt_id,
    edge_data=dict(
        weight=weight,
        description=description,
        keywords=keywords,
        source_id=source_id,
        file_path=file_path,
        created_at=edge_created_at,
        truncate=truncation_info,
    ),
)

# 修改后：
already_edge = await knowledge_graph_inst.get_edge(src_id, tgt_id)
edge_data = dict(already_edge) if already_edge else {}
edge_data.update(
    weight=weight,
    description=description,
    keywords=keywords,
    source_id=source_id,
    file_path=file_path,
    created_at=edge_created_at,
    truncate=truncation_info,
)
await knowledge_graph_inst.upsert_edge(src_id, tgt_id, edge_data=edge_data)
```

**修改原因**：这 2 处是 LightRAG 自身的不一致 — `aedit_entity` 和 `amerge_entities` 已经通过 `{**node_data, **updated_data}` 保留了自定义属性，唯独 `_merge` 系列没有。此修复可考虑提交回 LightRAG 上游。

### 9.3 brain_meta 扁平化

嵌套 dict 无法通过 GraphML 持久化，需要扁平化：

```python
# 不可行（GraphML 不支持 dict）：
{
    "brain_meta": {
        "level": "L0",
        "session_id": "xxx",
        "experience_type": "error"
    }
}

# 可行（扁平化字符串键值）：
{
    "brain_meta_level": "L0",
    "brain_meta_session_id": "xxx",
    "brain_meta_experience_type": "error"
}
```

所有 `brain_meta_*` 属性为字符串或数值类型，GraphML 完全兼容。

### 9.4 relation_type 编码到 keywords

LightRAG 的边 schema 没有 `relation_type` 字段。关系类型编码到 `keywords` 中：

```python
# 关系类型编码格式：
keywords = "error_experience,PDF,工具调用"

# 解析时从 keywords 中识别已知类型：
KNOWN_RELATION_TYPES = {
    "error_experience", "success_experience",
    "followed_by", "corrected_by", "led_to", "resolved_by",
    "prefers", "skilled_in", "remembers", "participated_in",
    "USED_FOR", "OFTEN_WITH", "associated_with",
    "brain_region_anchor",
}

def extract_relation_type(keywords: str) -> str | None:
    for kt in KNOWN_RELATION_TYPES:
        if kt in keywords:
            return kt
    return None
```

### 9.5 Fork 维护策略

- Fork 仓库，保持与上游同步（定期 merge upstream/main）
- 修改仅 2 处×2 行代码 + brain_meta 扁平化约定，冲突风险极低
- 将修复提交 PR 给 LightRAG 上游（是 bug fix，不是 feature）
- 上游合并后可移除 fork

---

## 10. 实施计划

### Phase -1: LightRAG Fork（前置）

1. Fork LightRAG 仓库
2. 修复 `_merge_nodes_then_upsert` 保留已有属性（2行改动）
3. 修复 `_merge_edges_then_upsert` 保留已有属性（2行改动）
4. 编写测试验证自定义属性在 insert→merge 后仍存在
5. 向上游提交 PR（bug fix：merge 应与 edit/merge_entities 行为一致）

### Phase 0: 社区检测基础设施

1. 添加 `leidenalg` + `igraph` 依赖
2. 实现 `region_detector.py`：
   - `detect_communities(graph)` — 完整 Leiden 聚类
   - `incremental_update(graph, partition, changed_nodes)` — 增量更新
   - `select_representatives(partition, graph)` — 选代表节点
   - `persist_partition(partition, graph)` — 持久化社区归属到节点属性
3. 实现 NetworkX ↔ igraph 转换工具

### Phase 1: 激活管理器 + 状态灯注入

1. 实现 `region_activation.py`：
   - `RegionActivationManager` 类
   - 激活规则（查询命中、工具调用、连带激活、手动激活/关闭）
   - 衰减逻辑（每轮对话衰减）
   - 手动控制优先级（手动关闭的脑区当轮不被自动重新点亮）
   - 会话生命周期管理（初始化、保存、恢复）
2. 实现 `region_injector.py`：
   - 脑区全景地图注入（状态灯 + 脑区名 + 一句话描述）
   - 分层注入逻辑（点亮=详细 / 即将熄灭=摘要 / 熄灭=仅名称）
   - 激活度加权查询（`apply_activation_weight`）
   - 上下文预算控制
3. 单元测试：衰减曲线、激活/衰减、连带激活、手动控制

### Phase 2: MCP 工具 + 上下文注入改造

1. 实现 `brain_region_activate` 工具（主 Agent 主动点亮脑区）
2. 实现 `brain_region_dim` 工具（主 Agent 主动关闭脑区）
3. 升级 `brain_recall` 工具（支持 region_aware 模式）
4. 实现 `brain_region_status` 工具（查看脑区激活状态）
5. 改造 `_inject_dynamic_resources()`：
   - 集成脑区全景地图注入 + 分层知识注入
   - 替换原有的向量检索注入

### Phase 3: Dream Evolver 升级 + 集成

1. 重写 Dream Evolver 提示词（6项→3项，增加时间链和连接优先原则）
2. Dream Evolver 写入目标从 Kuzu kg-server 迁移到 LightRAG ainsert_custom_kg
3. 脑区更新定时任务（`brain_region_update`）
4. 集成测试：完整流程（插入→聚类→激活→注入→衰减→手动控制→再激活）
5. 与 Phase 03 其他功能（consolidation、dedup）的兼容性验证

---

## 11. 风险分析

| 风险 | 影响 | 可能性 | 缓解 |
|------|------|--------|------|
| Leiden社区在小图上不稳定 | 中 | 中 | 图节点<50时不做检测，使用默认脑区 |
| NetworkX→igraph转换开销 | 中 | 低 | 增量模式下只转换受影响子图 |
| igraph与NetworkX内存双倍 | 中 | 中 | 大图时考虑直接用igraph替代NetworkX |
| 激活度衰减参数不适应用户节奏 | 中 | 中 | 参数可配置，提供调试工具 |
| 脑区重组导致激活状态错位 | 低 | 低 | 重组时映射旧脑区到新脑区 |
| 上下文注入token超预算 | 中 | 低 | 严格截断，优先注入高激活脑区 |
| 时间链断裂导致事件顺序错乱 | 高 | 中 | Dream Evolver提取时强制建立followed_by链；Session节点兜底 |
| 情景记忆与语义记忆互相干扰 | 中 | 低 | 分离关系类型：followed_by/corrected_by仅用于事件，associated_with用于知识 |
| Dream Evolver提取过多L0垃圾记忆 | 中 | 中 | L0高衰减率(0.05/天)自然清理；连接数作为隐含重要性信号 |
| Fork与上游LightRAG版本冲突 | 中 | 低 | 仅2处×2行改动，冲突极易解决；上游合并后可移除fork |
| 主Agent频繁手动点亮/关闭脑区导致上下文抖动 | 低 | 中 | 手动操作当轮生效，下一轮回归自动衰减模式；不会永久锁定 |
| 脑区全景地图token消耗 | 低 | 低 | 每脑区约15-20 tokens，10个脑区约150-200 tokens，可接受 |

---

## 12. 成功指标

| 指标 | 目标 | 衡量方式 |
|------|------|---------|
| 工作记忆保持 | 闲聊15轮后仍注入正确的工作知识 | 端到端测试场景 |
| 脑区激活精准度 | >80%被激活脑区与当前工作相关 | 人工评估50个样本 |
| 社区检测延迟 | 增量更新<2秒（1000节点图） | 自动化基准测试 |
| 上下文注入延迟 | <500ms | 端到端延迟测量 |
| 上下文token效率 | 脑图注入不超过4000 tokens | 运行时统计 |
| 时间链完整性 | 同一Session内事件100%有序 | 检查followed_by链连续性 |
| L0垃圾清理率 | 30天内90%的L0孤岛记忆被遗忘 | 图统计 |

---

# 实施方案细化（2026-04-24）

> 以下内容将方案文档的业务需求细化为 7 个可独立实施、测试、提交的模块。
> 两个关键修正已纳入：(1) 持续触发的 reinforce 机制；(2) 语义记忆与情景记忆分离。

## 模块总览

| # | 模块 | 依赖 | 新增/修改文件 | 预估行数 |
|---|------|------|-------------|---------|
| M1 | 社区检测引擎 | 无 | `niu_api/internal/region_detector.py` | ~250 |
| M2 | 脑区主节点管理 | M1 | `niu_api/internal/region_manager.py` | ~300 |
| M3 | 激活/衰减管理器 | M2 | `niu_api/internal/region_activation.py` | ~200 |
| M4 | 脑区上下文注入 | M3 | `niu_api/internal/region_injector.py` + 改造 `agent/runner.py` | ~350 |
| M5 | MCP 工具 + API 端点 | M3 | `agent/handler.py` + `niu_api/brain_api.py` | ~200 |
| M6 | Dream Evolver 升级 | M2 | `config/agents/dream-evolver.md` + `agent/injector/dream_writer.py` | ~250 |
| M7 | 定时任务 + 集成测试 | M1-M6 | `niu_api/scheduler.py` 改造 + `tests/` | ~300 |

**实施顺序**：

```
M1 → M2 → M3 → M4 + M5 + M6（可并行）→ M7
```

**前置条件**：Phase -1（LightRAG merge 保留自定义属性）✅ 已通过 monkey-patch 完成。

---

## M1: 社区检测引擎

### 文件

`niu_api/internal/region_detector.py`（新增）

### 依赖安装

```bash
pip install leidenalg python-igraph
```

### 数据结构

```python
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

@dataclass(frozen=True)
class RegionPartition:
    """单个脑区的分区结果"""
    community_id: str           # "community_0", "community_1", ...
    nodes: frozenset[str]       # 脑区内所有实体名
    representative: str         # 度最高的实体（代表节点）
    size: int                   # 实体数量

@dataclass
class CommunityDetectionResult:
    """社区检测结果"""
    regions: Dict[str, RegionPartition]  # community_id → RegionPartition
    total_nodes: int
    total_edges: int
    resolution: float
    is_incremental: bool        # 本次是否增量更新
```

### 核心类

```python
class CommunityDetector:
    """Leiden 社区检测引擎

    从 LightRAG 知识图谱提取图结构 → 运行 Leiden 社区检测 → 返回分区结果。
    支持完整聚类和增量更新两种模式。
    """

    MIN_GRAPH_SIZE = 50         # 节点 < 50 不做检测
    UPDATE_THRESHOLD_PCT = 5    # 图增长超 5% 触发增量更新

    def __init__(self, adapter: LightRAGAdapter): ...

    def detect_communities(
        self,
        resolution: float = 1.0,
    ) -> CommunityDetectionResult:
        """完整 Leiden 聚类

        流程：
        1. 从 LightRAG 获取全图 (get_knowledge_graph("*"))
        2. 转为 NetworkX DiGraph → igraph Graph
        3. 节点 < MIN_GRAPH_SIZE → 返回默认脑区（所有节点归入 community_0）
        4. 运行 leidenalg.find_partition() with resolution_parameter
        5. 为每个社区选度最高节点作为 representative
        6. 返回 CommunityDetectionResult
        """

    def incremental_update(
        self,
        existing_partition: Dict[str, RegionPartition],
        changed_nodes: Set[str],
        resolution: float = 1.0,
    ) -> CommunityDetectionResult:
        """增量更新

        流程：
        1. 从 LightRAG 获取全图
        2. 构建 igraph Graph
        3. 构建 initial_membership 从 existing_partition
        4. 设置 is_membership_fixed：已有节点=True，changed_nodes + 1-2跳邻居=False
        5. 运行 leidenalg.find_partition() with initial_membership + is_membership_fixed
        6. 更新 representative
        7. 返回 CommunityDetectionResult (is_incremental=True)
        """

    def should_run_detection(self, last_node_count: int) -> bool:
        """判断是否需要运行社区检测

        条件（满足任一）：
        - 从未运行过（last_node_count == 0）
        - 当前节点数 ≥ MIN_GRAPH_SIZE 且增长超 UPDATE_THRESHOLD_PCT
        """

    def persist_partition(
        self,
        result: CommunityDetectionResult,
        ingester: LightRAGIngester,
    ) -> None:
        """持久化社区归属到节点属性

        对每个节点写入 brain_meta_community_id 属性。
        依赖 Phase -1 的 merge 保留自定义属性。
        """
```

### igraph ↔ NetworkX 转换

```python
def _nx_to_igraph(nx_graph: nx.DiGraph) -> ig.Graph:
    """NetworkX DiGraph → igraph Graph（忽略方向，社区检测用无向图）"""
    return ig.Graph.from_networkx(nx_graph)

def _lightrag_to_nx(kg_result: Dict) -> nx.DiGraph:
    """LightRAG get_knowledge_graph("*") 结果 → NetworkX DiGraph"""
    G = nx.DiGraph()
    for node in kg_result.get("nodes", []):
        G.add_node(node.id, **node.properties)
    for edge in kg_result.get("edges", []):
        G.add_edge(edge.source, edge.target, **edge.properties)
    return G
```

### 冷启动策略

节点 < 50 时，所有实体归入 `community_0`（默认脑区），不做 Leiden 检测。此时脑区激活退化为现有的向量检索行为，零风险。

### 测试

```python
class TestCommunityDetector:
    def test_cold_start_under_50_nodes(self): ...    # → 默认脑区
    def test_first_detection_over_50_nodes(self): ... # → Leiden 分区
    def test_incremental_update(self): ...            # → 冻结+解冻+增量
    def test_should_run_detection(self): ...          # → 阈值判断
    def test_persist_partition(self): ...             # → brain_meta_community_id 写入
```

---

## M2: 脑区主节点管理

### 文件

`niu_api/internal/region_manager.py`（新增）

### 数据结构

```python
@dataclass(frozen=True)
class BrainRegionInfo:
    """脑区主节点的信息"""
    name: str                   # "brain:region:编程开发"
    label: str                  # "编程开发"
    community_id: str           # "community_3"
    description: str            # LLM 生成的摘要
    size: int                   # 脑区内实体数
    representative: str         # 度最高的实体
    members: List[str]          # 脑区内所有实体名
    updated_at: float           # 最后更新时间戳
```

### 核心类

```python
class RegionManager:
    """脑区主节点生命周期管理

    为每个 Leiden 社区创建 brain:region:{name} 主节点，
    承担三重角色：语义指针、搜索入口、元数据容器。
    """

    def __init__(
        self,
        adapter: LightRAGAdapter,
        ingester: LightRAGIngester,
    ): ...

    def create_region_nodes(
        self,
        partition: CommunityDetectionResult,
    ) -> List[str]:
        """为每个社区创建主节点 + 关系

        对每个社区：
        1. 收集社区内实体的名称+描述（跳过 brain:region:* 自身）
        2. LLM 生成脑区摘要 + 名称（调用 _summarize_region）
        3. inject_entity(brain:region:{name}, BrainRegion, summary)
           + brain_meta_region_id, brain_meta_size, brain_meta_representative,
             brain_meta_updated_at 扁平化属性
        4. inject_relation(brain:Niu → brain:region:{name},
           keywords="brain_region_anchor", weight=1.0)
        5. 对每个成员: inject_relation(brain:region:{name} → entity,
           keywords="belongs_to", weight=0.8)

        返回创建的 region 名称列表
        """

    def update_region_summaries(
        self,
        region_names: List[str],
    ) -> None:
        """重新生成指定脑区的摘要（成员变化后）

        1. 获取脑区当前成员（沿 belongs_to 关系）
        2. LLM 重新生成摘要
        3. 更新主节点 description（inject_entity 覆盖）
        """

    def get_all_regions(self) -> List[BrainRegionInfo]:
        """从 LightRAG 查询所有 entity_type=BrainRegion 的实体

        使用 list_entities(entity_type="BrainRegion")
        """

    def get_region_members(self, region_name: str) -> List[str]:
        """从 brain:region:{name} 出发，沿 belongs_to 关系获取成员

        使用 explore_node(region_name, depth=1) 然后过滤 belongs_to 边
        """

    def cleanup_stale_regions(
        self,
        current_partition: CommunityDetectionResult,
    ) -> List[str]:
        """清理不再存在的脑区主节点

        对比 current_partition 中的 community_id 与图中的 BrainRegion 实体，
        删除已不存在的脑区主节点及其 belongs_to 关系。
        """

    def _summarize_region(
        self,
        entity_summaries: List[str],
    ) -> tuple[str, str]:
        """LLM 生成脑区摘要和名称

        Args:
            entity_summaries: ["Python(skill): Python编程语言...", ...]

        Returns:
            (region_name, region_summary)
            例: ("编程开发", "Python编程(专家级)、NumPy数据处理、Web开发技术栈")

        使用 LightRAG 的 llm_model_instance 直接调用，避免额外 LLM 客户端。
        """
```

### 主节点数据结构（写入 LightRAG）

```python
{
    "entity_name": "brain:region:编程开发",
    "entity_type": "BrainRegion",
    "description": "Python编程(专家级)、NumPy数据处理、Web开发技术栈、数据分析方法、AI/ML项目经验",
    "source_id": "brain",
    "file_path": "brain://region",
    "brain_meta_region_id": "community_3",
    "brain_meta_size": "6",
    "brain_meta_representative": "Python",
    "brain_meta_updated_at": "1745366400",
}
```

### 测试

```python
class TestRegionManager:
    def test_create_region_nodes(self): ...          # 主节点+关系创建
    def test_update_region_summaries(self): ...      # 摘要更新
    def test_get_all_regions(self): ...              # 查询所有脑区
    def test_get_region_members(self): ...           # 获取成员
    def test_cleanup_stale_regions(self): ...        # 清理过期脑区
```

---

## M3: 激活/衰减管理器

### 文件

`niu_api/internal/region_activation.py`（新增）

### 数据结构

```python
@dataclass
class BrainRegionState:
    """脑区激活状态（会话级，不持久化到图）"""
    region_id: str              # community_id
    label: str                  # 脑区名称
    activation: float           # 当前激活度 0.0-1.0
    last_activated_at: float    # 上次激活时间戳
    activation_count: int       # 本会话被激活次数
    manually_dimmed: bool       # 本轮被手动关闭（当轮不被自动重新点亮）
```

### 核心类

```python
class RegionActivationManager:
    """会话级脑区激活/衰减管理

    核心机制：
    - 激活：查询命中脑区实体 → activation=1.0（拉满）
    - Reinforce：脑区内工具被调用 → activation=max(当前, 0.85)（保持高位）
    - 衰减：每轮 activation *= 0.92
    - 手动控制：activate/dim，手动 dim 当轮内不被自动重新点亮
    - 连带激活：被激活脑区的邻居脑区获得 0.3 × activation
    """

    def __init__(
        self,
        decay_factor: float = 0.92,
        activation_threshold: float = 0.3,
        spillover_factor: float = 0.3,
        tool_reinforce_value: float = 0.85,
    ): ...

    def initialize_from_regions(
        self,
        regions: List[BrainRegionInfo],
    ) -> None:
        """从 RegionManager 获取的脑区列表初始化激活状态

        所有脑区初始 activation=0.0
        """

    def activate_regions(
        self,
        hit_entities: List[str],
        entity_to_region: Dict[str, str],  # entity_name → region_id
    ) -> Set[str]:
        """根据查询命中的实体，激活所属脑区

        规则：
        - 查找 hit_entities 所属脑区
        - 跳过 manually_dimmed=True 的脑区
        - activation = 1.0（拉满，不是累加）
        - 连带激活：被激活脑区的邻居脑区获得 spillover_factor × activation
        返回被激活的 region_id 集合
        """

    def reinforce_by_tool_use(
        self,
        tool_name: str,
        tool_to_region: Dict[str, str],  # tool_name → region_id
    ) -> Optional[str]:
        """脑区内工具被实际调用时 reinforce

        规则：
        - 查找 tool_name 所属脑区
        - activation = max(当前值, tool_reinforce_value)
        - 跳过 manually_dimmed=True 的脑区
        返回被 reinforce 的 region_id，或 None
        """

    def manual_activate(self, region_labels: List[str]) -> None:
        """brain_region_activate 工具调用

        activation = 1.0
        manually_dimmed = False（取消手动关闭标记）
        """

    def manual_dim(self, region_labels: List[str]) -> None:
        """brain_region_dim 工具调用

        activation = 0.0
        manually_dimmed = True（当轮内不被自动重新点亮）
        """

    def decay_all(self) -> None:
        """每轮对话后衰减所有脑区

        规则：
        - activation *= decay_factor
        - 清除 manually_dimmed 标记（下一轮回归自动模式）
        """

    def get_active_regions(self) -> List[BrainRegionState]:
        """获取 activation > activation_threshold 的脑区，按激活度降序"""

    def get_region_map(self) -> List[BrainRegionState]:
        """获取所有脑区状态（含熄灭的），用于全景地图注入"""

    def get_status_light(self, activation: float) -> str:
        """三状态灯
        > 0.7 → 🟢 (点亮)
        > 0.1 → 🟡 (即将熄灭)
        else  → ⚫ (熄灭)
        """
```

### Reinforce 稳态分析

持续使用某脑区工具时：

```
轮次  事件           activation
 1    工具调用       0.85
 2    衰减           0.85 × 0.92 = 0.78
 3    工具调用       max(0.78, 0.85) = 0.85
 4    衰减           0.85 × 0.92 = 0.78
 ...  稳态在 0.78-0.85 之间振荡，远高于阈值 0.3
```

完全没人理时：

```
轮次  activation
 1    1.00
 5    0.66
10    0.43
15    0.28  ← 仍超阈值，仍在注入
20    0.19  ← 低于阈值，停止注入
30    0.08  ← 很暗
```

### 测试

```python
class TestRegionActivation:
    def test_activate_regions(self): ...              # activation=1.0
    def test_reinforce_by_tool_use(self): ...         # activation=max(当前, 0.85)
    def test_reinforce_steady_state(self): ...        # 持续调用→稳态0.78-0.85
    def test_decay_curve(self): ...                   # 0.92^n 衰减曲线
    def test_spillover_activation(self): ...          # 连带激活
    def test_manual_dim_blocks_auto(self): ...        # 手动关闭当轮不被自动重新点亮
    def test_decay_clears_manually_dimmed(self): ...  # decay_all 清除标记
    def test_get_status_light(self): ...              # 三状态灯
```

---

## M4: 脑区上下文注入

### 文件

- `niu_api/internal/region_injector.py`（新增）
- `agent/runner.py`（改造 `_inject_dynamic_resources()`）

### 核心类

```python
class BrainContextInjector:
    """脑区激活度加权的上下文注入

    按激活度分层注入脑区知识到系统提示词：
    - 全景地图（始终注入，~150-200 tokens）
    - 高激活(>0.7)：详细知识（实体+关系+文档片段）
    - 中激活(0.3-0.7)：摘要
    - 低激活(<0.3)：不注入详细内容
    """

    CONTEXT_BUDGET = {
        "total": 4000,
        "high_activation": 2000,
        "mid_activation": 1200,
        "low_activation": 400,
        "skills": 400,
    }

    def __init__(
        self,
        adapter: LightRAGAdapter,
        activation_mgr: RegionActivationManager,
        region_mgr: RegionManager,
    ): ...

    def inject_brain_context(
        self,
        query_context: str,
    ) -> str:
        """基于脑区激活度的上下文注入（主入口）

        流程：
        1. 用 query_context 做 LightRAG 查询 → 提取命中实体
        2. activation_mgr.activate_regions(hit_entities)
        3. 如果命中 brain:region:* 主节点 → 二次 local 查询展开脑区内部知识
        4. activation_mgr.decay_all()
        5. 按激活度分层格式化注入内容
        返回注入文本
        """

    def format_region_map(
        self,
        regions: List[BrainRegionState],
    ) -> str:
        """脑区全景地图（始终注入）

        格式：
        ## 脑区状态 (6个脑区)
        🟢 编程开发 — Python/NumPy/Web技术栈，你擅长编程 (6实体)
        🟢 项目管理 — AI_Bot项目，你是主开发者 (4实体)
        🟡 日常偏好 — 你偏好暗色主题，远程办公 (3实体)
        ⚫ 财务知识 — 报销流程、预算审批 (2实体)
        """

    def format_detailed_region(
        self,
        region: BrainRegionState,
        members: List[Dict],
        budget: int,
    ) -> str:
        """高激活(>0.7)：注入实体+关系+关联文档片段

        格式：
        ### [编程开发] (活跃)
        实体: Python(expert), NumPy, Data_Analysis, Web_Development
        关系:
        - 你擅长Python(expert级别)，从2019年开始用于AI/ML
        - Python与NumPy通过数据科学生态关联
        知识: [相关文档片段，最多3条]

        严格控制在 budget tokens 内，超出时截断低优先级内容。
        """

    def format_summary_region(
        self,
        region: BrainRegionState,
    ) -> str:
        """中激活(0.3-0.7)：注入摘要

        格式：
        ### [项目管理] (近期)
        你在参与AI_Bot项目，是主开发者。项目使用Python/Web技术栈。
        """

    def apply_activation_weight(
        self,
        query_results: List[Dict],
        boost_factor: float = 0.3,
    ) -> List[Dict]:
        """激活度加权查询

        被激活脑区的实体在搜索结果中获得额外得分：
        final_score = lightrag_score + region.activation × boost_factor

        使得激活脑区的实体在检索结果中排名更高。
        """
```

### runner.py 改造

`_inject_dynamic_resources()` 内的改造：

```python
# 改造前（现有流程）：
#   1. LightRAGAdapter.search_multi_lightrag() → skills + mcp_tools + knowledge
#   2. _search_tool_signal_skills_lightrag() → tool-signal skills
#   3. interaction_habits + brain memories
#   4. 格式化注入

# 改造后：
#   0. brain_injector.inject_brain_context(query_context)
#      → 激活脑区 + 获取分层注入内容
#   1. LightRAGAdapter.search_multi_lightrag() → skills + mcp_tools + knowledge
#   2. brain_injector.apply_activation_weight(results)
#      → 激活度加权排序
#   3. _search_tool_signal_skills_lightrag() → tool-signal skills
#   4. interaction_habits + brain memories
#   5. 组装注入文本：
#      a. 脑区全景地图（始终注入）
#      b. 分层知识注入（高/中激活脑区）
#      c. skills + mcp_tools + knowledge（加权排序后）
#      d. interaction_habits + brain memories
```

**关键**：步骤 0 在步骤 1 之前执行，确保脑区先被激活，后续检索才能受益于激活度加权。

### 测试

```python
class TestBrainContextInjector:
    def test_format_region_map(self): ...              # 全景地图格式
    def test_format_detailed_region(self): ...         # 高激活详细注入
    def test_format_summary_region(self): ...          # 中激活摘要注入
    def test_apply_activation_weight(self): ...        # 激活度加权
    def test_context_budget_control(self): ...         # 预算截断
    def test_region_master_hit_triggers_expansion(self): ...  # 主节点命中→展开
```

---

## M5: MCP 工具 + API 端点

### 文件

- `agent/handler.py`（新增 3 个工具处理函数）
- `niu_api/brain_api.py`（新增，API 路由）

### 新增 MCP 工具

#### `brain_region_activate` — 主动点亮脑区

```python
TOOL_SCHEMA = {
    "name": "brain_region_activate",
    "description": "主动点亮一个或多个脑区，使其知识立即注入上下文。当你判断接下来的工作需要某个领域的知识时使用。",
    "parameters": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要点亮的脑区名称列表，如 ['编程开发', '项目管理']"
            },
            "reason": {
                "type": "string",
                "description": "为什么要点亮这些脑区（用于记忆记录）"
            }
        },
        "required": ["regions"]
    }
}
```

**处理函数**：调用 `activation_mgr.manual_activate(regions)` → 返回激活后的脑区状态。

#### `brain_region_dim` — 主动关闭脑区

```python
TOOL_SCHEMA = {
    "name": "brain_region_dim",
    "description": "主动关闭一个或多个脑区，停止注入其详细知识。当你确认某领域知识不再需要时使用，可节省上下文空间。",
    "parameters": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关闭的脑区名称列表"
            },
            "reason": {
                "type": "string",
                "description": "为什么要关闭这些脑区（可选）"
            }
        },
        "required": ["regions"]
    }
}
```

**处理函数**：调用 `activation_mgr.manual_dim(regions)` → 返回关闭后的脑区状态。

#### `brain_region_status` — 查看脑区状态

```python
TOOL_SCHEMA = {
    "name": "brain_region_status",
    "description": "Show current brain region activation states. Shows which regions are lit up and their activation levels.",
    "parameters": {
        "type": "object",
        "properties": {
            "include_dark": {
                "type": "boolean",
                "description": "Include regions below activation threshold. Default: false"
            }
        }
    }
}
```

**处理函数**：调用 `activation_mgr.get_region_map()` → 格式化返回脑区状态列表。

### 升级现有工具

#### `brain_recall` — 增加 region_aware 模式

```python
# 新增参数：
"mode": {
    "type": "string",
    "enum": ["hybrid", "region_aware"],
    "description": "Recall mode. 'region_aware' uses brain region activation for weighted retrieval. Default: region_aware"
},
"activate_only": {
    "type": "boolean",
    "description": "If true, only activate regions without returning results. Useful for pre-loading context. Default: false"
}
```

### API 端点

```python
# niu_api/brain_api.py

GET /api/brain/regions
    → 返回所有脑区及激活状态
    → 调用 region_mgr.get_all_regions() + activation_mgr.get_region_map()

POST /api/brain/regions/consolidate
    → 手动触发社区检测
    → 调用 detector.detect_communities() + region_mgr.create_region_nodes()

GET /api/brain/regions/{name}/members
    → 获取脑区成员实体
    → 调用 region_mgr.get_region_members(name)
```

### 工具调用时的 reinforce 集成

在 `handler.py` 的 `dispatch()` 方法中，工具被调用后：

```python
# 现有：tool_lifecycle.hit_tool(tool_name)
# 新增：activation_mgr.reinforce_by_tool_use(tool_name, tool_to_region)
```

这样工具被调用时既更新 ToolLifecycle 分数，又 reinforce 对应脑区。

### 测试

```python
class TestBrainRegionTools:
    def test_brain_region_activate(self): ...    # 手动点亮
    def test_brain_region_dim(self): ...         # 手动关闭
    def test_brain_region_status(self): ...      # 查看状态
    def test_brain_recall_region_aware(self): ... # region_aware 模式
    def test_tool_dispatch_reinforces_region(self): ...  # 工具调用→reinforce
```

---

## M6: Dream Evolver 升级

### 文件

- `config/agents/dream-evolver.md`（重写提示词）
- `agent/injector/dream_writer.py`（新增，脑图写入层）

### 核心变更：语义记忆与情景记忆分离

**两条独立流水线**：

```
流水线A：语义记忆（知识类）
  输入信号：偏好、技能、概念、工具关系
  写入格式：实体 + associated_with/USED_FOR/OFTEN_WITH 关系
  特征：按权重建边，无时序，联想式检索
  示例：Python ──USED_FOR──→ 数据分析

流水线B：情景记忆（事件类）
  输入信号：错误/成功经验、决策过程、任务流程
  写入格式：brain:event 实体 + followed_by/corrected_by 时间链
  特征：按时间链建边，有序，时序式检索
  示例：做PPT ──followed_by──→ 选模板A ──corrected_by──→ 选模板B
```

### DreamWriter 类

```python
class DreamWriter:
    """Dream Evolver 的脑图写入层

    封装 LightRAG 写入操作，提供语义记忆和情景记忆两种写入接口。
    """

    def __init__(self, ingester: LightRAGIngester): ...

    # ============== 流水线A：语义记忆 ==============

    def write_semantic_entity(
        self,
        name: str,
        entity_type: str,       # Person, Concept, Skill, ...
        description: str,
        level: str = "L0",      # L0/L1/L2
    ) -> Dict:
        """写入语义实体（知识类）

        创建实体 + brain:Niu → 实体关系（prefers/skilled_in/remembers）
        不需要时间链。
        """

    def write_semantic_relation(
        self,
        src: str,
        tgt: str,
        relation_type: str,     # USED_FOR, OFTEN_WITH, associated_with
        description: str = "",
    ) -> Dict:
        """写入语义关系（知识类）

        直接调用 inject_relation。
        """

    # ============== 流水线B：情景记忆 ==============

    def write_episodic_event(
        self,
        event_name: str,
        description: str,
        experience_type: str,   # "error" | "success"
        level: str = "L1",
        related_entities: Optional[List[str]] = None,
        prev_event_name: Optional[str] = None,
        is_correction: bool = False,
        session_id: Optional[str] = None,
    ) -> Dict:
        """写入情景事件（事件类）

        流程：
        1. 创建 brain:event:{event_name} 实体
           entity_type: Event
           brain_meta_experience_type: experience_type
           brain_meta_origin_level: level
           brain_meta_session_id: session_id
        2. brain:Niu → brain:event 关系
           keywords: "{experience_type}_experience,{关键词}"
           weight: LEVEL_DEFAULTS[level]["weight"]
        3. brain:event → related_entities 关系
           keywords: "associated_with"
           weight: 0.5
        4. 时间链（关键！不能断）：
           - 有 prev_event_name:
             prev_event → brain:event 关系
             keywords: "corrected_by" (is_correction) 或 "followed_by"
           - 无 prev_event_name:
             挂在 Session 节点上（brain:session:{session_id} → brain:event, keywords="contains"）
        """

    def get_last_event_in_session(
        self,
        session_id: str,
    ) -> Optional[str]:
        """获取 Session 内最后一个事件名（用于建立时间链）

        从 brain:session:{session_id} 出发，沿 contains 关系找到所有事件，
        按 brain_meta_created_at 排序，返回最后一个。
        """

    # ============== 画像更新 ==============

    def update_profile(
        self,
        preference: Optional[str] = None,
        skill: Optional[str] = None,
    ) -> Dict:
        """更新 brain:Niu 的画像

        preference → brain:Niu ──prefers──→ 偏好实体
        skill → brain:Niu ──skilled_in──→ 技能实体
        累积式：不覆盖已有信息
        """
```

### Dream Evolver 提示词重写

`config/agents/dream-evolver.md` 核心结构：

```markdown
# 核心原则

1. **先分类，再处理** — 每条消息先判断属于语义记忆还是情景记忆
2. **连接优先** — 每条记忆必须与已有实体建立关系，孤岛记忆是无用的
3. **时间链不可断** — 事件之间必须用 followed_by/corrected_by 串联
4. **宁多勿少** — 不确定重要性时就记，遗忘曲线会自然筛选

# 第一阶段：记忆类型识别

对每条新消息，判断属于哪类：
- 提到偏好/技能/概念/工具/人物 → **语义记忆**（走流水线A）
- 提到事件/决策/经验/流程/纠正 → **情景记忆**（走流水线B）
- 两者都有 → 分别处理

# 第二阶段A：语义记忆写入

- 创建/更新实体（entity_type: Person/Concept/Skill/...）
- 建立实体间关系（associated_with, USED_FOR, OFTEN_WITH）
- 不需要时间链
- 分级：闲聊提及→L0, 用户明确→L1, 强调"记住"→L2

# 第二阶段B：情景记忆写入

- 创建 brain:event 实体（entity_type: Event）
- 必须找到前一个事件，建立 followed_by/corrected_by 链
- 链不能断——找不到前事件时挂在 Session 节点上
- 错误经验: keywords首词=error_experience
- 成功经验: keywords首词=success_experience
- 分级：闲聊提及→L0, 用户明确→L1, 关键经验→L1(weight=0.8), 强调"记住"→L2

# 第三阶段：画像更新

- 新偏好 → brain:Niu --prefers--> 实体
- 新技能 → brain:Niu --skilled_in--> 实体
- 累积式更新，不覆盖已有信息
```

### 旧6项 → 新3项映射

| 旧工作项 | → | 新工作项 | 流水线 |
|---------|---|---------|--------|
| 1.错误经验 | → | **A.经验提取** | 流水线B（情景记忆） |
| 2.成功经验 | ↗ | | |
| 3.工具方言 | → | **B.关系构建** | 流水线A（语义记忆） |
| 6.KG实体/关系 | ↗ | | |
| 4.用户状态 | → | **C.画像更新** | 流水线A（语义记忆） |
| 5.用户画像 | ↗ | | |

### 测试

```python
class TestDreamWriter:
    def test_write_semantic_entity(self): ...          # 语义实体写入
    def test_write_semantic_relation(self): ...        # 语义关系写入
    def test_write_episodic_event_with_chain(self): ... # 情景事件+时间链
    def test_write_episodic_event_correction(self): ... # 纠正事件(corrected_by)
    def test_write_episodic_event_no_prev(self): ...   # 无前事件→挂Session
    def test_get_last_event_in_session(self): ...      # 获取最后事件
    def test_update_profile(self): ...                 # 画像更新
```

---

## M7: 定时任务 + 集成测试

### 定时任务扩展

在现有定时任务基础上增加：

| 任务 | 周期 | 说明 |
|------|------|------|
| `brain_region_update` | 每日02:00 | 增量更新社区归属 + 刷新主节点摘要 |

**实现**：

```python
async def task_brain_region_update():
    """脑区更新定时任务

    流程：
    1. detector.should_run_detection() 判断是否需要运行
    2. 如果需要：detector.incremental_update() 或 detect_communities()
    3. region_mgr.create_region_nodes() 创建/更新主节点
    4. region_mgr.cleanup_stale_regions() 清理过期脑区
    5. region_mgr.update_region_summaries() 刷新摘要
    """
```

### 集成测试

```python
# tests/test_brain_region_integration.py

class TestFullBrainRegionFlow:
    """完整流程集成测试"""

    def test_insert_detect_activate_inject(self):
        """插入 → 聚类 → 激活 → 注入"""

    def test_activate_decay_reactivate(self):
        """激活 → 衰减 → 再激活（reinforce）"""

    def test_manual_control_overrides_auto(self):
        """手动控制优先级高于自动"""

    def test_tool_use_reinforces_region(self):
        """工具调用 reinforce 脑区"""

    def test_spillover_activation(self):
        """连带激活"""

    def test_dream_writer_semantic_vs_episodic(self):
        """语义记忆与情景记忆分离写入"""

    def test_dream_writer_time_chain_integrity(self):
        """时间链完整性"""

    def test_context_budget_not_exceeded(self):
        """上下文预算不超限"""
```

### 配置

在 `~/.niu/preferences.json` 的 `brain_graph` 配置中增加：

```json
{
  "brain_graph": {
    "region": {
      "enabled": true,
      "algorithm": "leiden",
      "resolution": 1.0,
      "min_graph_size": 50,
      "incremental_update": true,
      "neighbor_unfreeze_depth": 2,

      "decay_factor": 0.92,
      "activation_boost": 1.0,
      "activation_threshold": 0.3,
      "tool_reinforce_value": 0.85,
      "spillover_factor": 0.3,

      "context_budget_tokens": 4000,
      "high_activation_budget": 2000,
      "mid_activation_budget": 1200,
      "skills_budget": 400,

      "query_boost_factor": 0.3,
      "update_threshold_pct": 5
    }
  }
}
```

---

## 实施检查清单

每个模块完成后的验收标准：

### M1 完成标准
- [ ] `leidenalg` + `igraph` 安装成功
- [ ] `CommunityDetector` 实现完整聚类 + 增量更新
- [ ] 冷启动(<50节点)返回默认脑区
- [ ] 分区结果可持久化到节点属性
- [ ] 5 个单元测试通过

### M2 完成标准
- [ ] `RegionManager` 可创建/更新/查询/清理脑区主节点
- [ ] 主节点 description 由 LLM 生成摘要
- [ ] belongs_to 关系正确建立
- [ ] brain_meta_* 扁平化属性正确写入
- [ ] 5 个单元测试通过

### M3 完成标准
- [ ] `RegionActivationManager` 实现激活/reinforce/衰减/手动控制
- [ ] Reinforce 稳态在 0.78-0.85 之间
- [ ] 手动 dim 当轮不被自动重新点亮
- [ ] decay_all 清除 manually_dimmed 标记
- [ ] 8 个单元测试通过

### M4 完成标准
- [ ] `BrainContextInjector` 实现分层注入
- [ ] 脑区全景地图始终注入
- [ ] 激活度加权查询正确排序
- [ ] 上下文预算不超限
- [ ] runner.py 改造后现有功能不受影响
- [ ] 6 个单元测试通过

### M5 完成标准
- [ ] 3 个新 MCP 工具可被主 Agent 调用
- [ ] brain_recall 支持 region_aware 模式
- [ ] 工具调用时自动 reinforce 对应脑区
- [ ] 3 个 API 端点可访问
- [ ] 5 个单元测试通过

### M6 完成标准
- [ ] Dream Evolver 提示词重写完成（6项→3项，双流水线）
- [ ] `DreamWriter` 实现语义记忆 + 情景记忆两种写入
- [ ] 时间链不断裂（followed_by/corrected_by）
- [ ] 无前事件时挂在 Session 节点
- [ ] 7 个单元测试通过

### M7 完成标准
- [ ] `brain_region_update` 定时任务注册并运行
- [ ] 8 个集成测试通过
- [ ] 完整流程端到端验证通过
- [ ] 配置项可从 preferences.json 读取
