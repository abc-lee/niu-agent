# 脑区社区算法输入范围修正 + 全量根因修复 实施计划 (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 Leiden 社区算法输入范围（排除已直连脑区的实体），并同步修复全部已查明根因（0 实体读取 bug、24h 间隔无跨进程持久化、dissolve 异常静默吞掉），让脑区系统按预期单调收敛。

**Architecture:**
- 算法侧（根因 B）：`CommunityDetector.detect_communities` 在过滤脑区主节点后，加一步过滤"已直连脑区的实体"（通过 `get_all_region_members()` 拿已归属集合）。这样每次跑只算"游离实体"，已归属实体永不再参与。
- 清理侧（根因 B 配套）：`cleanup_stale_regions` 的 stale 判定调整——当 `current_members`（已归属）跟 `community_members`（游离）无交集时，是 Task 1 排除导致的天然无交集，不是脑区过时。改为：只有 `current_members` 本身为空才判 stale；`best_jaccard == 0` 但 `current_members` 非空时跳过。过时脑区清理交给 `dissolve_shrunk_regions`（基于成员数持续 < 100）。
- 读取侧（根因 C）：`_refresh_activation_manager` 把"循环逐个调 `get_region_members`"换成"一次性调 `get_all_region_members` + 失败保护 + 覆盖率检查"，避免单 region 读取异常污染全部内存映射。
- 启动触发侧（根因 A）：`_sync_loop` 进入循环前调 `_load_status()`，若距上次同步不足 `sync_interval * 0.9` 则等待剩余时间再跑。让 24h 间隔跨进程持久化。加 `elapsed < 0` 保护系统时间回拨。
- 清理侧（根因 D）：`_merge_and_dissolve` 的 dissolve 异常从 `logger.debug` 升级到 `logger.warning`，让 dissolve 失败可见。配合 Task 2 修复后 `dissolve_shrunk_regions` 能读到真实成员数，shrink_count 能正常累加。
- LLM 起名逻辑保留不变——只要输入正确，起名就是给真正的新社区起名，不会产生语义变体堆积。

**Tech Stack:** Python 3.11+，LightRAG NetworkX 图，leidenalg/igraph 社区检测，pytest 测试。

---

## 修改的文件

| 文件 | 改动 | 责任 |
|------|------|------|
| `niu_api/internal/region_detector.py` L137-150 之后 | `detect_communities` 加"排除已归属实体"步骤 | 根因 B：算法输入范围修正 |
| `niu_api/internal/region_manager.py` L817-837 | `cleanup_stale_regions` stale 判定调整：`current_members` 非空时不判 stale | 根因 B 配套：避免 Task 1 排除导致误删 |
| `agent/injector/region_sync.py` L363-373 | `_refresh_activation_manager` 改批量读取 + 失败保护 + 覆盖率检查 | 根因 C：0 实体 bug 修复 |
| `agent/injector/region_sync.py` L610-641 | `_sync_loop` 进入循环前调 `_load_status` 跳过首次同步 + 系统时间回拨保护 | 根因 A：24h 间隔跨进程持久化 |
| `agent/injector/region_sync.py` L493-494, L525-526 | merge/dissolve 异常从 `logger.debug` 升级到 `logger.warning` | 根因 D：dissolve 异常可见 |
| `tests/test_region_detector.py` | 新增测试：已归属实体被排除 + 现有测试 mock `get_all_region_members` | 验证根因 B |
| `tests/test_region_sync.py` | 新增 3 个测试：批量读取失败保护、status file 跳过首次同步、dissolve 异常升级 | 验证根因 A/C/D |
| `tests/test_region_manager.py` | 新增测试：Task 1 排除后 `cleanup_stale_regions` 不误删 | 验证根因 B 配套 |

---

## Task 1: 算法输入范围修正——排除已直连脑区的实体

**Files:**
- Modify: `niu_api/internal/region_detector.py:100-205`（`detect_communities` 方法）
- Test: `tests/test_region_detector.py`

**背景**：当前 `detect_communities` L137-150 只过滤了脑区主节点（24 个"XX脑区"节点），但没过滤直连脑区的实体（4000+ 条"包含"边的对端）。导致每次 Leiden 都把同一批已归属实体重新聚成社区。

**修改点**：在 L150 之后（过滤脑区主节点之后，L152 检查图谱大小之前），加一步过滤"已直连脑区的实体"。

- [ ] **Step 1: 写失败测试——已归属实体不参与算法**

在 `tests/test_region_detector.py` 追加测试：

```python
def test_detect_communities_excludes_entities_connected_to_regions():
    """直连脑区的实体（一级成员）应被排除出 Leiden 算法输入"""
    from unittest import mock
    from niu_api.internal.region_detector import CommunityDetector

    # 构造图快照：3 个脑区主节点 + 5 个已归属实体 + 5 个游离实体
    nodes = [
        {"name": "智家脑区", "type": "brainregion"},
        {"name": "工作脑区", "type": "brainregion"},
        {"name": "聊天脑区", "type": "brainregion"},
        # 已归属实体（直连脑区，应被排除）
        {"name": "已归属实体1", "type": "technology"},
        {"name": "已归属实体2", "type": "technology"},
        {"name": "已归属实体3", "type": "technology"},
        # 游离实体（未直连脑区，应保留参与算法）
        {"name": "游离实体A", "type": "concept"},
        {"name": "游离实体B", "type": "concept"},
        {"name": "游离实体C", "type": "concept"},
    ]
    edges = [
        # 已归属实体 → 脑区（包含边）
        {"source": "智家脑区", "target": "已归属实体1", "keywords": "包含"},
        {"source": "智家脑区", "target": "已归属实体2", "keywords": "包含"},
        {"source": "工作脑区", "target": "已归属实体3", "keywords": "包含"},
        # 游离实体之间相互连接（应被聚成社区）
        {"source": "游离实体A", "target": "游离实体B", "keywords": "相关"},
        {"source": "游离实体B", "target": "游离实体C", "keywords": "相关"},
        {"source": "游离实体A", "target": "游离实体C", "keywords": "相关"},
    ]

    fake_adapter = mock.MagicMock()
    fake_adapter.get_graph_snapshot = mock.Mock(return_value={"nodes": nodes, "edges": edges})

    # mock 源模块的 get_all_region_members（函数级 import 会从源模块拿）
    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={
            "智家脑区": ["已归属实体1", "已归属实体2"],
            "工作脑区": ["已归属实体3"],
            "聊天脑区": [],
        },
    ):
        detector = CommunityDetector(fake_adapter)
        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

    all_partition_members = []
    for p in result.partitions:
        all_partition_members.extend(p.entity_names)
    assert "已归属实体1" not in all_partition_members
    assert "已归属实体2" not in all_partition_members
    assert "已归属实体3" not in all_partition_members
    assert "游离实体A" in all_partition_members
    assert "游离实体B" in all_partition_members
    assert "游离实体C" in all_partition_members
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd <repo_root> && python -m pytest tests/test_region_detector.py::test_detect_communities_excludes_entities_connected_to_regions -v
```

Expected: FAIL（已归属实体出现在分区里）

- [ ] **Step 3: 修改 `detect_communities` 加排除步骤**

在 `niu_api/internal/region_detector.py` 的 `detect_communities` 方法里，L150（过滤脑区主节点的 if 块结尾）之后，L152（`if len(nodes) < min_graph_size`）之前，加：

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
                "排除 %d 个已归属实体（直连脑区的一级成员），剩余 %d 个游离实体参与算法",
                before_count - len(nodes), len(nodes),
            )
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd <repo_root> && python -m pytest tests/test_region_detector.py::test_detect_communities_excludes_entities_connected_to_regions -v
```

Expected: PASS

- [ ] **Step 5: 给现有 region_detector 测试 mock `get_all_region_members` 避免触发真实 LightRAG**

现有 18 个测试会因 Task 1 加的真实 `get_all_region_members()` 调用而触发 LightRAG 初始化（3-5 秒）。在 `tests/test_region_detector.py` 顶部加 autouse fixture：

```python
import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _mock_get_all_region_members(request):
    """所有 region_detector 测试默认 mock get_all_region_members 返回空 dict
    （排除步骤 early skip），避免触发真实 LightRAG 初始化。
    需要测试排除逻辑的用例可以覆盖此 fixture。
    """
    with mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={},
    ):
        yield
```

- [ ] **Step 6: 跑全量 region_detector 测试，确保没回归**

```bash
cd <repo_root> && python -m pytest tests/test_region_detector.py -v
```

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
cd <repo_root> && git add niu_api/internal/region_detector.py tests/test_region_detector.py
git commit -m "fix(region_detector): 排除已直连脑区的实体，避免重复发现同一社区

每次跑 Leiden 前排除已通过'包含'边直连脑区的实体（脑区一级成员），
只对游离实体跑算法。这样已归属实体永不再参与，算法单调收敛。

测试加 autouse fixture mock get_all_region_members 返回空 dict，
避免现有 18 个测试触发真实 LightRAG 初始化。"
```

---

## Task 2: 修复 cleanup_stale_regions 误删——Task 1 排除后 Jaccard 失效兜底

**Files:**
- Modify: `niu_api/internal/region_manager.py:817-837`（`cleanup_stale_regions` 的 stale 判定分支）
- Test: `tests/test_region_manager.py`

**背景**：Task 1 排除已归属实体后，`cleanup_stale_regions` L791-802 的 Jaccard 比对会失效：`current_members`（已归属实体，100+ 个）跟 `community_members`（游离实体）天然无交集，`best_jaccard == 0`，触发 L817-826 删除所有非默认脑区。

**修改点**：L817 的 `else` 分支（`best_jaccard == 0`）增加保护——只有当 `current_members` 本身为空（脑区真的没成员了）才判 stale 删除；`current_members` 非空但 `best_jaccard == 0` 时（Task 1 排除导致的天然无交集），跳过不删也不漂移。过时脑区清理交给 `dissolve_shrunk_regions`（基于成员数持续 < 100）。

- [ ] **Step 1: 写失败测试——Task 1 排除后脑区不被误删**

在 `tests/test_region_manager.py` 追加测试：

```python
def test_cleanup_stale_regions_skips_delete_when_region_has_members_but_no_overlap():
    """脑区有成员但跟 community 无交集（Task 1 排除导致）时，不应删除"""
    from unittest import mock
    from niu_api.internal.region_manager import RegionManager
    from niu_api.internal.region_detector import CommunityDetectionResult, RegionPartition

    # 构造：脑区"智家脑区"有 100 个已归属成员，但 community 里全是游离实体（无交集）
    fake_region = mock.MagicMock()
    fake_region.name = "智家脑区"
    fake_region.label = "智家"

    manager = RegionManager.__new__(RegionManager)  # 跳过 __init__
    manager._adapter = mock.MagicMock()

    with mock.patch(
        "niu_api.internal.region_manager.RegionManager.get_all_regions",
        return_value=[fake_region],
    ), mock.patch(
        "niu_api.internal.region_manager.is_default_region",
        return_value=False,
    ), mock.patch(
        "niu_api.internal.lightrag_manager.get_all_region_members",
        return_value={"智家脑区": [f"已归属实体{i}" for i in range(100)]},
    ):
        # partition 里只有游离实体（跟脑区成员无交集）
        partition = RegionPartition(
            region_id=0, region_name="region_0",
            entity_names=["游离实体A", "游离实体B"],
            entity_types={}, edge_count=1, modularity_score=0.5,
            entity_name_to_type={},
        )
        detection_result = CommunityDetectionResult(
            partitions=[partition], total_nodes=2, total_edges=1,
            total_regions=1, modularity=0.5, timestamp="2026-07-06T00:00:00Z",
        )

        # dry_run=False 验证不会真的删除
        removed, drifted, drifted_cids = manager.cleanup_stale_regions(
            detection_result, dry_run=False,
        )

    # 断言：脑区没被删除（current_members 非空，best_jaccard==0 但不删）
    assert removed == [], "脑区有成员时不应因 Jaccard=0 被删除"
    assert drifted == [], "也不应判漂移"
    # delete_entity 不应被调用
    manager._adapter.delete_entity.assert_not_called()
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd <repo_root> && python -m pytest tests/test_region_manager.py::test_cleanup_stale_regions_skips_delete_when_region_has_members_but_no_overlap -v
```

Expected: FAIL（当前代码 L817 `best_jaccard == 0` 会删除）

- [ ] **Step 3: 修改 `cleanup_stale_regions` 的 stale 分支**

在 `niu_api/internal/region_manager.py` 把 L817-837 的 `else` 分支替换为：

```python
            else:
                # best_jaccard == 0：脑区成员跟所有社区都无交集
                # 三种情况：
                # 1. region.name 不在 region_member_map 里 → get_all_region_members 读取失败漏掉
                #    该脑区，跳过避免误删（保守）
                # 2. region.name 在 map 里且 current_members 为空 → 脑区真的没成员了，判 stale 删除
                # 3. region.name 在 map 里且 current_members 非空 → Task 1 排除已归属实体导致的
                #    天然无交集（脑区成员是已归属，社区里是游离），不删除不漂移
                #    过时脑区清理交给 dissolve_shrunk_regions（基于成员数持续 < 100）
                if region.name not in region_member_map:
                    # 读取失败漏掉该脑区，不判 stale 避免误删
                    logger.warning(
                        "脑区 %s 不在 get_all_region_members 返回结果中（读取失败？），跳过 stale 判定避免误删",
                        region.name,
                    )
                elif not current_members:
                    # 脑区在 map 里且成员确实为空，判 stale 删除
                    if not dry_run:
                        delete_result = self._adapter.delete_entity(region.name)
                        if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                            removed.append(region.name)
                            logger.info(
                                "删除空成员脑区: %s (无成员，Jaccard=0)",
                                region.name,
                            )
                        else:
                            logger.warning(
                                "删除空成员脑区失败: %s — %s",
                                region.name,
                                delete_result.get("message", "unknown") if isinstance(delete_result, dict) else "error",
                            )
                    else:
                        logger.info(
                            "[dry_run] 将删除空成员脑区: %s (无成员，Jaccard=0)",
                            region.name,
                        )
                else:
                    # 脑区有成员但跟社区无交集（Task 1 排除导致），跳过
                    logger.debug(
                        "脑区 %s 有 %d 成员但跟当前社区无交集（Task 1 排除已归属实体），跳过 stale 判定",
                        region.name, len(current_members),
                    )
```

- [ ] **Step 4: 更新受影响的现有测试**

v2 修改 `cleanup_stale_regions` 的 stale 分支后，`tests/test_region_manager.py` 里两个现有测试的语义会变化：

**测试 1：`test_removes_stale_region_nodes`（L440-489）**

原意：OldRegion脑区有成员 ["OldEntity", "LegacyLib"]，跟 community_0 无交集 → 删除。
v2 新逻辑：有成员但无交集（Task 1 排除导致）→ 跳过不删。
过时脑区清理职责已转移到 `dissolve_shrunk_regions`（基于成员数持续 < 100）。

把这个测试改为验证新行为——"有成员但无交集时跳过"：

```python
    @pytest.mark.asyncio
    async def test_removes_stale_region_nodes(self):
        """v2: 脑区有成员但跟社区无交集（Task 1 排除导致）时跳过，不删除

        过时脑区清理职责转移到 dissolve_shrunk_regions（基于成员数持续 < 100）。
        """
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "OldRegion脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_99 | brain_meta_size:2 | brain_meta_representative:OldEntity | brain_meta_updated_at:1745366400",
                },
            ],
        }

        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django"],
                entity_types={"language": 1, "framework": 1},
                edge_count=1,
                modularity_score=0.15,
            ),
        ])

        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {
                    "Python脑区": ["Python", "Django"],
                    "OldRegion脑区": ["OldEntity", "LegacyLib"],
                },
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        # v2: OldRegion 有成员但无交集（Task 1 排除导致），跳过不删
        assert removed == []
        assert drifted == []
        adapter.delete_entity.assert_not_called()
```

**测试 2：`test_cleanup_all_when_no_current_partitions`（L533-575）**

原意：两个脑区都有成员，无分区可匹配 → 全部删除。
v2 新逻辑：有成员但无交集 → 全部跳过。

把这个测试改为验证新行为——"有成员时跳过"+ 新增"成员为空时删除"的验证：

```python
    @pytest.mark.asyncio
    async def test_cleanup_all_when_no_current_partitions(self):
        """v2: 脑区有成员时跳过（Task 1 排除导致），成员为空时才删"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "EmptyRegion脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_1 | brain_meta_size:0 | brain_meta_representative: | brain_meta_updated_at:1745366400",
                },
            ],
        }

        empty_partition = CommunityDetectionResult(
            partitions=[],
            total_nodes=0,
            total_edges=0,
            total_regions=0,
            modularity=0.0,
            timestamp="2026-04-24T12:00:00+00:00",
        )

        # Mock: Python脑区有成员（跳过），EmptyRegion脑区成员为空（删除）
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {
                    "Python脑区": ["Python", "Django"],
                    "EmptyRegion脑区": [],
                },
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(empty_partition)

        # v2: 只有空成员脑区被删，有成员的跳过
        assert len(removed) == 1
        assert "EmptyRegion脑区" in removed
        adapter.delete_entity.assert_called_once_with("EmptyRegion脑区")
```

- [ ] **Step 5: 跑测试验证通过**

```bash
cd <repo_root> && python -m pytest tests/test_region_manager.py::test_cleanup_stale_regions_skips_delete_when_region_has_members_but_no_overlap tests/test_region_manager.py::TestCleanupStaleRegions -v
```

Expected: 全部 PASS（新测试 + 更新的两个现有测试都通过）

- [ ] **Step 6: 跑现有 region_manager 全量测试，确保没回归**

```bash
cd <repo_root> && python -m pytest tests/test_region_manager.py -v
```

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
cd <repo_root> && git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "fix(region_manager): cleanup_stale_regions 不因 Task 1 排除导致误删脑区

Task 1 排除已归属实体后，cleanup_stale_regions 的 Jaccard 比对失效：
current_members（已归属）跟 community_members（游离）天然无交集，
best_jaccard==0 会误删所有非默认脑区。

修改 stale 判定三分支：
1. 脑区不在 region_member_map 里（读取失败漏掉）→ 跳过避免误删
2. 脑区在 map 里且成员为空 → 删除（脑区真的没成员了）
3. 脑区在 map 里且成员非空 → 跳过（Task 1 排除导致天然无交集）

过时脑区清理交给 dissolve_shrunk_regions（基于成员数持续 < 100）。
更新 test_removes_stale_region_nodes 和 test_cleanup_all_when_no_current_partitions
适配新行为。"
```

---

## Task 3: 修复 0 实体 bug——批量读取 + 失败保护 + 覆盖率检查

**Files:**
- Modify: `agent/injector/region_sync.py:363-373`（`_refresh_activation_manager` 的成员读取循环）
- Test: `tests/test_region_sync.py`

**背景**：当前 L366-373 在 `for region in all_regions` 循环里逐个调 `get_region_members`，单 region 读取异常被 `except: logger.warning` 静默吞掉，`region.members` 保持 `[]`，污染 `_entity_to_region` 内存映射。GraphML 实测证明注入是成功的（14 个"0 实体"脑区实际有 108-114 条"包含"边），问题在读取→内存映射链路。

**修改点**：把循环逐个调用改为**一次性调 `get_all_region_members()`**，加"批量读取失败 early return"和"覆盖率检查"（返回的脑区数 vs 总脑区数，覆盖率 < 50% 视为读取失败）。

- [ ] **Step 1: 写失败测试——批量读取失败时不污染内存映射**

在 `tests/test_region_sync.py` 追加测试：

```python
def test_refresh_activation_manager_does_not_overwrite_on_bulk_read_failure(monkeypatch):
    """get_all_region_members 返回空（读取失败）时，不应覆盖现有 _entity_to_region 映射"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock

    sync = RegionSync(sync_interval=86400)

    # 构造已有激活管理器
    fake_existing_mgr = mock.MagicMock()
    fake_existing_mgr._entity_to_region = {"existing_entity": "existing_region脑区"}
    fake_existing_mgr._member_counts = {"existing_region脑区": 1}

    # 构造 fake region（用 spec 避免 MagicMock name 特殊参数问题）
    fake_region = mock.MagicMock()
    fake_region.name = "智家脑区"
    fake_region.members = []
    fake_region.description = "d1"

    with mock.patch("agent.brain_tools.get_activation_mgr", return_value=fake_existing_mgr), \
         mock.patch("agent.brain_tools.set_activation_mgr") as mock_set, \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGIngester"), \
         mock.patch(
             "niu_api.internal.region_manager.RegionManager.get_all_regions",
             return_value=[fake_region],
         ), \
         mock.patch(
             "niu_api.internal.lightrag_manager.get_all_region_members",
             return_value={},  # 空 dict 模拟读取失败
         ):
        sync._refresh_activation_manager({})

    # 断言：early return，initialize_from_regions 没被调用
    fake_existing_mgr.initialize_from_regions.assert_not_called()
    # set_activation_mgr 也不应被调用（early return 前不设置）
    mock_set.assert_not_called()
```

- [ ] **Step 2: 写第二个测试——覆盖率 < 50% 时也不覆盖**

在 `tests/test_region_sync.py` 追加：

```python
def test_refresh_activation_manager_skips_when_coverage_too_low(monkeypatch):
    """get_all_region_members 返回部分脑区（覆盖率 < 50%）时，不覆盖现有映射"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock

    sync = RegionSync(sync_interval=86400)

    fake_existing_mgr = mock.MagicMock()

    # 3 个脑区，但 get_all_region_members 只返回 1 个（覆盖率 33% < 50%）
    fake_regions = []
    for name in ["智家脑区", "工作脑区", "聊天脑区"]:
        r = mock.MagicMock()
        r.name = name
        r.members = []
        r.description = "d"
        fake_regions.append(r)

    with mock.patch("agent.brain_tools.get_activation_mgr", return_value=fake_existing_mgr), \
         mock.patch("agent.brain_tools.set_activation_mgr"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGIngester"), \
         mock.patch(
             "niu_api.internal.region_manager.RegionManager.get_all_regions",
             return_value=fake_regions,
         ), \
         mock.patch(
             "niu_api.internal.lightrag_manager.get_all_region_members",
             return_value={"智家脑区": ["实体1"]},  # 只返回 1/3 脑区
         ):
        sync._refresh_activation_manager({})

    fake_existing_mgr.initialize_from_regions.assert_not_called()
```

- [ ] **Step 3: 跑测试验证失败**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_refresh_activation_manager_does_not_overwrite_on_bulk_read_failure tests/test_region_sync.py::test_refresh_activation_manager_skips_when_coverage_too_low -v
```

Expected: FAIL

- [ ] **Step 4: 修改 `_refresh_activation_manager` 改为批量读取 + 覆盖率检查**

在 `agent/injector/region_sync.py` 的 `_refresh_activation_manager` 方法里，把 L363-373（"BUG 2 fix" 注释块 + 循环逐个调 `get_region_members`）替换为：

```python
            # 批量读取所有脑区的成员（一次性调 get_all_region_members）
            # 避免循环逐个调用时单 region 异常污染整个 _entity_to_region
            from niu_api.internal.lightrag_manager import get_all_region_members as lightrag_get_all_region_members
            try:
                region_members_map = lightrag_get_all_region_members()
            except Exception as e:
                logger.warning(
                    "[RegionSync] get_all_region_members 批量读取异常，跳过激活管理器刷新: %s",
                    e,
                )
                stats["errors"].append(f"get_all_region_members: {e}")
                return

            # 批量读取返回空 = 图未就绪或读取失败，不覆盖现有映射
            if not region_members_map:
                logger.warning(
                    "[RegionSync] get_all_region_members 返回空（图未就绪或读取失败），跳过激活管理器刷新"
                )
                return

            # 覆盖率检查：返回的脑区数 vs 总脑区数，< 50% 视为部分失败，不覆盖
            total_regions = len(all_regions)
            covered_regions = sum(1 for r in all_regions if r.name in region_members_map)
            if total_regions > 0 and covered_regions / total_regions < 0.5:
                logger.warning(
                    "[RegionSync] get_all_region_members 覆盖率 %.0f%% (%d/%d) < 50%%，跳过激活管理器刷新避免部分失败污染",
                    covered_regions / total_regions * 100, covered_regions, total_regions,
                )
                return

            # 把成员填充到 region 对象上（缺失的 region 保持空 list）
            for region in all_regions:
                region.members = region_members_map.get(region.name, [])
```

- [ ] **Step 5: 跑测试验证通过**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_refresh_activation_manager_does_not_overwrite_on_bulk_read_failure tests/test_region_sync.py::test_refresh_activation_manager_skips_when_coverage_too_low -v
```

Expected: PASS

- [ ] **Step 6: 跑现有 region_sync 全量测试**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py -v
```

Expected: 全部 PASS。如果有测试 mock 了 `get_region_members`（单数），更新为 `get_all_region_members`（复数）。

- [ ] **Step 7: Commit**

```bash
cd <repo_root> && git add agent/injector/region_sync.py tests/test_region_sync.py
git commit -m "fix(region_sync): 批量读取脑区成员 + 覆盖率检查，读取失败不污染 _entity_to_region

_refresh_activation_manager 从循环逐个调 get_region_members 改为一次性调
get_all_region_members。加覆盖率检查：返回脑区数 < 总数 50% 视为部分失败，
early return 保留现有映射不覆盖。

修复'脑区实体数=0'bug：GraphML 实测证明注入成功（108-114 条包含边），
问题全在读取→内存映射链路。"
```

---

## Task 4: 24h 间隔跨进程持久化——`_sync_loop` 跳过首次同步

**Files:**
- Modify: `agent/injector/region_sync.py:610-641`（`_sync_loop` 方法）
- Test: `tests/test_region_sync.py`

**背景**：当前 `_sync_loop` L635-641 进入 `while True` 后无条件 `run_sync()`，`_load_status()` (L543) 定义了但全代码库无调用点（只有测试调用）。`last_region_sync.json` 里 `last_sync` 字段从未参与决策。导致每次重启必然触发首次同步，24h 间隔只在进程内生效。

**修改点**：在 `_sync_loop` 进入 `while True` 之前，调 `_load_status()` 读 `last_sync`，若距上次同步不足 `sync_interval * 0.9`，则等待剩余时间。加 `elapsed < 0` 保护系统时间回拨。

- [ ] **Step 1: 写测试——距上次同步不足 24h 则跳过首次同步**

在 `tests/test_region_sync.py` 追加测试：

```python
def test_sync_loop_skips_first_sync_when_recently_synced(tmp_path):
    """距上次同步不足 sync_interval*0.9 时，_sync_loop 跳过首次同步"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock
    from datetime import datetime, timedelta
    import json

    sync = RegionSync(sync_interval=86400)
    sync._status_file = tmp_path / "last_region_sync.json"

    recent_time = (datetime.now() - timedelta(minutes=5)).isoformat()
    sync._status_file.write_text(json.dumps({
        "last_sync": recent_time,
        "stats": {"regions_created": 0},
    }))

    run_sync_called = []
    sync.run_sync = mock.Mock(side_effect=lambda: run_sync_called.append(True))

    # 用真实 threading.Event，通过 set 控制退出
    sync._brain_ready.set()
    sync._stop_event.set()  # 让所有 wait 立即返回 True，循环跑一次就退出

    with mock.patch(
        "agent.injector.region_sync.wait_lightrag_ready", return_value=True
    ):
        sync._sync_loop()

    # 断言：run_sync 没被调用（距上次同步 5 分钟 < 24h*0.9）
    # _stop_event 已 set，所以 _stop_event.wait(wait_seconds) 立即返回 True，
    # 然后 while True 里 _stop_event.wait(sync_interval) 也立即返回 True 退出
    assert len(run_sync_called) == 0, "距上次同步不足 24h，不应触发 run_sync"
```

- [ ] **Step 2: 写第二个测试——系统时间回拨（last_sync 是未来时间）不卡住**

在 `tests/test_region_sync.py` 追加：

```python
def test_sync_loop_handles_future_last_sync(tmp_path):
    """last_sync 是未来时间（系统回拨）时，不卡住等待"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock
    from datetime import datetime, timedelta
    import json

    sync = RegionSync(sync_interval=86400)
    sync._status_file = tmp_path / "last_region_sync.json"

    # last_sync 是 1 天后的未来时间
    future_time = (datetime.now() + timedelta(days=1)).isoformat()
    sync._status_file.write_text(json.dumps({
        "last_sync": future_time,
        "stats": {},
    }))

    run_sync_called = []
    sync.run_sync = mock.Mock(side_effect=lambda: run_sync_called.append(True))

    sync._brain_ready.set()
    sync._stop_event.set()  # 立即退出

    with mock.patch(
        "agent.injector.region_sync.wait_lightrag_ready", return_value=True
    ):
        sync._sync_loop()

    # 断言：run_sync 应该被调用（未来时间应被视为 elapsed=0，立即跑首次同步）
    assert len(run_sync_called) >= 1, "未来时间应被视为 elapsed<=0，立即跑首次同步"
```

- [ ] **Step 3: 跑测试验证失败**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_sync_loop_skips_first_sync_when_recently_synced tests/test_region_sync.py::test_sync_loop_handles_future_last_sync -v
```

Expected: FAIL

- [ ] **Step 4: 修改 `_sync_loop` 加 status file 跳过逻辑 + 时间回拨保护**

在 `agent/injector/region_sync.py` 的 `_sync_loop` 方法里，把 L632-641 替换为：

```python
        # Wait for brain region initialization to complete before first sync.
        # This prevents the race where _refresh_activation_manager() calls
        # manager.get_all_regions() before create_default_regions() has finished.
        if not self._brain_ready.wait(timeout=60):
            logger.warning("[RegionSync] Brain region init not signaled after 60s, proceeding anyway")

        # 跨进程 24h 间隔持久化：读 status file，若距上次同步不足 sync_interval*0.9 则等待
        # 避免"每次重启都触发首次同步"——24h 间隔不仅在进程内生效，跨重启也生效
        try:
            status = self._load_status()
            last_sync_str = status.get("last_sync") if status else None
            if last_sync_str:
                try:
                    last_sync = datetime.fromisoformat(last_sync_str)
                    elapsed = (datetime.now() - last_sync).total_seconds()
                    # 系统时间回拨保护：elapsed < 0 视为 0，立即跑首次同步
                    if elapsed < 0:
                        logger.warning(
                            "[RegionSync] last_sync 是未来时间（系统时间回拨？），立即首次同步"
                        )
                        elapsed = 0
                    min_interval = self.sync_interval * 0.9  # 10% 容差
                    # elapsed=0 时不进等待分支（立即跑首次同步）
                    # 仅当 0 < elapsed < min_interval 时才等待剩余时间
                    if 0 < elapsed < min_interval:
                        wait_seconds = min_interval - elapsed
                        logger.info(
                            "[RegionSync] 距上次同步 %.0f 秒，不足 %.0f 秒，等待 %.0f 秒后再首次同步",
                            elapsed, min_interval, wait_seconds,
                        )
                        if self._stop_event.wait(timeout=wait_seconds):
                            return  # 收到 stop 信号，退出
                except (ValueError, TypeError) as e:
                    logger.warning("[RegionSync] 解析 last_sync 失败，立即首次同步: %s", e)
        except Exception as e:
            logger.warning("[RegionSync] 读 status file 失败，立即首次同步: %s", e)

        while True:
            try:
                self.run_sync()
            except Exception as e:
                logger.error(f"[RegionSync] Sync loop error: {e}")
            if self._stop_event.wait(self.sync_interval):
                break
```

确认 `from datetime import datetime` 已在文件顶部 import（`agent/injector/region_sync.py` L23 已 import）。

- [ ] **Step 5: 跑测试验证通过**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_sync_loop_skips_first_sync_when_recently_synced tests/test_region_sync.py::test_sync_loop_handles_future_last_sync -v
```

Expected: PASS

- [ ] **Step 6: 跑现有 region_sync 全量测试**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py -v
```

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
cd <repo_root> && git add agent/injector/region_sync.py tests/test_region_sync.py
git commit -m "fix(region_sync): _sync_loop 读 status file 跳过首次同步 + 时间回拨保护

_load_status 之前定义了但无调用点（死代码），导致每次重启都触发首次同步。
现在 _sync_loop 进入循环前读 last_sync，距上次同步不足 sync_interval*0.9
则等待剩余时间。加 elapsed<0 保护系统时间回拨（视为 0，立即跑同步）。

24h 间隔不再只在进程内生效，跨重启也生效。"
```

---

## Task 5: dissolve 异常升级——从 debug 升到 warning

**Files:**
- Modify: `agent/injector/region_sync.py:493-494, 523-526`（`_merge_and_dissolve` 方法的两个 except 块）
- Test: `tests/test_region_sync.py`

**背景**：当前 `_merge_and_dissolve` L493-494（merge 异常）和 L525-526（dissolve 异常）都用 `logger.debug` 静默吞掉异常。导致 dissolve 失败时无任何可见日志。Task 3 修复了 0 实体读取 bug 后，`dissolve_shrunk_regions` L1010 `members = self.get_region_members(region.name)` 能读到真实成员数，`current_size < shrink_threshold` 才会正确触发 `shrink_count += 1`，3 轮后 dissolve。Task 5 的异常升级让失败可见，不直接修复根因 D（根因 D 的真正修复是 Task 3）。

- [ ] **Step 1: 写失败测试——dissolve 异常被 warning 级别记录**

注意：项目用 loguru（`from loguru import logger`），不是标准 logging。`caplog` 抓不到 loguru 日志。改用 monkeypatch 拦截 `logger.warning` 调用。

在 `tests/test_region_sync.py` 追加测试：

```python
def test_merge_and_dissolve_logs_warning_on_dissolve_exception(monkeypatch):
    """dissolve 异常应被 logger.warning 记录，不是 logger.debug"""
    from agent.injector import region_sync
    from unittest import mock

    # 拦截 loguru logger 的 warning/debug 调用
    warning_calls = []
    debug_calls = []
    monkeypatch.setattr(
        region_sync.logger,
        "warning",
        lambda *args, **kwargs: warning_calls.append(args[0] if args else None),
    )
    monkeypatch.setattr(
        region_sync.logger,
        "debug",
        lambda *args, **kwargs: debug_calls.append(args[0] if args else None),
    )

    sync = region_sync.RegionSync(sync_interval=86400)

    with mock.patch(
        "niu_api.internal.region_manager.RegionManager.dissolve_shrunk_regions",
        side_effect=RuntimeError("test dissolve failure"),
    ), mock.patch(
        "niu_api.internal.lightrag_adapter.LightRAGAdapter"
    ), mock.patch(
        "niu_api.internal.lightrag_adapter.LightRAGIngester"
    ), mock.patch(
        "agent.brain_tools.get_activation_mgr", return_value=None
    ):
        sync._merge_and_dissolve({})

    # 断言：warning 调用里包含 "Dissolve" 或 "dissolve"
    assert any("Dissolve" in str(msg) or "dissolve" in str(msg) for msg in warning_calls), \
        f"dissolve 异常应被 warning 记录，实际 warning 调用: {warning_calls}"
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_merge_and_dissolve_logs_warning_on_dissolve_exception -v
```

Expected: FAIL（当前代码用 `logger.debug`，caplog 在 WARNING 级别抓不到）

- [ ] **Step 3: 修改两处 except 从 debug 升级到 warning**

在 `agent/injector/region_sync.py` 的 `_merge_and_dissolve` 方法里：

把 L493-494：
```python
        except Exception as e:
            logger.debug(f"[RegionSync] Merge check skipped: {e}")
```
改为：
```python
        except Exception as e:
            logger.warning(f"[RegionSync] Merge check failed: {e}")
```

把 L525-526：
```python
        except Exception as e:
            logger.debug(f"[RegionSync] Dissolve check skipped: {e}")
```
改为：
```python
        except Exception as e:
            logger.warning(f"[RegionSync] Dissolve check failed: {e}")
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py::test_merge_and_dissolve_logs_warning_on_dissolve_exception -v
```

Expected: PASS

- [ ] **Step 5: 跑现有 region_sync 全量测试**

```bash
cd <repo_root> && python -m pytest tests/test_region_sync.py -v
```

Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
cd <repo_root> && git add agent/injector/region_sync.py tests/test_region_sync.py
git commit -m "fix(region_sync): merge/dissolve 异常从 debug 升级到 warning

dissolve 异常被 logger.debug 静默吞掉，导致 dissolve 失败时无可见日志。
升级到 warning 让失败可见。配合 Task 3 的 0 实体读取修复，dissolve 现在
能正确读到成员数，shrink_count 能正常累加到阈值后清理。"
```

---

## Task 6: 端到端验证

**Files:**
- 验证脚本（不提交，临时跑）

**目的**：在真实环境跑一次启动，确认：
1. 算法只发现真正的新社区（不会重复发现已归属实体）
2. 14 个原"0 实体"脑区现在能正确显示成员数
3. 不会误删非默认脑区
4. 第二次启动不触发同步（24h 间隔跨进程持久化）

- [ ] **Step 1: 确认 Task 1-5 都已 commit**

```bash
cd <repo_root> && git log --oneline -6
```

Expected: 看到 5 个 fix commit

- [ ] **Step 2: 启动程序，观察日志**

```bash
cd <repo_root> && ./niu &
sleep 90
grep -E "排除.*已归属实体|覆盖率|跳过激活管理器刷新|create_region_nodes|过滤.*小社区|距上次同步|立即首次同步" <repo_root>/logs/llm_interaction_$(date +%Y%m%d).log | tail -30
```

Expected:
- 看到 "排除 N 个已归属实体（直连脑区的一级成员），剩余 M 个游离实体参与算法"
- M 远小于 2318
- 如果 M < 50，看到 "图谱节点数 M < min_graph_size 50，跳过社区检测"——正确，没有新社区
- 看到 "距上次同步" 或 "立即首次同步"（取决于上次同步时间）

- [ ] **Step 3: 检查脑区状态**

```bash
curl -s http://localhost:9876/api/brain/regions | python3 -m json.tool | grep -E "name|member_count|activation" | head -50
```

Expected: 之前显示 0 实体的脑区现在显示 100+ 实体；非默认脑区没被误删

- [ ] **Step 4: 二次启动验证单调收敛 + 24h 间隔**

```bash
# 用 pgrep + kill -TERM 优雅退出（禁止 pkill -f niu）
pgrep -f "niu_api" | xargs kill -TERM
sleep 3
cd <repo_root> && ./niu &
sleep 90
grep -E "距上次同步|立即首次同步|regions_created|排除.*已归属实体" <repo_root>/logs/llm_interaction_$(date +%Y%m%d).log | tail -10
```

Expected:
- 第二次启动看到 "距上次同步 X 秒，不足 86400 秒，等待 Y 秒后再首次同步"——24h 间隔跨进程持久化生效
- 没看到 "排除已归属实体" 或 "regions_created"——因为没跑同步

- [ ] **Step 5: 杀掉测试进程**

```bash
pgrep -f "niu_api" | xargs kill -TERM
pgrep -f "./niu" | xargs kill -TERM 2>/dev/null
sleep 3
ps aux | grep -E "niu_api|./niu" | grep -v grep
# 确认没有残留进程
```

---

## Self-Review

### 1. Spec coverage

用户需求："所有已查明的问题一并修复"。已查明的 4 个根因：

- ✅ **根因 A（24h 间隔无跨进程持久化）** → Task 4（`_sync_loop` 读 status file 跳过首次同步 + 时间回拨保护）
- ✅ **根因 B（算法输入未排除已归属实体 + 去重字符串比对问题）** → Task 1（`detect_communities` 排除已直连脑区的实体）+ Task 2（`cleanup_stale_regions` stale 判定调整，避免 Task 1 排除导致误删）
  - 注意：Task 1 排除已归属实体后，算法不再重复发现同一批实体，LLM 起名只用于真正的新社区，不会产生语义变体堆积。去重的字符串比对问题（L385 `is_existing`）不需要单独修——因为输入正确后，每次新建的都是真正的新社区，字符串比对天然正确。
- ✅ **根因 C（0 实体 bug，读取异常污染内存映射）** → Task 3（`_refresh_activation_manager` 批量读取 + 失败保护 + 覆盖率检查）
- ✅ **根因 D（dissolve 异常静默吞掉）** → Task 5（异常从 debug 升级到 warning）。根因 D 的真正修复是 Task 3——0 实体修复后 `dissolve_shrunk_regions` 能读到真实成员数，shrink_count 能正常累加。Task 5 让 dissolve 失败可见。

用户其他要求：
- ✅ "所有没有与任何脑区有直连的实体，都作为送入算法重新计算的实体" → Task 1
- ✅ "已直连脑区的实体（一级成员）排除，二级三级保留" → Task 1
- ✅ "大模型起名逻辑不变" → 计划里没动 LLM 起名相关代码
- ✅ "已有的脏数据不用管" → 计划里没写清理脏数据的步骤

### 2. Placeholder scan

- 没有 "TBD" / "TODO" / "implement later"
- 没有 "add appropriate error handling"（错误处理都给了具体代码）
- 所有代码块都是完整的
- 所有测试都有具体断言

### 3. Type consistency

- `get_all_region_members()` 在 Task 1/2/3 都用到，签名一致（返回 `dict[str, list[str]]`）
- `region.members` 字段在 Task 3 里被赋值 `list[str]`
- `_load_status()` 返回 `dict`，Task 4 读 `status.get("last_sync")`，与 `_save_status` 写入的 `last_sync` 字段名一致
- `_sync_loop` 的 `self.sync_interval` 和 `self._stop_event` 与 `__init__` 定义一致
- Task 2 的 `RegionPartition` / `CommunityDetectionResult` 导入路径与 `region_detector.py` 定义一致

### 4. 审查阻断修复

v1 审查的 4 个阻断：
- ✅ **v1 阻断 1（Task 1 排除导致 cleanup_stale_regions 误删）** → Task 2 修复（stale 判定三分支）
- ✅ **v1 阻断 2（Task 1 测试 mock 路径无效）** → Task 1 Step 1 mock 路径改为 `niu_api.internal.lightrag_manager.get_all_region_members`
- ✅ **v1 阻断 3（Task 3 测试 MagicMock name 特殊参数 + 未 mock LightRAG）** → Task 3 测试用 `fake_region.name = "智家脑区"` 赋值（不用构造函数 name 参数），且 mock 了 `LightRAGAdapter`/`LightRAGIngester`
- ✅ **v1 阻断 4（Task 4 测试替换 threading.Event.wait 脆弱）** → Task 4 测试改用真实 Event + `_stop_event.set()` 控制退出

v2 审查的 2 个阻断：
- ✅ **v2 阻断 1（Task 2 未提供两个现有测试更新代码）** → v3 Task 2 Step 4 给出 `test_removes_stale_region_nodes` 和 `test_cleanup_all_when_no_current_partitions` 完整更新代码
- ✅ **v2 阻断 2（Task 2 `if not current_members` 会被读取失败触发误删）** → v3 Task 2 Step 3 改为三分支判定：`region.name not in region_member_map` 跳过 / `not current_members` 删 / 有成员跳过

v3 审查的 1 个阻断：
- ✅ **v3 阻断 1（Task 4 `elapsed=0` 仍进等待分支，未来时间会卡 21.6h）** → v4 Task 4 Step 4 把 `if elapsed < min_interval:` 改为 `if 0 < elapsed < min_interval:`（elapsed=0 时不进等待，立即跑首次同步）

v4 审查的 1 个阻断：
- ✅ **v4 阻断 1（Task 5 测试用 `caplog` 抓 loguru 日志，caplog 抓不到）** → v5 Task 5 Step 1 改用 `monkeypatch.setattr(region_sync.logger, "warning", ...)` 拦截 loguru logger.warning 调用（项目用 loguru 不是标准 logging，`tests/conftest.py` 无桥接）

### 5. Corner case 覆盖

| Corner case | 覆盖 Task |
|---|---|
| Task 1 排除后 cleanup_stale_regions Jaccard 失效 | Task 2 |
| Task 1 测试 mock 路径 | Task 1 Step 1 |
| Task 3 部分失败（覆盖率低） | Task 3 Step 2 + 覆盖率检查 |
| Task 4 status file 未来时间 | Task 4 Step 2 + elapsed<0 保护 |
| Task 4 status file 不存在 | Task 4（`last_sync_str=None` 跳过等待） |
| Task 4 status file 格式不对 | Task 4（`except (ValueError, TypeError)`） |
| 现有测试触发真实 LightRAG | Task 1 Step 5 autouse fixture |
| 开发场景 24h 间隔 | 未覆盖（用户可手动删 status file 跳过） |

---

## 执行交接

计划完成并保存到 `docs/superpowers/plans/2026-07-06-region-algorithm-input-fix.md`。两种执行方式：

**1. Subagent-Driven（推荐）** - 每个 Task 派新子 Agent 实现，Task 之间审查，迭代快

**2. Inline Execution** - 在当前会话里批量执行，检查点审查

要哪种？
