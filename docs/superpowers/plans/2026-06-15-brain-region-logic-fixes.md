# 脑区系统 6 个逻辑 Bug 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复脑区系统中 6 个运行逻辑层面的 bug，确保数据在创建、读取、更新路径上一致，并在异常和并发路径上安全

**Architecture:** 修复遵循"最小修改"原则——每个 bug 只改必要的代码路径，修复之间不互相冲突。执行顺序按依赖关系排列。

**Tech Stack:** Python 3.11+, pytest, igraph, leidenalg

---

## 依赖关系

```
Task 4 (问题4) ──→ Task 1 (问题1) 依赖 Task 4 的 drifted_community_ids
Task 1 (问题1) ──→ Task 2 (问题2) 依赖 Task 1 的返回值 + Task 4 的 drifted
Task 5 (问题5) ──→ 依赖 Task 4 + Task 1 + Task 2 的接口变更（统一更新调用者）
Task 3 (问题3) ──── 独立
Task 6 (问题6) ──── 独立（依赖 Task 5 的代码基础）
```

建议执行顺序：**Task 4 → Task 1 → Task 2 → Task 5 → Task 6 → Task 3**

**注意**：
- Task 1 依赖 Task 4 的 `drifted_community_ids` 返回值（`create_region_nodes` 的 `skip_community_ids` 参数），所以 Task 4 必须在 Task 1 之前执行。
- Task 2 依赖 Task 1 的 `created` 返回值和 Task 4 的 `drifted` 返回值来排除 Step 5 的 summary 更新。
- **Task 5 负责统一更新所有调用者**：`_manage_region_nodes` 和 `incremental_update` 的修改（三元组解包 + skip_community_ids + created/drifted 排除 + dry_run 两阶段）统一由 Task 5 完成，Task 4 和 Task 2 不修改这两个调用者。这样避免同一段代码被多个 Task 重复修改导致 Edit 匹配失败。
- Task 4 执行时同时添加 `create_region_nodes` 的 `skip_community_ids` 参数签名（方法体暂不实现），避免调用者传入未知参数导致 TypeError。
- Task 6 添加互斥锁，在 Task 5 修改后的代码基础上工作。

**⚠️ 临时不可运行窗口**：Task 4 完成后到 Task 5 执行前，`_manage_region_nodes` 和 `incremental_update` 仍用旧方式调用 `cleanup_stale_regions`（只解包一个返回值），会触发 ValueError。因此 Task 4 到 Task 5 必须在同一工作会话中连续执行，中间不运行测试。

---

## Task 1 [CRITICAL]：`create_region_nodes` 跳过已存在脑区的关系注入

**Bug 根因**：`create_region_nodes` 不检查脑区是否已存在，对已存在脑区重复注入 entity + relationship + chunk。虽然 LightRAG 底层使用 `nx.Graph`（非 MultiGraph），重复 `add_edge` 不会创建平行边，但会：  (a) 覆盖脑区描述为不含类型信息的劣质 summary；  (b) 覆盖边属性（weight 重置、created_at 刷新）；  (c) 浪费 LLM 调用（`_generate_labels` 为已存在脑区重新生成名称）。

**Files:**
- Modify: `niu_api/internal/region_manager.py:201-340`
- Modify: `tests/test_region_manager.py`

- [ ] **Step 1: 修改 `create_region_nodes` 方法**

**注意**：审核发现 3 个必须处理的问题：(a) 已存在脑区必须从 Pass 1 和 Pass 2 中也过滤掉，否则 LLM 仍会为已存在脑区调用，且非确定性可能导致标签不匹配；(b) 代码 222-227 行已经调用了 `self.get_all_regions()`，应复用该调用同时构建 `existing_labels` 和 `existing_region_names`，不要加第二次调用；(c) `created_regions` 的语义从"所有处理过的脑区"变为"真正新建的脑区"。

修改步骤：

1. 扩展现有的 `get_all_regions()` 调用（line 222-227），同时构建 `existing_labels` 和 `existing_region_names`：

```python
# 复用 get_all_regions() 调用，同时构建 labels 和 full names
existing_region_names: set[str] = set()
existing_labels: list[str] = []
try:
    for region in self.get_all_regions():
        existing_region_names.add(region.name)
        label = region.label or region.name.removesuffix(REGION_SUFFIX)
        existing_labels.append(label)
except Exception:
    pass
```

2. 在 Pass 1 中，过滤掉已存在脑区对应的分区（避免为已存在脑区浪费 LLM 调用）：

```python
# Pass 1: Filter valid communities, excluding already-existing regions
valid_communities = []
for partition in partitions:
    members = [name for name in partition.entity_names if not name.endswith(REGION_SUFFIX)]
    if not members:
        continue
    entity_summaries = self._build_entity_summaries(
        members, partition.entity_types,
        partition.entity_name_to_type,
    )
    # 暂时保留所有社区，是否已存在在 Pass 3 中判断
    valid_communities.append((partition, members, entity_summaries))
```

3. 在 Pass 2 中，`_generate_labels` 仍然为所有社区生成标签（包括已存在的），但 `existing_labels` 用于去重，LLM 会避免生成重名标签。这是安全的——即使 LLM 为已存在脑区生成了新标签，Pass 3 的 `is_existing` 检查也会正确跳过关系注入。

4. 在 Pass 3 的循环中，检查脑区是否已存在：

```python
for (partition, members, entity_summaries), region_label in zip(valid_communities, labels):
    region_name = f"{region_label}{REGION_SUFFIX}"
    is_existing = region_name in existing_region_names

    # ... 构建 description（已存在和新建都一样）...

    # Always upsert entity (updates description for existing regions)
    all_entities.append({...})

    if is_existing:
        logger.info("跳过已存在脑区的关系注入: %s (只更新描述)", region_name)
        continue

    # Below only for NEW regions — relationships + chunks
    # ... 脑区锚点边 + 包含边 + chunk ...

    created_regions.append(region_name)
```

关键变化：
1. 复用 `get_all_regions()` 调用构建 `existing_labels` 和 `existing_region_names`
2. 已存在脑区只 append entity（更新描述），不 append relationship 和 chunk
3. 只有新建脑区才 append 到 `created_regions`
4. `created_regions` 的语义从"处理过的脑区"变为"真正新建的脑区"

**注意**：`brain_region_api.py` 的 `consolidate_brain_regions` 也调用 `create_region_nodes`（line 183），返回值语义变更后 `regions_created` 计数会更小（只含真正新建的），这是正确行为，无需额外修改。

**注意**：Task 4 的 `cleanup_stale_regions` 会返回 `drifted_community_ids: set[str]`（漂移脑区对应的分区 ID 集合）。`create_region_nodes` 需要新增 `skip_community_ids` 参数来跳过这些分区，避免 LLM 为漂移脑区生成不同标签导致重复创建。详见 Task 4 Step 1 的返回值设计。

- [ ] **Step 1.5: 为 `create_region_nodes` 新增 `skip_community_ids` 参数**

在方法签名中新增可选参数：

```python
def create_region_nodes(
    self,
    partition_result: CommunityDetectionResult,
    skip_community_ids: set[str] | None = None,
) -> list[str]:
```

在 Pass 1 的循环中，跳过漂移脑区对应的分区（这些分区已由 `_update_drifted_regions` 处理）：

```python
# Pass 1: Filter valid communities
valid_communities = []
for partition in partition_result.partitions:
    community_id = f"community_{partition.region_id}"
    # Skip partitions already handled by drift update
    if skip_community_ids and community_id in skip_community_ids:
        logger.debug("跳过漂移脑区对应的分区: %s", community_id)
        continue
    members = [
        name for name in partition.entity_names
        if not name.endswith(REGION_SUFFIX)
    ]
    if not members or len(members) < MIN_COMMUNITY_SIZE:
        continue
    # ...
```

调用者更新：
- `region_sync.py` 的 `_manage_region_nodes`：传入 `skip_community_ids=drifted_cids`（从 `cleanup_stale_regions` 返回值获取）
- `incremental_update`：同样传入 `skip_community_ids=drifted_cids`
- `brain_region_api.py` 的 `consolidate_brain_regions`：同样传入 `skip_community_ids=drifted_cids`

**设计说明**：被 `skip_community_ids` 跳过的分区不会出现在 `valid_communities` 中，也不会在 Pass 3 的 `is_existing` 检查中被处理。这是正确的——漂移脑区的描述和成员关系已在 `_update_drifted_regions`（由 `cleanup_stale_regions` 内部调用）中更新，`create_region_nodes` 不需要再处理它。

- [ ] **Step 2: 更新 docstring**

将 `create_region_nodes` 的返回值文档从 "List of created region names" 改为 "List of newly created region names (excludes existing regions that were only updated)"。

- [ ] **Step 3: 写测试**

在 `TestCreateRegionNodes` 中新增 `test_skips_relationship_injection_for_existing_regions`：
- Mock `get_all_regions` 返回已存在的脑区
- 验证 `inject_custom_kg` 的 relationships 和 chunks 为空
- 验证 `created_regions` 不包含已存在脑区

- [ ] **Step 4: 验证**

Run: `python -m py_compile niu_api/internal/region_manager.py && python -m pytest tests/test_region_manager.py -v`

---

## Task 2 [IMPORTANT]：`update_region_summaries` 类型信息保留

**Bug 根因 (D-16)**：`update_region_summaries`（region_manager.py:413）调用 `_build_entity_summaries(members, {}, {})`，第三个参数 `entity_name_to_type` 为空字典，导致所有实体被标记为 "unknown" 类型，覆盖了首次创建时含类型信息的精确 summary。每次 24h 同步执行 `update_region_summaries` 时，稳定脑区的类型信息被永久丢失。

`_manage_region_nodes` 和 `incremental_update` 中对 `update_region_summaries` 的调用方式变更（排除 created/drifted 脑区）由 Task 5 统一完成。

**Files:**
- Modify: `niu_api/internal/region_manager.py:413` — `update_region_summaries` 类型信息修复

- [ ] **Step 1: 修复 `update_region_summaries` 的类型信息丢失 (D-16)**

当前代码（region_manager.py:413）使用空类型映射，导致稳定脑区的类型信息被覆盖为 "unknown"。修复方式：从 NetworkX 图中批量读取成员实体的 `entity_type` 属性，构建 `entity_name_to_type` 映射。

```python
# 在 update_region_summaries 方法中，替换 line 413：
# 旧代码：entity_summaries = self._build_entity_summaries(members, {}, {})
# 新代码：从图中批量读取成员实体的类型
from niu_api.internal.lightrag_manager import get_lightrag, graph_read_lock
entity_name_to_type: dict[str, str] = {}
try:
    rag = get_lightrag()
    if rag is not None:
        kg = rag.chunk_entity_relation_graph
        nx_graph = kg._graph if hasattr(kg, "_graph") else kg
        if nx_graph is not None:
            with graph_read_lock():
                for member in members:
                    member_lower = member.lower() if isinstance(member, str) else member
                    if member_lower in nx_graph:
                        node_data = nx_graph.nodes[member_lower]
                        etype = node_data.get("entity_type", "")
                        if etype:
                            entity_name_to_type[member] = etype
except Exception:
    pass  # 读取失败时回退到空映射，不影响功能
entity_summaries = self._build_entity_summaries(members, {}, entity_name_to_type or None)
```

**设计说明**：从 `nx_graph.nodes[member]` 读取 `entity_type` 属性是最高效的方式（直接读内存，无需 API 调用）。如果读取失败（图不可用），回退到空映射——此时 summary 质量与当前代码一致，不会退化。

- [ ] **Step 2: 验证**

Run: `python -m py_compile niu_api/internal/region_manager.py`

---

## Task 4 [IMPORTANT]：`cleanup_stale_regions` 增加成员重叠度检查

**Bug 根因**：`cleanup_stale_regions` 通过比较 `community_id` 判断脑区是否过时。但 `_build_partitions` 每次重新编号为连续 ID (community_0, community_1, ...)，导致只要社区数量不变，community_id 集合不变，cleanup 不删除任何脑区。即使社区成员完全改变，旧标签也不会被更新。

**修复方案**：除了 community_id 匹配外，增加成员重叠度检查（Jaccard 相似度）。对于 community_id 匹配但成员漂移严重的脑区，更新其描述和成员关系（而非删除）。

**Files:**
- Modify: `niu_api/internal/region_manager.py:510-564`
- Modify: `niu_api/internal/lightrag_manager.py` — 新增 `remove_region_edges` 函数
- Modify: `niu_api/brain_region_api.py` — 简单调用者更新（三元组解包 + skip_community_ids）
- Modify: `tests/test_region_manager.py` — 旧测试解包方式更新

- [ ] **Step 1: 修改 `cleanup_stale_regions`**

增加 `drift_threshold` 参数（默认 0.3）。核心逻辑重新设计为**基于成员内容的匹配**，不依赖 community_id：

1. 使用 `get_all_region_members()` 批量读取所有脑区成员（一次图快照，避免 N+1 的 `get_region_members()` 调用）
2. 从分区结果构建 `community_id → member set` 映射
3. 对每个已存在脑区（跳过默认脑区），计算与**所有**新分区的 Jaccard 相似度，找最佳匹配
4. 根据最佳匹配结果：
   - 最佳 Jaccard >= drift_threshold → 脑区稳定，无需操作
   - 0 < 最佳 Jaccard < drift_threshold → 脑区漂移，用最佳匹配分区的成员更新
   - 最佳 Jaccard == 0（无任何成员重叠） → 过时，删除
5. 构建漂移映射 `drift_info: dict[str, tuple[str, set[str]]]`（脑区名 → (最佳匹配 community_id, 成员集)）
6. 内部调用 `_update_drifted_regions(drift_info, current_partition)` 执行漂移更新
7. 返回 `(removed, drifted, drifted_community_ids)` 三元组

```python
def cleanup_stale_regions(
    self,
    current_partition: CommunityDetectionResult,
    drift_threshold: float = 0.3,
    dry_run: bool = False,
) -> tuple[list[str], list[str], set[str]]:
    """返回 (removed_region_names, drifted_region_names, drifted_community_ids)

    漂移检测基于成员内容的 Jaccard 相似度，不依赖 community_id 匹配。
    这样即使 Leiden 重新编号导致 community_id 不稳定，也能正确判断脑区状态。

    当 dry_run=True 时，只检测不执行（不删除脑区、不执行漂移更新），
    用于两阶段模式：先检测后创建，创建成功后再执行删除。

    内部调用 _update_drifted_regions 执行漂移更新，所有调用者自动受益。
    drifted_community_ids 用于 create_region_nodes 的 skip_community_ids 参数，
    避免为漂移脑区对应的分区重复创建脑区。
    """
    # 1. 批量读取所有脑区成员（一次图快照，避免 N+1）
    from niu_api.internal.lightrag_manager import get_all_region_members
    region_member_map = get_all_region_members()

    # 2. 从分区结果构建 community_id → member set 映射
    community_members: dict[str, set[str]] = {}
    for partition in current_partition.partitions:
        cid = f"community_{partition.region_id}"
        members = {
            name for name in partition.entity_names
            if not name.endswith(REGION_SUFFIX)
        }
        if members:
            community_members[cid] = members

    # 3. 获取所有已存在脑区
    existing_regions = self.get_all_regions()

    # 安全检查：如果 region_member_map 为空但存在非默认脑区，
    # 说明读取可能失败（图暂时不可用），跳过漂移检测避免误删
    has_non_default = any(not is_default_region(r.name) for r in existing_regions)
    if not region_member_map and has_non_default:
        logger.warning("get_all_region_members 返回空但存在脑区，跳过漂移检测")
        return [], [], set()

    removed: list[str] = []
    drifted: list[str] = []
    drift_info: dict[str, tuple[str, set[str]]] = {}
    drifted_community_ids: set[str] = set()

    for region in existing_regions:
        # 保护默认脑区 — 不参与漂移检测和删除
        if is_default_region(region.name):
            logger.debug("保护默认脑区: %s", region.name)
            continue

        # 获取该脑区当前成员
        current_members = set(region_member_map.get(region.name, []))
        if not current_members:
            # 无成员的脑区视为过时
            if not dry_run:
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    removed.append(region.name)
                    logger.info("删除无成员脑区: %s", region.name)
            else:
                removed.append(region.name)
                logger.debug("[dry_run] 检测到无成员脑区: %s", region.name)
            continue

        # 计算与所有新分区的 Jaccard 相似度，找最佳匹配
        best_jaccard = 0.0
        best_cid = ""
        best_member_set: set[str] = set()
        for cid, new_members in community_members.items():
            intersection = current_members & new_members
            union = current_members | new_members
            jaccard = len(intersection) / len(union) if union else 0.0
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_cid = cid
                best_member_set = new_members

        # 根据最佳匹配结果分类
        if best_jaccard >= drift_threshold:
            # 脑区稳定，无需操作
            logger.debug("脑区稳定: %s (Jaccard=%.2f)", region.name, best_jaccard)
        elif best_jaccard > 0:
            # 脑区漂移，记录漂移信息
            drift_info[region.name] = (best_cid, best_member_set)
            drifted.append(region.name)
            drifted_community_ids.add(best_cid)
            logger.info(
                "检测到脑区漂移: %s (Jaccard=%.2f, best_match=%s)",
                region.name, best_jaccard, best_cid,
            )
        else:
            # 无任何成员重叠，过时，删除
            if not dry_run:
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    removed.append(region.name)
                    logger.info("删除过时脑区: %s (无成员重叠)", region.name)
            else:
                removed.append(region.name)
                logger.debug("[dry_run] 检测到过时脑区: %s (无成员重叠)", region.name)

    # 4. 内部执行漂移更新（所有调用者自动受益）
    # dry_run 时只检测不执行
    if drift_info and not dry_run:
        self._update_drifted_regions(drift_info, current_partition)

    if removed:
        logger.info("共清理 %d 个过时脑区节点", len(removed))
    if drifted:
        logger.info("共更新 %d 个漂移脑区", len(drifted))

    return removed, drifted, drifted_community_ids
```

**关键设计变更**：
1. 不再先按 community_id 匹配再检查 Jaccard，而是对每个脑区计算与所有分区的 Jaccard。复杂度从 O(脑区数) 增加到 O(脑区数 * 分区数)，但通常两者都 < 10，性能无影响。
2. 使用 `get_all_region_members()` 批量读取（一次图快照），而非 N+1 的 `get_region_members()` 调用。
3. 默认脑区通过 `is_default_region()` 保护，不参与漂移检测和删除。
4. `_update_drifted_regions` 在 `cleanup_stale_regions` 内部调用，所有调用者（`_manage_region_nodes`、`incremental_update`、`consolidate_brain_regions`）自动受益，无需单独调用。
5. 返回 `drifted_community_ids: set[str]`，供 `create_region_nodes` 的 `skip_community_ids` 参数使用，避免为漂移脑区对应的分区重复创建脑区。

**注意**：返回值从 `list[str]` 改为 `tuple[list[str], list[str], set[str]]`。现有调用者需要改为 `removed, drifted, drifted_cids = manager.cleanup_stale_regions(...)`。

- [ ] **Step 1.5: 在 `lightrag_manager.py` 中新增 `remove_region_edges` 函数**

直接从 NetworkX 图中删除指定脑区的指定类型边，使用 `graph_write_lock` 保护。

**重要**：必须通过 `kg._graph` 访问底层 `nx.Graph` 对象，使用 NetworkX 原生 API。`NetworkXStorage` 类没有 `get_neighbors()` 和 `remove_edge()` 方法。参考 `lightrag_manager.py:322` 中的 `kg._graph` 访问模式。

```python
def remove_region_edges(region_name: str, edge_type: str) -> int:
    """Remove edges of a specific type from a brain region node.

    Directly operates on the internal NetworkX graph under write lock.

    Args:
        region_name: Brain region entity name
        edge_type: Edge keywords to match (case-insensitive)

    Returns:
        Number of edges removed.

    Note: Assumes nx.Graph (not MultiGraph). If LightRAG ever switches
    to MultiGraph, get_edge_data returns dict-of-dict and this function
    will silently skip all edges (caught by isinstance check).
    """
    removed = 0
    try:
        rag = get_lightrag()
        if rag is None:
            return 0
        kg = rag.chunk_entity_relation_graph
        if kg is None:
            return 0
        # Access internal nx.Graph directly (NetworkXStorage has no get_neighbors/remove_edge)
        nx_graph = kg._graph if hasattr(kg, "_graph") else kg
        if nx_graph is None:
            return 0
        # Normalize node ID to lowercase (LightRAG's NetworkXStorage does this on insert)
        region_name = region_name.lower() if isinstance(region_name, str) else region_name
        with graph_write_lock():
            if region_name not in nx_graph:
                return 0
            for neighbor_id in list(nx_graph.neighbors(region_name)):
                edge_data = nx_graph.get_edge_data(region_name, neighbor_id)
                if not isinstance(edge_data, dict):
                    continue
                keywords = edge_data.get("keywords", "")
                if keywords.lower() == edge_type.lower():
                    nx_graph.remove_edge(region_name, neighbor_id)
                    removed += 1
    except Exception as e:
        logger.debug("remove_region_edges failed for %s: %s", region_name, e)
    return removed
```

**附带发现**：现有 `_decay_structural_edges`（region_manager.py:1278-1299）也使用了不存在的 `kg.get_neighbors()` / `kg.remove_edge()`，导致边衰减从未真正工作。此问题不在本次修复范围内，但应记录为已知 bug。

验证：`python -m py_compile niu_api/internal/lightrag_manager.py`

- [ ] **Step 2: 新增 `_update_drifted_regions` 方法**

此方法由 `cleanup_stale_regions` 内部调用，不对外暴露。所有调用者通过 `cleanup_stale_regions` 的返回值获取漂移信息。

**注意**：审核发现必须处理删除旧的"包含"边。只重新注入新边不删旧边，会导致脑区成员随时间膨胀。

**注意**：`remove_region_edges` 只删除边，不修改实体数据。实体描述中的 `community_id` 仍然有效，后续 `get_all_regions()` 可以正确读取。

**注意**：漂移更新在 `cleanup_stale_regions` 内部执行，`create_region_nodes` 随后运行。对于漂移脑区，`create_region_nodes` 通过 `skip_community_ids` 参数跳过对应分区，所以不会有 entity upsert 覆盖漂移更新写入的描述。

**注意**：`remove_region_edges` 和 `inject_custom_kg` 之间没有原子性保证。如果 `inject_custom_kg` 失败，脑区会暂时没有"包含"边。但 `dissolve_shrunk_regions` 需要 `shrink_rounds=3` 连续周期才会解散，所以单次失败不会导致脑区被误删。

对漂移脑区：
1. 删除该脑区所有现有的"包含"边（通过 `delete_entity` 删除脑区节点后重建，或通过直接操作 NetworkX 图删除边）
2. 用分区数据重新生成 summary（含类型信息）
3. 更新描述（entity upsert）
4. 重新注入新成员的"包含"边

**关于 `community_members` 参数**：`cleanup_stale_regions` 在漂移检测时已经构建了 `community_id → member set` 映射（从当前分区结果），并确定了每个漂移脑区对应的 `community_id`。`_update_drifted_regions` 接收这个映射，确保使用正确的成员集——即 Jaccard 计算时产生重叠的那个新分区成员集，而非基于 community_id 字符串查找可能因重新编号而错位的成员集。

具体实现：
```python
def _update_drifted_regions(
    self,
    drift_info: dict[str, tuple[str, set[str]]],
    current_partition: CommunityDetectionResult,
) -> None:
    """Update regions whose membership has drifted.

    Called internally by cleanup_stale_regions — not for external use.

    Note: 脑区锚点边（Niu -> region, keywords="脑区锚点"）不受影响，
    remove_region_edges 只删除"包含"边，锚点边在首次创建时注入后无需重新注入。

    Args:
        drift_info: {region_name: (best_match_community_id, new_member_set)}
            直接由 cleanup_stale_regions 的漂移检测构建，
            避免通过 community_id 间接查找可能因重新编号而错位的成员集。
        current_partition: Current community detection result for type info lookup.
    """
    # 1. 删除漂移脑区的旧"包含"边
    from niu_api.internal.lightrag_manager import remove_region_edges
    for region_name in drift_info:
        remove_region_edges(region_name, edge_type=BELONGS_TO_RELATION)

    # 2. 重新生成描述和注入新边
    all_entities = []
    all_relationships = []
    for region_name, (best_cid, new_members) in drift_info.items():
        if not new_members:
            continue
        # 找到对应的分区，获取类型信息
        partition = next(
            (p for p in current_partition.partitions if f"community_{p.region_id}" == best_cid),
            None,
        )
        entity_summaries = self._build_entity_summaries(
            list(new_members),
            partition.entity_types if partition else {},
            partition.entity_name_to_type if partition else None,
        )
        summary = self._generate_region_summary(entity_summaries)
        representative = list(new_members)[0].replace("<SEP>", "-").replace("|", "-")
        label = region_name.removesuffix(REGION_SUFFIX)
        description = _encode_description(
            summary=summary, region_id=best_cid,
            size=len(new_members), representative=representative,
            updated_at=time.time(),
        )
        all_entities.append({
            "entity_name": region_name, "entity_type": REGION_ENTITY_TYPE,
            "description": description, "source_id": REGION_SOURCE_ID,
        })
        for member in new_members:
            all_relationships.append({
                "src_id": region_name, "tgt_id": member,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{member} belongs to region {label}",
                "weight": 0.5, "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })
    if all_entities or all_relationships:
        self._ingester.inject_custom_kg(
            entities=all_entities, relationships=all_relationships,
            chunks=[], source_id=REGION_SOURCE_ID,
        )
```

**注意**：`_update_drifted_regions` 是私有方法，由 `cleanup_stale_regions` 内部调用。外部调用者不需要也不应该直接调用此方法。

- [ ] **Step 3: 更新简单调用者 + 添加 `skip_community_ids` 参数签名**

返回值从 `list[str]` 改为 `tuple[list[str], list[str], set[str]]`。**⚠️ `_manage_region_nodes`、`incremental_update` 和 `consolidate_brain_regions` 的调用者更新统一由 Task 5 完成**，避免同一段代码被多个 Task 重复修改导致 Edit 匹配失败。

以下简单调用者在本 Step 中更新：

1. `tests/test_region_manager.py:474,509,545` — 3 处测试中的旧调用：
   ```python
   removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)
   ```

2. **添加 `create_region_nodes` 的 `skip_community_ids` 参数签名**（方法体暂不实现，留给 Task 1）：
   ```python
   def create_region_nodes(
       self,
       partition_result: CommunityDetectionResult,
       skip_community_ids: set[str] | None = None,
   ) -> list[str]:
   ```
   参数有默认值 `None`，现有调用不受影响。方法体中的过滤逻辑由 Task 1 实现。

- [ ] **Step 4: 写测试**

新增三个测试（注意：`cleanup_stale_regions` 返回三元组 `removed, drifted, drifted_cids`，新测试必须使用三元组解包）：
- `test_detects_membership_drift`：成员完全不重叠时，触发漂移更新。断言 `drifted` 非空且 `drifted_cids` 包含对应 community_id
- `test_no_drift_when_membership_overlaps`：成员重叠度高时不标记漂移。断言 `drifted` 为空且 `drifted_cids` 为空集
- `test_default_regions_protected_from_drift`：默认脑区不参与漂移检测和删除。断言 `removed` 中无默认脑区名

- [ ] **Step 5: 验证**

Run: `python -m py_compile niu_api/internal/region_manager.py && python -m pytest tests/test_region_manager.py::TestCleanupStaleRegions -v`

---

## Task 3 [IMPORTANT]：`assign_entities_to_default_regions` 的关键词与用户自定义脑区脱钩

**Bug 根因**：`REGION_KEYWORDS` 硬编码 6 个固定脑区名，但 `get_default_regions_config()` 从 preferences.json 动态读取。当前 preferences.json 已有 7 个默认脑区（含"组织机构"），但 `REGION_KEYWORDS` 没有对应条目，导致该脑区永远分不到实体。

**设计原则**：
- `preferences.json` 跟随仓库分发，程序启动时自动从仓库拷贝到运行目录，不存在"没有 keywords 字段"的问题
- 不需要硬编码 fallback — 配置就是唯一的数据源
- `memory/skills/brain-region-management.md` 是主 Agent 的脑区管理手册，需要补充 `keywords` 字段的说明

**Files:**
- Modify: `niu_api/internal/region_manager.py:1311-1336,1476-1483`
- Modify: `memory/preferences.json` — 为每个默认脑区添加 `keywords` 字段
- Modify: `memory/skills/brain-region-management.md` — 补充 `keywords` 字段说明

- [ ] **Step 1: 为 `memory/preferences.json` 的每个默认脑区添加 `keywords` 字段**

```json
{
  "label": "聊天历史",
  "description": "日常对话中提炼的偏好、技能和经验记忆",
  "priority": "core",
  "keywords": ["偏好", "习惯", "设置", "配置", "喜欢", "想要"]
},
{
  "label": "文档库",
  "description": "用户导入的文档和资料，经解析后入库的知识",
  "priority": "core",
  "keywords": ["文档", "文件", "PDF", "Word", "Markdown", "笔记"]
},
{
  "label": "知识体系",
  "description": "系统化组织的概念、关系和理论体系",
  "priority": "core",
  "keywords": ["概念", "理论", "方法", "原理", "定义", "技术"]
},
{
  "label": "人际关系",
  "description": "人物实体、关系网络、社交图谱",
  "priority": "category",
  "keywords": ["人物", "家人", "朋友", "同事", "联系人", "人名"]
},
{
  "label": "工作事务",
  "description": "工作相关的项目、任务、决策记录",
  "priority": "category",
  "keywords": ["项目", "任务", "会议", "决策", "工作", "进度"]
},
{
  "label": "生活事务",
  "description": "日常生活相关的日程、健康、财务",
  "priority": "category",
  "keywords": ["日程", "健康", "财务", "旅行", "生活", "日常"]
},
{
  "label": "组织机构",
  "description": "公司、部门、机构等组织实体和关系网络",
  "priority": "category",
  "keywords": ["公司", "部门", "机构", "组织", "团队", "单位"]
}
```

- [ ] **Step 2: 扩展 `get_default_regions_config` 的 fallback 也加上 keywords**

`region_manager.py:1311-1336` 的 fallback 默认值必须与 `preferences.json` 一致（含"组织机构"和 keywords）。这样当 `preferences.json` 不存在时（极罕见），fallback 仍然有完整数据。

- [ ] **Step 3: 修改 `assign_entities_to_default_regions` 使用动态关键词**

删除硬编码 `REGION_KEYWORDS`，改为在 `assign_entities_to_default_regions` **函数内部**从 `get_default_regions_config()` 动态构建（注意：这是函数内的局部变量，不是模块级变量）：

```python
def assign_entities_to_default_regions(adapter, entity_keywords=None):
    # ...
    existing_regions = get_brain_regions()
    if not existing_regions:
        return {"assigned": 0, "regions": 0}

    # 动态构建关键词映射（从配置读取，替代硬编码）
    REGION_KEYWORDS: dict[str, list[str]] = {}
    for region_def in get_default_regions_config():
        region_name = f"{region_def['label']}{REGION_SUFFIX}"
        keywords = region_def.get("keywords", [])
        if keywords:
            REGION_KEYWORDS[region_name] = keywords

    rag = adapter._get_rag()
    # ... 后续逻辑不变 ...
```

不需要 fallback — `preferences.json` 跟随仓库分发，程序启动时自动拷贝到运行目录。

- [ ] **Step 3.5: 修复 `assign_entities_to_default_regions` 的 size 膨胀 bug (D-15)**

当前代码（region_manager.py:1565-1571）使用 `old_size + new_size` 累加，每次同步都会导致 size 膨胀（因为已分配的实体被重复计入）。修复为使用实际成员数：

```python
# 旧代码（会导致 size 膨胀）：
# new_size = assigned_counts[name]
# old_size = int(parsed.get("size", "0") or "0")
# size=old_size + new_size,

# 新代码：使用实际成员数，避免累加膨胀
from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
actual_members = lightrag_get_region_members(name)
updated_desc = _encode_description(
    summary=parsed.get("summary", ""),
    region_id=parsed.get("region_id", ""),
    size=len(actual_members),  # 实际成员数，不累加
    representative=parsed.get("representative", ""),
    updated_at=time.time(),
)
```

- [ ] **Step 4: 更新 `memory/skills/brain-region-management.md`**

补充 `keywords` 字段的说明，让主 Agent 知道如何配置和增加新的默认脑区：

```markdown
### Brain Region Configuration Fields

Each default brain region in `brain_regions.defaults` has these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `label` | Yes | Short name for the region (e.g., "聊天历史"). The system appends "脑区" automatically. |
| `description` | Yes | Human-readable description of what this region contains |
| `priority` | Yes | Either `"core"` (always shown) or `"category"` (shown when active) |
| `keywords` | Yes | List of keywords used to automatically assign entities to this region. Entities whose name or description contains any keyword will be assigned. |

### Adding a New Default Brain Region

1. Edit `~/.niu/preferences.json`
2. Add a new entry to `brain_regions.defaults`:
   ```json
   {
     "label": "新脑区名",
     "description": "这个脑区包含什么内容",
     "priority": "category",
     "keywords": ["关键词1", "关键词2", "关键词3"]
   }
   ```
3. Restart the system

The `keywords` field controls automatic entity assignment — when the system starts, it scans all entities and assigns each to the region whose keywords best match the entity's name and description.
```

- [ ] **Step 5: 验证**

Run: `python -m py_compile niu_api/internal/region_manager.py && python -m pytest tests/test_region_manager.py -v`

---

## Task 5 [CRITICAL]：统一更新调用者 + 激活管理器保护 + 非原子性修复

**Bug 根因**：三个相关联的缺陷：

**(a) 调用者更新**：Task 4 改了 `cleanup_stale_regions` 的返回值（三元组），Task 1 加了 `skip_community_ids` 参数，Task 2 要求排除 created/drifted 脑区。`_manage_region_nodes` 和 `incremental_update` 这两个复杂调用者需要一次性重写，包含所有这些变更 + dry_run 两阶段模式。**本 Task 负责统一完成**，避免同一段代码被多个 Task 重复修改导致 Edit 匹配失败。

**(b) D-12**：`_refresh_activation_manager` 在 `_manage_region_nodes` 的 cleanup/create 步骤失败后仍无条件执行。如果 `get_all_regions()` 因 LightRAG 不可用而返回空列表，`initialize_from_regions([])` 会清空所有激活状态（`_regions`、`_entity_to_region`、`_label_index` 全部重置），导致 agent 的脑区激活系统完全失效。

**(c) D-13**：`cleanup_stale_regions` 成功删除旧脑区后，`create_region_nodes` 失败，此时 KG 中旧脑区已删、新脑区未创，所有检测脑区丢失直到下次同步（24小时后）。

**Files:**
- Modify: `agent/injector/region_sync.py:209-339` — `_manage_region_nodes` dry_run 两阶段 + `_refresh_activation_manager` 空列表保护
- Modify: `niu_api/internal/region_manager.py:1191-1234` — `incremental_update` 两阶段模式 + 三元组解包
- Modify: `niu_api/brain_region_api.py:145-216` — `consolidate_brain_regions` 两阶段模式

- [ ] **Step 1: 修复 `_refresh_activation_manager` 空列表保护**

在 `initialize_from_regions` 调用前增加空列表检查：

```python
all_regions = manager.get_all_regions()

# 安全检查：空列表意味着读取失败或图不可用，
# 不应用空列表覆盖现有激活状态（会导致激活系统完全失效）
if not all_regions:
    logger.warning("[RegionSync] get_all_regions 返回空，跳过激活管理器刷新")
    return
```

- [ ] **Step 2: 修复 `_manage_region_nodes` 的非原子性问题**

核心思路：**create 成功后再 cleanup**，而非先删后创。这样即使 create 失败，旧脑区仍然存在。

但 `cleanup_stale_regions` 现在包含漂移更新逻辑（Task 4），漂移更新必须在 create 之前执行（因为漂移脑区的分区需要被 create 跳过）。所以不能简单调换顺序。

正确的方案是**将 cleanup 拆分为"检测 + 标记"和"执行删除"两步**：

```python
# Step 3a: 检测过时和漂移脑区（不执行删除/更新）
cleanup_ok = True
try:
    removed, drifted, drifted_cids = manager.cleanup_stale_regions(
        detection_result, dry_run=True,
    )
except Exception as e:
    logger.warning(f"[RegionSync] cleanup detection failed: {e}")
    removed, drifted, drifted_cids = [], [], set()
    cleanup_ok = False

# Step 4: Create region nodes (skip drifted community partitions)
created: list[str] = []
try:
    created = manager.create_region_nodes(detection_result, skip_community_ids=drifted_cids)
    stats["regions_created"] = len(created)
except Exception as e:
    logger.warning(f"[RegionSync] create_region_nodes failed: {e}")
    stats["errors"].append(f"create: {e}")

# Step 3b: 执行删除和漂移更新（仅在 create 成功且 dry_run 未失败时）
# 如果 create 失败（created 为空且 detection_result 有分区），
# 保留旧脑区，避免数据丢失
# 如果 dry_run 失败（cleanup_ok=False），跳过执行避免重复创建
if (created or not detection_result.partitions) and cleanup_ok:
    try:
        actual_removed, actual_drifted, _ = manager.cleanup_stale_regions(
            detection_result, dry_run=False,
        )
        stats["regions_removed"] = len(actual_removed)
    except Exception as e:
        logger.warning(f"[RegionSync] cleanup execution failed: {e}")
        stats["errors"].append(f"cleanup: {e}")
elif not cleanup_ok:
    logger.warning("[RegionSync] dry_run 失败，跳过 cleanup 执行避免重复创建")
else:
    logger.warning("[RegionSync] create_region_nodes 失败，保留旧脑区避免数据丢失")

# Step 5: Update region summaries (exclude created and drifted)
# created 脑区已有准确 summary（来自分区类型数据），drifted 脑区已由 _update_drifted_regions 更新
# 对它们调用 update_region_summaries 会用空类型映射覆盖精确 summary（D-16 退化）
try:
    if hasattr(manager, "update_region_summaries"):
        all_regions = manager.get_all_regions()
        created_set = set(created)
        drifted_set = set(actual_drifted) if cleanup_ok else set()
        region_names = [r.name for r in all_regions
                        if r.name not in created_set and r.name not in drifted_set]
        manager.update_region_summaries(region_names)
        stats["regions_updated"] = len(region_names)
except Exception as e:
    logger.debug(f"[RegionSync] update_region_summaries skipped: {e}")
```

**注意**：需要为 `cleanup_stale_regions` 新增 `dry_run` 参数：
- `dry_run=True`：只检测（计算 removed/drifted/drifted_cids），不执行删除和漂移更新
- `dry_run=False`（默认）：执行完整逻辑（当前行为）

```python
def cleanup_stale_regions(
    self,
    current_partition: CommunityDetectionResult,
    drift_threshold: float = 0.3,
    dry_run: bool = False,
) -> tuple[list[str], list[str], set[str]]:
```

在 `dry_run=True` 时，跳过 `delete_entity` 和 `_update_drifted_regions` 调用，只返回检测结果。

- [ ] **Step 2.5: 修复 `incremental_update` — 两阶段模式 + 三元组解包**

`region_manager.py:1191-1234` 的 `incremental_update` 当前使用 `removed = self.cleanup_stale_regions(partition)` 单返回值解包，Task 4 改返回值为三元组后这里会 ValueError。改为与 `_manage_region_nodes` 一致的两阶段模式：

```python
def incremental_update(self) -> dict:
    try:
        from niu_api.internal.region_detector import CommunityDetector
        from agent.injector.region_sync import REGION_CONFIG_DEFAULTS

        detector = CommunityDetector(self._adapter)
        partition = detector.detect_communities(
            resolution=REGION_CONFIG_DEFAULTS.get("resolution", 1.0),
            min_graph_size=REGION_CONFIG_DEFAULTS.get("min_graph_size", 50),
            min_community_size=REGION_CONFIG_DEFAULTS.get("min_community_size", 100),
        )
        if partition is None or partition.total_regions < 1:
            return {"regions_created": 0, "regions_removed": 0, "regions_drifted": 0,
                    "regions_updated": 0, "edges_disconnected": 0}

        # 两阶段模式：先检测后执行（解决 D-13 非原子性）
        cleanup_ok = True
        try:
            removed, drifted, drifted_cids = self.cleanup_stale_regions(partition, dry_run=True)
        except Exception as e:
            logger.warning("incremental_update cleanup detection failed: %s", e)
            removed, drifted, drifted_cids = [], [], set()
            cleanup_ok = False

        # Create new regions (skip drifted community partitions)
        created: list[str] = []
        try:
            created = self.create_region_nodes(partition, skip_community_ids=drifted_cids)
        except Exception as e:
            logger.warning("incremental_update create_region_nodes failed: %s", e)

        # Execute cleanup only if create succeeded and dry_run succeeded
        actual_removed, actual_drifted = [], []
        if (created or not partition.partitions) and cleanup_ok:
            try:
                actual_removed, actual_drifted, _ = self.cleanup_stale_regions(partition, dry_run=False)
            except Exception as e:
                logger.warning("incremental_update cleanup execution failed: %s", e)
        elif not cleanup_ok:
            logger.warning("incremental_update dry_run 失败，跳过 cleanup 执行")
        else:
            logger.warning("incremental_update create_region_nodes 失败，保留旧脑区")

        # Update summaries for stable regions (exclude created and drifted)
        all_regions = self.get_all_regions()
        created_set = set(created)
        drifted_set = set(actual_drifted)
        existing_region_names = [r.name for r in all_regions
                                 if r.name not in created_set and r.name not in drifted_set]
        self.update_region_summaries(existing_region_names)

        # Decay structural edges
        disconnected = self._decay_structural_edges(all_regions)

        return {
            "regions_created": len(created),
            "regions_removed": len(actual_removed),
            "regions_drifted": len(actual_drifted),
            "regions_updated": len(existing_region_names),
            "edges_disconnected": disconnected,
        }
    except Exception as e:
        logger.warning("incremental_update failed: %s", e)
        return {"regions_created": 0, "regions_removed": 0, "regions_drifted": 0,
                "regions_updated": 0, "edges_disconnected": 0}
```

- [ ] **Step 2.6: 修复 `consolidate_brain_regions` — 两阶段模式**

`brain_region_api.py:145-216` 的 `consolidate_brain_regions` 当前是先 cleanup 后 create，存在 D-13 非原子性。改为 dry_run 两阶段模式，与 `_manage_region_nodes` 一致：

```python
# Step 1: Detect communities (unchanged)
detection_result = detector.detect_communities(...)

if not detection_result.partitions:
    return {"status": "ok", "message": "No communities detected", "regions_created": 0}

region_mgr = _get_region_mgr()

# Step 2: dry_run detect (Phase 1)
cleanup_ok = True
try:
    removed, drifted, drifted_cids = region_mgr.cleanup_stale_regions(detection_result, dry_run=True)
except Exception as e:
    logger.error("[Consolidate] cleanup detection failed: %s", e)
    removed, drifted, drifted_cids = [], [], set()
    cleanup_ok = False

# Step 3: Create region nodes (Phase 2)
created = region_mgr.create_region_nodes(detection_result, skip_community_ids=drifted_cids)

# Step 4: Execute cleanup only if create succeeded and dry_run succeeded (Phase 3)
if (created or not detection_result.partitions) and cleanup_ok:
    try:
        actual_removed, actual_drifted, _ = region_mgr.cleanup_stale_regions(detection_result, dry_run=False)
        removed = actual_removed
        drifted = actual_drifted
    except Exception as e:
        logger.error("[Consolidate] cleanup execution failed: %s", e)
elif not cleanup_ok:
    logger.warning("[Consolidate] dry_run failed, skipping cleanup execution")
else:
    logger.warning("[Consolidate] create_region_nodes failed, preserving stale regions")
    created = []

# Step 5: Initialize activation manager (with D-12 empty list protection)
activation_mgr = _get_activation_mgr()
if activation_mgr is not None:
    regions = region_mgr.get_all_regions()
    if not regions:
        logger.warning("[Consolidate] get_all_regions returned empty, skipping activation init")
    else:
        from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
        for region in regions:
            try:
                region.members = lightrag_get_region_members(region.name)
            except Exception as exc:
                logger.warning("Failed to fetch members for region %s: %s", region.name, exc)
        activation_mgr.initialize_from_regions(regions)
        from niu_api.internal.region_neighbors import build_neighbor_map
        neighbor_map = build_neighbor_map([
            {"community_id": r.community_id, "members": r.members}
            for r in regions
        ])
        activation_mgr.set_region_neighbors(neighbor_map)
        logger.info("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))

return {
    "status": "ok",
    "regions_created": len(created),
    "regions_removed": len(removed),
    "regions_drifted": len(drifted),
    "total_regions": detection_result.total_regions,
    "modularity": round(detection_result.modularity, 4),
}
```

注意：`brain_region_api.py` 加入 Task 5 的 Files 列表。

- [ ] **Step 3: 验证**

Run: `python -m py_compile agent/injector/region_sync.py && python -m py_compile niu_api/internal/region_manager.py`

---

## Task 6 [IMPORTANT]：API 触发与定时同步无互斥保护

**Bug 根因**：`POST /api/brain/regions/consolidate`（`brain_region_api.py`）与定时 `run_sync()`（`region_sync.py`）可能同时运行。两个线程同时调用 cleanup + create，导致：
- 重复删除脑区
- 重复创建脑区
- KG 数据不一致（两次 `inject_custom_kg` 交叉写入）

**Files:**
- Modify: `agent/injector/region_sync.py` — 添加互斥锁
- Modify: `niu_api/brain_region_api.py` — 使用同一把锁

- [ ] **Step 1: 在 `RegionSync` 中添加互斥锁和公共方法**

```python
class RegionSync:
    def __init__(self, sync_interval: int = 86400) -> None:
        self.sync_interval = sync_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._brain_ready = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_region_sync.json"
        self._sync_lock = threading.Lock()  # 互斥：防止 API 触发与定时同步并发

    def try_acquire_sync(self) -> bool:
        """尝试获取同步锁（非阻塞）。用于防止并发同步。"""
        return self._sync_lock.acquire(blocking=False)

    def release_sync(self) -> None:
        """释放同步锁。"""
        self._sync_lock.release()
```

在 `run_sync` 方法入口获取锁（非阻塞，获取失败时跳过）：

```python
def run_sync(self) -> dict:
    """Execute one full sync cycle."""
    if not self.try_acquire_sync():
        logger.warning("[RegionSync] 另一个同步正在运行，跳过本次")
        return {"regions_created": 0, "regions_removed": 0, "errors": ["skipped: concurrent sync"]}
    try:
        return self._run_sync_impl()
    finally:
        self.release_sync()

def _run_sync_impl(self) -> dict:
    """实际同步逻辑（原 run_sync 内容）"""
    # ... 原有代码不变 ...
```

**注意**：当 `cleanup_stale_regions(dry_run=True)` 失败时，`drifted_cids` 为空集，`create_region_nodes` 不会跳过漂移分区。极端情况下 LLM 可能为漂移分区生成不同标签导致重复脑区。为降低此风险，在 dry_run 失败时用一个标志位跳过后续的 cleanup(dry_run=False)：

```python
# Step 3a: 检测过时和漂移脑区（不执行删除/更新）
cleanup_ok = True
try:
    removed, drifted, drifted_cids = manager.cleanup_stale_regions(
        detection_result, dry_run=True,
    )
except Exception as e:
    logger.warning(f"[RegionSync] cleanup detection failed: {e}")
    removed, drifted, drifted_cids = [], [], set()
    cleanup_ok = False

# ... create ...

# Step 3b: 执行删除和漂移更新（仅在 create 成功且 dry_run 未失败时）
if (created or not detection_result.partitions) and cleanup_ok:
    # ... execute cleanup ...
elif not cleanup_ok:
    logger.warning("[RegionSync] dry_run 失败，跳过 cleanup 执行避免重复创建")
```

- [ ] **Step 2: `brain_region_api.py` 使用同一把锁**

```python
def consolidate_brain_regions(req: ConsolidateRequest = None):
    # ... docstring ...

    # 获取 RegionSync 的互斥锁，防止与定时同步并发
    from agent.injector.region_sync import get_region_sync
    sync = get_region_sync(auto_start=False)
    lock_acquired = False
    if sync is not None and not sync.try_acquire_sync():
        raise HTTPException(
            status_code=409,
            detail="Another brain region sync is in progress. Please try again later.",
        )
    if sync is not None:
        lock_acquired = True
    try:
        # ... 原有 consolidate 逻辑 ...
    finally:
        if lock_acquired:
            sync.release_sync()
```

- [ ] **Step 3: 验证**

Run: `python -m py_compile agent/injector/region_sync.py && python -m py_compile niu_api/brain_region_api.py`

---

## 全局验证

完成所有任务后：

- [ ] **运行完整测试套件**

Run: `python -m pytest tests/test_region_manager.py tests/test_region_detector.py -v`

- [ ] **确认 `incremental_update` 一致性**

验证 `incremental_update` 也受益于 Task 4 的 drift 检测（因为它也调用 `cleanup_stale_regions`）。

- [ ] **同步配置文件到运行目录**

`memory/` 目录下的文件修改后，必须手动同步到运行目录 `~/.niu/`。程序只在文件不存在时自动拷贝，不检查文件变化。

```bash
# 同步 preferences.json（添加了 keywords 字段）
cp memory/preferences.json ~/.niu/preferences.json

# 同步 skills 文件（添加了 keywords 字段说明）
cp memory/skills/brain-region-management.md ~/.niu/skills/brain-region-management.md
```
