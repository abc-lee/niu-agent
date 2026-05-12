# 脑区功能整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复脑区功能的 8 个结构性问题 + 功能闭环缺失环节，并调整创建/删除阈值，预置常用脑区

**Architecture:** 分 5 个阶段实施，每阶段独立可测试。**阶段 0 为最高优先级的功能闭环修复**

**Tech Stack:** Python, LightRAG, Leiden, NetworkX

---

## 功能闭环评估结果（2026-05-12 Agent 评估）

**问题根源：** 在 `c8a3aea7`（temp: backup before LLM blocking diagnosis）提交中，`region_injector.py` 的激活机制从 LightRAG 向量检索改成了简单关键词匹配。这是诊断 LLM 阻塞时的临时改动，但事件循环死锁问题已不存在，应该恢复。

| 缺失环节 | 严重性 | 代码位置 | 问题说明 |
|----------|--------|----------|---------|
| **激活触发** | 🔴 高 | `region_injector.py:107-114` | 使用关键词匹配而非向量检索，无法语义激活脑区 |
| **加权检索** | 🔴 高 | `runner.py:762-770` | `apply_activation_weight()` 方法存在但从未调用 |
| **衰减时机** | 🟡 中 | `region_injector.py:121` | 在注入内部调用，而非每轮对话结束时 |
| **预置脑区保护** | 🟡 中 | `region_sync.py:336-358` | 合并逻辑未检查 `community_id`，可能误合并预置脑区 |
| **预置脑区成员** | 🟡 中 | `region_manager.py` | 预置脑区创建时无成员，无法被实体激活 |

---

## 核实结果（2026-05-12）

| # | 问题 | 状态 | 核实详情 |
|---|------|------|---------|
| **P0** | 边动力学闭环断裂 | ❌ 仍存在 | `brain_tools.py:387` delta=0.1, max=1.0；`region_manager.py:974` 只处理 `_region:` 前缀 |
| **P1** | spillover 激活不工作 | ❌ 仍存在 | `brain_region_api.py:192` neighbor_map 为空字典 |
| **P1** | brain_region_prompt 旧格式 | ✅ **已修复** | 文件位于 `niu_api/internal/brain_region_prompt.py`，已使用自然语言格式 |
| **P2** | incremental_update 未实现 | ✅ 已修复 | `region_manager.py:882-921` 已有完整实现 |
| **P2** | _summarize_region 是启发式 | ❌ 仍存在 | `region_manager.py:810-876` 未调用 LLM |
| **P2** | leidenalg 未声明依赖 | ❌ 仍存在 | 未在 requirements.txt 中找到 |
| **P3** | 边权重 delta 不一致 | ❌ 仍存在 | 0.1 vs 0.85 |
| **P3** | brain_region_prompt 注入量过大 | ✅ **已修复** | 注入内容已精简 |

**修复进度：** 3/8 已修复，5/8 仍存在

---

## 新增需求

### 需求 1：调整脑区创建/删除阈值

**当前配置：**
- 创建脑区：`min_community_size = 10`（社区成员 ≥ 10 才创建脑区）
- 删除脑区：`shrink_threshold = 3`（成员 < 3 才标记萎缩）

**调整目标：**
- 创建脑区：`min_community_size = 100`（社区成员 ≥ 100 才创建脑区）
- 删除脑区：`shrink_threshold = 100`（成员 < 100 才标记萎缩）

**文件：**
- `niu_api/internal/region_detector.py:102` - detect_communities 参数
- `niu_api/internal/region_manager.py:544` - dissolve_shrunk_regions 参数
- `niu_api/internal/region_manager.py:894` - incremental_update 调用

### 需求 2：预置常用脑区

**当前默认脑区（3 个）：**
```python
DEFAULT_REGIONS = {
    "聊天历史": {"description": "日常对话中提炼的偏好、技能和经验记忆"},
    "文档库": {"description": "用户导入的文档和资料，经解析后入库的知识"},
    "知识体系": {"description": "系统化组织的概念、关系和理论体系"},
}
```

**问题：** Leiden 社区检测生成的脑区名字太奇怪（如 `region_0`, `region_1`），需要预置更多有意义的脑区

**头脑风暴：常用脑区分类**

基于个人知识管理场景，建议预置以下脑区：

| 脑区名称 | 描述 | 预期内容 |
|---------|------|---------|
| 聊天历史 | 日常对话中提炼的偏好、技能和经验记忆 | 用户偏好、技能经验 |
| 文档库 | 用户导入的文档和资料，经解析后入库的知识 | PDF、Word、Markdown |
| 知识体系 | 系统化组织的概念、关系和理论体系 | 概念、理论、方法论 |
| 编程开发 | 编程语言、框架、工具、代码片段 | Python、Rust、API |
| 项目管理 | 项目信息、任务、里程碑、决策记录 | 项目状态、进度 |
| 人物关系 | 人物实体、关系网络、社交图谱 | 家人、朋友、同事 |
| 照片记忆 | 照片内容、人物、地点、事件 | 照片元数据、人物识别 |
| 日程安排 | 日程、提醒、待办事项 | 时间管理、计划 |
| 财务记录 | 账单、收支、投资记录 | 财务数据 |
| 健康档案 | 健康数据、医疗记录、运动数据 | 健康信息 |
| 旅行足迹 | 旅行地点、行程、游记 | 地点、事件 |
| 阅读笔记 | 书籍、文章、阅读摘要 | 阅读内容 |
| 工作记录 | 工作日志、会议纪要、决策 | 工作内容 |

**实施策略：**
1. 保留 3 个核心默认脑区（聊天历史、文档库、知识体系）
2. 新增 11 个扩展默认脑区
3. 扩展脑区按需激活（用户首次相关内容入库时自动创建）

---

## 阶段 0：功能闭环修复（最高优先级）

**背景：** 脑区激活/加权检索的核心功能不工作，必须先修复才能让整个系统运转。

### Task 0.1：恢复向量检索激活机制

**Files:**
- Modify: `niu_api/internal/region_injector.py`

**问题：** `inject_brain_context()` 使用简单关键词匹配，无法语义激活脑区

**原代码（错误）：**
```python
# Step 2: Simple keyword matching to activate regions
hit_entities: list[str] = []
query_lower = query_context.lower()
for entity_name in entity_to_region.keys():
    if entity_name.lower() in query_lower or query_lower in entity_name.lower():
        hit_entities.append(entity_name)
```

**正确代码（恢复到 5192efda 版本）：**
```python
# Step 1: Query LightRAG to find hit entities (vector search)
hit_entities: list[str] = []
region_knowledge: dict[str, str] = {}  # region_label -> knowledge text

try:
    # Program auto-call: keywords=[query_context] skips LLM extraction.
    # The full context as keyword is a deliberate trade-off: it avoids
    # 5-30s LLM latency per turn. Vector search still returns results
    # by semantic similarity; keywords only boost graph-traversal matches.
    query_result = self._adapter.query_data(
        query_context, mode="local", top_k=20, keywords=[query_context]
    )

    if query_result and isinstance(query_result, dict):
        data = query_result.get("data", {})
        if not data:
            data = query_result
        entities = data.get("entities", [])
        hit_entities = [
            e.get("entity_name", e.get("id", ""))
            for e in entities
            if e.get("entity_name") or e.get("id")
        ]
except Exception as e:
    logger.warning("脑区注入查询失败: %s", e)
```

**注意：** 实体名已使用自然语言格式（如"安安"而非"person:{uuid}"），`brain_region_prompt.py` 已修复。代码中直接使用 `entity_name` 即可，无需特殊处理。

**重要：** `region_knowledge` 变量需要从 `query_result` 中提取并传入 `_format_injection_content()`。当前代码返回 `self._format_injection_content({})`，恢复向量检索后应改为：

```python
# 从查询结果中提取脑区知识
if query_result and isinstance(query_result, dict):
    # ... 提取 hit_entities
    # 提取知识片段，按脑区分组
    context_data = query_result.get("data", query_result)
    for entity in context_data.get("entities", []):
        region_label = entity_to_region.get(entity.get("entity_name", ""))
        if region_label and region_label not in region_knowledge:
            region_knowledge[region_label] = entity.get("description", "")

# ...

# Step 4: Format injection content with region_knowledge
return self._format_injection_content(region_knowledge)
```

- [ ] **Step 1：恢复 `inject_brain_context()` 的向量检索逻辑**

将 `region_injector.py:100-125` 的关键词匹配代码替换为上述向量检索代码，并确保 `region_knowledge` 被正确填充和传入。

- [ ] **Step 2：验证修改**

Run: `grep -n "query_data\|Simple keyword" niu_api/internal/region_injector.py`

---

### Task 0.2：集成加权检索机制

**Files:**
- Modify: `agent/runner.py`

**问题：** `apply_activation_weight()` 方法存在但从未调用

**审核发现：** 原方案中 `skills_results` 和 `knowledge_results` 变量不存在，实际变量名是 `lightrag_skills` 和 `lightrag_knowledge`（`runner.py:722,736`）

- [ ] **Step 1：在 `_inject_dynamic_resources()` 中调用加权方法**

修改 `runner.py:761-770`，在第 770 行之后（`injection = "\n".join(parts)` 之前）添加：

```python
# Brain region activation context (uses cached injector)
try:
    _brain_injector = self._get_brain_injector()
    if _brain_injector is not None:
        brain_context = _brain_injector.inject_brain_context(context)
        if brain_context:
            parts.append(f"\n## 脑区激活上下文\n{brain_context}")
            logger.debug(f"Brain context injected: {len(brain_context)} chars")
        
        # NEW: Apply activation weight to LightRAG search results
        # lightrag_skills 和 lightrag_knowledge 是 list[dict]，每个 dict 有 "score" 字段
        # 注意：加权调用应在脑区注入之后、格式化之前
        if lightrag_skills:
            lightrag_skills[:] = _brain_injector.apply_activation_weight(lightrag_skills)
        if lightrag_knowledge:
            lightrag_knowledge[:] = _brain_injector.apply_activation_weight(lightrag_knowledge)
except Exception as e:
    logger.debug(f"BrainContextInjector not available: {e}")
```

**注意：** 使用 `list[:] =` 原地修改，确保后续 `_format_lightrag_entities_for_prompt()` 使用加权后的结果。代码插入位置应在第 770 行之后、第 772 行（`injection = "\n".join(parts)`）之前。

- [ ] **Step 2：验证修改**

Run: `grep -n "apply_activation_weight" agent/runner.py`

---

### Task 0.3：修正衰减时机

**Files:**
- Modify: `niu_api/internal/region_injector.py`
- Modify: `agent/runner.py`

**问题：** `decay_all()` 在 `inject_brain_context()` 内部调用，而非每轮对话结束时

**审核发现：** 需确认 `runner.py` 中 `_on_turn_end()` 的调用时机。该回调应在每轮对话结束时由 `agent_runner_loop()` 调用。

- [ ] **Step 1：验证 `_on_turn_end()` 调用时机**

Run: `grep -n "_on_turn_end\|on_turn_end" agent/runner.py`

Expected: 找到调用点，确认每轮结束时调用

- [ ] **Step 2：从 `inject_brain_context()` 移除 `decay_all()` 调用**

删除 `region_injector.py:121` 的 `self._activation_mgr.decay_all()` 调用。

- [ ] **Step 3：在 `runner.py` 的轮次结束回调中添加衰减**

当前 `_on_turn_end()` 签名（`runner.py:425`）：
```python
def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
```

在方法内部，`self._refresh_user_memories(messages)` 之后添加衰减调用：

```python
def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
    """每轮对话结束后的回调"""
    # ... 现有代码
    
    # 衰减脑区激活度（在 _refresh_user_memories 之后）
    try:
        from agent.brain_tools import get_activation_mgr
        mgr = get_activation_mgr()
        if mgr is not None:
            mgr.decay_all()
    except Exception as e:
        logger.debug(f"Brain region decay failed: {e}")
    
    # ... 返回结果
```

- [ ] **Step 4：验证修改**

Run: `grep -n "decay_all" agent/runner.py niu_api/internal/region_injector.py`

---

### Task 0.4：保护预置脑区不被合并

**Files:**
- Modify: `agent/injector/region_sync.py`

**问题：** `_merge_and_dissolve()` 合并时未检查预置脑区

**审核发现：** 原方案的判断条件有逻辑错误，应简化为只检查 `region_id` 是否为空。

- [ ] **Step 1：在合并逻辑中添加预置脑区保护**

修改 `region_sync.py:336-358`：

```python
for source_id, target_id in candidates:
    source_state = activation_mgr.get_region_state(source_id)
    target_state = activation_mgr.get_region_state(target_id)
    if source_state is None or target_state is None:
        continue

    # 保护预置脑区（community_id/region_id 为空的是预置脑区）
    if not source_state.region_id:
        logger.debug("跳过预置脑区合并: %s", source_state.label)
        continue
    if not target_state.region_id:
        logger.debug("跳过预置脑区作为合并目标: %s", target_state.label)
        continue

    # 合并 KG 节点...
```

- [ ] **Step 2：验证修改**

Run: `grep -n "预置脑区\|not source_state.region_id" agent/injector/region_sync.py`

---

### Task 0.5：预置脑区成员初始化

**Files:**
- Modify: `niu_api/internal/region_manager.py`

**问题：** 预置脑区创建时无成员，无法被实体激活

**解决方案：** 在 `create_default_regions()` 后，需要将已有实体按语义分配到预置脑区。

- [ ] **Step 1：添加 `assign_entities_to_default_regions()` 方法**

```python
def assign_entities_to_default_regions(
    self,
    adapter: Any,
    entity_keywords: dict[str, list[str]] | None = None,
) -> dict:
    """将已有实体分配到预置脑区。

    Args:
        adapter: LightRAGAdapter instance.
        entity_keywords: 实体名 -> 关键词列表的映射（可选，用于精确匹配）

    Returns:
        Dict with assigned counts per region.

    实现逻辑：
    1. 获取所有预置脑区及其描述
    2. 遍历知识图谱中的所有实体
    3. 对每个实体：
       a. 如果提供了 entity_keywords，使用关键词匹配
       b. 否则，使用向量相似度匹配实体描述与脑区描述
    4. 创建 belongs_to 关系（weight=0.5）
    """
    from niu_api.internal.lightrag_manager import get_brain_regions
    
    existing_regions = get_brain_regions()
    if not existing_regions:
        return {"assigned": 0, "regions": 0}
    
    rag = adapter._get_rag()
    if rag is None:
        return {"assigned": 0, "regions": 0}
    
    kg = rag.chunk_entity_relation_graph
    if kg is None:
        return {"assigned": 0, "regions": 0}
    
    # 脑区关键词映射（用于启发式匹配）
    REGION_KEYWORDS = {
        "聊天历史脑区": ["偏好", "习惯", "设置", "配置"],
        "文档库脑区": ["文档", "文件", "PDF", "Word", "Markdown"],
        "知识体系脑区": ["概念", "理论", "方法", "原理"],
        "人际关系脑区": ["人物", "家人", "朋友", "同事", "联系人"],
        "工作事务脑区": ["项目", "任务", "会议", "决策", "工作"],
        "生活事务脑区": ["日程", "健康", "财务", "旅行", "生活"],
    }
    
    assigned_counts: dict[str, int] = {}
    all_relationships: list[dict] = []
    
    # 遍历所有实体节点
    for node_id, node_data in kg._graph.nodes(data=True):
        if not isinstance(node_data, dict):
            continue
        entity_name = node_data.get("entity_name", node_id)
        entity_desc = node_data.get("description", "")
        
        # 跳过脑区节点本身
        if entity_name.endswith("脑区"):
            continue
        
        # 匹配逻辑：关键词匹配 + 描述相似度
        best_region = None
        best_score = 0.0
        
        for region_name, keywords in REGION_KEYWORDS.items():
            if region_name not in existing_regions:
                continue
            
            # 关键词匹配
            score = 0.0
            for kw in keywords:
                if kw in entity_name or kw in entity_desc:
                    score += 1.0
            
            if score > best_score:
                best_score = score
                best_region = region_name
        
        # 如果匹配成功，创建 belongs_to 关系
        if best_region and best_score > 0:
            all_relationships.append({
                "src_id": best_region,
                "tgt_id": entity_name,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{entity_name} 属于 {best_region}",
                "weight": 0.5,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })
            assigned_counts[best_region] = assigned_counts.get(best_region, 0) + 1
    
    # 批量注入关系
    if all_relationships:
        try:
            from niu_api.internal.lightrag_ingester import LightRAGIngester
            ingester = LightRAGIngester()
            ingester.inject_custom_kg(
                entities=[],
                relationships=all_relationships,
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
        except Exception as e:
            logger.warning(f"批量注入实体-脑区关系失败: {e}")
    
    return {"assigned": sum(assigned_counts.values()), "regions": len(assigned_counts)}
```

**说明：** 这个任务可以在阶段 4 完成后执行，用于将现有实体分配到新的预置脑区。

---

## 阶段 1：P0 边动力学闭环修复

### Task 1.1：统一边权重初始值

**Files:**
- Modify: `niu_api/internal/region_manager.py`

**审核发现：** 边权重是在 `region_manager.py` 的 `create_region_nodes()` 方法中设置的，不是在 `inject_custom_kg` 中。

- [ ] **Step 1：修改 `create_region_nodes()` 中的边权重初始值**

修改 `region_manager.py:245-260`：

```python
# Step 4: Collect anchor relation from Niu to region
all_relationships.append({
    "src_id": NIU_ENTITY,
    "tgt_id": region_name,
    "keywords": ANCHOR_RELATION,
    "description": f"Brain region anchor: {region_label}",
    "weight": 0.5,  # 从 1.0 改为 0.5
    "source_id": REGION_SOURCE_ID,
    "file_path": REGION_FILE_PATH,
})

# Step 5: Collect belongs_to relations from region to each member
for member in members:
    all_relationships.append({
        "src_id": region_name,
        "tgt_id": member,
        "keywords": BELONGS_TO_RELATION,
        "description": f"{member} belongs to region {region_label}",
        "weight": 0.5,  # 从 0.8 改为 0.5
        "source_id": REGION_SOURCE_ID,
        "file_path": REGION_FILE_PATH,
    })
```

- [ ] **Step 2：验证修改**

Run: `grep -n "weight.*=.*0.5" niu_api/internal/region_manager.py`

---

### Task 1.2：修改 _decay_structural_edges 处理范围

**Files:**
- Modify: `niu_api/internal/region_manager.py`

- [ ] **Step 1：修改 _decay_structural_edges 函数（第 923-988 行）**

将边衰减逻辑从只处理 `_region:` 前缀改为处理所有脑区相关边：

```python
def _decay_structural_edges(
    self,
    regions: list[BrainRegionInfo],
    decay_factor: float = 0.5,
    threshold: float = 0.1,
) -> int:
    """Decay and disconnect low-weight structural edges.

    处理所有脑区相关边：
    - _region:contains（脑区包含成员）
    - _session:*（会话相关）
    - brain_region_anchor（脑区锚点）
    """
    disconnected = 0
    try:
        from niu_api.internal.lightrag_manager import graph_write_lock

        rag = self._adapter._get_rag()
        if rag is None:
            return 0

        kg = rag.chunk_entity_relation_graph
        if kg is None:
            return 0

        # 脑区相关的边关键词前缀
        REGION_EDGE_PREFIXES = ("_region:", "_session:", "brain_region_")

        with graph_write_lock():
            for region in regions:
                try:
                    neighbors = kg.get_neighbors(region.name)
                except AttributeError:
                    continue

                if not neighbors:
                    continue

                for neighbor_id, edge_data in list(neighbors.items()):
                    if not isinstance(edge_data, dict):
                        continue
                    keywords = edge_data.get("keywords", "")
                    # 处理所有脑区相关边（不限前缀）
                    if any(keywords.startswith(prefix) for prefix in REGION_EDGE_PREFIXES):
                        old_weight = float(edge_data.get("weight", 0.5))
                        new_weight = old_weight * decay_factor
                        if new_weight < threshold:
                            try:
                                kg.remove_edge(region.name, neighbor_id)
                            except Exception:
                                pass
                            disconnected += 1
                        else:
                            edge_data["weight"] = new_weight
    except Exception as e:
        logger.warning("Edge decay failed: %s", e)

    return disconnected
```

- [ ] **Step 2：验证修改**

Run: `grep -n "REGION_EDGE_PREFIXES" niu_api/internal/region_manager.py`

---

### Task 1.3：统一 reinforce delta

**Files:**
- Modify: `agent/brain_tools.py`

**审核发现：** `_session:` 前缀在当前代码中未找到使用痕迹。边关键词前缀主要是 `_region:` 和 `brain_region_`。建议保留 `_session:` 以备将来扩展，但需确认是否实际存在。

- [ ] **Step 0：确认 `_session:` 前缀是否存在**

Run: `grep -rn "_session:" niu_api/ agent/ --include="*.py" | head -20`

Expected: 找到使用位置或确认不存在

- [ ] **Step 1：修改 _reinforce_edge_weight 函数（第 351-395 行）**

```python
# 统一常量定义（文件顶部）
REINFORCE_DELTA = 0.15  # 统一与设计文档对齐（原来是 0.1）
MAX_EDGE_WEIGHT = 2.0   # 允许权重超过 1.0（原来是 1.0）

def _reinforce_edge_weight(region_id: str, delta: float = REINFORCE_DELTA) -> None:
    """Boost weight of structural edges for a brain region node."""
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        rag = adapter._get_rag()
        if rag is None:
            return

        kg = rag.chunk_entity_relation_graph
        if kg is None:
            return

        region_node = kg.get_node(region_id)
        if region_node is None:
            return

        try:
            neighbors = kg.get_neighbors(region_id)
        except AttributeError:
            return
        if not neighbors:
            return

        for neighbor_id, edge_data in list(neighbors.items()):
            if not isinstance(edge_data, dict):
                continue
            keywords = edge_data.get("keywords", "")
            # 处理所有脑区相关边
            if any(keywords.startswith(prefix) for prefix in ("_region:", "_session:", "brain_region_")):
                old_weight = edge_data.get("weight", 0.5)
                new_weight = min(MAX_EDGE_WEIGHT, float(old_weight) + delta)
                if new_weight > float(old_weight):
                    edge_data["weight"] = new_weight
                    logger.debug(
                        "Edge weight reinforced: %s -> %s (%s): %.2f -> %.2f",
                        region_id, neighbor_id, keywords, float(old_weight), new_weight,
                    )
    except Exception as e:
        logger.debug("Edge weight reinforce failed: %s", e)
```

- [ ] **Step 2：修改 reinforce_on_tool_use 调用（第 319 行）**

```python
def reinforce_on_tool_use(tool_name: str, reinforce_delta: float = REINFORCE_DELTA) -> str | None:
    # ... 保持不变，只修改默认参数
```

---

### Task 1.4：协调 RegionActivationManager 的 reinforce 值

**Files:**
- Modify: `niu_api/internal/region_activation.py`

- [ ] **Step 1：修改 tool_reinforce_value 默认值（第 93 行）**

```python
def __init__(
    self,
    decay_factor: float = 0.92,
    activation_threshold: float = 0.3,
    spillover_factor: float = 0.3,
    tool_reinforce_value: float = 0.85,  # 保持不变，与 edge reinforce 不同
) -> None:
```

**说明：** `tool_reinforce_value = 0.85` 是激活值，`REINFORCE_DELTA = 0.15` 是边权重增量，两者语义不同，无需统一。

---

### Task 1.5：现有边迁移脚本

**Files:**
- Create: `scripts/migrate_edge_weights.py`

- [ ] **Step 1：创建迁移脚本**

```python
#!/usr/bin/env python3
"""
迁移现有图谱边权重：将 weight=1.0 改为 weight=0.5

一次性运行脚本，用于修复现有数据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from niu_api.internal.lightrag_adapter import LightRAGAdapter
from niu_api.internal.lightrag_manager import graph_write_lock


def migrate_edge_weights():
    """将所有脑区相关边的 weight 从 1.0 改为 0.5"""
    adapter = LightRAGAdapter()
    rag = adapter._get_rag()
    if rag is None:
        print("LightRAG 不可用")
        return

    kg = rag.chunk_entity_relation_graph
    if kg is None:
        print("知识图谱不可用")
        return

    REGION_EDGE_PREFIXES = ("_region:", "_session:", "brain_region_")

    migrated = 0
    with graph_write_lock():
        # 遍历所有边
        for u, v, data in kg._graph.edges(data=True):
            if not isinstance(data, dict):
                continue
            keywords = data.get("keywords", "")
            if any(keywords.startswith(prefix) for prefix in REGION_EDGE_PREFIXES):
                old_weight = data.get("weight", 1.0)
                if old_weight == 1.0:
                    data["weight"] = 0.5
                    migrated += 1

    print(f"迁移完成：{migrated} 条边权重从 1.0 改为 0.5")


if __name__ == "__main__":
    migrate_edge_weights()
```

- [ ] **Step 2：运行迁移脚本（可选，需用户确认）**

Run: `python scripts/migrate_edge_weights.py`

---

## 阶段 2：P1 spillover + 阈值调整

### Task 2.1：实现 neighbor_map 构建

**Files:**
- Modify: `niu_api/brain_region_api.py`
- Modify: `agent/injector/region_sync.py`
- Create: `niu_api/internal/region_neighbors.py`（可选，用于公共函数）

**审核发现：** 原方案的判断条件错误，`==` 应为 `!=`。同时建议抽取为公共函数避免重复代码。

- [ ] **Step 1：创建公共函数 `build_neighbor_map()`（推荐）**

创建 `niu_api/internal/region_neighbors.py`：

```python
"""Brain region neighbor map construction.

Provides utility to build neighbor relationships between brain regions
based on shared members.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_neighbor_map(
    regions: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Build neighbor map for spillover activation.

    Two regions are neighbors if they share at least one member.

    Args:
        regions: List of region info dicts, each with:
            - community_id: str
            - members: list[str]

    Returns:
        Dict mapping community_id -> set of neighbor community_ids.
    """
    neighbor_map: dict[str, set[str]] = {}

    for region in regions:
        neighbors = set()
        region_id = region.get("community_id", "")
        region_members = set(region.get("members", []))

        if not region_id or not region_members:
            continue

        for other in regions:
            other_id = other.get("community_id", "")
            other_members = set(other.get("members", []))

            # Different community + shared members = neighbors
            if region_id != other_id and region_members & other_members:
                neighbors.add(other_id)

        if neighbors:
            neighbor_map[region_id] = neighbors

    logger.debug("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))
    return neighbor_map
```

- [ ] **Step 2：在 `brain_region_api.py` 中使用公共函数**

修改 `brain_region_api.py:144-205`：

```python
from niu_api.internal.region_neighbors import build_neighbor_map

# ... 在 consolidate_brain_regions 函数中

# BUG 3 fix: Build neighbor map for spillover based on shared members
neighbor_map = build_neighbor_map([
    {"community_id": r.community_id, "members": r.members}
    for r in regions
])
activation_mgr.set_region_neighbors(neighbor_map)
logger.info("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))
```

- [ ] **Step 3：在 `region_sync.py` 中使用公共函数**

修改 `region_sync.py:290-296`：

```python
from niu_api.internal.region_neighbors import build_neighbor_map

# ... 在 _refresh_activation_manager 方法中

# 构建邻居映射（替代空字典）
neighbor_map = build_neighbor_map([
    {"community_id": r.community_id, "members": r.members}
    for r in regions
])
activation_mgr.set_region_neighbors(neighbor_map)
```

---

### Task 2.2：调整脑区创建阈值

**Files:**
- Modify: `niu_api/internal/region_detector.py`
- Modify: `niu_api/internal/region_manager.py`

- [ ] **Step 1：修改 region_detector.py 的默认参数（第 101-102 行）**

```python
def detect_communities(
    self, resolution: float = 1.0, min_graph_size: int = 50,
    min_community_size: int = 100,  # 从 10 改为 100
) -> CommunityDetectionResult:
```

- [ ] **Step 2：修改 region_manager.py 的 incremental_update 调用（第 894 行）**

```python
partition = detector.detect_communities(
    resolution=1.0,
    min_community_size=100,  # 从 10 改为 100
)
```

- [ ] **Step 3：修改 region_manager.py 的 dissolve_shrunk_regions 默认参数（第 544 行）**

```python
def dissolve_shrunk_regions(
    self,
    shrink_threshold: int = 100,  # 从 3 改为 100
    shrink_rounds: int = 3,
) -> list[str]:
```

---

## 阶段 3：P2 incremental_update + summarize + leidenalg

### Task 3.1：添加 leidenalg 依赖

**Files:**
- Modify: `mcp-servers/lightrag-server/pyproject.toml`
- Modify: `agent/pyproject.toml`

**审核发现：** 项目使用 `pyproject.toml` 管理依赖，而非 `requirements.txt`。

- [ ] **Step 1：添加依赖到 `mcp-servers/lightrag-server/pyproject.toml`**

在 `dependencies` 数组中添加：

```toml
dependencies = [
    # ... 现有依赖
    "python-igraph>=0.11",
    "leidenalg>=0.10",
]
```

- [ ] **Step 2：添加依赖到 `agent/pyproject.toml`**

同样在 `dependencies` 数组中添加：

```toml
dependencies = [
    # ... 现有依赖
    "python-igraph>=0.11",
    "leidenalg>=0.10",
]
```

- [ ] **Step 3：验证安装**

Run: `pip install python-igraph leidenalg`

---

### Task 3.2：改进 _summarize_region（可选）

**Files:**
- Modify: `niu_api/internal/region_manager.py`

**方案 A（推荐）：改进启发式**

```python
def _summarize_region(
    self,
    entity_summaries: list[str],
) -> tuple[str, str]:
    """Generate region name and summary from entity descriptions.

    改进的启发式方法：
    1. 统计实体类型，选择最常见的类型作为脑区类别
    2. 用 top3 实体名拼接作为摘要
    """
    if not entity_summaries:
        return ("unknown", "空区域")

    # 解析类型和名称
    type_counts: dict[str, int] = {}
    entity_names: list[str] = []

    for summary in entity_summaries:
        match = re.match(r"([^(]+)\(([^)]+)\)", summary)
        if match:
            name = match.group(1).strip()
            etype = match.group(2).strip()
            type_counts[etype] = type_counts.get(etype, 0) + 1
            entity_names.append(name)
        else:
            entity_names.append(summary.strip())
            type_counts["unknown"] = type_counts.get("unknown", 0) + 1

    if not entity_names:
        return ("unknown", "空区域")

    # 选择最常见的类型作为脑区类别
    sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    region_category = sorted_types[0][0] if sorted_types else "unknown"

    # 用第一个实体名作为脑区标签
    region_label = entity_names[0].replace("|", "-")

    # 用 top3 实体名拼接作为摘要
    top_names = entity_names[:3]
    region_summary = "、".join(top_names)
    if len(entity_names) > 3:
        region_summary += f"等{len(entity_names)}个{region_category}相关实体"

    return (region_label, region_summary)
```

---

## 阶段 4：预置常用脑区

### Task 4.1：扩展 DEFAULT_REGIONS

**Files:**
- Modify: `niu_api/internal/region_manager.py`

**设计理念：**

脑区模拟人脑的功能分区。当用户在某个领域工作时，相关脑区被"点亮"（激活），
后续的检索会优先从该脑区获取内容（加权检索）。脑区会缓慢"熄灭"（衰减），
除非再次被激活。

**脑区划分原则：**
1. **核心脑区（保留原有）**：按数据来源划分，始终创建
2. **大类脑区（新增）**：按知识领域划分，边界清晰，LLM 容易判断

| 脑区 | 类型 | 描述 | 典型内容 |
|------|------|------|---------|
| 聊天历史 | 核心（保留） | 日常对话中提炼的偏好、技能和经验记忆 | 用户偏好、技能经验 |
| 文档库 | 核心（保留） | 用户导入的文档和资料，经解析后入库的知识 | PDF、Word、Markdown |
| 知识体系 | 核心（保留） | 系统化组织的概念、关系和理论体系 | 概念、理论、方法论 |
| 人际关系 | 大类（新增） | 人物实体、关系网络、社交图谱 | 家人、朋友、同事 |
| 工作事务 | 大类（新增） | 工作相关的项目、任务、决策记录 | 项目、会议、决策 |
| 生活事务 | 大类（新增） | 日常生活相关的日程、健康、财务 | 日程、健康、财务 |

**边界说明：**
- 照片内容自动归入人际关系（人物）或生活事务（地点/事件）
- 代码相关内容归入知识体系或工作事务

- [ ] **Step 1：修改 DEFAULT_REGIONS 定义（第 993-1003 行）**

```python
DEFAULT_REGIONS: dict[str, dict] = {
    # 核心脑区（按数据来源划分，始终创建）
    "聊天历史": {
        "description": "日常对话中提炼的偏好、技能和经验记忆",
        "priority": "core",
    },
    "文档库": {
        "description": "用户导入的文档和资料，经解析后入库的知识",
        "priority": "core",
    },
    "知识体系": {
        "description": "系统化组织的概念、关系和理论体系",
        "priority": "core",
    },
    # 大类脑区（按知识领域划分，边界清晰）
    "人际关系": {
        "description": "人物实体、关系网络、社交图谱",
        "priority": "category",
    },
    "工作事务": {
        "description": "工作相关的项目、任务、决策记录",
        "priority": "category",
    },
    "生活事务": {
        "description": "日常生活相关的日程、健康、财务",
        "priority": "category",
    },
}
```

- [ ] **Step 2：修改 create_default_regions 函数（第 1006-1077 行）**

```python
def create_default_regions(
    adapter: Any,
    ingester: Any,
    include_category: bool = True,  # 是否创建大类脑区（默认创建）
) -> dict:
    """Create default brain region master nodes.

    Args:
        adapter: LightRAGAdapter instance.
        ingester: LightRAGIngester instance.
        include_category: 是否创建大类脑区（默认创建）

    Returns:
        Dict with created and existing counts.
    """
    from niu_api.internal.lightrag_manager import get_brain_regions

    all_entities: list[dict] = []
    all_relationships: list[dict] = []
    created = 0
    existing = 0

    existing_regions = get_brain_regions()

    for region_label, config in DEFAULT_REGIONS.items():
        # 跳过大类脑区（除非明确请求）
        if config.get("priority") == "category" and not include_category:
            continue

        region_name = f"{region_label}{REGION_SUFFIX}"

        if region_name in existing_regions:
            existing += 1
            continue

        all_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": config["description"],
        })
        all_relationships.append({
            "src_id": NIU_ENTITY,
            "tgt_id": region_name,
            "keywords": ANCHOR_RELATION,
            "description": f"缺省脑区锚点: {region_label}",
            "source_id": REGION_SOURCE_ID,
            "file_path": REGION_FILE_PATH,
        })
        created += 1

    # Batch inject...
    if all_entities or all_relationships:
        try:
            result = ingester.inject_custom_kg(
                entities=all_entities,
                relationships=all_relationships,
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning(
                    "批量注入默认脑区失败: %s",
                    result.get("message", "unknown"),
                )
                return {"created": 0, "existing": existing}
            logger.info(
                "批量注入 %d 个默认脑区, %d 条锚点关系",
                len(all_entities),
                len(all_relationships),
            )
        except Exception as e:
            logger.warning(f"批量注入默认脑区失败: {e}")
            return {"created": 0, "existing": existing}

    return {"created": created, "existing": existing}
```

---

## 测试计划

### 阶段 0 功能闭环测试

**测试 0.1：向量检索激活**

- [ ] **Step 1：启动 API 服务**

Run: `python -m niu_api`

- [ ] **Step 2：发送测试消息**

发送消息："告诉我关于 Python 编程的内容"

- [ ] **Step 3：检查日志**

Run: `grep "Brain context injected" logs/api.log`

Expected: 看到脑区注入日志，包含实体命中信息

**测试 0.2：加权检索**

- [ ] **Step 1：检查 apply_activation_weight 调用**

Run: `grep -n "apply_activation_weight" agent/runner.py`

Expected: 找到调用代码

- [ ] **Step 2：发送测试消息并检查加权效果**

发送消息后，检查 LightRAG 检索结果是否按激活度加权

**测试 0.3：衰减时机**

- [ ] **Step 1：发送多轮对话**

发送 3 轮对话，观察脑区激活度变化

- [ ] **Step 2：检查衰减日志**

Run: `grep "decay_all\|Brain region decay" logs/api.log`

Expected: 每轮结束时看到衰减日志

### 阶段 1 边动力学测试

**测试 1.1：边权重初始值**

- [ ] **Step 1：创建新脑区**

Run: `python -c "from niu_api.internal.region_manager import RegionManager; rm = RegionManager(); rm.create_region_nodes('测试脑区', ['实体1', '实体2'])"`

- [ ] **Step 2：检查边权重**

Run: `python scripts/check_edge_weights.py`（需创建）

Expected: 所有脑区相关边 weight=0.5

**测试 1.2：边衰减**

- [ ] **Step 1：运行边衰减**

调用 `_decay_structural_edges()`

- [ ] **Step 2：检查衰减后的权重**

Expected: 权重从 0.5 衰减到 0.25

**测试 1.3：边强化**

- [ ] **Step 1：触发工具使用强化**

调用 `reinforce_on_tool_use("memory-server/remember")`

- [ ] **Step 2：检查强化后的权重**

Expected: 权重增加 0.15

### 阶段 2 spillover + 阈值测试

**测试 2.1：spillover 激活**

- [ ] **Step 1：创建有共享成员的脑区**

创建两个脑区，共享部分实体

- [ ] **Step 2：激活其中一个脑区**

激活脑区 A，观察脑区 B 是否获得 spillover 激活

- [ ] **Step 3：检查激活日志**

Expected: 看到邻居脑区获得部分激活度

**测试 2.2：阈值调整**

- [ ] **Step 1：运行社区检测**

调用 `detect_communities(min_community_size=100)`

- [ ] **Step 2：检查创建的脑区**

Expected: 只有成员 ≥ 100 的社区才创建脑区

### 集成测试

- [ ] **完整脑区创建流程**：从文档入库 → 实体提取 → 社区检测 → 脑区创建
- [ ] **边权重验证**：验证初始值、衰减、强化全流程
- [ ] **spillover 激活验证**：验证邻居脑区激活传播
- [ ] **预置脑区保护**：验证预置脑区不被合并

---

## 单元测试文件（需创建）

- [ ] `tests/test_region_activation.py` - spillover 测试
- [ ] `tests/test_region_manager.py` - 边衰减测试
- [ ] `tests/test_brain_tools.py` - reinforce 测试

---

## 不做的事

1. **不修改 LightRAG 核心代码** — 只修改适配层
2. **不删除现有脑区数据** — 只修改边权重
3. **不改变脑区实体命名规范** — `{label}脑区` 格式保持不变
4. **不修改 RegionSync 的 8 步流程** — 只填充空实现
