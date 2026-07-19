# 脑区 dissolve 阈值恢复 + 孤岛保护 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `dissolve_shrunk_regions` 的 `shrink_threshold` 从越权改的 10 恢复到用户要求的 100；并在 dissolve 执行前加孤岛保护——只要有成员实体 total_degree ≤ 1 就取消本次 dissolve，shrink_count 不清零（下轮重新扫）。

**Architecture:** 两处独立改动：(1) `region_manager.dissolve_shrunk_regions` 默认参数 + 注释恢复 100；(2) 同函数在 `shrink_count >= shrink_rounds` 分支内、`_find_most_similar_neighbor` 调用之前加一道"孤岛检查"——遍历所有成员查 `nx_graph.degree(member)`，有任何一个 ≤ 1 就跳过 dissolve，shrink_count 保持原值不清零。缺省脑区保护（`is_default_region` L1059）已有，不重复加。

**Tech Stack:** Python 3.11, NetworkX, pytest

---

## 关键背景知识

### 当前 dissolve 逻辑（`dissolve_shrunk_regions` L1010-1173）

每个脑区节点 description 里存 `brain_meta_shrink_count:N`（萎缩计数）。每轮同步（24h）：

1. 拿脑区当前成员数 `current_size`
2. 跟阈值 `shrink_threshold` 比：
   - `current_size < shrink_threshold` → `shrink_count +1`
   - 否则 → `shrink_count 清零`
3. 如果 `shrink_count >= shrink_rounds`（默认 3）→ 执行 dissolve：把成员挪给最相似邻居脑区 + 删除脑区节点

### 越权改动历史

- **某 commit**：阈值从 3 提到 100（用户要求）
- **commit `4f03f10d` (2026-07-13)**：擅自把 100 改成 10，理由"100 误判正常小脑区导致僵尸"
- **本次 Task 1**：恢复 100（用户要求）
- **保留 4f03f10d 的另一改动**：0 成员脑区也累加 shrink_count（用户确认该删就删）

### 缺省脑区保护（已存在）

`dissolve_shrunk_regions` L1059 `if is_default_region(region.name): continue`——缺省脑区（`~/.niu/preferences.json` 配置的 `brain_regions`）直接跳过，不会被 dissolve。**本次不重复加**。

### 孤岛保护（本次新增）

dissolve 执行前，遍历该脑区所有成员，查每个成员的 `nx_graph.degree(member)`：
- 如果**所有**成员 `degree >= 2` → 安全，执行 dissolve
- 如果**有任何一个**成员 `degree <= 1` → 取消本次 dissolve，`shrink_count` 继续按规则累加（current_size < threshold 就 +1），下轮重新扫

**为什么 degree <= 1 是孤岛风险**：成员跟脑区的归属边是 1 条边。如果成员 total_degree = 1，说明它只有这一条边——删了脑区，成员就变孤岛（0 条边）。degree = 0 不可能（成员必须有归属边才会被算作成员），但防御性也跳过。

**为什么 shrink_count 继续累加而不是清零**：脑区本身已经萎缩多轮，是"该删"的状态。只是因为孤岛保护挡住了。下轮同步时再扫一次，如果孤岛成员后来多了别的边，dissolve 就能成功；如果一直只有 1 条边，脑区永远不被删（这是用户要的"避免孤岛"）。**注意**：shrink_count 不是"保持原值不变"，而是继续按 `current_size < threshold` 规则累加（每轮 +1），只是不清零。

### nx_graph 获取方式

参考 L605-619 已有模式：`self._adapter._get_rag()` → `rag.chunk_entity_relation_graph` → `kg._graph if hasattr(kg, "_graph") else kg`，配合 `graph_read_lock()`。

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `niu_api/internal/region_manager.py` | (1) `dissolve_shrunk_regions` 默认参数 + 注释恢复 100<br>(2) 新增 `_has_isolated_member` 辅助方法<br>(3) dissolve 执行前调孤岛检查 | 修改逻辑 + 新增方法 |
| `tests/test_region_manager_decay.py` | 新增 dissolve 阈值 + 孤岛保护测试 | 新增测试 |
| `tests/test_region_manager.py` | 更新现有 dissolve 测试断言（如依赖 10 阈值需改） | 修改测试 |

---

## Task 1: 恢复 shrink_threshold 默认值为 100

**Files:**
- Modify: `niu_api/internal/region_manager.py:1010-1030`（`dissolve_shrunk_regions` 函数签名 + docstring）

- [ ] **Step 1: 写失败测试**

在 `tests/test_region_manager_decay.py` 末尾追加：

```python
def test_dissolve_shrink_threshold_default_is_100():
    """dissolve_shrunk_regions 默认 shrink_threshold 必须是 100（用户要求，4f03f10d 越权改成 10 要恢复）"""
    import inspect
    from niu_api.internal.region_manager import RegionManager
    sig = inspect.signature(RegionManager.dissolve_shrunk_regions)
    default = sig.parameters["shrink_threshold"].default
    assert default == 100, \
        f"shrink_threshold 默认值必须是 100（用户要求），实际 {default}（4f03f10d 越权改成 10）"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py::test_dissolve_shrink_threshold_default_is_100 -v
```

预期：FAIL，`assert 10 == 100`

- [ ] **Step 3: 恢复默认值 + 注释 + docstring**

读 `niu_api/internal/region_manager.py:1010-1033`。用 Edit 工具替换：

**old_string**（实际从代码读，下面是预期形式）：

```python
    def dissolve_shrunk_regions(
        self,
        shrink_threshold: int = 10,  # 成员数 < 10 才判萎缩（原 100 误判正常小脑区）
        shrink_rounds: int = 3,
    ) -> list[str]:
        """Dissolve regions that have been shrinking for multiple sync cycles.

        A region is "shrunk" when its member count < shrink_threshold.
        After shrink_rounds consecutive sync cycles of being shrunk,
        the region is dissolved: members are reassigned to the most
        similar neighbor region, and the region node is deleted.

        Shrink tracking is stored in the region description field
        as ``brain_meta_shrink_count:N``.

        Args:
            shrink_threshold: Minimum members before region is "shrunk" (default 10)
                Lowered from 100 to 10 — 100 caused normal small regions
                (members < 100) to be flagged as shrunk, leading to zombie
                regions when dissolve flow was interrupted mid-way.
            shrink_rounds: Consecutive shrunk cycles before dissolution (default 3)

        Returns:
            List of dissolved region entity names.
        """
```

**new_string**：

```python
    def dissolve_shrunk_regions(
        self,
        shrink_threshold: int = 100,  # 成员数 < 100 才判萎缩（用户要求；4f03f10d 曾越权改成 10，已恢复）
        shrink_rounds: int = 3,
    ) -> list[str]:
        """Dissolve regions that have been shrinking for multiple sync cycles.

        A region is "shrunk" when its member count < shrink_threshold.
        After shrink_rounds consecutive sync cycles of being shrunk,
        the region is dissolved: members are reassigned to the most
        similar neighbor region, and the region node is deleted.

        **孤岛保护**（本次新增）：dissolve 执行前会检查所有成员的 total_degree，
        有任何一个成员 degree <= 1（删脑区会变孤岛）就取消本次 dissolve，
        shrink_count 继续按规则累加（current_size < threshold 就 +1），下轮重新扫。
        详见 `_has_isolated_member`。

        Shrink tracking is stored in the region description field
        as ``brain_meta_shrink_count:N``.

        Args:
            shrink_threshold: Minimum members before region is "shrunk" (default 100)
                用户明确要求 100（4f03f10d 曾越权改成 10 已恢复）。
            shrink_rounds: Consecutive shrunk cycles before dissolution (default 3)

        Returns:
            List of dissolved region entity names.
        """
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py::test_dissolve_shrink_threshold_default_is_100 -v
```

预期：PASS

- [ ] **Step 5: 跑回归测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py tests/test_region_manager.py -v 2>&1 | tail -20
```

预期：所有测试通过（如果 test_region_manager.py 有依赖 10 阈值的失败，记录下来下一步修）

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager_decay.py niu_api/internal/region_manager.py
git commit -m "fix(region_manager): 恢复 dissolve_shrunk_regions shrink_threshold 默认值到 100

4f03f10d (2026-07-13) 越权把 shrink_threshold 从 100 改成 10，
理由是\"100 误判正常小脑区导致僵尸\"。但 100 是用户明确要求的阈值，
越权改动必须恢复。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 新增 `_has_isolated_member` 辅助方法

**Files:**
- Modify: `niu_api/internal/region_manager.py`（在 `dissolve_shrunk_regions` 函数后插入新方法）
- Test: `tests/test_region_manager_decay.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_region_manager_decay.py` 末尾追加：

```python
def test_has_isolated_member_returns_true_when_any_member_degree_is_1():
    """有任何一个成员 total_degree=1 → 返回 True（会变孤岛）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    # 构造图：脑区 A + 成员 X（只有归属边，degree=1）+ 成员 Y（有归属边+知识边，degree=2）
    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员X", entity_type="concept")
    g.add_node("成员Y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员X", keywords="包含", weight=1.0)  # X 只有这条边
    g.add_edge("测试脑区", "成员Y", keywords="包含", weight=1.0)
    g.add_edge("成员Y", "其他实体", keywords="相关", weight=1.0)  # Y 有 2 条边

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])
    assert result is True, "成员X degree=1，应返回 True（会变孤岛）"


def test_has_isolated_member_returns_false_when_all_members_degree_ge_2():
    """所有成员 total_degree >= 2 → 返回 False（安全可解散）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员X", entity_type="concept")
    g.add_node("成员Y", entity_type="concept")
    g.add_node("其他实体A", entity_type="concept")
    g.add_node("其他实体B", entity_type="concept")
    g.add_edge("测试脑区", "成员X", keywords="包含", weight=1.0)
    g.add_edge("测试脑区", "成员Y", keywords="包含", weight=1.0)
    g.add_edge("成员X", "其他实体A", keywords="相关", weight=1.0)  # X 有 2 条边
    g.add_edge("成员Y", "其他实体B", keywords="相关", weight=1.0)  # Y 有 2 条边

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])
    assert result is False, "所有成员 degree>=2，应返回 False（安全）"


def test_has_isolated_member_returns_true_when_member_not_in_graph():
    """成员不在图里（数据不一致）→ 返回 True（保守，阻止 dissolve）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    # 成员 X 不在图里

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "成员不在图里应保守返回 True（阻止 dissolve）"


def test_has_isolated_member_empty_members_returns_false():
    """空成员列表 → 返回 False（脑区 0 成员，无孤岛风险）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member([])
    assert result is False, "空成员列表应返回 False"


def test_has_isolated_member_rag_none_returns_true():
    """RAG 实例拿不到 → 返回 True（保守，阻止 dissolve）"""
    from unittest import mock
    from niu_api.internal.region_manager import RegionManager

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = None

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "RAG 拿不到应保守返回 True（阻止 dissolve）"


def test_has_isolated_member_lowercase_lookup():
    """成员名小写查找（跟现有代码 region_manager.py L614-615 模式一致）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")  # 节点 id 小写
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    # 传入 "成员X"（大写），图里是 "成员x"（小写）——应小写化后找到
    result = manager._has_isolated_member(["成员X"])
    assert result is False, "小写查找后成员x degree=2 应返回 False"


def test_has_isolated_member_non_string_member_skipped():
    """成员名是 int 类型 → 跳过该成员，不影响其他成员检查"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    # 传入 [123, "成员X"]——123 是 int 跳过，"成员X" degree=2
    result = manager._has_isolated_member([123, "成员X"])
    assert result is False, "int 成员跳过，字符串成员 degree=2 应返回 False"


def test_has_isolated_member_nx_graph_none_returns_true():
    """nx_graph 是 None → 返回 True（保守阻止 dissolve）"""
    from unittest import mock
    from niu_api.internal.region_manager import RegionManager

    fake_rag = mock.MagicMock()
    # chunk_entity_relation_graph 没有 _graph 属性且本身是 None
    fake_rag.chunk_entity_relation_graph = None

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "nx_graph 是 None 应保守返回 True"


def test_has_isolated_member_second_member_isolated_returns_true():
    """第一个成员安全，第二个成员 degree=1 → 仍返回 True（遍历所有成员）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")  # degree=2（安全）
    g.add_node("成员y", entity_type="concept")  # degree=1（孤岛）
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("测试脑区", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)  # x 有 2 条边
    # y 只有归属边，degree=1

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])
    assert result is True, "第二个成员 degree=1 应返回 True（遍历所有成员）"


def test_has_isolated_member_degree_zero_returns_true():
    """成员 degree=0（理论上不可能但防御性）→ 返回 True"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    # 孤立节点，没有任何边
    g.add_node("成员x", entity_type="concept")

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "degree=0 应返回 True（防御性，<=1 都算孤岛风险）"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py -k "has_isolated_member" -v 2>&1 | tail -20
```

预期：FAIL，`AttributeError: 'RegionManager' object has no attribute '_has_isolated_member'`

- [ ] **Step 3: 实现 `_has_isolated_member` 方法**

读 `niu_api/internal/region_manager.py` 找到 `dissolve_shrunk_regions` 函数结束位置（约 L1173 `return dissolved`）。在它之后、`_find_most_similar_neighbor` 之前插入新方法。

用 Edit 工具，old_string 用 `def _find_most_similar_neighbor` 这一行（确认精确位置后），new_string 在前面加新方法。

新方法代码：

```python
    def _has_isolated_member(self, members: list[str]) -> bool:
        """检查成员列表里是否有任何一个成员 total_degree <= 1（删脑区会变孤岛）。

        用于 dissolve_shrunk_regions 执行前的安全检查：
        - 所有成员 degree >= 2 → 返回 False（安全，可解散）
        - 有任何一个成员 degree <= 1 → 返回 True（会变孤岛，阻止解散）
        - 成员不在图里 / RAG 拿不到 → 返回 True（保守，阻止解散）
        - 空成员列表 → 返回 False（脑区 0 成员，无孤岛风险）

        成员名小写查找：get_all_region_members 返回的成员名直接来自 nx_graph
        边数据（lightrag_manager.py L433-445），而 LightRAG graph 节点 id 全部
        小写（lightrag_manager.py L385 注释）。现有代码 region_manager.py L614-615
        也是 member.lower() 直接小写查找。本函数跟现有模式一致，直接小写。

        Args:
            members: 成员实体名列表（来自 get_all_region_members）

        Returns:
            True 表示有孤岛风险，应取消 dissolve；False 表示安全可解散
        """
        if not members:
            return False

        # 方法内 import（跟 region_manager.py L604 模式一致，避免循环 import）
        from niu_api.internal.lightrag_manager import graph_read_lock

        try:
            rag = self._adapter._get_rag()
            if rag is None:
                return True  # RAG 拿不到，保守阻止 dissolve

            kg = rag.chunk_entity_relation_graph
            nx_graph = kg._graph if hasattr(kg, "_graph") else kg
            if nx_graph is None:
                return True  # 图拿不到，保守阻止 dissolve

            with graph_read_lock():
                for member in members:
                    if not isinstance(member, str):
                        continue  # 防御性：跳过非字符串成员
                    # 直接小写查找（跟现有代码 region_manager.py L614-615 模式一致）
                    node_id = member.lower()
                    if node_id not in nx_graph:
                        # 成员不在图里（数据不一致），保守阻止 dissolve
                        return True
                    degree = nx_graph.degree(node_id)
                    if degree <= 1:
                        return True  # 找到孤岛风险成员

            return False  # 所有成员 degree >= 2

        except Exception as e:
            logger.warning("_has_isolated_member 检查失败，保守阻止 dissolve: %s", e)
            return True
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py -k "has_isolated_member" -v 2>&1 | tail -15
```

预期：6 个测试全部通过

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager_decay.py niu_api/internal/region_manager.py
git commit -m "feat(region_manager): 新增 _has_isolated_member 辅助方法

检查成员列表里是否有任何一个成员 total_degree <= 1（删脑区会变孤岛）。
供 dissolve_shrunk_regions 执行前调用的安全检查。

- 所有成员 degree >= 2 → False（安全）
- 有任何成员 degree <= 1 → True（孤岛风险，阻止解散）
- 成员不在图 / RAG 拿不到 → True（保守阻止）
- 空成员列表 → False（无孤岛风险）
- 大小写不敏感查找（先原始名再小写）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: dissolve 执行前加孤岛保护

**Files:**
- Modify: `niu_api/internal/region_manager.py:1087-1136`（`dissolve_shrunk_regions` 的 dissolve 执行分支）

- [ ] **Step 1: 写失败测试**

在 `tests/test_region_manager_decay.py` 末尾追加。**关键**：用 `RegionManager(adapter, ingester)` + `BrainRegionInfo` 正规实例化模式（跟现有 `tests/test_region_manager.py` L107/1571 一致），**不要**用 `mock.MagicMock(name="脑区A")`——MagicMock 的 `name` 是内部属性，`.name` 返回 child mock 而非字符串，会让 dissolve 代码全部失效。

```python
def test_dissolve_cancelled_when_member_has_only_one_edge():
    """dissolve 执行前发现有成员 degree=1 → 取消 dissolve，shrink_count 持久化（+1 后值）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    # 构造图：脑区A + 成员X（degree=1，孤岛风险）+ 成员Y（degree=2）
    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)  # x 只有这条边（degree=1）
    g.add_edge("脑区a", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员y", "其他实体", keywords="相关", weight=1.0)  # y 有 2 条边

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=2, representative="成员x",
            members=["成员x", "成员y"], updated_at=1745366400,
        )
    ]

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x", "成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=None):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 关键断言：dissolve 被取消（成员x degree=1 是孤岛风险）
    assert dissolved == [], "有孤岛风险成员时应取消 dissolve，返回空列表"
    # 脑区节点不应被删除
    adapter.delete_entity.assert_not_called()
    # shrink_count 应持久化（从 2 累加到 3，因为 current_size=2 < 100）
    ingester.inject_custom_kg.assert_called()
    call = ingester.inject_custom_kg.call_args
    entities = call.kwargs.get("entities", [])
    desc = entities[0].get("description", "") if entities else ""
    assert "brain_meta_shrink_count:3" in desc, \
        f"shrink_count 应累加到 3（从 2 +1），实际 {desc}"


def test_dissolve_executed_when_all_members_have_multiple_edges():
    """所有成员 degree >= 2 → 正常执行 dissolve"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体a", entity_type="concept")
    g.add_node("其他实体b", entity_type="concept")
    g.add_node("目标脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>目标")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)
    g.add_edge("脑区a", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体a", keywords="相关", weight=1.0)  # x degree=2
    g.add_edge("成员y", "其他实体b", keywords="相关", weight=1.0)  # y degree=2

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=2, representative="成员x",
            members=["成员x", "成员y"], updated_at=1745366400,
        )
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="2",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x", "成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 关键断言：dissolve 正常执行
    assert dissolved == ["脑区a"], "所有成员 degree>=2 时应正常 dissolve"
    adapter.delete_entity.assert_called_once_with("脑区a")


def test_dissolve_cancelled_persists_shrink_count_for_next_round():
    """dissolve 被孤岛保护取消后，shrink_count 持久化（累加后值），下轮重新扫"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    # 脑区A 成员X 只有 1 条边（孤岛风险），dissolve 应被取消
    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)  # x degree=1

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        )
    ]

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=None):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    assert dissolved == [], "孤岛保护应取消 dissolve"
    # shrink_count 从 2 累加到 3（current_size=1 < 100），持久化等下轮重新扫
    ingester.inject_custom_kg.assert_called()
    call = ingester.inject_custom_kg.call_args
    entities = call.kwargs.get("entities", [])
    desc = entities[0].get("description", "") if entities else ""
    assert "brain_meta_shrink_count:3" in desc, \
        f"shrink_count 应累加到 3 持久化，实际 {desc}"


def test_dissolve_zero_member_region_not_blocked_by_island_check():
    """0 成员脑区 → _has_isolated_member([]) 返回 False → 正常 dissolve（用户需求第3条）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=0, representative="",
            members=[], updated_at=1745366400,
        )
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="2",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"脑区a": []}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 0 成员脑区该删就删（孤岛保护不挡）
    assert dissolved == ["脑区a"], "0 成员脑区应正常 dissolve（无孤岛风险）"
    adapter.delete_entity.assert_called_once_with("脑区a")


def test_dissolve_default_region_skipped_no_island_check():
    """缺省脑区仍被 is_default_region 跳过，不进孤岛检查"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_shrink_count:5<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("文档库脑区", "成员x", keywords="包含", weight=1.0)  # x degree=1（孤岛风险）

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "文档库脑区", "description": "brain_meta_priority:permanent<SEP>brain_meta_shrink_count:5<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="文档库脑区", label="文档库", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        )
    ]

    # is_default_region 返回 True（缺省脑区）
    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=True), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"文档库脑区": ["成员x"]}):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 缺省脑区直接跳过，不进孤岛检查，不删
    assert dissolved == [], "缺省脑区应被跳过，不 dissolve"
    adapter.delete_entity.assert_not_called()


def test_dissolve_multiple_regions_one_blocked_one_succeeds():
    """多个脑区同时 dissolve：脑区A 被孤岛保护挡住、脑区B 正常 dissolve"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager, BrainRegionInfo

    g = nx.Graph()
    # 脑区A：成员x degree=1（孤岛风险）
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)
    # 脑区B：成员y degree=2（安全）
    g.add_node("脑区b", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("脑区b", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员y", "其他实体", keywords="相关", weight=1.0)
    # 目标脑区
    g.add_node("目标脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>目标")

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [
            {"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"},
            {"id": "脑区b", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"},
        ]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        ),
        BrainRegionInfo(
            name="脑区b", label="脑区b", community_id="2",
            description="测试", size=1, representative="成员y",
            members=["成员y"], updated_at=1745366400,
        ),
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="3",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.region_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x"], "脑区b": ["成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 脑区A 被孤岛保护挡住，脑区B 正常 dissolve
    assert dissolved == ["脑区b"], \
        f"应只 dissolve 脑区b（脑区a 被孤岛保护挡住），实际 {dissolved}"
    adapter.delete_entity.assert_called_once_with("脑区b")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py -k "dissolve_cancelled or dissolve_executed" -v 2>&1 | tail -20
```

预期：FAIL（dissolve 没有孤岛保护，会直接执行删除）

- [ ] **Step 3: 修改 `dissolve_shrunk_regions` 加孤岛保护**

读 `niu_api/internal/region_manager.py:1087-1136` 确认精确内容。用 Edit 工具替换：

**old_string**（直接从 `region_manager.py:1087-1137` 复制，必须逐字符一致）：

```python
            # Check dissolution threshold before writing shrink_count
            if shrink_count >= shrink_rounds:
                # Region will be dissolved — skip shrink_count write
                target_region = self._find_most_similar_neighbor(
                    region, existing_regions, dissolved_names
                )

                reassign_rels: list[dict] = []
                if target_region:
                    # Reassign members to target via belongs_to relations
                    # (injected AFTER delete to avoid duplicate edges)
                    for member in members:
                        reassign_rels.append({
                            "src_id": target_region.name,
                            "tgt_id": member,
                            "keywords": BELONGS_TO_RELATION,
                            "description": f"{member} belongs to region {target_region.label}",
                            "weight": INITIAL_WEIGHT,  # Unified initial weight
                            "source_id": REGION_SOURCE_ID,
                            "file_path": REGION_FILE_PATH,
                        })

                # Delete the dissolved region node first (cascades old belongs_to edges)
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    dissolved.append(region.name)
                    dissolved_names.add(region.name)
                    logger.info(
                        "解散萎缩脑区: %s (成员 %d, 萎缩 %d 轮, 归入 %s)",
                        region.name, current_size, shrink_count,
                        target_region.name if target_region else "无",
                    )
                    # Bug 1: 同步刷新 activation_mgr 缓存，避免 LLM 立即查
                    # brain_region_status 仍看到已删脑区（死循环）
                    self._refresh_activation_cache_after_delete(region.name)

                    # Now inject new belongs_to relations for target region
                    if target_region and reassign_rels:
                        try:
                            self._ingester.inject_custom_kg(
                                entities=[],
                                relationships=reassign_rels,
                                chunks=[],
                                source_id=REGION_SOURCE_ID,
                            )
                        except Exception as e:
                            logger.debug("重新分配成员失败 %s -> %s: %s",
                                         region.name, target_region.name, e)
                else:
                    logger.warning("解散脑区失败: %s", region.name)
            elif shrink_count > 0 or parsed.get("shrink_count", "0") != "0":
```

**new_string**：

```python
            # Check dissolution threshold
            # 注意：不能用 if/elif 结构——孤岛保护取消 dissolve 时仍需走持久化分支
            # Python 语义下 elif 挂在外层 if 上，进入外层 if 分支后不会 fall-through 到 elif
            # 所以用独立 if + continue 模式
            should_dissolve = shrink_count >= shrink_rounds and not self._has_isolated_member(members)
            should_skip_persist = False  # dissolve 成功后跳过持久化

            if shrink_count >= shrink_rounds and not should_dissolve:
                # 孤岛保护：shrink_count 达标但有成员 total_degree<=1（删脑区会变孤岛）
                # 取消本次 dissolve，shrink_count 继续按规则累加（已经在 L1082-1085 +1 过了），
                # 下轮重新扫。走下面的持久化分支写 shrink_count（累加后值）
                logger.info(
                    "脑区 %s 已萎缩 %d 轮，但有成员 total_degree<=1（删脑区会变孤岛），"
                    "取消本次 dissolve，shrink_count 持久化为 %d 等下轮重新扫",
                    region.name, shrink_count, shrink_count,
                )
                # 不设 should_skip_persist=True，让下面的持久化分支执行

            if should_dissolve:
                # Region will be dissolved — skip shrink_count write
                target_region = self._find_most_similar_neighbor(
                    region, existing_regions, dissolved_names
                )

                reassign_rels: list[dict] = []
                if target_region:
                    # Reassign members to target via belongs_to relations
                    # (injected AFTER delete to avoid duplicate edges)
                    for member in members:
                        reassign_rels.append({
                            "src_id": target_region.name,
                            "tgt_id": member,
                            "keywords": BELONGS_TO_RELATION,
                            "description": f"{member} belongs to region {target_region.label}",
                            "weight": INITIAL_WEIGHT,  # Unified initial weight
                            "source_id": REGION_SOURCE_ID,
                            "file_path": REGION_FILE_PATH,
                        })

                # Delete the dissolved region node first (cascades old belongs_to edges)
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    dissolved.append(region.name)
                    dissolved_names.add(region.name)
                    logger.info(
                        "解散萎缩脑区: %s (成员 %d, 萎缩 %d 轮, 归入 %s)",
                        region.name, current_size, shrink_count,
                        target_region.name if target_region else "无",
                    )
                    # Bug 1: 同步刷新 activation_mgr 缓存，避免 LLM 立即查
                    # brain_region_status 仍看到已删脑区（死循环）
                    self._refresh_activation_cache_after_delete(region.name)

                    # Now inject new belongs_to relations for target region
                    if target_region and reassign_rels:
                        try:
                            self._ingester.inject_custom_kg(
                                entities=[],
                                relationships=reassign_rels,
                                chunks=[],
                                source_id=REGION_SOURCE_ID,
                            )
                        except Exception as e:
                            logger.debug("重新分配成员失败 %s -> %s: %s",
                                         region.name, target_region.name, e)
                else:
                    logger.warning("解散脑区失败: %s", region.name)
                # dissolve 成功后跳过下面的持久化（已 dissolve 不需要写 shrink_count）
                should_skip_persist = True

            if not should_skip_persist and (shrink_count > 0 or parsed.get("shrink_count", "0") != "0"):
```

**关键改动**：
1. **不能用 if/elif 结构**——Python 语义下进入外层 if 分支后不会 fall-through 到 elif。改用 3 个独立 if + `should_skip_persist` 标志位
2. `should_dissolve = shrink_count >= shrink_rounds and not self._has_isolated_member(members)`——同时满足"达标"和"无孤岛风险"才 dissolve
3. 孤岛取消时只打日志，`should_skip_persist` 保持 False，让第 3 个 if 执行持久化（shrink_count 累加后值）
4. dissolve 成功时设 `should_skip_persist = True`，跳过持久化（避免给已删脑区写 shrink_count）

- [ ] **Step 4: 跑测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py -k "dissolve_cancelled or dissolve_executed" -v 2>&1 | tail -20
```

预期：3 个测试全部通过

- [ ] **Step 5: 跑回归测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager_decay.py tests/test_region_manager.py -v 2>&1 | tail -30
```

预期：所有测试通过

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager_decay.py niu_api/internal/region_manager.py
git commit -m "feat(region_manager): dissolve 执行前加孤岛保护

dissolve_shrunk_regions 在 shrink_count >= shrink_rounds 分支内、
执行 dissolve 之前调 _has_isolated_member 检查所有成员 total_degree：
- 有任何成员 degree <= 1（删脑区会变孤岛）→ 取消本次 dissolve，
  shrink_count 保持原值不清零，下轮重新扫
- 所有成员 degree >= 2 → 正常执行 dissolve

避免删除脑区后成员变孤岛（0 条边）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 更新已有测试 + 文档

**Files:**
- Modify: `tests/test_region_manager.py`（更新依赖 10 阈值的测试，如有）
- Modify: `docs/manual-vector-store.md`（补充 dissolve 阈值 + 孤岛保护说明）

- [ ] **Step 1: 修复现有测试避免被孤岛保护破坏**

现有 3 个 dissolve 测试（`tests/test_region_manager.py` L1574 `test_uses_batch_read_not_singular_get_region_members`、L1669 `test_dissolve_uses_real_members_for_reassign`、L1844 `test_dissolve_shrunk_regions_calls_remove_region_after_delete`）用 `_make_mock_adapter_and_ingester()` 构造 adapter，**不设置 `adapter._get_rag`**——MagicMock 默认让 `_has_isolated_member` 返回 True（成员不在 MagicMock 图里），dissolve 被取消，断言失败。

**修复方案**：在这 3 个测试的 `with patch(...)` 块里加 `patch.object(manager, "_has_isolated_member", return_value=False)`，明确表达"这些测试不关心孤岛保护，只测各自职责"。

读 `tests/test_region_manager.py` 找到这 3 个测试的 `with patch(...)` 块，逐个用 Edit 工具加 `patch.object(manager, "_has_isolated_member", return_value=False)`。

**示例**（L1574 测试）：

**old_string**（实际从代码读）：

```python
        with patch(
            "niu_api.internal.lightrag_manager.get_region_members",
            return_value=[],  # 单数版本读取失败返回空
        ), patch(
            "niu_api.internal.lightrag_manager.get_all_region_members",
            return_value={"Python脑区": ["Python", "Django", "NumPy", "Pandas", "Flask"]},
        ), patch(
            "niu_api.internal.region_manager.is_default_region",
            return_value=False,
        ), patch.object(
            manager, "_find_most_similar_neighbor", return_value=None,
        ):
            dissolved = manager.dissolve_shrunk_regions(
                shrink_threshold=100, shrink_rounds=3
            )
```

**new_string**：

```python
        with patch(
            "niu_api.internal.lightrag_manager.get_region_members",
            return_value=[],  # 单数版本读取失败返回空
        ), patch(
            "niu_api.internal.lightrag_manager.get_all_region_members",
            return_value={"Python脑区": ["Python", "Django", "NumPy", "Pandas", "Flask"]},
        ), patch(
            "niu_api.internal.region_manager.is_default_region",
            return_value=False,
        ), patch.object(
            manager, "_find_most_similar_neighbor", return_value=None,
        ), patch.object(
            manager, "_has_isolated_member", return_value=False,  # 跳过孤岛保护，本测试不关心
        ):
            dissolved = manager.dissolve_shrunk_regions(
                shrink_threshold=100, shrink_rounds=3
            )
```

对 L1669 和 L1844 测试做同样修改（在 `with patch(...)` 块里加 `patch.object(manager, "_has_isolated_member", return_value=False)`）。

**注意**：L1669 测试如果断言"reassign_rels 含 5 条成员"，加 `_has_isolated_member return_value=False` 后 dissolve 能正常执行，reassign_rels 仍为 5 条，断言不变。

- [ ] **Step 2: 检查现有测试是否依赖 10 阈值**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -rn "shrink_threshold" tests/ 2>&1 | head -20
```

如果有测试显式传 `shrink_threshold=10` 或依赖默认值是 10，改为 100 或不传（用默认 100）。

读相关测试文件确认，用 Edit 工具逐个修复。

- [ ] **Step 3: 跑全量 region 测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/ -k "region or decay or dissolve" -v 2>&1 | tail -30
```

预期：所有测试通过

- [ ] **Step 4: 文档更新**

读 `docs/manual-vector-store.md` 找脑区 dissolve 章节。先用 grep 确认插入点：

```bash
grep -n "dissolve\|萎缩\|shrink" REDACTED_USER_PATH/tools/ai-bot/docs/manual-vector-store.md | head -10
```

找到 dissolve 算法说明段落后，补充：

**新增段落**：

```markdown
**脑区 dissolve 阈值**（2026-07-19 恢复 + 孤岛保护）：

`dissolve_shrunk_regions` 默认 `shrink_threshold=100`——成员数 < 100 才判萎缩，连续 3 轮（`shrink_rounds=3`）后执行 dissolve。

**孤岛保护**（2026-07-19 新增）：dissolve 执行前会检查所有成员的 `total_degree`：
- 所有成员 `degree >= 2` → 安全，执行 dissolve（成员挪给最相似邻居脑区 + 删除脑区节点）
- 有任何一个成员 `degree <= 1` → **取消本次 dissolve**，`shrink_count` 保持原值不清零，下轮重新扫

这避免删除脑区后成员变孤岛（0 条边）。如果孤岛成员后来多了别的边，下轮 dissolve 就能成功；如果一直只有 1 条边，脑区永远不被删。

**缺省脑区保护**：`is_default_region` 跳过 `~/.niu/preferences.json` 配置的缺省脑区，永远不会被 dissolve（即使 0 成员）。

**历史**：2026-07-13 commit `4f03f10d` 曾越权把 `shrink_threshold` 从 100 改成 10，2026-07-19 恢复。
```

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager.py docs/manual-vector-store.md
git commit -m "docs+test: 更新 dissolve 测试 + 文档补充阈值恢复和孤岛保护说明

- 现有测试如有依赖 10 阈值的改为 100
- 文档补充 dissolve 阈值恢复 + 孤岛保护机制 + 缺省脑区保护

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 验证清单（所有 Task 完成后跑）

```bash
cd REDACTED_USER_PATH/tools/ai-bot

# 1. 所有新增测试通过
python -m pytest tests/test_region_manager_decay.py -v

# 2. 全量 region 相关测试无回归
python -m pytest tests/ -k "region or decay or dissolve" -v

# 3. Python 语法检查
python -c "import ast; ast.parse(open('niu_api/internal/region_manager.py').read())"
```

---

## Self-Review

### 1. Spec coverage 检查

- ✅ 恢复阈值 100 — Task 1
- ✅ 0 成员脑区处理（保留 4f03f10d 的"0 成员也累加"）— Task 1 不动这部分
- ✅ 孤岛检查（dissolve 执行前，有 degree<=1 就取消）— Task 3
- ✅ shrink_count 不清零（下轮重新扫）— Task 3
- ✅ 缺省脑区保护 — 已存在 L1059，不重复加

### 2. Placeholder 检查

无 TBD/TODO。所有代码段完整可执行。

### 3. 类型一致性检查

- `_has_isolated_member(members: list[str]) -> bool` — 函数签名一致
- Task 3 调用 `self._has_isolated_member(members)` — members 来自 L1062 `region_member_map.get(region.name, [])`，类型 `list[str]`，对齐

### 4. 风险点

- **孤岛检查性能**：每个将 dissolve 的脑区都要遍历成员查 degree。实际场景下 dissolve 候选数量有限（只有连续 3 轮 < 100 成员的脑区），每个脑区成员数 < 100，每个成员查 degree 是 O(1)，总开销 O(N*100) 可接受。
- **大小写处理**：`_has_isolated_member` 直接用 `member.lower()` 查找，跟现有代码 `region_manager.py L614-615` 模式一致。`get_all_region_members` 返回的成员名直接来自 nx_graph 边数据，节点 id 全部小写。
- **shrink_count 累加语义**：孤岛保护取消 dissolve 时，shrink_count 已经在 L1082-1085 按 `current_size < threshold` 规则 +1 过了，走第 3 个 if 持久化分支（不是 elif fall-through，代码已改为 3 个独立 if），持久化的是累加后的值（不是原值）。下轮重新扫时如果仍 < threshold 会继续 +1。这符合用户"不清零，下轮重新扫"的诉求。
- **持久化分支命中条件**：孤岛保护取消 dissolve 时走第 3 个 if 持久化分支，条件 `shrink_count > 0 or parsed.get("shrink_count", "0") != "0"` —— shrink_count >= 3 必然 > 0，命中，会持久化 shrink_count 累加后值。
- **graph_read_lock 不嵌套**：`_has_isolated_member` 的锁是独立的，不嵌套在别的锁里。`dissolve_shrunk_regions` 调用链：`get_all_region_members`（已锁+释放）→ `_has_isolated_member`（重新拿锁）→ `_find_most_similar_neighbor`（不锁）→ `delete_entity` → `inject_custom_kg`。
- **0 成员脑区 + 孤岛保护组合**：0 成员脑区 members=[]，`_has_isolated_member([])` 返回 False（无孤岛风险）→ 正常 dissolve。这符合用户需求第 3 条"0 成员脑区该删就删"。
- **缺省脑区保护**：L1059 `is_default_region` 跳过缺省脑区，不进孤岛检查。已有逻辑，本次不重复加。
- **_has_isolated_member 异常返回 True**：如果 nx_graph 拿取有持续性问题（如 LightRAG 重启中），脑区永远删不掉——但这是保守策略，符合用户"避免孤岛"诉求。日志会区分"孤岛风险"和"检查失败"。
- **shrink_count 序列化兼容性**：`_parse_description` 和 `_encode_description` 处理 `brain_meta_shrink_count` 的逻辑不变，本次修改不破坏现有序列化。

### 5. 测试覆盖度统计

| 文件 | 测试数 | 覆盖场景 |
|------|--------|----------|
| `tests/test_region_manager_decay.py`（Task 1 新增 1 个） | 1 | 默认值 100 |
| `tests/test_region_manager_decay.py`（Task 2 新增 10 个） | 10 | 空成员/RAG None/单成员 degree=1/单成员 degree=2/成员不在图/小写查找/int 成员跳过/nx_graph None/第二成员孤岛/degree=0 |
| `tests/test_region_manager_decay.py`（Task 3 新增 6 个） | 6 | 孤岛取消/正常执行/shrink_count 持久化/0 成员不挡/缺省脑区跳过/多脑区混合 |

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-19-region-dissolve-threshold-and-island-protection.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
