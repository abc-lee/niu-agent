# 聊天记录 LightRAG 增量注入 + 时间线查询设计

> 日期：2026-04-27
> 状态：待审批
> 依赖：06-brain-region-activation.md（脑区激活架构）

## 1. Executive Summary

**核心问题**：entity-extractor 子 Agent 与 LightRAG 的实体提取功能存在表面重叠，但本质不同 — LightRAG 是"全量入库"引擎，没有判断内容价值的能力；entity-extractor 是"筛选提炼"器，只提取值得记住的内容。此外，系统缺失时间线查询能力和遗忘曲线定时任务。

**解决方案**：
1. **保留 entity-extractor**，重新定位为"内容提炼器"：从消息中筛选有价值内容（偏好、技能、关键经验），形成精炼文档提交给 LightRAG
2. **升级 dream-evolver**，新增 skill 维护功能：分析消息内容，判断是否需要建立新 skill 或修改旧 skill
3. entity-extractor 提炼的精炼文档按时间段增量分段注入 LightRAG，每段独立 doc_id
4. 新增时间线查询工具：先向量匹配内容，再沿时间链排序返回
5. 增强图遍历工具支持边类型过滤，避免时间链边干扰语义查询
6. 补齐遗忘曲线定时任务（衰减、巩固、清理）
7. 缺省创建"聊天历史"脑区主节点

---

## 2. 当前状态分析

### 2.1 entity-extractor 与 LightRAG 的关系（非重叠，互补）

**关键认知**：LightRAG 是"全量入库"引擎，没有判断内容价值的能力。
把原始聊天记录扔给 LightRAG，它会把所有内容都提取实体和关系，
不管是有价值的偏好还是无意义的闲聊。

entity-extractor 的核心价值是**筛选提炼**：
- "用户是否透露了偏好、期望等信息？" — 只提取值得记住的
- "是否使用了需要反复试错的方法？" — 只提取有复用价值的技能经验
- 筛选后形成精炼文档，提交给 LightRAG 入库

| 能力 | LightRAG `ainsert()` | entity-extractor 子 Agent |
|------|---------------------|--------------------------|
| 内容价值判断 | **无** — 全量入库 | **有** — 只提炼有价值内容 |
| 自动实体提取 | 内置 LLM 提取 | 不做（交给 LightRAG） |
| 自动关系提取 | 内置 LLM 提取 | 不做（交给 LightRAG） |
| brain_meta 标签 | 不支持 | 支持 (description 前缀) |
| 时间链关系 | 不支持 | 支持 (followed_by/corrected_by) |
| 脑区关联 | 不支持 | 支持 |
| 精炼文档生成 | 不支持 | 支持 — 输出高质量摘要 |

**结论**：entity-extractor 与 LightRAG **不重叠，互补**。
entity-extractor 负责"筛选提炼"，LightRAG 负责"语义入库"。

### 2.2 entity-extractor 与 dream-evolver 的分工

| 子 Agent | 核心职责 | 输出 |
|----------|---------|------|
| entity-extractor | 从消息中**提炼**有价值内容 | 精炼文档 → LightRAG ainsert |
| dream-evolver | **精加工** + skill 维护 | brain_meta/时间链/脑区 + skill patch/create |

**不重叠**：
- entity-extractor 做"提炼"（筛选有价值内容，形成精炼文档）
- dream-evolver 做"精加工"（LightRAG 做不到的精确控制）和"skill 维护"
- 经验提取由 entity-extractor 独占，dream-evolver 不再做

### 2.3 entity-extractor 的调用方

| 调用方 | 触发方式 | 说明 |
|--------|---------|------|
| 主 Agent `chat-with-entity-extractor` | 用户主动 | niu.md 注册了此 sub agent |
| 定时任务（每日8点） | 自动 | `__main__.py` 注册的 cron 任务 |
| KGScanner | 已禁用 | 不再生效 |

**注意**：强制压缩调用的是 dream-evolver，不是 entity-extractor。不存在"强制压缩和定时任务冲突"的问题。

### 2.4 时间线查询能力缺口

**当前完全缺失**。LightRAG 的 6 种查询模式都是语义检索：
- 无时间过滤参数
- 无"沿时间链遍历"能力
- 实体有 `created_at` 但查询不返回、不可过滤
- `followed_by`/`corrected_by` 链在写入端有部分实现，读取端无法沿链遍历

### 2.5 遗忘曲线实现状态

| 组件 | 状态 | 文件 |
|------|------|------|
| L0/L1/L2 分级定义 | 已实现 | `brain_graph.py` |
| 脑区激活衰减 (0.92/轮) | 已实现 | `region_activation.py` |
| 工具调用 reinforce | 已实现 | `brain_tools.py` |
| 边权重衰减 | 已实现 | `region_manager.py` `_decay_structural_edges()` |
| 脑区合并/巩固 | 已实现 | `brain_region_api.py` |
| **Ebbinghaus 遗忘曲线定时衰减** | **未实现** | 无定时任务 |
| **L0→L1→L2 巩固** | **未实现** | 无定时任务 |
| **低权重清理** | **未实现** | 无定时任务 |

---

## 3. 设计方案

### 3.1 重新定位 entity-extractor（内容提炼器）

**保留 entity-extractor**，重新定位其核心职责为"从消息中提炼有价值内容"。

**升级提示词**（参考提示词1）：

```markdown
## 核心任务

回顾上方对话，筛选出有价值的内容：

### 记忆提炼
用户是否透露了偏好、期望等信息？
- 偏好：如"我喜欢暗色主题" → 提炼为精炼摘要
- 期望：如"我希望报告自动生成" → 提炼为精炼摘要
- 身份：如"我是数据分析师" → 提炼为精炼摘要

### 技能提炼
是否使用了需要反复试错、或根据实际发现调整思路的非简易方法？
- 成功经验：如"用 X 方法解决了 Y 问题" → 提炼为精炼摘要
- 失败教训：如"Z 方法不适用于 W 场景" → 提炼为精炼摘要
- 工具发现：如"发现 A 工具有 B 能力" → 提炼为精炼摘要

### 输出格式
将提炼结果格式化为精炼文档，提交给 LightRAG 入库：
- 每条提炼内容一行，包含：类型标签 + 时间戳 + 精炼摘要
- 无价值内容不输出（闲聊、确认、简单问答等跳过）
```

**输出示例**：

```
[记忆提炼 2026-04-27]

## 14:23:15 偏好
用户偏好 Rust 语言，对所有权机制感兴趣

## 15:01:08 计划
用户明天要去上海出差

## 16:33:02 技能
换用新解析库处理PDF，效果优于旧库；旧库在大型PDF上有内存泄漏问题
```

**关键变化**：
- 旧 entity-extractor：逐条提取实体和关系，手动调用 `lightrag_insert_entity`/`lightrag_insert_relation`
- 新 entity-extractor：提炼有价值内容形成精炼文档，调用 `lightrag_insert` 整体入库
- LightRAG 对精炼文档做 ainsert，自动提取实体和关系，建立语义连接
- 精炼文档质量远高于原始聊天记录，LightRAG 的提取效果更好

### 3.2 升级 dream-evolver（新增 skill 维护）

**新增职责**：分析消息内容，判断是否需要建立新 skill 或修改旧 skill。

**可用工具**：dream-evolver 注册了 `lightrag-server` + `session-manager`，同时拥有系统内置工具 `file_read`、`file_patch`、`file_write`。

**新增提示词**（参考提示词2）：

```markdown
## Skill 维护

当使用一项技能并发现它过时、不完整或错误时，立即用 file_patch
对其进行修补——不要等着被问到。不维护的技能会成为负担。

### 判断规则
- 工具使用失败且找到了替代方案 → file_patch 修改旧 skill
- 发现 skill 描述不完整（缺少参数、边界条件） → file_patch 补充
- 发现 skill 已过时（API 变更、方法废弃） → file_patch 更新
- 新的工作模式反复出现但无对应 skill → file_write 创建新 skill

### 创建新 skill 的流程
1. 先用 file_read 读取 memory/skills/Write-SKILL.md，了解创建规范
2. 按照 Write-SKILL.md 的 RED-GREEN-REFACTOR 流程创建
3. 新 skill 文件存放在 memory/skills/ 目录下
4. 命名使用动词优先、连字符分隔（如 note-management.md）

### 修改旧 skill 的流程
1. 用 file_read 读取目标 skill 文件
2. 用 file_patch(path, old_content, new_content) 局部修改
3. old_content 必须在文件中唯一匹配（含空白/缩进）
```

**dream-evolver 的完整职责**：

| 职责 | 工具 | 说明 |
|------|------|------|
| **Skill 维护**（新增） | file_read + file_patch/file_write | 修改旧 skill 或创建新 skill |
| 精确控制 | lightrag_insert_entity/relation | brain_meta 标签、时间链、脑区关联（entity-extractor 做不到的） |
| 画像更新 | lightrag_insert_entity | 更新 brain:Niu 的偏好和技能 |

**不做的事**（由 entity-extractor 负责）：
- ~~经验提取~~ — entity-extractor 已提炼精炼文档给 LightRAG
- ~~关系构建~~ — LightRAG ainsert 自动提取

### 3.3 精炼文档增量分段注入

**核心原则**：entity-extractor 提炼的精炼文档按时间段增量注入，不删除重注入。

**数据流**：

```
message.db → entity-extractor(筛选提炼) → 精炼文档 → LightRAG ainsert(语义入库)
message.db → dream-evolver(skill维护) → skill patch/create
```

**分段策略**：

精炼文档按时间段分段，每段独立 doc_id：

```
refined:2026-04-27:001  (14:23-15:00 的精炼内容)
refined:2026-04-27:002  (15:01-16:30 的精炼内容)
refined:2026-04-27:003  (16:31-18:00 的精炼内容)
```

**分段规则**：
- 每段最多包含 N 条提炼内容（默认 20 条，可配置）
- 或每段最多 M 分钟的连续对话（默认 60 分钟，可配置）
- 段序号从 001 开始递增
- doc_id 格式：`refined:{date}:{seq:03d}`（注意前缀是 `refined:` 不是 `messages:`）

**增量逻辑**：
- 启动时查询 LightRAG 已有的当天段号（`refined:2026-04-27:*`）
- 找到最大段号 N
- 只处理第 N+1 段之后的消息，提炼后注入新段
- LLM 调用次数只与新增内容成正比

**流程**：

```
1. entity-extractor 从 message.db 读取新消息
2. 筛选提炼有价值内容（偏好、技能、经验）
3. 格式化为精炼文档：

   [记忆提炼 2026-04-27 段3]

   ## 16:31:08 技能
   换用新解析库处理PDF，效果优于旧库；旧库在大型PDF上有内存泄漏问题

   ## 16:33:02 偏好
   用户偏好用 Rust 处理性能敏感任务

4. 调用 lightrag_insert(content=精炼文档, doc_id="refined:2026-04-27:003")
5. LightRAG 自动提取实体和关系，建立语义连接
6. dream-evolver 对精炼内容做精加工：
   - 给关键实体打 brain_meta 标签
   - 建立时间链
   - 关联到 brain:region:聊天历史 脑区
   - 判断是否需要 skill patch/create
```

**与强制压缩的关系**：
- 强制压缩调用 dream-evolver，不调用 entity-extractor
- dream-evolver 在强制压缩时做全量精加工（brain_meta、时间链、skill 维护等）
- entity-extractor 的定时任务只做"提炼 + ainsert"
- 两者不冲突：entity-extractor 负责"提炼+粗入库"，dream-evolver 负责"精加工+skill维护"

**实现文件**：
- `config/agents/entity-extractor.md`（修改）— 重写提示词，从"实体提取"改为"内容提炼"
- `niu_api/internal/message_injector.py`（新增）— 精炼文档增量分段注入逻辑
- `niu_api/internal/scheduler/service.py`（修改）— 注册新的定时任务

### 3.3 时间线查询

**核心原则**：先向量匹配内容，再沿时间链排序返回。

**查询流程**：

```
用户查询 "我之前怎么处理PDF的？"
    ↓
1. 向量匹配 → 找到最相关的实体/事件
   lightrag_query_data(query="PDF处理", mode="local")
   → 返回: "PDF处理错误", "改用新方案", "Skill:PDF_Tool"
    ↓
2. 从匹配到的实体出发，沿时间链遍历
   lightrag_timeline_query(start_entities=["PDF处理错误"])
   → 沿 followed_by/corrected_by 链找到前后事件
    ↓
3. 按时间戳排序返回（最近的排最前，远期的排后面）
   → 结果:
     [2026-04-27] 改用新方案处理PDF (最近)
     [2026-04-26] PDF处理错误，用了旧工具 (前天)
     [2026-04-20] 第一次尝试处理PDF (更早)
```

**新增工具：`lightrag_timeline_query`**

```python
TOOL_SCHEMA = {
    "name": "lightrag_timeline_query",
    "description": "时间线查询：先向量匹配内容，再沿时间链排序返回。用于回忆事件序列、决策过程、问题解决历史。返回结果按时间由近到远排序。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询内容（先向量匹配，再沿时间链展开）"
            },
            "start_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：直接指定起始实体名（跳过向量匹配步骤）"
            },
            "direction": {
                "type": "string",
                "enum": ["backward", "forward", "both"],
                "description": "遍历方向。backward=由近到远（默认），forward=由远到近，both=双向"
            },
            "max_depth": {
                "type": "integer",
                "description": "时间链遍历深度（默认5）"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量上限（默认10）"
            }
        },
        "required": ["query"]
    }
}
```

**实现逻辑**：

```python
def timeline_query(query, start_entities=None, direction="backward",
                   max_depth=5, max_results=10):
    """
    Step 1: 向量匹配（如果未提供 start_entities）
    - 调用 lightrag_query_data(query, mode="local")
    - 从结果中提取实体名作为 start_entities

    Step 2: 沿时间链遍历
    - 对每个 start_entity，调用 lightrag_get_graph(explore, entity_name, depth=1)
    - 过滤出 followed_by/corrected_by/led_to/resolved_by 类型的边
    - 沿这些边继续遍历（递归，max_depth 控制）

    Step 3: 按时间排序返回
    - 收集所有遍历到的实体
    - 按 created_at 或 brain_meta_created_at 排序
    - direction=backward 时按时间降序（最近的排最前）
    - direction=forward 时按时间升序（最早的排最前）
    - 截断到 max_results
    """
```

**MCP 工具注册（虚拟磁盘模式）**：

所有 MCP 工具的 visibility 为 `hidden`，主 Agent 通过 `disk()` 虚拟磁盘间接调用。
新增工具需要修改 **4 个文件**：

| 序号 | 文件 | 修改内容 |
|------|------|----------|
| 1 | `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | TOOL_SCHEMAS 新增 `lightrag_timeline_query` schema；实现工具函数；注册到 `_TOOL_FUNCTIONS` |
| 2 | `config/mcp-servers.yaml` | `lightrag-server.tools` 下新增 `lightrag_timeline_query: {visibility: hidden}` |
| 3 | `config/disk/lightrag-server.yaml` | 新增 `lightrag_timeline_query` 的目录映射（name, short, long, parameters） |
| 4 | `niu_api/internal/lightrag_adapter.py` | 新增 `timeline_query()` 方法实现查询逻辑 |

**lightrag_get_graph edge_types 参数同样需要修改 4 个文件**：
1. `__init__.py` — TOOL_SCHEMAS 中 `lightrag_get_graph` 的 input_schema 新增 `edge_types` 参数
2. `mcp-servers.yaml` — 无需修改（工具名不变）
3. `disk/lightrag-server.yaml` — `lightrag_get_graph` 的 parameters 新增 `edge_types`
4. `lightrag_adapter.py` — `explore_node()` 方法新增 edge_types 过滤逻辑

### 3.4 图遍历边类型过滤

**增强现有工具：`lightrag_get_graph` explore 模式**

新增参数 `edge_types`：

```python
# 新增参数
"edge_types": {
    "type": "array",
    "items": {"type": "string"},
    "description": "过滤的边类型列表。可选值: followed_by, corrected_by, led_to, resolved_by, associated_with, USED_FOR, OFTEN_WITH, belongs_to, brain_region_anchor, _session:contains, _region:contains。不指定则返回所有边。"
}
```

**实现逻辑**：在 `lightrag_adapter.py` 的 `explore_node()` 方法中，遍历结果后按 `edge_types` 过滤边。过滤依据是边的 `keywords` 字段（关系类型编码在 keywords 中，见 06-brain-region-activation.md 第9.4节）。

**作用**：
- 语义查询时：`edge_types=["associated_with", "USED_FOR", "OFTEN_WITH"]` — 只看语义边
- 时间线查询时：`edge_types=["followed_by", "corrected_by", "led_to", "resolved_by"]` — 只看时间链边
- 脑区查询时：`edge_types=["belongs_to", "brain_region_anchor"]` — 只看脑区边

### 3.5 缺省脑区主节点

在 LightRAG 初始化时（或首次脑区检测时），自动创建三个缺省脑区：

```
brain:Niu ──brain_region_anchor──→ brain:region:聊天历史
                                        │
                                    belongs_to
                                        │
                                        ├── brain:session:2026-04-26
                                        ├── brain:session:2026-04-27
                                        └── 所有聊天事件实体

brain:Niu ──brain_region_anchor──→ brain:region:文档库
                                        │
                                    belongs_to
                                        │
                                        └── 所有文档实体

brain:Niu ──brain_region_anchor──→ brain:region:知识体系
                                        │
                                    belongs_to
                                        │
                                        └── 所有概念/技能实体
```

**聊天事件的默认连接**：
- 不再"没地方连就连到 brain:Niu"
- 而是默认连到 `brain:region:聊天历史`
- 时间链本身就是连接，不存在"没地方连"的问题
- Session 节点是时间线的锚点，不是兜底

**实现文件**：
- `niu_api/internal/region_manager.py`（修改）— 新增 `create_default_regions()` 方法
- `niu_api/__main__.py`（修改）— 启动时调用 `create_default_regions()`

### 3.6 遗忘曲线定时任务补齐

在 scheduler 中注册以下定时任务：

| 任务 | 周期 | 说明 |
|------|------|------|
| `brain_decay` | 每日03:00 | 关系级权重衰减：所有边的 weight *= (1 - decay_rate × days_since_last_access) |
| `brain_consolidate_l0_to_l1` | 每日04:00 | L0→L1 记忆巩固：access_count ≥ 3 的 L0 记忆升级为 L1 |
| `brain_consolidate_l1_to_l2` | 每周日05:00 | L1→L2 记忆巩固：access_count ≥ 10 的 L1 记忆升级为 L2 |
| `brain_cleanup` | 每周日06:00 | 低权重清理：weight < 0.1 的实体和关系标记为待删除 |

**实现文件**：
- `niu_api/internal/scheduler/service.py`（修改）— 注册定时任务
- `niu_api/internal/brain_graph.py`（修改）— 新增衰减/巩固/清理方法
- `niu_api/internal/lightrag_adapter.py`（修改）— 调用 LightRAG 更新实体/关系属性

---

## 4. 实施计划

### Phase 1: 重新定位 entity-extractor + 精炼文档注入

1. 重写 `config/agents/entity-extractor.md` 提示词：从"实体提取"改为"内容提炼"
2. 实现 `message_injector.py`：精炼文档增量分段注入逻辑
3. 修改 `niu_api/internal/scheduler/service.py`：注册新的定时任务
4. 测试：entity-extractor 提炼 → 精炼文档 → LightRAG ainsert → doc_id 去重

### Phase 2: 时间线查询 + 边类型过滤

1. 在 `lightrag-server/__init__.py` 中新增 `lightrag_timeline_query` 工具（TOOL_SCHEMAS + 函数 + _TOOL_FUNCTIONS）
2. 在 `config/mcp-servers.yaml` 中新增 `lightrag_timeline_query: {visibility: hidden}`
3. 在 `config/disk/lightrag-server.yaml` 中新增 `lightrag_timeline_query` 目录映射
4. 在 `lightrag_adapter.py` 中新增 `timeline_query()` 方法
5. 在 `lightrag-server/__init__.py` 中为 `lightrag_get_graph` 新增 `edge_types` 参数
6. 在 `config/disk/lightrag-server.yaml` 中为 `lightrag_get_graph` 新增 `edge_types` 参数
7. 在 `lightrag_adapter.py` 中 `explore_node()` 新增 edge_types 过滤逻辑
8. 测试：向量匹配 → 时间链遍历 → 排序返回；边类型过滤

### Phase 3: 缺省脑区 + 遗忘曲线定时任务

1. 实现 `create_default_regions()`（聊天历史、文档库、知识体系）
2. 注册遗忘曲线定时任务（衰减、巩固、清理）
3. 测试：衰减曲线、巩固升级、低权重清理

### Phase 4: dream-evolver 升级（精加工 + skill 维护）

1. 重写 dream-evolver 提示词：去掉经验提取（交给 entity-extractor），保留精加工 + 新增 skill 维护
2. 精加工：brain_meta 标签、时间链、脑区关联、画像更新
3. Skill 维护：file_read 读取 Write-SKILL.md 规范 → file_patch 修改旧 skill / file_write 创建新 skill
4. 集成测试：完整流程（entity-extractor 提炼 → LightRAG 入库 → dream-evolver 精加工 + skill 维护）

---

## 5. 风险分析

| 风险 | 影响 | 缓解 |
|------|------|------|
| 按天注入后追加新消息需删除重注入 | 中 | **已修正**：改为增量分段注入，每段独立 doc_id，不删除重注入 |
| 时间链边干扰语义图遍历 | 中 | edge_types 过滤参数 |
| dream-evolver 精加工与 ainsert 粗提取的实体合并 | 中 | 同名实体 upsert 语义，后者覆盖前者 |
| 遗忘曲线衰减过快导致重要记忆丢失 | 高 | L2 decay_rate=0.002/天，极慢衰减；reinforce 机制保活 |
| 缺省脑区在 Leiden 检测后被重组 | 低 | 缺省脑区是冷启动方案，Leiden 检测后自然替代 |
| 新增 MCP 工具未注册到虚拟磁盘 | 高 | 必须同步修改 4 个文件（__init__.py、mcp-servers.yaml、disk YAML、adapter） |

---

## 6. 成功指标

| 指标 | 目标 |
|------|------|
| entity-extractor 重新定位 | 提示词从"实体提取"改为"内容提炼"，输出精炼文档而非原始聊天 |
| 精炼文档增量注入去重 | 每段独立 doc_id，新段只注入新增内容，不重处理已有段 |
| 时间线查询精度 | 向量匹配 + 时间链排序，最近事件排最前 |
| 边类型过滤 | 语义查询不受时间链边干扰 |
| 遗忘曲线运行 | 每日03:00衰减、04:00巩固自动执行 |
| 缺省脑区创建 | 启动时自动创建聊天历史、文档库、知识体系三个脑区 |
| dream-evolver skill 维护 | 能判断是否需要 patch/create skill，不维护的技能成为负担 |