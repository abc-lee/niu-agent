# 脑区系统第二批修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复脑区系统第二批问题：D-11（API缺步骤）、D-7扩展（稳定脑区旧边未清）、_decay_structural_edges静默no-op

**Architecture:** 最小修改原则——Task 1 修复底层 no-op，Task 2 在 is_existing 路径增加边管理，Task 3 在 API 端点补齐步骤。按依赖顺序执行。

**Tech Stack:** Python 3.11+, NetworkX (nx.Graph), LightRAG

---

## 依赖关系

```
Task 1 (decay_structural_edges no-op + 公共方法) ──→ Task 3 (D-11) 依赖修复后的衰减方法
Task 2 (D-7 扩展) ──── 独立
Task 3 (D-11) ──→ 依赖 Task 1
Task 4 (Bug 清单更新) ──→ 依赖 Task 1-3/5-6 完成
Task 5 (定时同步补衰减) ──→ 依赖 Task 1 的公共方法
Task 6 (_reinforce_edge_weight no-op) ──── 独立（与 Task 1 互为衰减-强化对，必须同时完成）
```

建议执行顺序：**Task 1 → Task 2 → Task 6 → Task 3 → Task 5 → Task 4**

---

## Task 1: 修复 _decay_structural_edges — 从 no-op 改为直接操作 nx_graph

**问题**：`_decay_structural_edges` 调用 `kg.get_neighbors()` 和 `kg.remove_edge()`，这两个方法在 `NetworkXStorage` 上不存在，导致静默 no-op（永远返回 0）。

**Files:**
- Modify: `niu_api/internal/region_manager.py` — `_decay_structural_edges` 方法改为公共方法 `decay_structural_edges`

- [ ] **Step 1: 替换 `_decay_structural_edges` 方法体**

将当前方法替换为直接操作 `kg._graph` 的实现，与 `remove_region_stale_edges` 的模式一致。同时将方法名从 `_decay_structural_edges` 改为 `decay_structural_edges`（公共方法），消除私有方法调用问题：

```python
def decay_structural_edges(
    self,
    regions: list[BrainRegionInfo],
    decay_factor: float = 0.5,
    threshold: float = 0.1,
) -> int:
    """Decay and disconnect low-weight structural edges.

    Directly operates on the internal NetworkX graph under write lock,
    following the same pattern as remove_region_stale_edges.
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

        nx_graph = kg._graph if hasattr(kg, "_graph") else kg
        if nx_graph is None:
            return 0

        with graph_write_lock():
            for region in regions:
                region_key = region.name.lower() if isinstance(region.name, str) else region.name
                if region_key not in nx_graph:
                    continue

                for neighbor_id in list(nx_graph.neighbors(region_key)):
                    edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                    if edge_data is None:
                        continue
                    keywords = edge_data.get("keywords") or edge_data.get("type", "")
                    kw_lower = keywords.lower()
                    if kw_lower in STRUCTURAL_EDGE_TYPES_LOWER or kw_lower.startswith("_session:"):
                        old_weight = float(edge_data.get("weight", 0.5))
                        new_weight = old_weight * decay_factor
                        if new_weight < threshold:
                            nx_graph.remove_edge(region_key, neighbor_id)
                            disconnected += 1
                        else:
                            edge_data["weight"] = new_weight
    except Exception as e:
        logger.warning("Edge decay failed: %s", e)

    return disconnected
```

**注意**：`STRUCTURAL_EDGE_TYPES_LOWER` 已在第 63 行定义为 `frozenset`，无需添加。

- [ ] **Step 2: 更新内部调用者**

`incremental_update` 方法中调用 `self._decay_structural_edges(...)` 改为 `self.decay_structural_edges(...)`。

```bash
grep -n "_decay_structural_edges" niu_api/internal/region_manager.py
# 预期：只剩注释或文档引用，方法定义已改为 decay_structural_edges
```

- [ ] **Step 3: 验证修改**

```bash
grep -n "kg.get_neighbors\|kg.remove_edge" niu_api/internal/region_manager.py
# 预期：无匹配（已全部替换为 nx_graph 操作）

python -m py_compile niu_api/internal/region_manager.py
```

- [ ] **Step 4: Commit**

```bash
git add niu_api/internal/region_manager.py
git commit -m "fix(brain-region): decay_structural_edges uses nx_graph directly + rename to public method (batch2 Task 1)"
```

---

## Task 2: D-7 扩展 — create_region_nodes 对稳定脑区清理旧包含边并重新注入

**问题**：`create_region_nodes` 的 `is_existing` 路径跳过所有关系注入（只更新 entity 描述）。如果稳定脑区的成员集发生变化（某些成员移出了社区），旧的"包含"边不会被删除，导致实体出现在多个脑区的成员列表中。

**Files:**
- Modify: `niu_api/internal/region_manager.py` — `create_region_nodes` 方法

- [ ] **Step 1: 在方法开头初始化 `stale_edge_cleanup` 列表**

在 `created_regions: list[str] = []` 之后添加：

```python
stale_edge_cleanup: list[tuple[str, set[str]]] = []  # (region_name, new_members_set)
```

- [ ] **Step 2: 替换 `is_existing` 路径的逻辑**

将当前的：
```python
if is_existing:
    logger.info("跳过已存在脑区的关系注入: %s (只更新描述)", region_name)
    continue
```

替换为：

```python
if is_existing:
    # D-7 fix: For stable regions with changed membership,
    # inject new edges first then remove stale edges (same
    # inject-before-delete pattern as _update_drifted_regions).
    # Skip only when membership is identical.
    current_members = {m.lower() if isinstance(m, str) else m for m in self.get_region_members(region_name)}
    new_members_lower = {m.lower() if isinstance(m, str) else m for m in members}
    if current_members == new_members_lower:
        logger.debug("稳定脑区成员未变: %s", region_name)
        continue

    # Members changed — inject new edges for members not yet in graph
    added_members = new_members_lower - current_members
    if added_members:
        for member in members:
            if (member.lower() if isinstance(member, str) else member) not in current_members:
                all_relationships.append({
                    "src_id": region_name,
                    "tgt_id": member,
                    "keywords": BELONGS_TO_RELATION,
                    "description": f"{member} belongs to region {region_label}",
                    "weight": 0.5,
                    "source_id": REGION_SOURCE_ID,
                    "file_path": REGION_FILE_PATH,
                })
        logger.info(
            "稳定脑区成员变更: %s (+%d 成员)",
            region_name, len(added_members),
        )
    # Track stale edge removal (execute after batch inject)
    removed_members = current_members - new_members_lower
    if removed_members:
        stale_edge_cleanup.append((region_name, set(members)))
        logger.info(
            "稳定脑区成员变更: %s (-%d 旧成员, 将在注入后清理)",
            region_name, len(removed_members),
        )
    continue
```

- [ ] **Step 3: 在批量注入之后添加旧边清理逻辑**

**关键**：`stale_edge_cleanup` 必须在批量注入成功后才执行（inject-before-delete 原则）。如果注入失败，清空 `stale_edge_cleanup` 避免删除旧边。

在批量注入的 `inject_custom_kg` 调用处，修改错误处理：

```python
if all_entities or all_relationships:
    result = self._ingester.inject_custom_kg(
        entities=all_entities, relationships=all_relationships,
        chunks=all_chunks, source_id=REGION_SOURCE_ID,
    )
    if isinstance(result, dict) and result.get("status") == "error":
        logger.warning("脑区批量注入失败: %s", result.get("message", ""))
        stale_edge_cleanup.clear()  # 注入失败，不清理旧边
        return []
```

然后在 `logger.info("共创建 %d 个脑区节点", len(created_regions))` 之前添加：

```python
# D-7 fix: Remove stale "包含" edges for stable regions with changed membership
# Execute AFTER batch inject to follow inject-before-delete pattern
if stale_edge_cleanup:
    from niu_api.internal.lightrag_manager import remove_region_stale_edges
    for region_name, new_members in stale_edge_cleanup:
        try:
            removed_count = remove_region_stale_edges(
                region_name, BELONGS_TO_RELATION, new_members,
            )
            if removed_count > 0:
                logger.info(
                    "稳定脑区旧边清理: %s 移除 %d 条过期包含边",
                    region_name, removed_count,
                )
        except Exception as e:
            logger.warning(
                "稳定脑区旧边清理失败: %s — %s (继续处理其他脑区)",
                region_name, e,
            )
```

- [ ] **Step 4: 验证修改**

```bash
python -m py_compile niu_api/internal/region_manager.py
```

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/region_manager.py
git commit -m "fix(brain-region): create_region_nodes cleans stale edges for stable regions with changed membership (batch2 Task 2, D-7)"
```

---

## Task 3: D-11 — API consolidate_brain_regions 补齐合并/解散/衰减步骤

**问题**：API 触发的 `consolidate_brain_regions` 缺少定时同步路径中的多个步骤（assign_entities、update_summaries、merge、dissolve、decay、invalidate_cache）。

**Files:**
- Modify: `niu_api/brain_region_api.py` — `consolidate_brain_regions` 函数

- [ ] **Step 1: 在 Step 4（执行清理）之后添加 assign_entities 和 update_summaries**

在 Step 4 的 cleanup 执行逻辑之后、Step 5（初始化激活管理器）之前，插入：

```python
            # Step 4.5: Assign existing entities to default brain regions
            try:
                from niu_api.internal.region_manager import assign_entities_to_default_regions
                result = assign_entities_to_default_regions(adapter)
                assigned = result.get("assigned", 0)
                if assigned > 0:
                    logger.info("[Consolidate] Assigned %d entities to default regions", assigned)
            except Exception as e:
                logger.debug("[Consolidate] assign_entities_to_default_regions skipped: %s", e)

            # Step 5: Update region summaries (exclude created and drifted)
            try:
                all_regions = region_mgr.get_all_regions()
                created_set = set(created)
                drifted_set = set(drifted) if cleanup_ok else set()
                region_names = [r.name for r in all_regions
                                if r.name not in created_set and r.name not in drifted_set]
                region_mgr.update_region_summaries(region_names)
            except Exception as e:
                logger.debug("[Consolidate] update_region_summaries skipped: %s", e)
```

- [ ] **Step 2: 在初始化激活管理器之后、return 之前，添加合并/解散/衰减/缓存失效**

```python
            # Step 6: Merge co-activated regions
            regions_merged = 0
            try:
                if activation_mgr is not None:
                    from niu_api.internal.region_manager import is_default_region
                    candidates = activation_mgr.get_merge_candidates(
                        co_activation_threshold=REGION_CONFIG_DEFAULTS.get("co_activation_threshold", 0.9),
                    )
                    if candidates:
                        for source_id, target_id in candidates:
                            source_state = activation_mgr.get_region_state(source_id)
                            target_state = activation_mgr.get_region_state(target_id)
                            if source_state is None or target_state is None:
                                continue
                            if is_default_region(source_state.region_id):
                                continue
                            if is_default_region(target_state.region_id):
                                continue
                            try:
                                source_name = f"{source_state.label}脑区"
                                target_name = f"{target_state.label}脑区"
                                result = adapter.merge_entities(
                                    source_entities=[source_name],
                                    target_entity=target_name,
                                )
                                if isinstance(result, dict) and result.get("status") == "ok":
                                    regions_merged += 1
                                    activation_mgr.merge_region_into(source_id, target_id)
                                    logger.info(
                                        "[Consolidate] 合并脑区: %s -> %s",
                                        source_state.label, target_state.label,
                                    )
                            except Exception as e:
                                logger.debug("[Consolidate] merge_entities failed: %s", e)
            except Exception as e:
                logger.debug("[Consolidate] Merge check skipped: %s", e)

            # Step 7: Dissolve shrunk regions
            regions_dissolved = 0
            try:
                dissolved = region_mgr.dissolve_shrunk_regions(
                    shrink_threshold=REGION_CONFIG_DEFAULTS.get("shrink_threshold", 100),
                    shrink_rounds=REGION_CONFIG_DEFAULTS.get("shrink_rounds", 3),
                )
                regions_dissolved = len(dissolved)
                if dissolved and activation_mgr is not None:
                    for region_name in dissolved:
                        label = region_name.removesuffix("脑区")
                        try:
                            state = activation_mgr.find_region_by_label(label)
                            if state is not None:
                                activation_mgr.remove_region(state.region_id)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("[Consolidate] Dissolve check skipped: %s", e)

            # Step 8: Decay structural edges
            edges_disconnected = 0
            try:
                all_regions_for_decay = region_mgr.get_all_regions()
                if all_regions_for_decay:
                    edges_disconnected = region_mgr.decay_structural_edges(all_regions_for_decay)
            except Exception as e:
                logger.debug("[Consolidate] Edge decay skipped: %s", e)

            # Step 9: Invalidate cached tool-to-region mapping
            try:
                from agent.brain_tools import invalidate_tool_to_region
                invalidate_tool_to_region()
            except Exception:
                pass
```

- [ ] **Step 3: 更新返回值，添加新统计字段**

在返回值中添加：

```python
            return {
                "status": "ok",
                "regions_created": len(created),
                "regions_removed": len(removed),
                "regions_drifted": len(drifted),
                "regions_merged": regions_merged,
                "regions_dissolved": regions_dissolved,
                "edges_disconnected": edges_disconnected,
                "total_regions": detection_result.total_regions,
                "modularity": round(detection_result.modularity, 4),
            }
```

- [ ] **Step 4: 验证修改**

```bash
python -m py_compile niu_api/brain_region_api.py
```

- [ ] **Step 5: Commit**

```bash
git add niu_api/brain_region_api.py
git commit -m "fix(brain-region): consolidate_brain_regions adds merge/dissolve/decay steps (batch2 Task 3, D-11)"
```

---

## Task 4: 更新已知 Bug 清单

**Files:**
- Modify: `<claude_memory_dir>/brain-region-known-bugs.md`

- [ ] **Step 1: 标记 D-11 和 D-7 扩展为已修复，添加 _decay no-op 修复记录，添加 _reinforce_edge_weight no-op 为新已知问题**

在"已修复"部分添加：

```
- **D-11** ✅ — `consolidate_brain_regions` 补齐 assign/summary/merge/dissolve/decay/invalidate 步骤（batch2 Task 3）
- **D-7 扩展** ✅ — `create_region_nodes` 对稳定脑区清理旧包含边并重新注入（batch2 Task 2）
- **_decay_structural_edges no-op** ✅ — 改为公共方法 `decay_structural_edges`，使用 nx_graph 直接操作替代不存在的 kg.get_neighbors()/kg.remove_edge()（batch2 Task 1）
- **_reinforce_edge_weight no-op** ✅ — 改用 nx_graph 直接操作替代不存在的 kg.get_neighbors()（batch2 Task 6，与 Task 1 互为衰减-强化对）
```

从"未修复 > 中等"中移除 D-11 和 D-7 扩展的条目。

不再需要在"低"部分添加 `_reinforce_edge_weight no-op`，因为已在本次修复。

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "docs(brain-region): update known bugs — mark D-11/D-7 as fixed, add reinforce no-op (batch2)"
```

---

## Task 5: 定时同步路径补齐衰减步骤

**问题**：`region_sync.py` 的 `_run_sync_impl` 没有调用 `decay_structural_edges`，导致定时同步路径不执行边衰减。与 API 路径（Task 3 修复后）不一致。

**Files:**
- Modify: `agent/injector/region_sync.py` — `_manage_region_nodes` 方法

- [ ] **Step 1: 在 `_manage_region_nodes` 的 Step 5（update_region_summaries）之后添加衰减步骤**

在 `update_region_summaries` 的 try/except 块之后、统计信息返回之前，插入：

```python
            # Step 6: Decay structural edges
            try:
                all_regions_for_decay = manager.get_all_regions()
                if all_regions_for_decay:
                    disconnected = manager.decay_structural_edges(all_regions_for_decay)
                    if disconnected > 0:
                        stats["edges_disconnected"] = disconnected
                        logger.info("[RegionSync] 衰减断开 %d 条结构性边", disconnected)
            except Exception as e:
                logger.debug("[RegionSync] Edge decay skipped: %s", e)
```

- [ ] **Step 2: 验证修改**

```bash
python -m py_compile agent/injector/region_sync.py
```

- [ ] **Step 3: Commit**

```bash
git add agent/injector/region_sync.py
git commit -m "fix(brain-region): region_sync adds decay_structural_edges step (batch2 Task 5)"
```

---

## Task 6: 修复 _reinforce_edge_weight — 从 no-op 改为直接操作 nx_graph

**问题**：`brain_tools.py` 的 `_reinforce_edge_weight` 调用 `kg.get_neighbors()`（`NetworkXStorage` 没有此方法），`AttributeError` 被静默捕获导致 no-op。没有强化的衰减会在 3 次同步后（72 小时）断开所有结构性边，脑区系统结构性崩溃。必须与 Task 1 的衰减修复同时完成，否则衰减-强化动态平衡失效。

**Files:**
- Modify: `agent/brain_tools.py` — `_reinforce_edge_weight` 函数

- [ ] **Step 1: 替换 `_reinforce_edge_weight` 函数体**

将当前函数替换为直接操作 `kg._graph` 的实现，与 Task 1 的 `decay_structural_edges` 模式一致：

```python
def _reinforce_edge_weight(region_id: str, delta: float = REINFORCE_DELTA) -> None:
    """Boost weight of structural edges for a brain region node.

    Directly operates on the internal NetworkX graph under write lock,
    following the same pattern as decay_structural_edges.
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        from niu_api.internal.lightrag_manager import graph_write_lock

        adapter = LightRAGAdapter()
        rag = adapter._get_rag()
        if rag is None:
            return

        kg = rag.chunk_entity_relation_graph
        if kg is None:
            return

        nx_graph = kg._graph if hasattr(kg, "_graph") else kg
        if nx_graph is None:
            return

        region_key = region_id.lower() if isinstance(region_id, str) else region_id

        with graph_write_lock():
            if region_key not in nx_graph:
                return

            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                keywords = edge_data.get("keywords") or edge_data.get("type", "")
                kw_lower = keywords.lower()
                if kw_lower in STRUCTURAL_EDGE_TYPES_LOWER or kw_lower.startswith("_session:"):
                    old_weight = float(edge_data.get("weight", 0.5))
                    new_weight = min(MAX_EDGE_WEIGHT, old_weight + delta)
                    if new_weight > old_weight:
                        edge_data["weight"] = new_weight
                        logger.debug(
                            "Edge weight reinforced: %s -> %s (%s): %.2f -> %.2f",
                            region_key, neighbor_id, keywords, old_weight, new_weight,
                        )
    except Exception as e:
        logger.debug("Edge weight reinforce failed: %s", e)
```

- [ ] **Step 2: 验证修改**

```bash
grep -n "kg.get_neighbors\|kg.get_node" agent/brain_tools.py
# 预期：无匹配（已全部替换为 nx_graph 操作）

python -m py_compile agent/brain_tools.py
```

- [ ] **Step 3: Commit**

```bash
git add agent/brain_tools.py
git commit -m "fix(brain-region): _reinforce_edge_weight uses nx_graph directly — restores decay-reinforce balance (batch2 Task 6)"
```

---

## 全局验证

完成所有任务后：

- [ ] **语法检查**

```bash
python -m py_compile niu_api/internal/region_manager.py && python -m py_compile niu_api/brain_region_api.py
```

- [ ] **运行测试**

```bash
python -m pytest tests/test_region_manager.py -v
```

- [ ] **端到端验证**（真实数据 + 真实 LLM）

1. 启动完整服务
2. 触发 API 整合：`curl -X POST http://localhost:9876/api/brain/regions/consolidate`
3. 检查返回值包含 `regions_merged`、`regions_dissolved`、`edges_disconnected` 字段
4. 检查日志确认新步骤执行
5. 验证 `decay_structural_edges` 不再返回 0（如果有脑区相关边）
