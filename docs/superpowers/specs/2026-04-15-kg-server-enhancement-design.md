# kg-server 增强设计：置信度 + 图遍历 + 意外关联

> 版本：v1.0
> 日期：2026-04-15
> 状态：详细设计完成，待实施
> 前置文档：`personal-assistant-architecture-v2.md`、`feature-contacts.md`

---

## 一、设计目标

借鉴 Graphify 的图分析能力，为 kg-server 增强三个核心功能：

| 功能 | 价值 | 优先级 |
|------|------|--------|
| **置信度机制** | 区分用户创建 vs Agent 推断的关系，让数据可信度可见 | P1 |
| **图遍历工具** | 降低 Agent 使用知识图谱的门槛，无需手写 Cypher | P1 |
| **意外关联发现** | 发现"用户不知道自己知道"的隐藏关系 | P1 |

**核心理念**：知识图谱从"被动存储"变为"主动洞察引擎"。

---

## 二、现有架构

### 2.1 数据模型

**节点类型**（KuzuDB）：

| 节点 | 主键 | 属性 |
|------|------|------|
| Document | uri | title, content, source, created_at |
| Entity | id | name, type, description |
| Concept | name | description |

**关系类型**：

| 关系 | 起点 → 终点 | 属性 |
|------|------------|------|
| MENTIONS | Document → Entity | 无 |
| CONTAINS | Document → Concept | 无 |
| RELATED_TO | Entity → Entity | relation |

**现状问题**：
- Entity 和 Concept 缺少 `created_at`/`updated_at`
- 所有关系都没有置信度字段
- 缺少图分析工具（hub_entities、surprising_connections）
- Agent 使用图谱需要手写 Cypher，门槛高

### 2.2 人物图谱集成

**两个数据源**（见 `feature-contacts.md`）：

| 数据源 | 表 | 创建方式 |
|--------|-----|----------|
| 照片识别 | `persons` | 人脸识别自动创建 |
| 通讯录导入 | `contacts` | vCard/CSV 导入 |

**Contact 在图谱中的角色**（见架构 v2 第 599-611 行）：

| 关系 | 起点 → 终点 | 强度计算 |
|------|------------|----------|
| APPEARS_IN | Contact → Photo | 人脸置信度 |
| MENTIONED_IN | Contact → Document | 出现次数 |
| CO_OCCURS_WITH | Contact → Contact | 共现频率 × 权重 |
| WORKS_AT | Contact → Organization | 文档证据 |
| KNOWS | Contact → Contact | 用户确认 |

**核心优势**：我们的人物图谱和知识图谱已融合，比 Graphify 的纯代码图谱更丰富。

---

## 三、Schema 改造

### 3.1 KuzuDB 不支持 ALTER TABLE 加列

KuzuDB 的限制：无法给已存在的表添加列。

**解决方案**：清空重建（用户确认可接受）。

### 3.2 新增字段

#### 节点表改造

**Entity 表**：

```sql
CREATE NODE TABLE IF NOT EXISTS Entity (
    id STRING,
    name STRING,
    type STRING,
    description STRING,
    created_at STRING,        -- 新增：创建时间
    updated_at STRING,        -- 新增：更新时间
    PRIMARY KEY (id)
)
```

**Concept 表**：

```sql
CREATE NODE TABLE IF NOT EXISTS Concept (
    name STRING,
    description STRING,
    created_at STRING,        -- 新增：创建时间
    updated_at STRING,        -- 新增：更新时间
    PRIMARY KEY (name)
)
```

#### 关系表改造

**MENTIONS 关系**：

```sql
CREATE REL TABLE IF NOT EXISTS MENTIONS (
    FROM Document TO Entity,
    confidence FLOAT,         -- 新增：置信度 (0.0-1.0)
    created_at STRING         -- 新增：创建时间
)
```

**CONTAINS 关系**：

```sql
CREATE REL TABLE IF NOT EXISTS CONTAINS (
    FROM Document TO Concept,
    confidence FLOAT,         -- 新增：置信度 (0.0-1.0)
    created_at STRING         -- 新增：创建时间
)
```

**RELATED_TO 关系**：

```sql
CREATE REL TABLE IF NOT EXISTS RELATED_TO (
    FROM Entity TO Entity,
    relation STRING,
    confidence FLOAT,         -- 新增：置信度 (0.0-1.0)
    created_at STRING         -- 新增：创建时间
)
```

### 3.3 置信度约定

| 置信度 | 来源 | 说明 |
|--------|------|------|
| 1.0 | 用户手动创建 | 最可信，不可质疑 |
| 0.7-0.9 | LLM 从文档提取 | 高置信度提取 |
| 0.4-0.6 | Agent 推断 | 基于上下文推断 |
| 0.1-0.3 | 自动聚类/共现 | 算法发现的关系 |

**默认值**：现有工具创建的关系默认 `confidence = 1.0`（向后兼容）。

**时间戳格式**：ISO 8601 字符串（`2026-04-15T10:30:00Z`）。

---

## 四、新增工具设计

### 4.1 工具清单

| 工具 | 用途 | 参数 |
|------|------|------|
| `explore_node` | 从实体/概念出发探索邻居 | entity_id, depth, min_confidence |
| `find_path` | 两实体间最短路径 | from_id, to_id, max_depth |
| `hub_entities` | 最核心的实体（度中心度排序） | top_n, entity_type |
| `surprising_connections` | 意外关联发现 | top_n, min_co_occurrence |
| `graph_stats` | 图统计概览 | 无 |
| `graph_changelog` | 变更日志 | since_date |

### 4.2 工具详细设计

#### explore_node

**用途**：从指定节点出发，返回 N 层邻居和边（替代 BFS/DFS）。

**参数**：

```json
{
  "entity_id": "person_zhang_san",
  "depth": 2,
  "min_confidence": 0.5,
  "direction": "both"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| entity_id | string | 必填 | 实体 ID 或名称（支持模糊匹配） |
| depth | integer | 2 | 遍历深度（1-5） |
| min_confidence | float | 0.0 | 最小置信度过滤 |
| direction | string | both | 方向：both/outgoing/incoming |

**返回**：

```json
{
  "center": {"id": "person_zhang_san", "name": "张三", "type": "人物"},
  "nodes": [
    {"id": "doc_001", "name": "合同A", "type": "Document", "distance": 1},
    {"id": "person_li_si", "name": "李四", "type": "人物", "distance": 1}
  ],
  "edges": [
    {"source": "person_zhang_san", "target": "doc_001", "relation": "MENTIONED_IN", "confidence": 0.9},
    {"source": "person_zhang_san", "target": "person_li_si", "relation": "CO_OCCURS_WITH", "confidence": 0.3}
  ],
  "stats": {"nodes": 2, "edges": 2, "max_depth": 1}
}
```

**实现**（Cypher）：

```cypher
-- 1 层邻居
MATCH (center {id: $entity_id})-[r]-(neighbor)
WHERE r.confidence >= $min_confidence
RETURN neighbor, r

-- 2 层邻居
MATCH (center {id: $entity_id})-[r1]-(n1)-[r2]-(n2)
WHERE r1.confidence >= $min_confidence AND r2.confidence >= $min_confidence
RETURN n1, r1, n2, r2
```

---

#### find_path

**用途**：查找两个实体之间的最短路径。

**参数**：

```json
{
  "from_id": "person_zhang_san",
  "to_id": "person_wang_wu",
  "max_depth": 5
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| from_id | string | 必填 | 起点实体 ID |
| to_id | string | 必填 | 终点实体 ID |
| max_depth | integer | 5 | 最大跳数（1-10） |

**返回**：

```json
{
  "found": true,
  "hops": 3,
  "path": [
    {"id": "person_zhang_san", "name": "张三"},
    {"id": "doc_contract", "name": "合同A", "relation": "MENTIONED_IN", "confidence": 0.9},
    {"id": "org_abc", "name": "ABC公司", "relation": "CONTAINS", "confidence": 0.8},
    {"id": "person_wang_wu", "name": "王五", "relation": "WORKS_AT", "confidence": 1.0}
  ]
}
```

**实现**（Cypher）：

```cypher
MATCH path = SHORTESTPATH(
  (a {id: $from_id})-[*1..$max_depth]-(b {id: $to_id})
)
RETURN path
```

---

#### hub_entities

**用途**：返回最核心的实体（按度中心度排序）。

**参数**：

```json
{
  "top_n": 10,
  "entity_type": "人物"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| top_n | integer | 10 | 返回数量 |
| entity_type | string | null | 实体类型过滤（可选） |

**返回**：

```json
{
  "entities": [
    {"id": "person_zhang_san", "name": "张三", "type": "人物", "degree": 15, "pagerank": 0.12},
    {"id": "org_abc", "name": "ABC公司", "type": "组织", "degree": 12, "pagerank": 0.09}
  ]
}
```

**实现**：

1. **度中心度**（Cypher）：

```cypher
MATCH (e:Entity)
WHERE $entity_type IS NULL OR e.type = $entity_type
MATCH (e)-[r]-()
RETURN e.id, e.name, e.type, count(r) AS degree
ORDER BY degree DESC
LIMIT $top_n
```

2. **PageRank**（Python 层计算）：
   - 从 KuzuDB 导出子图到 NetworkX
   - 运行 `nx.pagerank()`
   - 写回结果（可选：持久化到节点属性）

---

#### surprising_connections

**用途**：发现"用户不知道自己知道"的隐藏关系。

**两阶段架构**：

**阶段 1 — 算法筛选候选**（kg-server 内部）：
- 查找共现 ≥ N 次但没有直接 RELATED_TO 的实体对
- 查找跨实体类型的关系（人物↔技术概念）
- 按评分排序，返回 top_n 候选

**阶段 2 — LLM 语义判断**（由调用方/子 Agent 负责）：
- kg-server 只返回候选列表
- 调用方自行决定是否让 LLM 判断

**参数**：

```json
{
  "top_n": 5,
  "min_co_occurrence": 2,
  "types": ["人物", "组织"]
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| top_n | integer | 5 | 返回数量 |
| min_co_occurrence | integer | 2 | 最小共现次数 |
| types | array | null | 跨类型过滤（如只返回人物↔组织） |

**返回**：

```json
{
  "candidates": [
    {
      "entity_a": {"id": "person_zhang_san", "name": "张三", "type": "人物"},
      "entity_b": {"id": "person_li_si", "name": "李四", "type": "人物"},
      "co_occurrence_count": 5,
      "shared_documents": ["doc_001", "doc_002", "doc_003"],
      "score": 0.85,
      "reason": "共同出现在 5 篇文档中，但没有直接关系边"
    },
    {
      "entity_a": {"id": "person_zhang_san", "name": "张三", "type": "人物"},
      "entity_b": {"id": "concept_ai", "name": "人工智能", "type": "技术概念"},
      "co_occurrence_count": 3,
      "shared_documents": ["doc_004", "doc_005"],
      "score": 0.72,
      "reason": "跨类型关联：人物与技术概念频繁共现"
    }
  ]
}
```

**评分公式**：

```
score = (
  co_occurrence_count * 0.4 +           -- 共现次数权重
  type_diversity_bonus * 0.3 +          -- 跨类型加成
  recency_factor * 0.3                   -- 时间衰减
)

其中：
- type_diversity_bonus: 
  - 人物↔人物 = 0.5
  - 人物↔组织 = 1.0
  - 人物↔技术概念 = 1.5
  
- recency_factor:
  - 最近 1 个月 = 1.0
  - 1-6 个月 = 0.8
  - 6-12 个月 = 0.6
  - 1 年以上 = 0.4
```

**实现**（Cypher）：

```cypher
-- 查找共现实体对
MATCH (d:Document)-[:MENTIONS]->(e1:Entity)
MATCH (d)-[:MENTIONS]->(e2:Entity)
WHERE e1.id < e2.id  -- 避免重复
WITH e1, e2, count(d) AS co_occurrence, collect(d.uri) AS shared_docs
WHERE co_occurrence >= $min_co_occurrence
-- 排除已有直接关系
AND NOT EXISTS {
  MATCH (e1)-[:RELATED_TO]-(e2)
}
RETURN e1, e2, co_occurrence, shared_docs
ORDER BY co_occurrence DESC
LIMIT $top_n
```

---

#### graph_stats

**用途**：一站式图统计。

**参数**：无

**返回**：

```json
{
  "nodes": {
    "total": 150,
    "by_type": {"人物": 45, "组织": 20, "技术概念": 85}
  },
  "edges": {
    "total": 320,
    "by_relation": {"MENTIONS": 200, "RELATED_TO": 120},
    "by_confidence": {
      "high (0.7-1.0)": 180,
      "medium (0.4-0.7)": 100,
      "low (0.0-0.4)": 40
    }
  },
  "density": 0.028,
  "connected_components": 3
}
```

**实现**（Cypher）：

```cypher
-- 节点统计
MATCH (e:Entity)
RETURN e.type, count(e)

-- 边统计
MATCH ()-[r]->()
RETURN type(r), count(r), avg(r.confidence)

-- 连通分量（需要 Python 层 NetworkX）
```

---

#### graph_changelog

**用途**：返回指定时间后的所有变更。

**参数**：

```json
{
  "since_date": "2026-04-08"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| since_date | string | 7 天前 | ISO 日期字符串 |

**返回**：

```json
{
  "new_entities": [
    {"id": "person_new", "name": "新联系人", "created_at": "2026-04-10"}
  ],
  "new_relations": [
    {"from": "person_a", "to": "doc_001", "relation": "MENTIONS", "confidence": 0.8, "created_at": "2026-04-12"}
  ],
  "summary": "新增 5 个实体，12 条关系"
}
```

**实现**（Cypher）：

```cypher
-- 新增实体
MATCH (e:Entity)
WHERE e.created_at >= $since_date
RETURN e

-- 新增关系
MATCH ()-[r]->()
WHERE r.created_at >= $since_date
RETURN r
```

---

### 4.3 Cypher 安全限制

**现状**：`query_graph` 工具允许任意 Cypher 查询，存在风险。

**改造**：

1. **只允许只读操作**：
   - 允许：`MATCH`、`RETURN`、`WITH`、`WHERE`、`ORDER BY`、`LIMIT`
   - 禁止：`CREATE`、`DELETE`、`SET`、`REMOVE`、`MERGE`、`DROP`

2. **实现**：
   - 使用正则表达式检查 Cypher 语句
   - 或在 KuzuDB 连接层面设置只读模式（如果支持）

---

## 五、实施路线

### 5.1 Phase 1：Schema 改造 + 置信度（1-2 天）

**任务**：
1. 修改 `_init_schema()` 函数，添加新字段
2. 修改 `create_entity`、`create_concept`，添加时间戳
3. 修改 `link_document_entity`、`link_document_concept`、`link_entities`，添加 `confidence` 和 `created_at` 参数
4. 添加 `_infer_confidence()` 辅助函数，根据调用来源推断置信度

**代码改动**：
- `mcp-servers/kg-server/src/niu_kg_server/__init__.py`
- 约 200 行修改

---

### 5.2 Phase 2：图遍历工具（1 天）

**任务**：
1. 实现 `explore_node` 工具
2. 实现 `find_path` 工具
3. 添加 Cypher 安全检查函数

**代码改动**：
- 新增 3 个工具 schema
- 新增 3 个工具实现函数
- 约 150 行新增

---

### 5.3 Phase 3：图分析工具（2-3 天）

**任务**：
1. 实现 `graph_stats` 工具（最简单）
2. 实现 `hub_entities` 工具（度中心度）
3. 实现 `surprising_connections` 工具（共现分析 + 评分）
4. 实现 `graph_changelog` 工具（时间戳查询）

**代码改动**：
- 新增 4 个工具 schema
- 新增 4 个工具实现函数
- 约 300 行新增

---

## 六、数据迁移方案

### 6.1 清空重建流程

**步骤**：

1. **备份现有数据**（可选）：
   ```bash
   cp ~/.niu/kg.db ~/.niu/kg.db.backup
   ```

2. **删除旧数据库**：
   ```python
   # kg-server 启动时检测到 schema 不匹配
   # 自动删除旧库，创建新库
   ```

3. **重新入库**：
   - 重新运行文件解析流程
   - 或手动导入关键数据

### 6.2 向后兼容

**现有工具的行为**：
- `create_entity`：`created_at` 和 `updated_at` 自动填充当前时间
- `link_*`：`confidence` 默认值 1.0，`created_at` 自动填充当前时间

**调用方无需修改**：新参数都是可选的，默认值保证兼容性。

---

## 七、与人物图谱的集成

### 7.1 Contact 节点的置信度

**来源分类**：

| 来源 | 置信度 | 说明 |
|------|--------|------|
| vCard 导入 | 1.0 | 用户主动导入 |
| 照片人脸关联 | 0.7-0.9 | 基于人脸匹配置信度 |
| Agent 推断 | 0.4-0.6 | 从文档提取人名并匹配 |

### 7.2 CO_OCCURS_WITH 关系的置信度

**计算公式**：

```
confidence = min(0.3, co_occurrence_count * 0.1)
```

- 1 次共现 = 0.1
- 2 次共现 = 0.2
- 3 次及以上 = 0.3（上限）

**原因**：共现关系是算法推断的，不应与用户手动创建的关系（1.0）混淆。

---

## 八、测试计划

### 8.1 单元测试

| 测试项 | 说明 |
|--------|------|
| Schema 初始化 | 验证新字段存在 |
| 置信度赋值 | 验证默认值和自定义值 |
| 时间戳自动填充 | 验证 created_at/updated_at |
| Cypher 安全检查 | 验证禁止 CREATE/DELETE |
| explore_node | 验证深度和置信度过滤 |
| find_path | 验证最短路径查找 |
| surprising_connections | 验证共现计数和评分 |

### 8.2 集成测试

**场景 1：置信度过滤**

```python
# 创建高置信度关系
link_document_entity(doc_uri, entity_id, confidence=0.9)

# 创建低置信度关系
link_document_entity(doc_uri, entity_id, confidence=0.3)

# 查询时过滤
result = explore_node(entity_id, min_confidence=0.5)
# 应只返回高置信度邻居
```

**场景 2：意外关联发现**

```python
# 张三和李四都出现在 3 篇文档中
# 但没有直接 RELATED_TO 边

candidates = surprising_connections(min_co_occurrence=2)
# 应返回张三-李四候选
```

---

## 九、参考资料

### Graphify 架构

- **流水线**：`detect() → extract() → build_graph() → cluster() → analyze() → report() → export()`
- **三级置信度**：EXTRACTED/INFERRED/AMBIGUOUS
- **图分析**：god_nodes、surprising_connections、suggest_questions、graph_diff
- **可视化**：HTML 交互图谱（D3.js）

### 我们的优势

| 维度 | Graphify | 我们 |
|------|----------|------|
| 数据类型 | 纯代码实体 | 人物 + 文档 + 照片 + 概念 |
| 图谱类型 | 代码关系图 | 人脉 + 知识融合图谱 |
| 关系丰富度 | 单一维度 | 多维度（共现、语义、时间） |
| LLM 参与 | 无（纯算法） | 可选（算法筛选 + LLM 判断） |

---

## 十、后续扩展

### 10.1 P2 功能

| 功能 | 价值 | 难度 |
|------|------|------|
| 社区发现 | 自动识别知识领域 | 中（需引入 igraph） |
| 可视化 | 让用户"看到"知识网络 | 高（需前端开发） |
| suggest_questions | 基于图结构自动生成探索问题 | 中 |

### 10.2 与 memory-server 的联动

**潜在冲突**：
- memory-server 的 L0/L1/L2 与 kg-server 的文档/实体/概念有重叠
- 需要明确数据边界

**建议**：
- memory-server：记忆层（L0 原文 + L1 摘要 + L2 向量）
- kg-server：关系层（实体 + 概念 + 关系）
- 两者互补，不重复存储

---

*文档结束*
