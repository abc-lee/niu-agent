# 脑区实体数实时刷新 + 统一事件驱动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 入库完成后，脑区实体数和聊天窗口统计数自动刷新，去掉聊天窗口的 60 秒盲轮询。

**Architecture:** 在 `_notify_ingest_completed()` 中触发轻量级实体映射刷新（`refresh_entity_mapping()`），该方法只更新 `_entity_to_region` 和 `_entity_type_counts` 内存缓存，不跑社区检测。前端 `ingest-completed` 事件回调中增加 `loadStats()`，去掉 `setInterval` 盲轮询。后端 `/api/stats` 从 `_entity_type_counts` 缓存读取，不再每次复制整图。

**Tech Stack:** Python（region_activation.py, kg_api.py, compat.py）+ JavaScript（chat.html）

---

## 审计发现

| 现状 | 问题 |
|------|------|
| 脑区 `_entity_to_region` 只在 RegionSync 的 `run_sync()` 中重建 | 每 24 小时才刷新一次 |
| 聊天窗口 `setInterval(loadStats, 60000)` | 60 秒盲轮询，即使没有变化也请求 |
| `/api/stats` 每次调用 `nx_graph.copy()` 后遍历全图 | 图大时开销显著（复制+遍历 O(N)） |
| `ingest-completed` SSE 事件已推送到前端 | 前端只隐藏进度条，不刷新统计 |
| `/api/stats` 的 persons/notes 计数与脑区实体数是同一数据源 | 但读取方式不同，无法复用缓存 |

---

## 目标状态

- 入库完成 → 一次事件同时刷新 `_entity_to_region`（脑区实体数）和 `_entity_type_counts`（聊天窗口统计数）
- 前端 `ingest-completed` 回调中调用 `loadStats()`，去掉 `setInterval` 盲轮询
- `/api/stats` 读 `_entity_type_counts` 缓存（O(1)），不再复制整图
- RegionSync 24 小时全量同步仍保留，新增的轻量刷新是增量补充

---

## Task 1: region_activation.py — 新增 `refresh_entity_mapping()` 和 `_entity_type_counts`

**Files:**
- Modify: `niu_api/internal/region_activation.py:112,127,188`

- [ ] **Step 1: 在 `__init__` 中新增 `_entity_type_counts` 字段**

在第 127 行 `self._member_counts: dict[str, int] = {}` 之后添加：

```python
        # Cached entity type counts (for /api/stats, updated on refresh)
        self._entity_type_counts: dict[str, int] = {}
```

- [ ] **Step 2: 在 `initialize_from_regions()` 末尾更新 `_entity_type_counts`**

在第 187 行（`logger.info` 之前）添加：

```python
            # Rebuild entity type counts from entity_type stored in region members
            self._entity_type_counts = self._compute_entity_type_counts(regions)
```

- [ ] **Step 3: 添加 `_compute_entity_type_counts()` 私有方法**

在 `initialize_from_regions()` 方法之后（约第 188 行后）添加：

```python
    def _compute_entity_type_counts(self, regions: list[BrainRegionInfo]) -> dict[str, int]:
        """Compute entity type counts from regions' member type info.

        Returns dict like {"person": 5, "note": 3, ...}.
        Falls back to counting from _entity_to_region if type info unavailable.
        """
        counts: dict[str, int] = {}
        for region in regions:
            for member in region.members:
                # BrainRegionInfo.members is list[str] of entity names.
                # Entity type is not carried in members list.
                # We'll need to query the graph for type info.
                pass
        return counts
```

**等等** — `_entity_type_counts` 不能从 `BrainRegionInfo.members`（只有名字没有类型）获取。需要从 NetworkX 图直接读取。改为在 `refresh_entity_mapping()` 中从图读取类型。

**修正方案**：不在 `initialize_from_regions` 中计算 `_entity_type_counts`，而是新增一个独立的 `refresh_entity_mapping()` 方法，该方法：
1. 从 NetworkX 图读 `_region:contains` 边更新 `_entity_to_region`
2. 从 NetworkX 图读节点 `entity_type` 更新 `_entity_type_counts`

- [ ] **Step 3（修正）: 在 `initialize_from_regions()` 方法之后添加 `refresh_entity_mapping()` 方法**

```python
    def refresh_entity_mapping(self) -> None:
        """Lightweight refresh of entity-to-region mapping and type counts.

        Reads _region:contains edges and node entity_types from the NetworkX
        graph, then updates _entity_to_region, _member_counts, and
        _entity_type_counts. Preserves activation state, neighbors, and
        co-activation data.

        Uses double-buffering: builds new mappings in temporary dicts first,
        then atomically swaps under self._lock to avoid readers seeing
        empty mappings during rebuild.

        Called after ingest completes — much cheaper than full run_sync().
        """
        from niu_api.internal.lightrag_manager import (
            get_all_region_members,
            graph_read_lock,
            get_lightrag,
        )

        try:
            # 1. Build new _entity_to_region and _member_counts (outside lock)
            all_members = get_all_region_members()
            new_entity_to_region: dict[str, str] = {}
            new_member_counts: dict[str, int] = {}
            for region_name, members in all_members.items():
                for member_name in members:
                    new_entity_to_region[member_name] = region_name
                new_member_counts[region_name] = len(members)

            # 2. Build new _entity_type_counts from node attributes (outside lock)
            new_entity_type_counts: dict[str, int] = {}
            rag = get_lightrag()
            if rag is not None:
                graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
                if graph_obj is not None:
                    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                    if nx_graph is not None:
                        with graph_read_lock():
                            snapshot = nx_graph.copy()
                        for node_name in snapshot.nodes():
                            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                            entity_type = attrs.get("entity_type", "").lower()
                            if entity_type:
                                new_entity_type_counts[entity_type] = new_entity_type_counts.get(entity_type, 0) + 1

            # 3. Atomic swap under self._lock (no clear() — readers never see empty)
            with self._lock:
                self._entity_to_region = new_entity_to_region
                self._member_counts = new_member_counts
                self._entity_type_counts = new_entity_type_counts

            logger.info(
                "实体映射刷新完成: %d 个实体映射, %d 种类型",
                len(new_entity_to_region),
                len(new_entity_type_counts),
            )
        except Exception as e:
            logger.warning("实体映射刷新失败: %s", e)
```

- [ ] **Step 4: 添加 `get_entity_type_counts()` 公开方法**

在 `get_members_of_region()` 方法之后（约第 451 行后）添加：

```python
    def get_entity_type_counts(self) -> dict[str, int]:
        """Get cached entity type counts (e.g., {"person": 5, "note": 3}).

        Returns the _entity_type_counts cache populated by refresh_entity_mapping().
        If cache is empty (not yet refreshed), returns empty dict — caller should
        fall back to graph traversal.
        """
        with self._lock:
            return dict(self._entity_type_counts)
```

- [ ] **Step 5: 验证 Python import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.internal.region_activation import RegionActivationManager; print('import ok')"`

Expected: `import ok`

- [ ] **Step 6: Commit**

```bash
git add niu_api/internal/region_activation.py
git commit -m "feat: add refresh_entity_mapping() and _entity_type_counts to RegionActivationManager"
```

---

## Task 2: region_sync.py — 新增 `refresh_entity_mapping_only()` 便捷方法

**Files:**
- Modify: `agent/injector/region_sync.py:340`

- [ ] **Step 1: 在 `_refresh_activation_manager()` 方法之后添加便捷方法**

在第 339 行（`_refresh_activation_manager` 结束）之后添加：

```python
    def refresh_entity_mapping_only(self) -> None:
        """Lightweight refresh: only update entity-to-region mapping and type counts.

        Does NOT run community detection, create/remove regions, or merge/dissolve.
        Much cheaper than run_sync() — intended for calling after ingest completes.

        Safe to call from any thread (uses RLock internally).
        """
        try:
            from agent.brain_tools import get_activation_mgr

            activation_mgr = get_activation_mgr()
            if activation_mgr is not None:
                activation_mgr.refresh_entity_mapping()
                logger.info("[RegionSync] Entity mapping refreshed (lightweight)")
            else:
                logger.debug("[RegionSync] No activation manager, skipping entity mapping refresh")
        except Exception as e:
            logger.warning("[RegionSync] Entity mapping refresh failed: %s", e)
```

- [ ] **Step 2: 在 `initialize_from_regions()` 末尾更新 `_entity_type_counts`**

在 `region_activation.py` 的 `initialize_from_regions()` 方法中，第 179 行 `self._member_counts[region.name] = len(region.members)` 之后、第 181 行 `preserved_count = ...` 之前添加：

```python
                # Update entity type counts from graph (for /api/stats cache)
                self._entity_type_counts = self._build_entity_type_counts()
```

这确保 RegionSync 24h 全量同步后 `_entity_type_counts` 也被更新，与 `_entity_to_region` 保持一致。不需要额外调用 `refresh_entity_mapping()`（避免重复读图）。

- [ ] **Step 3: 添加 `_build_entity_type_counts()` 私有方法**

在 `refresh_entity_mapping()` 方法之前添加：

```python
    def _build_entity_type_counts(self) -> dict[str, int]:
        """Build entity type counts from NetworkX graph node attributes.

        Called from initialize_from_regions() to populate _entity_type_counts
        without requiring a separate refresh_entity_mapping() call.
        """
        try:
            from niu_api.internal.lightrag_manager import graph_read_lock, get_lightrag

            counts: dict[str, int] = {}
            rag = get_lightrag()
            if rag is not None:
                graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
                if graph_obj is not None:
                    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                    if nx_graph is not None:
                        with graph_read_lock():
                            snapshot = nx_graph.copy()
                        for node_name in snapshot.nodes():
                            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                            entity_type = attrs.get("entity_type", "").lower()
                            if entity_type:
                                counts[entity_type] = counts.get(entity_type, 0) + 1
            return counts
        except Exception as e:
            logger.debug("_build_entity_type_counts failed: %s", e)
            return {}
```

- [ ] **Step 4: 在 `region_sync.py` 顶部确认已有 `get_region_sync` 导出**

Run: `grep -n "get_region_sync\|def get_region_sync" REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py`

确认存在 `get_region_sync()` 单例获取方法。如果不存在，需要添加。

- [ ] **Step 4: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.injector.region_sync import RegionSync; print('import ok')"`

Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add agent/injector/region_sync.py
git commit -m "feat: add refresh_entity_mapping_only() lightweight refresh to RegionSync"
```

---

## Task 3: kg_api.py — 入库完成后触发实体映射刷新

**Files:**
- Modify: `niu_api/kg_api.py:412-423`

- [ ] **Step 1: 在 `_notify_ingest_completed()` 中添加实体映射刷新调用**

当前代码（第 412-423 行）：

```python
def _notify_ingest_completed() -> None:
    """Push an ingest-completed SSE event to all connected clients."""
    from niu_api.chat import _main_loop, _sync_broadcast

    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    event = {"type": "ingest-completed"}
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass
```

替换为：

```python
def _notify_ingest_completed() -> None:
    """Push an ingest-completed SSE event to all connected clients.

    Also triggers a lightweight refresh of entity-to-region mapping so that
    brain region entity counts and /api/stats reflect the newly ingested data.
    """
    from niu_api.chat import _main_loop, _sync_broadcast

    loop = _main_loop
    if loop is not None and not loop.is_closed():
        event = {"type": "ingest-completed"}
        try:
            loop.call_soon_threadsafe(_sync_broadcast, event)
        except RuntimeError:
            pass

    # Refresh entity mapping in a background thread (non-blocking)
    # This updates _entity_to_region (brain region counts) and
    # _entity_type_counts (/api/stats) without a full run_sync().
    try:
        from agent.injector.region_sync import get_region_sync
        import threading

        sync = get_region_sync()
        if sync is not None:
            t = threading.Thread(
                target=sync.refresh_entity_mapping_only,
                name="entity-mapping-refresh",
                daemon=True,
            )
            t.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("Entity mapping refresh trigger failed: %s", e)
```

- [ ] **Step 2: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.kg_api import _notify_ingest_completed; print('import ok')"`

Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add niu_api/kg_api.py
git commit -m "feat: trigger entity mapping refresh on ingest completion"
```

---

## Task 3.5: region_sync.py — 启动时预热 `_entity_type_counts` 缓存

**Files:**
- Modify: `agent/injector/region_sync.py:517-548`

- [ ] **Step 1: 在 `_sync_loop` 首次 `run_sync()` 完成后调用 `refresh_entity_mapping()`**

当前代码在首次 `run_sync()` 完成后没有额外操作。在首次 `run_sync()` 的 `try` 块末尾添加实体映射刷新，确保启动后 `_entity_type_counts` 缓存立即可用。

找到 `_sync_loop` 中首次 `run_sync()` 调用（约第 536 行 `self.run_sync()`），在其之后添加：

```python
            # Warm up _entity_type_counts cache so /api/stats doesn't
            # fall back to graph traversal on first call
            try:
                from agent.brain_tools import get_activation_mgr
                activation_mgr = get_activation_mgr()
                if activation_mgr is not None:
                    activation_mgr.refresh_entity_mapping()
                    logger.info("[RegionSync] Entity type counts cache warmed up")
            except Exception as e:
                logger.debug("[RegionSync] Cache warmup failed (non-critical): %s", e)
```

**注意**：`get_activation_mgr` 已在 `_refresh_activation_manager()` 中 import，此处需要确保它在 `_sync_loop` 中也可用。检查文件顶部的 import 或在方法内部 import。

- [ ] **Step 2: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.injector.region_sync import RegionSync; print('import ok')"`

Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add agent/injector/region_sync.py
git commit -m "feat: warm up entity type counts cache after first RegionSync"
```

---

## Task 4: compat.py — `/api/stats` 从缓存读取实体类型计数

**Files:**
- Modify: `niu_api/compat.py:427-470`

- [ ] **Step 1: 重写 `/api/stats` 中的 persons/notes 计数逻辑**

当前代码（第 448-466 行）：

```python
        # Person and note counts: traverse NetworkX graph by entity_type
        rag = adapter._get_rag()
        if rag is not None:
            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            if graph_obj is not None:
                nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                if nx_graph is not None:
                    from niu_api.internal.lightrag_manager import graph_read_lock

                    with graph_read_lock():
                        snapshot = nx_graph.copy()

                    for node_name in snapshot.nodes():
                        attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                        entity_type = attrs.get("entity_type", "").lower()
                        if entity_type == "person":
                            persons += 1
                        elif entity_type in ("note", "knowledge"):
                            notes += 1
```

替换为：

```python
        # Person and note counts: from cached entity type counts (O(1))
        try:
            from agent.brain_tools import get_activation_mgr

            activation_mgr = get_activation_mgr()
            if activation_mgr is not None:
                type_counts = activation_mgr.get_entity_type_counts()
                if type_counts:
                    persons = type_counts.get("person", 0)
                    notes = type_counts.get("note", 0) + type_counts.get("knowledge", 0)
                else:
                    # Cache empty (not yet refreshed), fall back to graph traversal
                    persons, notes = _count_entities_from_graph(adapter)
            else:
                persons, notes = _count_entities_from_graph(adapter)
        except Exception:
            persons, notes = _count_entities_from_graph(adapter)
```

- [ ] **Step 2: 添加 `_count_entities_from_graph()` 回退函数**

在 `get_stats()` 函数之前（约第 427 行之前）添加：

```python
def _count_entities_from_graph(adapter) -> tuple[int, int]:
    """Fallback: count persons and notes by traversing NetworkX graph.

    Used when _entity_type_counts cache is empty (before first refresh).
    """
    persons = 0
    notes = 0
    try:
        rag = adapter._get_rag()
        if rag is not None:
            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            if graph_obj is not None:
                nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                if nx_graph is not None:
                    from niu_api.internal.lightrag_manager import graph_read_lock

                    with graph_read_lock():
                        snapshot = nx_graph.copy()

                    for node_name in snapshot.nodes():
                        attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                        entity_type = attrs.get("entity_type", "").lower()
                        if entity_type == "person":
                            persons += 1
                        elif entity_type in ("note", "knowledge"):
                            notes += 1
    except Exception:
        pass
    return persons, notes
```

- [ ] **Step 3: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.compat import get_stats; print('import ok')"`

Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "perf: /api/stats reads entity counts from cache, falls back to graph traversal"
```

---

## Task 5: chat.html — 事件驱动刷新 + 去掉盲轮询

**Files:**
- Modify: `ui/assistant/chat.html:1165-1168,1310-1311`

- [ ] **Step 1: 在 `ingest-completed` 回调中增加 `loadStats()` 调用**

当前代码（第 1165-1168 行）：

```javascript
    window.electronAPI.onIngestCompleted(() => {
      console.log('[Chat] ingest-completed SSE event received');
      hideProgress();
      stopIngestPolling();
    });
```

替换为：

```javascript
    window.electronAPI.onIngestCompleted(() => {
      console.log('[Chat] ingest-completed SSE event received');
      hideProgress();
      stopIngestPolling();
      loadStats();
    });
```

- [ ] **Step 2: 去掉 `setInterval` 盲轮询**

当前代码（第 1310-1311 行）：

```javascript
      loadStats();
      setInterval(loadStats, 60000);
```

替换为：

```javascript
      loadStats();
```

页面初始化时调用一次 `loadStats()` 获取初始值，后续由 `ingest-completed` 事件驱动刷新。

- [ ] **Step 3: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "feat: event-driven stats refresh on ingest-completed, remove 60s polling"
```

---

## Task 6: 验证 — 全链路测试

**Files:**
- No code changes, verification only

- [ ] **Step 1: 验证 `refresh_entity_mapping()` 可正常调用**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "
from niu_api.internal.region_activation import RegionActivationManager
mgr = RegionActivationManager()
mgr.refresh_entity_mapping()
print(f'entity_to_region: {len(mgr._entity_to_region)} entries')
print(f'entity_type_counts: {mgr._entity_type_counts}')
print(f'member_counts: {mgr._member_counts}')
"
```

Expected: 无异常，输出映射数量和类型计数

- [ ] **Step 1.5: 验证启动时缓存预热逻辑**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "
from agent.injector.region_sync import RegionSync
print('RegionSync import ok — cache warmup code path exists')
"
```

Expected: `RegionSync import ok — cache warmup code path exists`

- [ ] **Step 2: 验证 `/api/stats` 优先从缓存读取**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "
from niu_api.compat import _count_entities_from_graph
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
persons, notes = _count_entities_from_graph(adapter)
print(f'Graph traversal: persons={persons}, notes={notes}')
"
```

Expected: 无异常，输出计数

- [ ] **Step 3: 验证 `_notify_ingest_completed` 中线程启动正常**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "
from niu_api.kg_api import _notify_ingest_completed
# 不实际调用（需要 SSE loop），只验证 import
print('import ok')
"
```

Expected: `import ok`

- [ ] **Step 4: 验证 chat.html 不再有 setInterval 盲轮询**

Run: `grep -n "setInterval.*loadStats" REDACTED_USER_PATH/tools/ai-bot/ui/assistant/chat.html`

Expected: 无匹配（盲轮询已移除）

- [ ] **Step 5: 验证 chat.html 在 ingest-completed 回调中有 loadStats**

Run: `grep -A3 "onIngestCompleted" REDACTED_USER_PATH/tools/ai-bot/ui/assistant/chat.html`

Expected: 回调中包含 `loadStats()` 调用

- [ ] **Step 6: 手动验证完整流程**

1. 启动程序
2. 入库一个文档
3. 等待 pipeline 完成（进度条消失）
4. 检查聊天窗口左下角统计数据是否更新
5. 检查脑区实体数是否更新
6. 确认不再有 60 秒轮询（浏览器 DevTools Network 面板无周期性 /api/stats 请求）

---

## 执行顺序

```
Task 1 (region_activation.py)  ──>  Task 2 (region_sync.py)  ──>  Task 3 (kg_api.py)
Task 1 (region_activation.py)  ──>  Task 3.5 (region_sync.py cache warmup)
Task 4 (compat.py)             （依赖 Task 1 的 get_entity_type_counts）
Task 5 (chat.html)             （独立，前端改动）
Task 6 (验证)                   ──>  所有上述 Task
```

Task 1→2→3 必须按顺序（1 定义方法，2 提供便捷入口，3 调用入口）。Task 3.5 依赖 Task 1（`refresh_entity_mapping` 方法定义）。Task 4 依赖 Task 1 的 `get_entity_type_counts()`。Task 5 独立。Task 6 最后。

---

## 风险评估

| 风险 | 严重性 | 缓解措施 |
|------|----------|----------|
| `refresh_entity_mapping()` 在 LightRAG 不可用时失败 | 低 | 方法内部 try/except，失败只打 warning 不影响主流程 |
| `_entity_type_counts` 缓存为空时 `/api/stats` 返回 0 | 中 | 回退到 `_count_entities_from_graph()` 图遍历（原逻辑） |
| 入库完成时新实体尚无 `_region:contains` 边 | 低 | `assign_entities_to_default_regions` 仅在 RegionSync 24h 周期中执行，不在 pipeline 内。入库完成时 `_entity_to_region` 不含新实体（与当前行为一致，不是退化），`_entity_type_counts` 正确包含新实体（直接遍历节点属性） |
| 去掉 60s 轮询后，非入库场景的统计不更新 | 低 | 页面初始化时调用一次；非入库场景（如 MCP 工具直接入库）极少，可通过刷新页面更新 |
| `graph_read_lock()` 在 `refresh_entity_mapping` 中的并发 | 低 | 已有读写锁保护 NetworkX 图，`refresh_entity_mapping` 使用读锁 |
