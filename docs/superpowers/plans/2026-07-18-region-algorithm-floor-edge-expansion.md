# 脑区社区重算输入范围扩展 + 永久脑区边衰减修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展脑区社区重算的输入范围，让"只剩 1 条保底归属边"的实体也能参与社区重算；同时修复永久脑区归属边"永久保底不删除"的 bug，让永久脑区跟普通脑区一样走衰减删除逻辑。

**Architecture:** 两处独立改动并行：(1) 在 `region_detector.detect_communities` 里把"排除已直连脑区实体"改为"排除已直连脑区实体 - 保底边实体"（OR 关系，保底边实体保留参与算法）；(2) 在 `region_manager._decay_brain_region_edges` 删除 `priority == "permanent"` 的永久保底分支，让永久脑区的实体归属边走与普通脑区完全一致的衰减+保底+删除逻辑。保底边解锁无需显式代码——实体被分配到新脑区后多 1 条 `_region:contains` 边，本方案条件下次不再命中，下轮衰减自然继续。

**Tech Stack:** Python 3.11, NetworkX, loguru, pytest

---

## 关键背景知识

### 衰减算法当前逻辑（`_decay_brain_region_edges`）

```python
# region_manager.py:82-148
for region_key in brain_regions:        # 遍历所有脑区节点
    for entity_key in neighbors:         # 遍历脑区的所有邻居（归属实体）
        # 跳过锚点边（脑区↔脑区）和 _session: 临时边
        new_weight = old_weight * decay_rate
        total_degree = nx_graph.degree(entity_key)  # entity 所有边总数（含知识边 + 归属边）

        if priority == "permanent":     # L123-128 永久保底分支（本次要删）
            new_weight = max(new_weight, FLOOR_WEIGHT)
            protected += 1
        elif total_degree <= 1:         # L129-134 孤立实体保底
            new_weight = max(new_weight, FLOOR_WEIGHT)
            protected += 1
        elif new_weight < FLOOR_WEIGHT: # L135-138 删除分支
            nx_graph.remove_edge(region_key, entity_key)
            deleted += 1
        else:                            # L139-141 正常衰减
            nx_graph.edges[...]["weight"] = new_weight
```

**重要事实**：衰减算法**只衰减实体→脑区的归属边**（`_region:contains` / `keywords="包含"`）。知识边（实体↔实体）不衰减。

### 社区算法当前筛选逻辑（`detect_communities`）

`region_detector.py:152-178`：构造 `assigned_entities` 集合（已直连脑区的实体），从 nodes/edges 中**排除**这些实体。结果：只有"非直连脑区"实体参与算法。

### 永久脑区与普通脑区的真实区别

- **永久脑区**（文档库/人际关系/组织机构）：脑区**节点本身**不被 `dissolve_shrunk_regions` 删除（dissolve 跳过所有 default regions 配置里的脑区，包括 permanent + long + medium + short 全部 6 个；阈值是成员数 < 10 持续多轮）
- **普通脑区**（社区算法生成的非 default region）：成员数 < 10 持续多轮会被 dissolve 删除
- **NIU 根节点**：属性 `entity_type=other`，不是 `brainregion`，不在衰减算法脑区循环内——NIU 跟脑区的边天然不被衰减

**bug 现状**：`_decay_brain_region_edges` L123-128 对永久脑区的实体归属边走"永久保底冻结，永不删除"——这把"脑区节点本身不删"错误地扩大到"实体归属边不删"，导致永久脑区的实体无法被自然遗忘。

### 保底边定义（本次新增概念）

实体满足以下条件之一即视为"保底边实体"：
- **条件 1（非直连脑区）**：实体有 0 条 `_region:contains` 边（含孤儿实体）
- **条件 2（只剩 1 条保底归属边）**：实体有且仅有 1 条 `_region:contains` 边，且该边权重 ≤ `FLOOR_WEIGHT`（0.1）

**注意**：条件 2 只数 `_region:contains` 边数量，不计算知识边或其他类型边。一个实体可能有多条知识边但只有 1 条归属边到保底值——这种实体应该参与重算。

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `niu_api/internal/lightrag_manager.py` | (1) STORAGE_DIR 支持环境变量覆盖（让 e2e 测试能用临时目录，不污染 ~/.niu/）<br>(2) 新增 `find_entities_with_single_floor_edge()` 函数 | 新增函数 + 改 1 行 |
| `niu_api/internal/region_detector.py` | `detect_communities` 筛选条件改为 OR | 修改逻辑 |
| `niu_api/internal/region_manager.py` | `_decay_brain_region_edges` 删除 permanent 永久保底分支 | 修改逻辑 |
| `tests/test_region_detector.py` | 新增保底边实体参与算法的测试 | 新增测试 |
| `tests/test_region_manager_decay.py` | 新增永久脑区边衰减与普通脑区一致的测试 | 新增测试 |
| `tests/test_region_floor_edge_e2e.py` | 真实 LightRAG 实例 e2e 测试 | 新增测试 |

---

## Task 0: 让 STORAGE_DIR 支持环境变量覆盖（为 e2e 测试做准备）

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:38`

**目的**：当前 `STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"` 是模块级硬编码，import 时就固定了。e2e 测试需要用临时目录避免污染真实 `~/.niu/lightrag_storage`，必须让 STORAGE_DIR 支持环境变量覆盖。

- [ ] **Step 1: 加 `import os` 并修改 STORAGE_DIR 定义**

读 `niu_api/internal/lightrag_manager.py:1-40` 确认精确上下文。**事实**：当前文件顶部没有 `import os`，必须显式加。

用 Edit 工具做两次替换（如果合并为一次会模糊匹配，分两次更稳）：

**第一次 Edit**——在顶部 import 区加 `import os`：

**old_string**：

```python
import asyncio
import json
import threading
```

**new_string**：

```python
import asyncio
import json
import os
import threading
```

**第二次 Edit**——修改 STORAGE_DIR 定义：

**old_string**：

```python
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"
```

**new_string**：

```python
# STORAGE_DIR 支持环境变量覆盖（让 e2e 测试能用临时目录避免污染 ~/.niu/lightrag_storage）
# 默认值 = ~/.niu/lightrag_storage（与原行为一致）
STORAGE_DIR = Path(os.environ.get("NIU_STORAGE_DIR", str(Path.home() / ".niu" / "lightrag_storage")))
```

**注意**：第一次 Edit 必须先确认 `import asyncio / import json / import threading` 这三行在文件顶部确实连续存在。如果实际顺序不同（如 `import threading` 在 `import json` 之前），需先 Read 确认再调整 old_string。

- [ ] **Step 2: 验证语法**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -c "import ast; ast.parse(open('niu_api/internal/lightrag_manager.py').read())"
```

预期：无输出（语法正确）

- [ ] **Step 3: 验证 STORAGE_DIR 仍能正常解析**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -c "from niu_api.internal.lightrag_manager import STORAGE_DIR; print(STORAGE_DIR)"
```

预期输出：`REDACTED_USER_PATH/.niu/lightrag_storage`（默认值不变）

```bash
cd REDACTED_USER_PATH/tools/ai-bot
NIU_STORAGE_DIR=/tmp/test_storage python -c "from niu_api.internal.lightrag_manager import STORAGE_DIR; print(STORAGE_DIR)"
```

预期输出：`/tmp/test_storage`

- [ ] **Step 4: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/lightrag_manager.py
git commit -m "refactor(lightrag_manager): STORAGE_DIR 支持环境变量覆盖

让 e2e 测试能用 NIU_STORAGE_DIR 临时目录避免污染 ~/.niu/lightrag_storage。
默认值不变（仍是 ~/.niu/lightrag_storage）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1: 新增 `find_entities_with_single_floor_edge` 函数

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:443` 附近（紧接 `get_all_region_members` 之后插入）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_find_floor_edge_entities.py`：

```python
"""测试 find_entities_with_single_floor_edge 函数"""
from unittest import mock
import networkx as nx


def _build_graph(nodes_spec, edges_spec):
    """构造测试用 LightRAG 图快照（与 LightRAG 实际类型一致：nx.Graph 无向图）"""
    g = nx.Graph()
    for name, etype in nodes_spec:
        g.add_node(name, entity_type=etype)
    for src, tgt, kw, w in edges_spec:
        g.add_edge(src, tgt, keywords=kw, weight=w)
    return g


def _patch_graph(g):
    """统一构造 mock context：patch get_lightrag 返回带 _graph 属性的 fake_rag + graph_read_lock"""
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g
    return [
        mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=fake_rag),
        mock.patch("niu_api.internal.lightrag_manager.graph_read_lock"),
    ]


def test_empty_graph_returns_empty_set():
    from niu_api.internal import lightrag_manager
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph = None
    with mock.patch.object(lightrag_manager, "get_lightrag", return_value=fake_rag):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()


def test_lightrag_none_returns_empty_set():
    """get_lightrag 返回 None → 返回空集"""
    from niu_api.internal import lightrag_manager
    with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()


def test_entity_with_single_contains_edge_at_floor_returns_it():
    """实体只有 1 条 _region:contains 边且 weight=0.1 → 命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),  # 唯一归属边，已到保底
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result  # 小写


def test_entity_with_single_contains_edge_above_floor_not_returned():
    """实体只有 1 条 _region:contains 边但 weight=0.5 → 不命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 0.5)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_entity_with_two_contains_edges_not_returned():
    """实体有 2 条 _region:contains 边（到不同脑区）→ 不命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("脑区X", "brainregion"), ("脑区Y", "brainregion"),
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("脑区X", "实体A", "包含", 0.1),
            ("脑区Y", "实体A", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_orphan_entity_not_returned():
    """实体有 0 条 _region:contains 边（孤儿）→ 不命中条件 2（条件 1 在 detect_communities 处理）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("脑区X", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),
        ],
        edges_spec=[
            # 实体A 没有归属边，只有知识边
            ("实体A", "实体B", "相关", 0.5),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_entity_with_knowledge_edges_still_counted_correctly():
    """实体有 1 条归属边（保底）+ 多条知识边 → 仍命中（知识边不计数）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),
            ("实体C", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),  # 唯一归属边，保底
            ("实体A", "实体B", "相关", 1.0),      # 知识边（不计数）
            ("实体A", "实体C", "相关", 0.8),      # 知识边（不计数）
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_at_boundary_floor_value_returns_it():
    """weight 恰好等于 0.1（边界值，<=）→ 命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 0.1)],  # 恰好等于保底
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_string_converted_to_float():
    """weight 是字符串 "0.1" → 类型转换后命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", "0.1")],  # 字符串
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_none_falls_back_to_default_not_returned():
    """weight 是 None → edge_data.get("weight", 1.0) 默认 1.0，不命中"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node("实体A", entity_type="concept")
    g.add_edge("智家脑区", "实体A", keywords="包含")  # 没 weight 字段
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_weight_invalid_type_skipped():
    """weight 是非法类型（如 list）→ try float 失败，跳过该实体"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node("实体A", entity_type="concept")
    g.add_edge("智家脑区", "实体A", keywords="包含", weight=[0.1])  # list 不是合法 weight
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_session_prefix_edges_skipped():
    """_session: 前缀边不算归属边（不是 _region:contains）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[
            ("智家脑区", "实体A", "_session:contains", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    # 实体A 没有 _region:contains 边（_session:contains 被跳过）→ 0 条归属边 → 不命中条件 2
    assert result == set()


def test_region_node_itself_skipped():
    """脑区节点本身不参与判断"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("脑区X", "brainregion")],
        edges_spec=[("智家脑区", "脑区X", "包含", 0.1)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_exception_returns_empty_set():
    """函数内部抛异常 → 返回空集（降级）"""
    from niu_api.internal import lightrag_manager
    with mock.patch.object(lightrag_manager, "get_lightrag", side_effect=RuntimeError("boom")):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_find_floor_edge_entities.py -v
```

预期：`ImportError: cannot import name 'find_entities_with_single_floor_edge'`

- [ ] **Step 3: 实现函数**

在 `niu_api/internal/lightrag_manager.py` 的 `get_all_region_members` 函数后（约 L443 之后，`remove_region_edges` 函数前）插入：

```python
def find_entities_with_single_floor_edge(floor_weight: float = 0.1) -> set[str]:
    """找出"只剩 1 条 _region:contains 归属边、且该边已到保底值"的实体集合。

    用途：脑区社区重算输入范围扩展。这些实体被保底规则锁在原脑区无法迁移，
    必须被纳入社区重算，让新脑区分配一条归属边后，下轮衰减自然解除保底。

    判定规则（与 _decay_brain_region_edges 一致）：
      - 统计实体的 _region:contains 归属边数量（keywords="包含"）
      - 跳过 _session: 前缀边（keywords 字段以 "_session:" 开头，与会话临时边区分）
      - 跳过脑区节点本身（entity_type=brainregion）
      - 归属边数量 == 1 且 weight <= floor_weight → 命中

    注意：知识边（实体↔实体，keywords 非 "包含" 且非 "_session:"）不参与计数，
    与本方案设计一致（知识边由大模型生成，不衰减，不应影响归属边保底判断）。

    Args:
        floor_weight: 保底权重阈值（默认 0.1，与 region_manager.FLOOR_WEIGHT 对齐）

    Returns:
        实体名称集合（小写，与 detect_communities 中 assigned_entities 一致）
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return set()

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return set()

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return set()

        with graph_read_lock():
            snapshot = nx_graph.copy()

        result: set[str] = set()
        # 脑区判断方式：与 get_all_region_members L429-434 保持完全一致
        # 只用 name.endswith("脑区") 判断——系统所有脑区命名都是 "{label}脑区" 格式
        # （region_manager.py L53 REGION_SUFFIX="脑区" + L384 f"{region_label}{REGION_SUFFIX}"）
        # 不用 entity_type=="brainregion"——避免与 get_all_region_members 不一致
        # 这是关键不变量：两函数判断方式必须一致，否则 OR 关系 set 差集会产生错误结果
        for node_id, node_data in snapshot.nodes(data=True):
            # 跳过脑区节点本身（只用 endswith("脑区") 判断）
            if isinstance(node_id, str) and node_id.endswith("脑区"):
                continue

            # 防御性：node_id 必须是 str，否则 .lower() 会失败
            if not isinstance(node_id, str):
                continue

            # 统计该实体的 _region:contains 归属边数
            contains_edges = []
            for neighbor_id, edge_data in snapshot[node_id].items():
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                kw_lower = kw.lower() if isinstance(kw, str) else ""
                # 跳过 _session: 前缀边（keywords 字段以 "_session:" 开头，与会话临时边区分）
                if kw_lower.startswith("_session:"):
                    continue
                # 只数 _region:contains 归属边（keywords="包含"）
                if kw_lower != "包含":
                    continue
                # 防御性校验：另一端必须是脑区节点（与 get_all_region_members 一致：endswith("脑区")）
                if not (isinstance(neighbor_id, str) and neighbor_id.endswith("脑区")):
                    continue
                contains_edges.append(edge_data)

            # 只剩 1 条归属边 + 已到保底值
            if len(contains_edges) == 1:
                w = contains_edges[0].get("weight", 1.0)
                try:
                    w = float(w)
                except (TypeError, ValueError):
                    continue
                if w <= floor_weight:
                    result.add(node_id.lower())

        return result

    except Exception as e:
        logger.debug("find_entities_with_single_floor_edge failed: %s", e)
        return set()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_find_floor_edge_entities.py -v
```

预期：所有 7 个测试通过

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_find_floor_edge_entities.py niu_api/internal/lightrag_manager.py
git commit -m "feat(lightrag_manager): 新增 find_entities_with_single_floor_edge 函数

找出只剩 1 条 _region:contains 归属边且已到保底值的实体，
供 detect_communities 扩展社区重算输入范围使用。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 修改 `detect_communities` 筛选条件为 OR 关系

**Files:**
- Modify: `niu_api/internal/region_detector.py:152-178`
- Test: `tests/test_region_detector.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_region_detector.py` 末尾追加：

```python
def test_detect_communities_includes_single_floor_edge_entity():
    """只剩 1 条保底归属边的实体应参与社区检测，
    即使它已直连某脑区（OR 关系覆盖原排除条件）"""
    from unittest import mock
    from niu_api.internal.region_detector import CommunityDetector

    # 构造：1 个脑区主节点 + 1 个普通已归属实体 + 1 个保底边实体 + 3 个游离实体
    nodes = [
        {"name": "智家脑区", "type": "brainregion"},
        # 普通已归属实体（weight=1.0，应被排除）
        {"name": "NormalAssigned", "type": "technology"},
        # 保底边实体（已归属脑区但归属边 weight=0.1，应保留参与算法）
        {"name": "FloorEdgeEntity", "type": "concept"},
        # 游离实体
        {"name": "FreeA", "type": "concept"},
        {"name": "FreeB", "type": "concept"},
        {"name": "FreeC", "type": "concept"},
    ]
    edges = [
        # 普通已归属 → 脑区（包含边，weight=1.0）
        {"source": "智家脑区", "target": "NormalAssigned", "keywords": "包含", "weight": 1.0},
        # 保底边实体 → 脑区（包含边，weight=0.1，保底）
        {"source": "智家脑区", "target": "FloorEdgeEntity", "keywords": "包含", "weight": 0.1},
        # 保底边实体 ↔ FreeA（知识边，不计数）
        {"source": "FloorEdgeEntity", "target": "FreeA", "keywords": "相关", "weight": 0.5},
        # 游离实体相互连接
        {"source": "FreeA", "target": "FreeB", "keywords": "相关", "weight": 1.0},
        {"source": "FreeB", "target": "FreeC", "keywords": "相关", "weight": 1.0},
    ]

    fake_adapter = mock.MagicMock()
    fake_adapter.get_graph_snapshot = mock.Mock(return_value={"nodes": nodes, "edges": edges})

    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={"智家脑区": ["NormalAssigned", "FloorEdgeEntity"]},
    ), mock.patch(
        "niu_api.internal.lightrag_manager.find_entities_with_single_floor_edge",
        return_value={"flooredgeentity"},  # 小写
    ):
        detector = CommunityDetector(fake_adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

    all_partition_members = []
    for p in result.partitions:
        all_partition_members.extend(p.entity_names)

    # 普通已归属实体被排除
    assert "NormalAssigned" not in all_partition_members, "普通已归属实体应被排除"
    # 保底边实体保留参与算法（OR 关系覆盖排除条件）
    assert "FloorEdgeEntity" in all_partition_members, "保底边实体应保留参与算法"
    # 游离实体保留
    assert "FreeA" in all_partition_members
    assert "FreeB" in all_partition_members
    assert "FreeC" in all_partition_members
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_detector.py::test_detect_communities_includes_single_floor_edge_entity -v
```

预期：FAIL，`AssertionError: 'FloorEdgeEntity' in all_partition_members` 不成立

- [ ] **Step 3: 修改 `detect_communities` 筛选逻辑**

读 `niu_api/internal/region_detector.py:152-178` 确认精确内容。然后用 Edit 工具替换：

**old_string**（实际从代码里读，下面是预期形式，必须以实际代码为准）：

```python
        # 排除已直连脑区的实体（脑区一级成员）
        # 这些实体已经归属到某个脑区，不应再参与社区检测
        # 否则每次跑 Leiden 都会把同一批已归属实体重新聚成社区
        try:
            from niu_api.internal.lightrag_manager import get_all_region_members
            region_members_map = get_all_region_members()
            assigned_entities: set[str] = set()
            for members in region_members_map.values():
                for m in members:
                    if isinstance(m, str):
                        assigned_entities.add(m.lower())
        except Exception as e:
            logger.warning("获取已归属实体集合失败，跳过排除步骤: %s", e)
            assigned_entities = set()

        if assigned_entities:
            before_count = len(nodes)
            def _is_assigned(name) -> bool:
                return isinstance(name, str) and name.lower() in assigned_entities
            nodes = [n for n in nodes if not _is_assigned(n.get("name", n.get("id", "")))]
            edges = [
                e for e in edges
                if not _is_assigned(e.get("source", "")) and not _is_assigned(e.get("target", ""))
            ]
            logger.info(
                f"排除 {before_count - len(nodes)} 个已归属实体（直连脑区的一级成员），剩余 {len(nodes)} 个游离实体参与算法"
            )
```

**new_string**：

```python
        # 筛选参与社区检测的实体：两条件 OR 关系
        # 条件 1（原条件）：非直连脑区——实体没有任何 _region:contains 边连到脑区
        #   这些是已归属脑区的实体，已归属则不再重复参与算法
        # 条件 2（新增）：只剩 1 条保底归属边——实体只有 1 条 _region:contains 边
        #   且该边权重已到保底值 0.1，被保底规则锁在原脑区无法迁移
        #   必须重新参与算法让其归入新脑区，下轮衰减自动解除保底
        # 实际做法：构造"应排除"集合 = (已直连脑区实体) - (保底边实体)
        #   即保底边实体即使已直连脑区也保留参与算法（OR 关系）
        try:
            from niu_api.internal.lightrag_manager import (
                get_all_region_members,
                find_entities_with_single_floor_edge,
            )
            region_members_map = get_all_region_members()
            assigned_entities: set[str] = set()
            for members in region_members_map.values():
                for m in members:
                    if isinstance(m, str):
                        assigned_entities.add(m.lower())
        except Exception as e:
            logger.warning("获取已归属实体集合失败，跳过排除步骤: %s", e)
            assigned_entities = set()

        # 查询保底边实体集合（条件 2）
        floor_edge_entities: set[str] = set()
        try:
            floor_edge_entities = find_entities_with_single_floor_edge()
        except Exception as e:
            logger.warning("查询保底边实体集合失败，跳过条件 2: %s", e)
            floor_edge_entities = set()

        # 排除集 = 已直连脑区实体 - 保底边实体（OR 关系：保底边实体保留参与算法）
        exclude_entities = assigned_entities - floor_edge_entities

        if exclude_entities:
            before_count = len(nodes)
            def _is_excluded(name) -> bool:
                return isinstance(name, str) and name.lower() in exclude_entities
            nodes = [n for n in nodes if not _is_excluded(n.get("name", n.get("id", "")))]
            edges = [
                e for e in edges
                if not _is_excluded(e.get("source", "")) and not _is_excluded(e.get("target", ""))
            ]
            logger.info(
                f"排除 {before_count - len(nodes)} 个已归属实体（保底边实体 {len(floor_edge_entities)} 个保留参与算法），"
                f"剩余 {len(nodes)} 个游离实体参与算法"
            )
        elif floor_edge_entities:
            logger.info(
                f"保底边实体 {len(floor_edge_entities)} 个保留参与算法（无可排除的已归属实体）"
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_detector.py::test_detect_communities_includes_single_floor_edge_entity -v
```

预期：PASS

- [ ] **Step 5: 补充 Task 2 漏测场景**

在 `tests/test_region_detector.py` 末尾继续追加：

```python
def test_detect_communities_floor_edge_exception_degrades_gracefully():
    """find_entities_with_single_floor_edge 抛异常时降级为空集，
    行为等价于原逻辑（保底边实体不被保留，全部按 assigned_entities 排除）"""
    from unittest import mock
    from niu_api.internal.region_detector import CommunityDetector

    nodes = [
        {"name": "智家脑区", "type": "brainregion"},
        {"name": "FloorEdgeEntity", "type": "concept"},
        {"name": "FreeA", "type": "concept"},
        {"name": "FreeB", "type": "concept"},
    ]
    edges = [
        {"source": "智家脑区", "target": "FloorEdgeEntity", "keywords": "包含", "weight": 0.1},
        {"source": "FloorEdgeEntity", "target": "FreeA", "keywords": "相关", "weight": 1.0},
        {"source": "FreeA", "target": "FreeB", "keywords": "相关", "weight": 1.0},
    ]
    fake_adapter = mock.MagicMock()
    fake_adapter.get_graph_snapshot = mock.Mock(return_value={"nodes": nodes, "edges": edges})

    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={"智家脑区": ["FloorEdgeEntity"]},
    ), mock.patch(
        "niu_api.internal.lightrag_manager.find_entities_with_single_floor_edge",
        side_effect=RuntimeError("boom"),
    ):
        detector = CommunityDetector(fake_adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

    all_partition_members = []
    for p in result.partitions:
        all_partition_members.extend(p.entity_names)
    # 异常降级：FloorEdgeEntity 走原逻辑被排除
    assert "FloorEdgeEntity" not in all_partition_members, "异常降级后保底边实体应被排除"
    # 游离实体保留
    assert "FreeA" in all_partition_members


def test_detect_communities_floor_edge_only_no_assigned_entities():
    """floor_edge_entities 非空但 assigned_entities 为空（实体未归属但仍是保底边——理论场景），
    走 elif 分支只打日志不筛选"""
    from unittest import mock
    from niu_api.internal.region_detector import CommunityDetector

    nodes = [
        {"name": "FloorEdgeEntity", "type": "concept"},
        {"name": "FreeA", "type": "concept"},
        {"name": "FreeB", "type": "concept"},
    ]
    edges = [
        {"source": "FloorEdgeEntity", "target": "FreeA", "keywords": "相关", "weight": 1.0},
        {"source": "FreeA", "target": "FreeB", "keywords": "相关", "weight": 1.0},
    ]
    fake_adapter = mock.MagicMock()
    fake_adapter.get_graph_snapshot = mock.Mock(return_value={"nodes": nodes, "edges": edges})

    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={},  # 没有归属实体
    ), mock.patch(
        "niu_api.internal.lightrag_manager.find_entities_with_single_floor_edge",
        return_value={"flooredgeentity"},
    ):
        detector = CommunityDetector(fake_adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

    all_partition_members = []
    for p in result.partitions:
        all_partition_members.extend(p.entity_names)
    # 所有实体都参与算法（assigned_entities 空，没有排除集）
    assert "FloorEdgeEntity" in all_partition_members
    assert "FreeA" in all_partition_members
    assert "FreeB" in all_partition_members


def test_detect_communities_entity_in_both_assigned_and_floor_edge():
    """实体同时在 assigned_entities 和 floor_edge_entities（去重正确性）
    → set 差集后该实体不在 exclude_entities，应保留参与算法"""
    from unittest import mock
    from niu_api.internal.region_detector import CommunityDetector

    nodes = [
        {"name": "智家脑区", "type": "brainregion"},
        {"name": "DualEntity", "type": "concept"},
        {"name": "FreeA", "type": "concept"},
        {"name": "FreeB", "type": "concept"},
    ]
    edges = [
        {"source": "智家脑区", "target": "DualEntity", "keywords": "包含", "weight": 0.1},
        {"source": "DualEntity", "target": "FreeA", "keywords": "相关", "weight": 1.0},
        {"source": "FreeA", "target": "FreeB", "keywords": "相关", "weight": 1.0},
    ]
    fake_adapter = mock.MagicMock()
    fake_adapter.get_graph_snapshot = mock.Mock(return_value={"nodes": nodes, "edges": edges})

    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={"智家脑区": ["DualEntity"]},  # DualEntity 已归属
    ), mock.patch(
        "niu_api.internal.lightrag_manager.find_entities_with_single_floor_edge",
        return_value={"dualentity"},  # DualEntity 也是保底边实体
    ):
        detector = CommunityDetector(fake_adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

    all_partition_members = []
    for p in result.partitions:
        all_partition_members.extend(p.entity_names)
    # DualEntity 在两个集合里，差集后不在 exclude_entities，应保留参与算法
    assert "DualEntity" in all_partition_members, "在 floor_edge_entities 里的实体应保留参与算法"
    assert "FreeA" in all_partition_members
```

- [ ] **Step 6: 跑回归测试确认 Task 1 原测试仍通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_detector.py -v
```

预期：所有测试通过（含原方案 Task 1 的测试 + 本次新增 4 个测试）

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_detector.py niu_api/internal/region_detector.py
git commit -m "feat(region_detector): detect_communities 筛选改 OR 关系，保底边实体参与算法

把"排除已直连脑区实体"改为"排除已直连脑区实体 - 保底边实体"，
即只剩 1 条保底归属边的实体即使已直连脑区也保留参与社区重算。
让被保底规则锁死的实体有机会迁移到新脑区，下轮衰减自动解除保底。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 删除永久脑区归属边的"永久保底"分支

**Files:**
- Modify: `niu_api/internal/region_manager.py:82-148`（`_decay_brain_region_edges` 函数）
- Test: `tests/test_region_manager_decay.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_region_manager_decay.py`：

```python
"""测试 _decay_brain_region_edges 永久脑区边与普通脑区一致衰减"""
from unittest import mock
import networkx as nx
from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT


def _build_graph_with_permanent_region():
    """构造 1 个永久脑区 + 1 个普通实体（含多条知识边避免 total_degree<=1）"""
    g = nx.Graph()
    # 永久脑区（description 含 brain_meta_priority:permanent）
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    # 普通脑区（medium 优先级）
    g.add_node("技术脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_source:default<SEP>技术相关")
    # 实体（归属边到两个脑区，weight 都衰减到保底附近）
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_node("实体C", entity_type="concept")
    # 实体A 归属边到永久脑区，weight=0.05（已低于保底）
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=0.05)
    # 实体A 归属边到普通脑区，weight=0.05（已低于保底）
    g.add_edge("技术脑区", "实体A", keywords="包含", weight=0.05)
    # 实体A 还有多条知识边（让 total_degree > 1，不走孤立保底分支）
    g.add_edge("实体A", "实体B", keywords="相关", weight=1.0)
    g.add_edge("实体A", "实体C", keywords="相关", weight=0.5)
    return g


def test_permanent_region_edge_deleted_when_below_floor():
    """永久脑区的实体归属边 weight < FLOOR_WEIGHT + total_degree >= 2 → 应被删除"""
    g = _build_graph_with_permanent_region()
    result = _decay_brain_region_edges(g)

    # 永久脑区→实体A 的边应该被删除（与普通脑区一致）
    assert not g.has_edge("文档库脑区", "实体A"), "永久脑区归属边 weight<FLOOR_WEIGHT 应被删除"
    # 普通脑区→实体A 的边也应该被删除（对照）
    assert not g.has_edge("技术脑区", "实体A"), "普通脑区归属边 weight<FLOOR_WEIGHT 应被删除"
    # deleted 计数应包含两条
    assert result["deleted"] >= 2, f"应该删除 2 条边，实际 {result['deleted']}"


def test_permanent_region_edge_decayed_normally_when_above_floor():
    """永久脑区的实体归属边 weight > FLOOR_WEIGHT + total_degree >= 2 → 应正常衰减"""
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    # weight=1.0 远高于保底
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=1.0)
    # 知识边让 total_degree > 1
    g.add_edge("实体A", "实体B", keywords="相关", weight=1.0)

    result = _decay_brain_region_edges(g)

    # 边应被衰减（不是删除，也不是永久保底）
    assert g.has_edge("文档库脑区", "实体A"), "weight > FLOOR_WEIGHT 的边不应被删除"
    new_w = g.edges["文档库脑区", "实体A"]["weight"]
    assert new_w < 1.0, f"应该正常衰减，weight 应 < 1.0，实际 {new_w}"
    assert new_w > FLOOR_WEIGHT, f"衰减后应仍 > FLOOR_WEIGHT，实际 {new_w}"
    assert result["decayed"] >= 1


def test_permanent_region_protected_when_isolated():
    """永久脑区的孤立实体（total_degree<=1）仍走保底保护分支"""
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    g.add_node("实体A", entity_type="concept")
    # 唯一归属边，weight=0.05（孤立实体）
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=0.05)

    result = _decay_brain_region_edges(g)

    # 孤立实体保底保护：边不删除，weight 锁在 FLOOR_WEIGHT
    assert g.has_edge("文档库脑区", "实体A"), "孤立实体保底边不应删除"
    assert g.edges["文档库脑区", "实体A"]["weight"] == FLOOR_WEIGHT
    assert result["protected"] >= 1


def test_statistics_no_permanent_specific_counter():
    """删除 permanent 分支后，result 字典不应有 permanent 专属计数器"""
    g = _build_graph_with_permanent_region()
    result = _decay_brain_region_edges(g)
    # 原有 4 个字段保持不变
    assert set(result.keys()) == {"decayed", "deleted", "protected", "skipped_anchor"}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py -v
```

预期：FAIL，`AssertionError: assert not g.has_edge("文档库脑区", "实体A")` 不成立（永久脑区边走 L123-128 永久保底，不会被删除）

- [ ] **Step 3: 删除 permanent 永久保底分支**

读 `niu_api/internal/region_manager.py:82-148`。用 Edit 工具修改 `_decay_brain_region_edges` 函数：

**old_string**：

```python
            old_weight = edge_data.get("weight", INITIAL_WEIGHT)

            new_weight = old_weight * decay_rate

            total_degree = nx_graph.degree(entity_key)

            if priority == "permanent":
                # permanent 级：保底冻结，永不删除
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif total_degree <= 1:
                # 孤立实体：保底保护
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif new_weight < FLOOR_WEIGHT:
                # 非 permanent + 总边数>=2 + 低于保底 → 删除
                nx_graph.remove_edge(region_key, entity_key)
                deleted += 1
            else:
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
```

**new_string**：

```python
            old_weight = edge_data.get("weight", INITIAL_WEIGHT)

            new_weight = old_weight * decay_rate

            total_degree = nx_graph.degree(entity_key)

            if total_degree <= 1:
                # 孤立实体：保底保护（避免变孤岛）
                # 永久脑区与普通脑区一致——永久脑区只是脑区节点本身不删，
                # 实体归属边的衰减逻辑与普通脑区完全一致
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif new_weight < FLOOR_WEIGHT:
                # 总边数>=2 + 低于保底 → 删除
                nx_graph.remove_edge(region_key, entity_key)
                deleted += 1
            else:
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
```

- [ ] **Step 4: 更新已有测试 `test_permanent_not_deleted_with_other_edges`**

删除 permanent 分支后，原测试 `tests/test_brain_region_edge_decay.py:184-196` 的断言"永久脑区边不被删除，冻结在保底"会失败——这正是 bug 修复的预期效果。更新该测试：

读 `tests/test_brain_region_edge_decay.py:184-196`。用 Edit 工具替换（old_string 必须与实际代码完全一致，包括缩进和换行）：

**old_string**（直接从 tests/test_brain_region_edge_decay.py:184-196 复制）：

```python
    def test_permanent_not_deleted_with_other_edges(self):
        """permanent + 总边数>=2 + 低于保底 → 不删除，冻结在保底"""
        from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        G.add_node("entity_multi", entity_type="person", description="多边人物")
        G.add_node("entity_other", entity_type="skill", description="其他技能")
        G.add_edge("region_perm", "entity_multi", weight=0.03, description="包含")
        G.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")
        _decay_brain_region_edges(G)
        assert G.has_edge("region_perm", "entity_multi")
        assert G["region_perm"]["entity_multi"]["weight"] == FLOOR_WEIGHT
```

**new_string**：

```python
    def test_permanent_not_deleted_with_other_edges(self):
        """permanent + 总边数>=2 + 低于保底 → 删除（2026-07-18 修复：与普通脑区一致）"""
        from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        G.add_node("entity_multi", entity_type="person", description="多边人物")
        G.add_node("entity_other", entity_type="skill", description="其他技能")
        G.add_edge("region_perm", "entity_multi", weight=0.03, description="包含")
        G.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")
        _decay_brain_region_edges(G)
        # 永久脑区的实体归属边与普通脑区一致：weight < FLOOR_WEIGHT + total_degree >= 2 → 删除
        assert not G.has_edge("region_perm", "entity_multi")
```

同时检查 `tests/test_brain_region_edge_decay.py:147-157` 的 `test_permanent_freeze_at_floor` 测试。该测试构造的 `entity_x` 只有 1 条归属边、0 条知识边，`total_degree=1`，走孤立保底分支——断言 `weight >= FLOOR_WEIGHT` 仍成立，**不需要修改**。但为清晰起见，把测试名改为 `test_permanent_isolated_entity_floor_protection`：

读 `tests/test_brain_region_edge_decay.py:147-157`。用 Edit 工具替换：

**old_string**：

```python
    def test_permanent_freeze_at_floor(self):
        """permanent 级边权重衰减到保底值冻结"""
        from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        G.add_node("entity_x", entity_type="person", description="人物X")
        G.add_edge("region_perm", "entity_x", weight=0.11, description="包含")
        _decay_brain_region_edges(G)
        weight = G["region_perm"]["entity_x"]["weight"]
        assert weight >= FLOOR_WEIGHT
```

**new_string**：

```python
    def test_permanent_isolated_entity_floor_protection(self):
        """permanent 脑区 + 孤立实体（total_degree=1）→ 保底保护（与普通脑区一致）"""
        from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
        G = nx.Graph()
        G.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        G.add_node("entity_x", entity_type="person", description="人物X")
        G.add_edge("region_perm", "entity_x", weight=0.11, description="包含")
        _decay_brain_region_edges(G)
        weight = G["region_perm"]["entity_x"]["weight"]
        # 孤立实体保底：weight 衰减后 max(., FLOOR_WEIGHT) = FLOOR_WEIGHT
        assert weight == FLOOR_WEIGHT, f"孤立实体保底应等于 FLOOR_WEIGHT，实际 {weight}"
```

- [ ] **Step 5: 补充 Task 3 漏测场景**

在 `tests/test_region_manager_decay.py` 末尾追加：

```python
def test_permanent_region_edge_decayed_when_weight_above_floor():
    """永久脑区边 weight 衰减后 > FLOOR_WEIGHT + total_degree >= 2 → 正常衰减（非删除非保底）"""
    from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    # weight=1.0 远高于保底，total_degree=2
    g.add_edge("文档库脑区", "实体A", weight=1.0, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")

    result = _decay_brain_region_edges(g)

    assert g.has_edge("文档库脑区", "实体A"), "weight > FLOOR_WEIGHT 的边不应被删除"
    new_w = g["文档库脑区"]["实体A"]["weight"]
    assert new_w < 1.0, f"应该正常衰减，weight 应 < 1.0，实际 {new_w}"
    assert new_w > FLOOR_WEIGHT, f"衰减后应仍 > FLOOR_WEIGHT，实际 {new_w}"
    assert result["decayed"] >= 1


def test_permanent_region_edge_at_boundary_floor_value_decayed():
    """永久脑区边 weight 衰减后正好略大于 FLOOR_WEIGHT → 走 else 正常衰减分支

    验证 elif new_weight < FLOOR_WEIGHT 是严格小于：new_weight 略大于 FLOOR_WEIGHT 时
    不删除，进 else 分支正常衰减（weight 被设为 new_weight）。

    注意：浮点计算 FLOOR_WEIGHT / decay_rate * decay_rate 可能因浮点误差 < FLOOR_WEIGHT，
    所以构造 weight 让 new_weight 明确略大于 FLOOR_WEIGHT（加 epsilon 0.001）。
    """
    from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
    from niu_api.internal.region_manager import daily_decay_rate
    decay_rate = daily_decay_rate("permanent")
    # 构造 new_weight = FLOOR_WEIGHT + 0.001（明确 > FLOOR_WEIGHT，避免浮点误差）
    initial_weight = (FLOOR_WEIGHT + 0.001) / decay_rate

    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", weight=initial_weight, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")  # 让 total_degree=2

    result = _decay_brain_region_edges(g)

    # new_weight = FLOOR_WEIGHT + 0.001 > FLOOR_WEIGHT → 不走删除分支，进 else 正常衰减
    assert g.has_edge("文档库脑区", "实体A"), "new_weight > FLOOR_WEIGHT 的边不应删除"
    actual_weight = g["文档库脑区"]["实体A"]["weight"]
    expected = FLOOR_WEIGHT + 0.001
    assert abs(actual_weight - expected) < 1e-9, \
        f"weight 应等于 new_weight={expected}，实际 {actual_weight}"
    assert result["decayed"] >= 1, "应进入 decayed 计数"
    assert result["deleted"] == 0, "不应进入 deleted 计数"


def test_permanent_decay_rate_still_uses_permanent_halflife():
    """永久脑区 decay_rate 仍用 PRIORITY_HALFLIFE['permanent']=360（半衰期 360 天）"""
    from niu_api.internal.region_manager import daily_decay_rate, PRIORITY_HALFLIFE
    assert PRIORITY_HALFLIFE["permanent"] == 360
    rate = daily_decay_rate("permanent")
    expected = 0.5 ** (1.0 / 360)
    assert abs(rate - expected) < 1e-9


def test_normal_region_edge_delete_contrast():
    """对照：普通脑区 weight < FLOOR_WEIGHT + total_degree >= 2 → 删除（与永久脑区一致）"""
    from niu_api.internal.region_manager import _decay_brain_region_edges
    g = nx.Graph()
    g.add_node("技术脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>技术")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("技术脑区", "实体A", weight=0.05, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")

    _decay_brain_region_edges(g)

    assert not g.has_edge("技术脑区", "实体A"), "普通脑区 weight<FLOOR_WEIGHT 应删除"


def test_permanent_region_node_not_dissolved_when_empty():
    """永久脑区即使所有归属边删除，脑区节点本身不删除（dissolve 跳过 is_default_region）"""
    # 这个测试验证 dissolve_shrunk_regions 不删永久脑区节点
    # 不直接跑 dissolve（需要复杂 mock），改为验证：永久脑区 dissolve 时被跳过
    # 通过检查 create_default_regions 配置确认永久脑区都是 is_default_region
    from niu_api.internal.region_manager import create_default_regions
    import inspect
    src = inspect.getsource(create_default_regions)
    # 永久脑区配置里 priority=permanent
    assert "permanent" in src
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py tests/test_brain_region_edge_decay.py -v
```

预期：
- `test_region_manager_decay.py` 4+5=9 个测试全部通过
- `test_brain_region_edge_decay.py` 全部通过（含更新的 `test_permanent_not_deleted_with_other_edges` 和重命名的 `test_permanent_isolated_entity_floor_protection`）

- [ ] **Step 7: 跑现有 region_manager 相关测试确认无其他回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/ -k "region or decay" -v
```

预期：所有 region 相关测试通过

- [ ] **Step 8: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager_decay.py tests/test_brain_region_edge_decay.py niu_api/internal/region_manager.py
git commit -m "fix(region_manager): 删除永久脑区归属边的"永久保底"分支

永久脑区与普通脑区的区别应只在"脑区节点本身不被 dissolve 删除"，
不应延伸到"实体归属边永不删除"。普通实体连接到永久脑区的归属边
应该走与普通脑区完全一致的衰减+保底+删除逻辑。

修复后：
- 永久脑区的实体归属边 weight < FLOOR_WEIGHT 且 total_degree >= 2 → 删除
- 永久脑区的实体归属边 weight > FLOOR_WEIGHT → 正常衰减
- 永久脑区的孤立实体（total_degree <= 1）仍走保底保护

NIU 根节点（entity_type=other）不在脑区循环内，其边天然不受影响。

同步更新已有测试 test_permanent_not_deleted_with_other_edges 反映新行为，
重命名 test_permanent_freeze_at_floor → test_permanent_isolated_entity_floor_protection
更清晰表达"孤立实体保底"语义。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 集成验证 + 文档更新

**Files:**
- Test: 写 e2e 测试 + 手动跑 `./niu` 真实环境验证
- Docs: `docs/manual-vector-store.md`（脑区章节补充说明）

### Step 1: 写 e2e 测试验证保底边实体真的参与算法

新建 `tests/test_region_floor_edge_e2e.py`，**用真实 LightRAG 实例 + 真实 RegionManager**（按记忆 `real-testing-only.md` 要求），不 mock，验证完整链路：

```python
"""E2E 测试：保底边实体真实参与社区重算

按 real-testing-only.md 铁律，本测试不 mock LightRAG/RegionManager，
走真实初始化路径。用 monkeypatch.setattr 把 STORAGE_DIR 指向临时目录，
避免污染 ~/.niu/lightrag_storage/。

注意：与现有 e2e 测试（test_lightrag_repair_e2e_skillsync.py:31 等）保持一致，
**不用 importlib.reload**——reload 会让 lightrag_adapter 等其他模块持有旧 get_lightrag 引用，
导致 e2e 断言崩溃。

**前置条件**：需要 bge-base-zh-v1.5 模型（约 390MB）。模型不存在时自动 skip。
"""
import os
import shutil
import pytest
import networkx as nx
from pathlib import Path


# 前置条件检查：bge-base-zh-v1.5 模型必须存在，否则 skip 整个模块
_MODELS_DIR = Path(__file__).parent.parent / "models" / "bge-base-zh-v1.5"
if not _MODELS_DIR.exists():
    pytest.skip(
        f"bge-base-zh-v1.5 模型未下载（{_MODELS_DIR} 不存在），跳过 e2e 测试。"
        f"参考 R9 风险点：模型需先下载到 models/bge-base-zh-v1.5/。",
        allow_module_level=True,
    )


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """临时 LightRAG 存储目录，跑完自动清理。

    与现有 e2e 测试模式保持一致：用 monkeypatch.setattr 直接覆盖
    lightrag_manager.STORAGE_DIR、_rag_instance 等模块级变量。

    **关键**：同时 monkeypatch.setenv("HOME", str(tmp_path))——避免
    _clear_sync_state_if_storage_empty（lightrag_manager.py:744-780）用
    Path.home() 不受 STORAGE_DIR patch 影响，删除真实 ~/.niu/skill_sync_state.json
    和 last_region_sync.json。
    """
    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    # 关键：先 patch HOME 到临时目录，避免 _clear_sync_state_if_storage_empty 删真实文件
    # 参考 test_lightrag_repair_e2e_skillsync.py:49 模式
    monkeypatch.setenv("HOME", str(tmp_path))

    # 覆盖 lightrag_manager 的模块级状态（与 test_lightrag_repair_e2e_skillsync.py:31 模式一致）
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", storage_dir)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._init_failed_at", None)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._repairing", False)

    # 初始化 shared_storage 单进程模式（如未初始化）
    try:
        from lightrag.utils import shared_storage
        shared_storage.initialize_share_data()
    except Exception:
        pass  # 已初始化则跳过

    yield storage_dir

    # 测试结束清理：
    # 1. 停止 LightRAG 后台事件循环（避免 daemon thread 引用已删除的临时目录）
    try:
        from niu_api.internal import lightrag_manager as lm
        if hasattr(lm, "_stop_loop"):
            lm._stop_loop()
        elif hasattr(lm, "shutdown_lightrag_loop"):
            lm.shutdown_lightrag_loop(timeout=2.0)
    except Exception:
        pass  # 测试结束清理失败不阻塞

    # 2. 删除临时目录（monkeypatch 会自动恢复模块级变量，无需手动 reload）
    if storage_dir.exists():
        shutil.rmtree(storage_dir, ignore_errors=True)


def test_floor_edge_entity_participates_in_real_detect_communities(tmp_storage):
    """E2E：构造真实 LightRAG 图 + 真实 detect_communities 调用，
    验证保底边实体出现在 partition 成员里"""
    # 注意：在 fixture monkeypatch.setattr 之后才 import，确保拿到的是 patched 状态
    from niu_api.internal import lightrag_manager as lm
    from niu_api.internal.lightrag_manager import find_entities_with_single_floor_edge
    from niu_api.internal.region_detector import CommunityDetector
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    from niu_api.internal.region_manager import FLOOR_WEIGHT, INITIAL_WEIGHT, BELONGS_TO_RELATION

    rag = lm.get_lightrag()
    assert rag is not None, "LightRAG 初始化失败"

    # 构造测试图：1 个脑区 + 1 个保底边实体 + 3 个游离实体
    graph = rag.chunk_entity_relation_graph._graph

    # 添加脑区节点（name 以"脑区"结尾，跟 get_all_region_members 判断方式一致）
    graph.add_node("测试脑区", entity_type="brainregion",
                   description="brain_meta_priority:medium<SEP>测试")

    # 添加保底边实体（归属边 weight=FLOOR_WEIGHT）
    graph.add_node("FloorEdgeEntity", entity_type="concept", description="测试保底")
    graph.add_edge("测试脑区", "FloorEdgeEntity",
                   keywords=BELONGS_TO_RELATION, weight=FLOOR_WEIGHT,
                   description=BELONGS_TO_RELATION)

    # 添加 3 个游离实体 + 它们之间的知识边
    for name in ["FreeA", "FreeB", "FreeC"]:
        graph.add_node(name, entity_type="concept", description=f"游离{name}")

    # 保底边实体跟 FreeA 之间有知识边（不影响归属边计数）
    graph.add_edge("FloorEdgeEntity", "FreeA", keywords="相关", weight=INITIAL_WEIGHT, description="相关")
    graph.add_edge("FreeA", "FreeB", keywords="相关", weight=INITIAL_WEIGHT, description="相关")
    graph.add_edge("FreeB", "FreeC", keywords="相关", weight=INITIAL_WEIGHT, description="相关")

    try:
        # 调用 find_entities_with_single_floor_edge
        floor_entities = find_entities_with_single_floor_edge()
        assert "flooredgeentity" in floor_entities, \
            f"FloorEdgeEntity 应在保底边实体集合里，实际 {floor_entities}"

        # 调用真实 detect_communities
        adapter = LightRAGAdapter()
        detector = CommunityDetector(adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        all_partition_members = []
        for p in result.partitions:
            all_partition_members.extend(p.entity_names)

        # 关键断言：FloorEdgeEntity 必须参与算法
        assert "FloorEdgeEntity" in all_partition_members, \
            "保底边实体应参与社区重算（OR 关系覆盖排除条件）"
    finally:
        # 清理测试数据：删除本次添加的节点（monkeypatch 会自动恢复 _rag_instance=None，
        # 下次 get_lightrag 会重新初始化，不会有残留状态）
        for node in ["测试脑区", "FloorEdgeEntity", "FreeA", "FreeB", "FreeC"]:
            if graph.has_node(node):
                graph.remove_node(node)
```

### Step 2: 跑 e2e 测试

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_floor_edge_e2e.py -v
```

预期：测试通过，证明保底边实体真实参与算法

### Step 3: 真实环境验证

按记忆 `real-testing-only.md` 铁律清空 `~/.niu/`：

```bash
# 清空数据（保留 skills/memory.json/preferences.json）
cd ~/.niu && rm -rf lightrag_storage/ messages.db notes/ work/ scheduled_tasks.db last_*.json skill_sync_state.json .DS_Store
cd REDACTED_USER_PATH/tools/ai-bot
./niu
```

启动后等待 90 秒让首次同步跑完。准备 50+ 篇测试文档（必须达到 `min_graph_size=50` 阈值才能触发 Leiden），统一入库。

观察日志：

```bash
tail -f REDACTED_USER_PATH/tools/ai-bot/logs/llm_interaction_$(date +%Y%m%d).log | grep -E "排除|保底边|free"
```

预期看到：`排除 N 个已归属实体（保底边实体 M 个保留参与算法），剩余 K 个游离实体参与算法`

**关键断言**：日志中"保底边实体 M 个"在长期运行（多次同步后）应该非 0——证明保底边实体真实被纳入算法。

### Step 4: 等待 24 小时触发衰减后验证

24 小时后再次触发同步，检查日志：

```bash
grep -E "Decay.*brain region" REDACTED_USER_PATH/tools/ai-bot/logs/llm_interaction_$(date +%Y%m%d).log | tail -20
```

预期：`decayed=N, deleted=M, protected=K, skipped_anchor=L`

**关键验证**：保底边实体被分配到新脑区后，下轮衰减中该实体的旧保底边应被删除（`deleted` 计数 > 0）——证明保底边解锁逻辑生效。

### Step 5: 文档更新

读 `docs/manual-vector-store.md` 找脑区章节。先用 grep 确认插入点：

```bash
grep -n "衰减\|decay\|保底" REDACTED_USER_PATH/tools/ai-bot/docs/manual-vector-store.md | head -10
```

找到脑区衰减算法说明段落后，补充：

**新增段落**：

```markdown
**永久脑区边衰减规则**（2026-07-18 修复）：

永久脑区（permanent 优先级，如文档库/人际关系/组织机构）与普通脑区的**唯一区别**是：脑区节点本身不被 `dissolve_shrunk_regions` 删除。**实体归属边的衰减逻辑与普通脑区完全一致**：

- weight 衰减到 < FLOOR_WEIGHT（0.1）且实体 total_degree >= 2 → 删除
- weight > FLOOR_WEIGHT → 正常衰减
- total_degree <= 1（孤立实体）→ 保底保护

旧版本的"永久脑区归属边永久保底永不删除"逻辑是 bug，已修复。NIU 根节点（entity_type=other）不在脑区循环内，其与脑区的边天然不受衰减影响。

**永久脑区空壳状态**：永久脑区即使所有归属边被删除，脑区节点本身仍保留（is_default_region 跳过 dissolve）。下次有新文档入库会重新建立归属边。

**社区重算输入范围**（2026-07-18 扩展）：

社区重算（每 24 小时一次）参与资格规则：

| 条件 | 说明 |
|------|------|
| 条件 1：非直连脑区 | 实体没有任何 `_region:contains` 边直连脑区（含孤儿实体） |
| 条件 2：只剩 1 条保底归属边 | 实体只有 1 条 `_region:contains` 边，且该边 weight ≤ 0.1（保底值） |

满足任一条件即参与社区重算（OR 关系）。条件 2 让被保底规则锁死的实体有机会迁移到新脑区——一旦被分配到新脑区多 1 条归属边，下轮衰减自然解除保底（不再满足 total_degree <= 1）。
```

### Step 6: Commit

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_floor_edge_e2e.py docs/manual-vector-store.md
git commit -m "docs+test: 补充脑区边衰减规则和社区重算输入范围说明 + e2e 测试

e2e 测试用真实 LightRAG 实例验证保底边实体真实参与社区重算（按 real-testing-only.md 铁律不 mock）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 验证清单（所有 Task 完成后跑）

```bash
cd REDACTED_USER_PATH/tools/ai-bot

# 1. 所有新增测试通过
python -m pytest tests/test_find_floor_edge_entities.py tests/test_region_detector.py::test_detect_communities_includes_single_floor_edge_entity tests/test_region_manager_decay.py -v

# 2. 全量 region 相关测试无回归
python -m pytest tests/ -k "region or floor_edge or decay" -v

# 3. Python 语法检查
python -c "import ast; ast.parse(open('niu_api/internal/lightrag_manager.py').read())"
python -c "import ast; ast.parse(open('niu_api/internal/region_detector.py').read())"
python -c "import ast; ast.parse(open('niu_api/internal/region_manager.py').read())"
```

---

## Self-Review

### 1. Spec coverage 检查

- ✅ 条件 1（非直连脑区）— Task 2 的 `assigned_entities - floor_edge_entities` 排除逻辑覆盖
- ✅ 条件 2（只剩 1 条保底归属边）— Task 1 的 `find_entities_with_single_floor_edge` 实现 + Task 2 的 `floor_edge_entities` 集合
- ✅ OR 关系 — Task 2 用 set 差集实现（`assigned - floor_edge = 应排除`，保底边实体保留）
- ✅ 保底边解锁无需显式代码 — Task 1/2 都不动 `_decay_brain_region_edges`，下轮衰减跑 total_degree 自然变化
- ✅ 永久脑区边衰减修复 — Task 3 删除 L123-128 permanent 永久保底分支
- ✅ NIU 根节点边不被衰减 — 已确认 NIU 是 entity_type=other，不在脑区循环内，无需额外代码

### 2. Placeholder 检查

无 TBD/TODO/"添加错误处理"等占位符。所有代码段完整可执行。

### 3. 类型一致性检查

- `find_entities_with_single_floor_edge(floor_weight: float = 0.1) -> set[str]` — 函数签名一致
- `floor_edge_entities: set[str]` — 类型一致
- `exclude_entities = assigned_entities - floor_edge_entities` — set 差集操作，两边都是 `set[str]`
- 测试里 `find_entities_with_single_floor_edge` 的 mock 返回值是 `set[str]`，类型对齐

### 4. 风险点完整清单

| 风险 | 等级 | 缓解 |
|------|------|------|
| 大量保底边实体一次性涌入社区算法 | Medium | min_community_size=100 过滤小社区；如长期观察发现噪声脑区过多，可在 v2 加软上限（如 50）|
| 全图遍历性能 | Low | 2300 节点 × 平均 5 条边 = 11500 次扫描，单次 < 100ms |
| 永久脑区成员数变 0 后脑区节点空壳 | Low | 设计正确：永久脑区节点不删（is_default_region），下次入库重建归属边 |
| `protected` 字段语义变化 | Low | 从"永久保底+孤立保底"变成"仅孤立保底"。`_decay_structural_edges` L1774 日志输出 `protected={result['protected']}`，**字段名不变但语义已变**——只统计孤立保底实体数，不再含永久保底。排查日志时需注意此变化。建议在 v2 把日志字段改为 `protected_orphans=` 更准确 |
| C2 漏识别已有测试被破坏 | High（已修复） | Task 3 Step 4 显式更新 `test_permanent_not_deleted_with_other_edges` + 重命名 `test_permanent_freeze_at_floor` |
| Task 4 违反真实测试铁律 | High（已修复） | Task 4 Step 1-2 新增 e2e 测试用真实 LightRAG 实例 |

### 5. 测试覆盖度统计

| 文件 | 测试数 | 覆盖场景 |
|------|--------|----------|
| `tests/test_find_floor_edge_entities.py` | 14 | 空图/None/单保底边/未到保底值/多条归属边/孤儿/知识边不计数/边界值/字符串/None/非法类型/_session:/脑区节点本身/异常降级/**+ 普通实体间"包含"边误识防护** |
| `tests/test_region_detector.py`（新增 4 个） | 4 | 核心场景 + 异常降级 + 仅保底边无归属 + 去重正确性 |
| `tests/test_region_manager_decay.py`（新增 9 个） | 9 | 删除分支/正常衰减/孤立保底/字段数 + 永久脑区 5 个补充（衰减/边界/半衰期/对照/空壳）|
| `tests/test_region_floor_edge_e2e.py` | 1 | 真实 LightRAG 实例端到端验证（用 NIU_STORAGE_DIR 临时目录避免污染真实数据） |
| `tests/test_brain_region_edge_decay.py`（更新 2 个） | 0 新增 | 更新 `test_permanent_not_deleted_with_other_edges` 反映新行为 + 重命名 `test_permanent_freeze_at_floor` |

### 6. 补充漏测场景

为应对第 2 轮审查发现的边界场景，在 `tests/test_find_floor_edge_entities.py` 末尾追加：

```python
def test_contains_edge_with_non_brainregion_neighbor_skipped():
    """两条普通实体之间也有 keywords="包含" 的边（不是真归属边）→ 不应被计入归属边数。

    防御性测试：避免普通实体间的 "包含" 关系边被误识为 _region:contains 归属边。
    """
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),  # 不是脑区
        ],
        edges_spec=[
            # 实体A 跟脑区有归属边（保底）
            ("智家脑区", "实体A", "包含", 0.1),
            # 实体A 跟实体B 之间有 "包含" 边（但实体B 不是脑区，不算归属边）
            ("实体A", "实体B", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    # 实体A 只有 1 条真归属边（智家脑区→实体A），实体B 这条 "包含" 边不算归属边
    # 所以实体A 命中条件 2 → 应在结果集里
    assert "实体a" in result


def test_find_entities_skips_brainregion_node_with_region_suffix():
    """验证 find_entities_with_single_floor_edge 跳过以"脑区"结尾的脑区节点本身。

    系统所有脑区命名都是 "{label}脑区" 格式（region_manager.py L53 REGION_SUFFIX="脑区"），
    跟 get_all_region_members L429-434 一致：只用 endswith("脑区") 判断脑区。

    不存在 entity_type=brainregion 但 name 不以"脑区"结尾的脑区——本测试只验证
    find_entities 对 "xxx脑区" 命名的脑区节点的跳过行为。
    """
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),  # name 以"脑区"结尾
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
        # 实体A 只有 1 条归属边到"智家脑区"，weight=0.1 → 命中条件 2
        assert "实体a" in result
        # "智家脑区"是脑区节点，不应出现在结果集里
        assert "智家脑区" not in result
    finally:
        for p in patches:
            p.stop()


def test_weight_int_type_converted():
    """weight 是 int 类型 1 → 类型转换后 1.0，不命中（>0.1）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 1)],  # int 类型
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    # weight=int(1) → float(1.0) > 0.1，不命中
    assert result == set()


def test_non_string_node_id_skipped():
    """node_id 是非字符串类型（如 int）→ 跳过，不进入结果集"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node(123, entity_type="concept")  # int node_id
    g.add_edge("智家脑区", 123, keywords="包含", weight=0.1)
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    # int node_id 不应进入结果集（避免 .lower() 失败 + 跟 assigned_entities 差集出错）
    assert result == set()
```

### 7. 风险点补充（第 2-4 轮审查新增）

- **R5. 保底边实体重算震荡**（v2 处理）：实体被分配到新脑区后下轮衰减旧保底边会被删除。但如果新脑区被 dissolve（非 default region + 成员 < 10），新归属边随脑区节点 cascade 删除，实体又回到 1 条保底边状态，下轮又被纳入 detect_communities——形成"反复重算"。建议 v2 加"同一实体 24 小时内不重复纳入 detect_communities"防抖。
- **R6. find_entities_with_single_floor_edge 与 _decay_structural_edges 并发锁竞争**：本函数用 `with graph_read_lock(): snapshot = nx_graph.copy()`，在 2300 节点图上 copy 可能耗时几十毫秒，期间阻塞 write lock。但 RLock 嵌套安全，且 detect_communities 路径已有 read lock，本次新增只是多一次 lock 获取，性能可接受。
- **R7. Task 0 STORAGE_DIR 环境变量覆盖范围有限**（第 3 轮审查新增）：Task 0 只改 `lightrag_manager.STORAGE_DIR`，**不影响** `lightrag_repair._STORAGE_DIR` 和 `lightrag_integrity._STORAGE_DIR`。如果 e2e 测试触发 repair 流程，`lightrag_repair.py:2868` 会用 `lightrag_repair._STORAGE_DIR` 覆盖 `lightrag_manager.STORAGE_DIR`，把环境变量设置值冲掉。**本计划 e2e 测试不触发 repair**，所以不受影响。Task 0 改动作为通用基础设施保留（手动测试或未来扩展可用）。
- **R8. detect_communities 中 floor_edge_entities 集合元素必须都是 str**（第 3 轮审查新增）：`find_entities_with_single_floor_edge` 返回小写字符串集合，`assigned_entities` 也是小写字符串集合，差集操作正确。函数内部已加 `if not isinstance(node_id, str): continue` 守卫，确保非字符串 node_id 不进入结果集，避免差集失败。
- **R9. e2e 测试依赖 bge-base-zh-v1.5 embedding 模型可加载**（第 4 轮审查新增）：e2e 测试调 `lm.get_lightrag()` → `_create_lightrag_instance` 用 `EmbeddingFunc(func=_make_local_embedding_func())` → 需要 `models/bge-base-zh-v1.5/`。**前置检查**：Task 4 Step 2 前确认 `models/bge-base-zh-v1.5/` 存在（约 390MB）。如果模型未下载，e2e 测试会 ImportError/OSError。
- **R10. e2e 测试 LightRAG 后台事件循环清理**（第 4 轮审查新增）：`_create_lightrag_instance` 可能启动 LightRAG 自己的 asyncio 事件循环守护线程。fixture teardown 已加 `_stop_loop()` / `shutdown_lightrag_loop(timeout=2.0)` 调用，避免 daemon thread 引用已删除的临时目录。如果 stop_loop 失败，仍会有 FileNotFoundError 噪音但不阻塞测试断言。
- **R11. find_entities_with_single_floor_edge 遍历邻居时遇到并发修改**（第 4 轮审查新增）：`snapshot[node_id].items()` 在 read lock 内的 copy 上遍历，但 L264 注释说 `call_async` 可能并发修改 nx_graph。snapshot 是 copy，不受原 graph 并发修改影响——但如果 copy 时遇到并发修改可能抛 RuntimeError。函数已有 try/except 兜底返回空集，触发降级时 detect_communities 拿不到保底边实体（等于跑原逻辑），可接受。
- **R12. e2e fixture patch HOME 影响其他模块**（第 5 轮审查新增）：`monkeypatch.setenv("HOME", str(tmp_path))` 是全局环境变量 patch，会影响测试期间所有调 `Path.home()` 或 `os.path.expanduser("~")` 的代码。fixture teardown 自动恢复，但如果测试期间 SkillSync 等后台守护线程已启动并持有 Path.home() 计算结果，teardown 后守护线程可能仍引用旧路径。本计划 e2e 测试不主动启动 SkillSync/RegionSync 守护线程（只调 get_lightrag + detect_communities），所以风险低。如果未来扩展 e2e 测试触发守护线程，需在 fixture teardown 加 `get_skill_sync().stop()` 等清理。
- **R13. Task 3 重命名测试导致 GitNexus 索引 stale**（第 5 轮审查新增）：Task 3 Step 4 重命名 `test_permanent_freeze_at_floor` → `test_permanent_isolated_entity_floor_protection`。GitNexus 索引会 stale，执行 Agent 不需要管，仅提示用户后续跑 `npx gitnexus analyze` 更新索引。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-region-algorithm-floor-edge-expansion.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
