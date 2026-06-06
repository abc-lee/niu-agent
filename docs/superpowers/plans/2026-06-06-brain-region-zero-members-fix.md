# 脑区"0实体"Bug 修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复脑区状态地图显示"0实体"的 bug，让成员计数正确反映图谱中的实际数据。

**Architecture:** 两层修复：(1) `_refresh_activation_manager` 改用 `lightrag_manager.get_region_members()` 直接读 NetworkX 图，替代不可靠的 `RegionManager.get_region_members()` 路径；(2) `format_region_map` 优先使用 `region_members_map` 参数，fallback 时也走直接读图路径。同时修复 `lightrag_manager` 中 `get_region_members()` 和 `get_all_region_members()` 的无向图单向匹配问题。

**Tech Stack:** Python, NetworkX, LightRAG

---

## 核心原则

1. **直接读图优先** — 用 `lightrag_manager` 的直接 NetworkX 图遍历替代 `RegionManager.get_region_members()` 的 `explore_node` + `call_async` 路径
2. **双向匹配** — NetworkX 无向图中边的 `(src, tgt)` 顺序不确定，必须双向匹配
3. **防御性 fallback** — `format_region_map` 优先用参数，fallback 也走可靠路径
4. **不改 LightRAG fork** — 本次修复只改 ai-bot 项目代码

---

## 根因回顾

```
format_region_map 显示 "0实体"
  ← _get_member_count 查 _entity_to_region 映射
    ← _entity_to_region 在 initialize_from_regions 中从 region.members 构建
      ← region.members 在 _refresh_activation_manager 中通过
        RegionManager.get_region_members() 赋值
          ← 该方法走 explore_node → call_async → get_knowledge_graph 路径
            ← 此路径可能因 BFS 截断、异步超时、relation 精确匹配问题
              返回空列表

而 lightrag_manager.get_region_members() 直接读 NetworkX 图的边，
不依赖 call_async，更可靠。
```

---

## 执行顺序

1. **提交1**：Task 1（lightrag_manager 双向匹配修复）
2. **提交2**：Task 2（_refresh_activation_manager 改用 lightrag_manager）
3. **提交3**：Task 3（format_region_map 成员计数修复 + 测试更新）

---

### Task 1: lightrag_manager 双向边匹配修复

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_manager.py:233-237,275-282`

- [ ] **Step 1: 修改 `get_region_members()` 添加双向匹配**

```python
# 修改前 (line 233-237):
        members = []
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if src == region_name and edge_type.lower() == "_region:contains":
                members.append(tgt)

        return members

# 修改后:
        members = []
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "_region:contains":
                if src == region_name:
                    members.append(tgt)
                elif tgt == region_name:
                    members.append(src)

        return members
```

- [ ] **Step 2: 修改 `get_all_region_members()` 添加双向匹配**

```python
# 修改前 (line 275-282):
        region_members: dict[str, list[str]] = {}
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "_region:contains":
                # src is region, tgt is member
                if src not in region_members:
                    region_members[src] = []
                region_members[src].append(tgt)

        return region_members

# 修改后:
        region_members: dict[str, list[str]] = {}
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "_region:contains":
                # NetworkX 无向图中 src/tgt 顺序不确定，需双向判断
                if src.endswith("脑区") or src.startswith("brain:region:"):
                    region, member = src, tgt
                elif tgt.endswith("脑区") or tgt.startswith("brain:region:"):
                    region, member = tgt, src
                else:
                    continue
                if region not in region_members:
                    region_members[region] = []
                region_members[region].append(member)

        return region_members
```

- [ ] **Step 3: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_manager.py`
Expected: 无输出

- [ ] **Step 4: 提交**

```bash
git add niu_api/internal/lightrag_manager.py
git commit -m "fix: add bidirectional edge matching in lightrag_manager region member queries"
```

---

### Task 2: _refresh_activation_manager 改用 lightrag_manager 直接读图

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py:280-286`

- [ ] **Step 1: 替换 get_region_members 调用**

```python
# 修改前 (line 280-286):
            for region in all_regions:
                try:
                    region.members = manager.get_region_members(region.name)
                except Exception as e:
                    logger.debug(
                        f"[RegionSync] get_region_members failed for {region.name}: {e}"
                    )

# 修改后:
            from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
            for region in all_regions:
                try:
                    region.members = lightrag_get_region_members(region.name)
                    if not region.members:
                        logger.warning(
                            "[RegionSync] get_region_members returned empty for %s",
                            region.name,
                        )
                except Exception as e:
                    logger.warning(
                        "[RegionSync] get_region_members failed for %s: %s",
                        region.name, e,
                    )
```

注：`lightrag_manager.get_region_members()` 直接读 NetworkX 图，不依赖 `call_async`，不会死锁，数据更可靠。异常日志级别从 `debug` 改为 `warning`，便于排查。

- [ ] **Step 2: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py`
Expected: 无输出

- [ ] **Step 3: 提交**

```bash
git add agent/injector/region_sync.py
git commit -m "fix: use lightrag_manager direct graph read for region member population"
```

---

### Task 3: format_region_map 成员计数修复 + 测试更新

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_injector.py:189-191`
- Modify: `REDACTED_USER_PATH/tools/ai-bot/tests/test_region_injector.py:113-142`

- [ ] **Step 1: 修改 format_region_map 成员计数逻辑**

`format_region_map` 接收 `region_members_map` 参数，但该参数只包含**向量检索命中的实体**（Top N），不是脑区的全部成员。所以不能直接用它替代 `_get_member_count` 来显示成员总数。

正确做法：当 `region_members_map` 为 None 时，调用 `get_all_region_members()` 获取完整映射；当 `region_members_map` 存在时，仍然用 `_get_member_count`（因为 Task 2 已修复 `_entity_to_region` 映射，`_get_member_count` 现在能正确返回数据）。

但更可靠的方案是：始终优先用 `region_members_map` 获取成员计数，当它为 None 时 fallback 到直接读图。

```python
# 修改前 (line 189-191):
        for region in sorted_regions:
            light = self._activation_mgr.get_status_light(region.activation)
            member_count = self._get_member_count(region.region_id)

# 修改后:
        for region in sorted_regions:
            light = self._activation_mgr.get_status_light(region.activation)
            if region_members_map is not None:
                member_count = len(region_members_map.get(region.region_id, []))
            else:
                member_count = self._get_member_count(region.region_id)
```

注：Task 2 修复了 `_refresh_activation_manager` 的成员填充，所以 `_get_member_count` 的 fallback 路径现在也是可靠的。但 `region_members_map` 作为调用方传入的显式参数，优先使用它更符合数据流设计。

- [ ] **Step 2: 更新测试 test_format_region_map_with_status_lights**

现有测试（line 113-142）调用 `format_region_map(regions)` 不传 `region_members_map`。因为 `_make_activation_manager()` 通过 `initialize_from_regions` 正确构建了 `_entity_to_region`，所以 `_get_member_count` fallback 能正确返回数据。测试本身不需要改，但需要增加一个验证 `region_members_map` 参数的测试。

在 `TestFormatRegionMap` 类中添加测试：

```python
    def test_format_region_map_with_members_map(self):
        """region_members_map 参数正确传递时使用它计算成员数"""
        activation_mgr = _make_activation_manager()
        _set_activation(activation_mgr, "community_0", 1.0)

        injector = _make_injector(activation_mgr)
        regions = activation_mgr.get_region_map()

        # 传入只包含部分成员的 region_members_map
        region_members_map = {
            "community_0": ["Python"],
            "community_1": ["AI_Bot"],
        }

        result = injector.format_region_map(regions, region_members_map)

        assert "## 脑区状态 (4个脑区)" in result
        # community_0 只有1个成员在 map 中
        assert "(1实体)" in result
        # community_1 只有1个成员在 map 中
        assert "项目管理" in result
```

- [ ] **Step 3: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_region_injector.py -v`
Expected: 所有测试通过

- [ ] **Step 4: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_injector.py`
Expected: 无输出

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/region_injector.py tests/test_region_injector.py
git commit -m "fix: format_region_map uses region_members_map parameter for member count"
```

---

## 验证

完成三个 Task 后，需验证：

1. **语法检查**：所有修改文件通过 `python -m py_compile`
2. **单元测试**：`python -m pytest tests/test_region_injector.py tests/test_region_sync.py -v`
3. **集成验证**：启动应用后确认脑区状态地图显示正确的成员数（非0）
