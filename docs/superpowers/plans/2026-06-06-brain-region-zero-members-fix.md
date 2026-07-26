# 脑区"0实体"Bug 修复 实施计划

> **⚠️ 历史文档**：本文档中使用 `brain:Niu`、`brain:region:xxx`、`brain:concept:xxx`、`brain:event:xxx`、`brain:person:xxx`、`brain:session:xxx`、`event:xxx`、`skill:xxx`、`person:xxx` 等冒号前缀实体名的描述已过时。当前系统要求所有实体名必须使用自然语言（如 `Niu`、`编程开发脑区`、`Python`、`海滩日落事件`），禁止冒号前缀格式。详见 `docs/kg-dev-dictionary.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复脑区状态地图显示"0实体"的 bug，让成员数据直接从 NetworkX 图中读取。

**Architecture:** 脑区节点在图中只有2种边：1条 `brain_region_anchor` + N条 `_region:contains`。所以 `边数 - 1 = 成员数`。修复方案就是让所有"获取脑区成员"的地方统一走 `lightrag_manager.get_region_members()` 直接读 NetworkX 图，不走不可靠的内存映射路径。

**Tech Stack:** Python, NetworkX

---

## 根因

`_get_member_count` → `_get_members` → `_activation_mgr.get_members_of_region()` → 查 `_entity_to_region` 内存映射 → 映射为空 → 显示0。

而 NetworkX 图中 `_region:contains` 边就在那里，`lightrag_manager.get_region_members()` 已经能直接读到。

---

## 执行顺序

1. **提交1**：Task 1（lightrag_manager 双向匹配修复）
2. **提交2**：Task 2（所有 get_region_members 调用统一改为 lightrag_manager 版本）

---

### Task 1: lightrag_manager 双向边匹配修复

**Files:**
- Modify: `<repo_root>/niu_api/internal/lightrag_manager.py:233-237,275-282`

当前 `get_region_members()` 只检查 `src == region_name`，在无向图中 `src/tgt` 顺序不确定，会遗漏成员。`get_all_region_members()` 同样假设 `src` 总是脑区，也有此问题。

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

脑区名特征：以"脑区"结尾或以"brain:region:"开头。用此判断哪端是脑区。

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
                # 无向图中 src/tgt 顺序不确定，需判断哪端是脑区
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

Run: `python -m py_compile <repo_root>/niu_api/internal/lightrag_manager.py`
Expected: 无输出

- [ ] **Step 4: 提交**

```bash
git add niu_api/internal/lightrag_manager.py
git commit -m "fix: add bidirectional edge matching in get_region_members and get_all_region_members"
```

---

### Task 2: 所有 get_region_members 调用统一改为 lightrag_manager 版本

涉及3个文件：

| 文件 | 行号 | 当前调用 | 改为 |
|------|------|----------|------|
| `region_injector.py` | 476-478 | `_activation_mgr.get_members_of_region(region_id)` | `lightrag_get_region_members(region_id)` |
| `region_sync.py` | 282 | `manager.get_region_members(region.name)` | `lightrag_get_region_members(region.name)` |
| `brain_region_api.py` | ~192 | `region_mgr.get_region_members(region.name)` | `lightrag_get_region_members(region.name)` |

- [ ] **Step 1: 修改 region_injector.py 的 `_get_members` 方法**

```python
# 修改前 (line 476-478):
    def _get_members(self, region_id: str) -> list[str]:
        """Get member entity names for a region from activation manager."""
        return self._activation_mgr.get_members_of_region(region_id)

# 修改后:
    def _get_members(self, region_id: str) -> list[str]:
        """Get member entity names for a region from NetworkX graph."""
        from niu_api.internal.lightrag_manager import get_region_members
        return get_region_members(region_id)
```

- [ ] **Step 2: 修改 region_sync.py 的 get_region_members 调用**

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
                except Exception as e:
                    logger.warning(
                        "[RegionSync] get_region_members failed for %s: %s",
                        region.name, e,
                    )
```

注：导入放在函数内部，与 `_refresh_activation_manager` 中其他导入风格一致（line 267-270）。`manager` 变量仍需保留，因为第276行 `get_all_regions()` 还依赖它。

- [ ] **Step 3: 修改 brain_region_api.py 的 get_region_members 调用**

读取 `<repo_root>/niu_api/brain_region_api.py`，找到 `region.members = region_mgr.get_region_members(region.name)` 的调用，改为：

```python
# 修改前:
region.members = region_mgr.get_region_members(region.name)

# 修改后:
from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
region.members = lightrag_get_region_members(region.name)
```

- [ ] **Step 4: 语法检查**

Run: `python -m py_compile niu_api/internal/region_injector.py agent/injector/region_sync.py niu_api/brain_region_api.py`
Expected: 无输出

- [ ] **Step 5: 运行测试**

Run: `cd <repo_root> && python -m pytest tests/test_region_injector.py tests/test_region_sync.py -v`
Expected: 所有测试通过

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_injector.py agent/injector/region_sync.py niu_api/brain_region_api.py
git commit -m "fix: all get_region_members calls now use lightrag_manager direct graph read"
```
