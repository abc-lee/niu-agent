# LightRAG 知识图谱大小写不敏感彻底修复 Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 彻底解决 LightRAG 知识图谱大小写敏感问题——无论 LLM 提取的实体名是大写还是小写，写入 vdb 和 GraphML 时统一小写，查询时大小写不敏感。根因是 `sanitize_and_normalize_extracted_text` 用于 entity_name/source/target 时不做 lower 化。**两条写入路径都要修**：LLM 提取走 operate.py L401/L490/L493，脑区注入走 lightrag.py `ainsert_custom_kg` L2465/L2466（在 sort 之前 lower，否则 sort 用大写、vdb 存小写又造成不一致）。

**Architecture:** 在 LightRAG fork 源码（`<lightrag_fork_path>/`）改两条路径：(1) operate.py L401/L490/L493 调完 `sanitize_and_normalize_extracted_text` 后追加 `.lower()`；(2) lightrag.py `ainsert_custom_kg` 的 entity_name/src_id/tgt_id 加 `.lower()`（src/tgt 在 L2465/L2466 处 lower，让后续 sort/dedup/has_nodes_batch 都用 lower 值）。改完清理 site-packages 旧物理副本 + 重装 Fork。存量数据迁移用 `repair_entity_sync`（entities）+ 新增 `repair_relationship_sync`（relationships，**重算 __id__ 时必须 sorted(src, tgt) 后拼接**，跟 operate.py L1586-1589 一致），且 `repair_entity_sync` 修正 `__id__` 重算为 `compute_mdhash_id(lower_name, prefix="ent-")`。niu_api region 字典构建时 key lower 化 + 查询时入参 lower 化（双重保险）。

**Tech Stack:** Python 3.11+，LightRAG fork（networkx + nano-vectordb），pytest

---

## Context

### 当前 bug

启动检测说"OK"不弹窗，但运行时 LightRAG 报 `WARNING: Some nodes are missing, maybe the storage is damaged`。

**根因链路**（已全量排查确认）：

1. LLM 提取实体名 "Apple"（大写）
2. `operate.py` L401 `entity_name = sanitize_and_normalize_extracted_text(...)` — 不做 `.lower()`，entity_name 还是 "Apple"
3. L1156/L1973 `upsert_node(entity_name, ...)` — `networkx_impl.py` `_normalize_node_id` 内部 lower → GraphML node id = "apple"
4. L1979 `compute_mdhash_id(str(entity_name), prefix="ent-")` — 用原始 "Apple" 算 vdb id → vdb id 跟 GraphML 不一致
5. L1983 `data_for_vdb = {entity_vdb_id: {"entity_name": entity_name, ...}}` — vdb entity_name 字段 = "Apple"（大写）
6. **两个存储不一致**：GraphML = "apple"（lower），vdb entity_name = "Apple"（原始），vdb __id__ = hash("Apple")

**关于 "nodes are missing" 警告的真实触发路径**（v3 修正）：

`lightrag/base.py` L500-512 `get_nodes_batch` 默认实现用**原始 node_id**（可能大写）作为 dict key，不是 lower key。所以 `nodes_dict.get("Apple")` 能命中 `result["Apple"]`，**不是大小写导致查不到**。

"nodes are missing" 真实触发条件是 GraphML 里**真的没有**这个节点（`get_node` 返回 None）——这是真孤儿节点或真数据不一致，不是大小写问题。

**大小写问题真正导致的后果**是 `check_entity_sync` 检测到 `case_mismatch`（vdb entity_name 大写 vs GraphML node id lower），触发启动弹窗（lightrag_integrity.py L335）。修复方向（源头 lower 化）是对的，但不是为了消除 "nodes are missing"，而是为了消除 `case_mismatch` + 保证 vdb/GraphML 一致性 + 避免后续查询/repair 时的假孤儿。

### 两条写入路径（v3 覆盖）

**路径 1：LLM 提取**（operate.py）
- L401 `entity_name` / L490 `source` / L493 `target` — 调 `sanitize_and_normalize_extracted_text` 后不 lower
- L441 `entity_type` / L520 `edge_keywords` — 已有 `.lower()`，不改
- L444 `entity_description` / L523 `edge_description` — description 不能 lower，不改

**路径 2：脑区注入/region_sync**（lightrag.py `ainsert_custom_kg`）
- L2422-2453：entity_data["entity_name"] 直接写入 GraphML 和 vdb，**完全不经 sanitize 也不 lower**
- L2465-2466：`src_id = relationship_data["src_id"]` / `tgt_id = relationship_data["tgt_id"]` 直接取原始值
- L2467 `relation_key = tuple(sorted((src_id, tgt_id)))` 和 L2516 `normalized_src_id, normalized_tgt_id = sorted((src_id, tgt_id))` 都会排序——**如果 src_id/tgt_id 大写，sorted 用大写排序，但 GraphML 节点 id 是 lower**，导致 L2481 `has_nodes_batch` 查不到大写节点，L2499 把大写当 missing 节点重新创建（虽然 `_normalize_node_id` 会再 lower，但 dedup 用大写 key 可能产生重复条目）
- niu_api 的脑区注入（region_sync/lightrag_adapter inject_custom_kg）走这条路径，如果脑区 member 含大写，会写入大写 entity_name，导致修复后复发
- **v3 修复位置**：在 L2465/L2466 取值时就 `.lower()`，让后续 sort/dedup/has_nodes_batch 都用 lower 值

### 排查报告发现的存量数据问题

**`repair_entity_sync` 已有 bug**（v3 修正）：L339 `new_item["__id__"] = lower_name` 用裸 lower_name（如 "apple"），但 LightRAG 写入 vdb 时 `__id__` 是 `compute_mdhash_id(entity_name, prefix="ent-")` 算的 hash id（如 "ent-xxxxx"）。修复后 vdb 的 `__id__` 跟新写入 id 不匹配，查询时查不到。需改为 `new_item["__id__"] = compute_mdhash_id(lower_name, prefix="ent-")`。

**vdb_relationships 未迁移**（v3 新增）：`repair_entity_sync` 只修 vdb_entities，不碰 vdb_relationships。但关系的 src_id/tgt_id 同样可能存大写，且 **LightRAG 写入关系 id 时会先 `sorted((src, tgt))` 再 `compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")`**（operate.py L1586-1589 / L2514-2520 / lightrag.py L2467/L2516）。存量关系 vdb 如果 src="Banana" tgt="Apple"（原始顺序），id 用大写算；repair 时只 lower 不 sort，会算成 `compute_mdhash_id("banana"+"apple")`，但 LightRAG 新写入是 `sorted → "apple","banana" → compute_mdhash_id("apple"+"banana")`——**两个 id 不一致**，修复后存量关系 id 跟新写入 id 对不上，查询时还是 missing。需新增 `repair_relationship_sync`，重算 id 时 **先 sorted 再拼接**，同时 src_id/tgt_id 也存排序后的值。

### 关键约束（用户铁律）

- **Fork 源码可以改**——改 `<lightrag_fork_path>/` 本地源码，推送到 Fork 仓库，重新 `pip install`。**禁止直接改安装后的代码**（`python/lib/python3.11/site-packages/lightrag/`）
- **修改前必须先做临时提交备份**（铁律 #3）
- **测试必须用真实数据**（铁律 #5）
- **python/ 目录必须是完整自包含 Python 安装**（铁律 #6）
- **git 操作后必须修复文件权限**（铁律 #7）— 主仓库 git 操作后跑 `find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x`
- **禁止 `git reset --hard`** / `git push`（主仓库；Fork 仓库可以 push）

### 关键代码位置（Fork 源码）

| 文件 | 行号 | 内容 | 改动 |
|------|------|------|------|
| `<lightrag_fork_path>/lightrag/operate.py` | L401-403 | `entity_name = sanitize_and_normalize_extracted_text(...)` | **加 .lower()** |
| `<lightrag_fork_path>/lightrag/operate.py` | L490-492 | `source = sanitize_and_normalize_extracted_text(...)` | **加 .lower()** |
| `<lightrag_fork_path>/lightrag/operate.py` | L493-495 | `target = sanitize_and_normalize_extracted_text(...)` | **加 .lower()** |
| `<lightrag_fork_path>/lightrag/operate.py` | L413-415 | `entity_type = sanitize_and_normalize_extracted_text(...)` | 不改（L441 已 lower） |
| `<lightrag_fork_path>/lightrag/operate.py` | L444 | `entity_description = sanitize_and_normalize_extracted_text(...)` | 不改（description 不能 lower） |
| `<lightrag_fork_path>/lightrag/operate.py` | L517-520 | `edge_keywords = sanitize_and_normalize_extracted_text(...)` | 不改（L520 已 lower） |
| `<lightrag_fork_path>/lightrag/operate.py` | L523 | `edge_description = sanitize_and_normalize_extracted_text(...)` | 不改（description 不能 lower） |
| `<lightrag_fork_path>/lightrag/operate.py` | L1586-1589 | `if src > tgt: src, tgt = tgt, src; rel_vdb_id = compute_mdhash_id(src + tgt, prefix="rel-")` | 不改（参考实现，repair_relationship_sync 要对齐） |
| `<lightrag_fork_path>/lightrag/lightrag.py` | L2422-2453 | `ainsert_custom_kg` entity_data["entity_name"] 直接写入 | **加 .lower()** |
| `<lightrag_fork_path>/lightrag/lightrag.py` | L2465-2466 | `src_id = relationship_data["src_id"]; tgt_id = relationship_data["tgt_id"]` | **加 .lower()（在 sort 之前）** |

### vdb id 格式（关键）

- **vdb_entities.json**：`__id__` = `compute_mdhash_id(entity_name, prefix="ent-")` → "ent-xxxxx" 格式
- **vdb_relationships.json**：`__id__` = `compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")` → "rel-xxxxx" 格式（**必须先 sorted 再拼接**）
- **存量迁移时必须重算 __id__**，不能用裸 entity_name/src+tgt，且关系必须先 sorted

### niu_api 层联动（region 字典）

| 文件 | 行号 | 内容 |
|------|------|------|
| `niu_api/internal/region_injector.py` | L69-72 | `entity_to_region` 字典构建（`activate_for_query` 方法内联）|
| `niu_api/internal/region_activation.py` | L174-175 | `new_entity_to_region[entity_name] = region.name` |
| `niu_api/internal/lightrag_adapter.py` | L460 | `data.get("entity_name") in member_set` |

### 已有修复（保留 + 修正）

- `niu_api/internal/lightrag_integrity.py` `check_entity_sync` — 保留作为兜底
- `niu_api/internal/lightrag_repair.py` `repair_entity_sync` — **修正 L339 __id__ 重算** + **新增 `repair_relationship_sync`**

---

## File Structure

### 修改文件（Fork 源码）

- `<lightrag_fork_path>/lightrag/operate.py` — L401/L490/L493 三处加 `.lower()`
- `<lightrag_fork_path>/lightrag/lightrag.py` — `ainsert_custom_kg` entity_name/src_id/tgt_id 加 `.lower()`（src/tgt 在 L2465/L2466 处 lower，让后续 sort/dedup 都用 lower）

### 修改文件（niu_api 层）

- `niu_api/internal/lightrag_repair.py` — 修正 `repair_entity_sync` 的 `__id__` 重算 + 新增 `repair_relationship_sync`（重算 id 时先 sorted）
- `niu_api/internal/region_injector.py` — region 字典构建时 key lower 化 + 查询时入参 lower
- `niu_api/internal/region_activation.py` — 同上
- `niu_api/internal/lightrag_adapter.py` — member_set 构建时 lower 化

### 新建文件

- `<lightrag_fork_path>/tests/test_case_insensitive.py` — Fork 源码的 lower 化测试
- `tests/test_region_case_insensitive.py` — niu_api region 字典 lower 化测试
- `tests/test_lightrag_relationship_sync.py` — `repair_relationship_sync` 测试（含 src>tgt 顺序用例 + 自环用例）

### 不改文件

- `utils.py` 的 `sanitize_and_normalize_extracted_text` / `normalize_extracted_info` — 不改（description 不能统一 lower）
- `niu_api/internal/lightrag_integrity.py` — `check_entity_sync` 保留
- `lightrag/base.py` L500-512 `get_nodes_batch` — 不改（用原始 node_id 作 key 是 LightRAG 设计，不是 bug）

---

## Task 1: Fork 源码加 .lower()（LLM 提取路径 + 脑区注入路径）

**目标：** 在 `<lightrag_fork_path>/` 改两条路径：(1) operate.py L401/L490/L493 加 .lower()；(2) lightrag.py `ainsert_custom_kg` 的 entity_name/src_id/tgt_id 加 .lower()（src/tgt 在 L2465/L2466 处 lower，让后续 sort/dedup/has_nodes_batch 都用 lower）。

**Files:**
- Modify: `<lightrag_fork_path>/lightrag/operate.py`（L401/L490/L493）
- Modify: `<lightrag_fork_path>/lightrag/lightrag.py`（L2422-2453/L2465-2466）
- Test: `<lightrag_fork_path>/tests/test_case_insensitive.py`（新建）

- [ ] **Step 1: 临时备份 Fork 源码**

```bash
cd <lightrag_fork_path>
git add -A && git commit -m "backup: 加 .lower() 前临时备份

待改：operate.py L401/L490/L493 + lightrag.py ainsert_custom_kg
" || echo "nothing to commit"
```

- [ ] **Step 2: 写失败测试 — LLM 提取路径 entity_name lower**

创建 `<lightrag_fork_path>/tests/test_case_insensitive.py`：

```python
"""测试 entity_name/source/target 提取后统一 lower 化。"""
import pytest
from lightrag.operate import _handle_single_entity_extraction, _handle_single_relationship_extraction


def test_entity_name_lowered():
    """LLM 提取 'Apple'（大写），经 sanitize 后应该是 'apple'（小写）。"""
    record = ["entity", "Apple", "organization", "A fruit company"]
    result = _handle_single_entity_extraction(record, "chunk-test", 1234567890)
    assert result is not None
    assert result["entity_name"] == "apple", f"entity_name 应 lower 化，实际: {result['entity_name']}"
    assert result["entity_type"] == "organization"
    # description 不 lower（自然语言保留大小写）
    assert "fruit" in result["description"].lower() or "fruit" in result["description"]


def test_entity_name_mixed_case_lowered():
    """混合大小写 'XX分行高速公路苏通卡项目' 应该 lower 化。"""
    record = ["entity", "XX分行高速公路苏通卡项目", "project", "某个项目"]
    result = _handle_single_entity_extraction(record, "chunk-test", 1234567890)
    assert result is not None
    assert result["entity_name"] == "xx分行高速公路苏通卡项目"


def test_relationship_source_target_lowered():
    """关系的 source/target 也应该 lower 化。"""
    record = ["relation", "Apple", "Steve Jobs", "founder", "Apple was founded by Steve Jobs"]
    result = _handle_single_relationship_extraction(record, "chunk-test", 1234567890)
    assert result is not None
    assert result["src_id"] == "apple", f"src_id 应 lower 化，实际: {result['src_id']}"
    assert result["tgt_id"] == "steve jobs", f"tgt_id 应 lower 化，实际: {result['tgt_id']}"
    # description 不 lower
    assert "Apple" in result.get("description", "") or "apple" in result.get("description", "").lower()


def test_relationship_self_loop_dropped_after_lower():
    """source='Apple' target='apple' 改后都 lower 成 'apple'，自环检测触发 return None。
    
    这是 LightRAG 既有语义（_normalize_node_id 早就 lower 化节点 id，自环早就在丢弃），
    v2 只是把 vdb 侧也对齐——不是 v2 引入的新 bug。lower 化后更早触发，跟修复前行为一致。
    """
    record = ["relation", "Apple", "apple", "self", "self relation"]
    result = _handle_single_relationship_extraction(record, "chunk-test", 1234567890)
    assert result is None, "自环关系应被丢弃（source==target after lower）"


def test_ainsert_custom_kg_entity_name_lowered():
    """ainsert_custom_kg 路径的 entity_name 也应该 lower 化（脑区注入路径）。"""
    import asyncio
    from lightrag.lightrag import LightRAG
    # 这个测试需要 mock LightRAG 实例，验证 ainsert_custom_kg 内部 lower 化
    # 简化：直接验证 entity_data["entity_name"] 在传入 upsert 前是 lower
    # 执行者读 lightrag.py L2422-2453 实际代码后调整测试
    # 关键断言：如果传入 entity_name="Apple"，写入 GraphML/vdb 时应该是 "apple"
    pytest.skip("执行者读 lightrag.py ainsert_custom_kg 实际代码后补全此测试")
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd <lightrag_fork_path>
python -m pytest tests/test_case_insensitive.py -v
```

Expected: FAIL — `entity_name` 是 "Apple"（大写），断言 `== "apple"` 失败

- [ ] **Step 4: 改 L401 entity_name 加 .lower()**

用 Edit 工具改 `<lightrag_fork_path>/lightrag/operate.py` L401-403：

改前：
```python
        entity_name = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        )
```

改后：
```python
        entity_name = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        ).lower()
```

- [ ] **Step 5: 改 L490 source 加 .lower()**

改前（L490-492）：
```python
        source = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        )
```

改后：
```python
        source = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        ).lower()
```

- [ ] **Step 6: 改 L493 target 加 .lower()**

改前（L493-495）：
```python
        target = sanitize_and_normalize_extracted_text(
            record_attributes[2], remove_inner_quotes=True
        )
```

改后：
```python
        target = sanitize_and_normalize_extracted_text(
            record_attributes[2], remove_inner_quotes=True
        ).lower()
```

- [ ] **Step 7: 改 lightrag.py ainsert_custom_kg — entity_name 在 dedup 循环开头加 .lower()**

读 `<lightrag_fork_path>/lightrag/lightrag.py` L2422-2453。**关键**：必须在 L2423 dedup 循环开头就 lower，让后续 dedup key、`entity_nodes`、`all_entities_data`、`compute_mdhash_id` 全部用 lower 值。如果只在 `entity_nodes.append` 那一行 lower，会漏掉 dedup key（"Apple" 和 "apple" 会被当两个不同 entity 各自保留，vdb 产生两条记录）。

改前（L2422-2425）：
```python
            deduped_entities: dict[str, dict[str, Any]] = {}
            for entity_data in custom_kg.get("entities", []):
                entity_name = entity_data["entity_name"]
                deduped_entities.pop(entity_name, None)
                deduped_entities[entity_name] = entity_data
```

改后：
```python
            deduped_entities: dict[str, dict[str, Any]] = {}
            for entity_data in custom_kg.get("entities", []):
                entity_name = entity_data["entity_name"].lower()
                entity_data["entity_name"] = entity_name  # 同步更新字典，让下游 deduped_entities.values() 取到的也是 lower
                deduped_entities.pop(entity_name, None)
                deduped_entities[entity_name] = entity_data
```

**说明**：
- L2423 `entity_name = entity_data["entity_name"].lower()` — dedup key 用 lower
- L2424 `entity_data["entity_name"] = entity_name` — 同步更新字典原值，让 L2431 `entity_name = entity_data["entity_name"]` 取到的也是 lower（L2431 不需要再改）
- 后续 L2444 `entity_id: entity_name`、L2451 `entity_nodes.append((entity_name, ...))`、L2453 `node_data_copy["entity_name"] = entity_name` 全部自动用 lower 值
- `compute_mdhash_id` 用 lower 化的 entity_name 算 vdb id → 跟 GraphML node id（lower）一致

**注意**：执行者读 L2422-2453 实际代码后，确认 dedup 循环结构跟上述改前/改后一致。如果代码结构有差异（如变量名不同），按实际代码调整，但原则不变：**在 dedup 循环开头 lower entity_name，并同步更新 entity_data 字典**。

- [ ] **Step 8: 改 lightrag.py ainsert_custom_kg — relation src_id/tgt_id 在 sort 之前加 .lower()**

读 L2465-2466：
```python
src_id = relationship_data["src_id"]
tgt_id = relationship_data["tgt_id"]
```

改为：
```python
src_id = relationship_data["src_id"].lower()
tgt_id = relationship_data["tgt_id"].lower()
```

**关键**：必须在 L2465/L2466 处 lower，让后续 L2467 `relation_key = tuple(sorted((src_id, tgt_id)))`、L2477-2479 `needed_node_ids.add(src_id/tgt_id)`、L2481 `has_nodes_batch`、L2516 `sorted((src_id, tgt_id))` 全部用 lower 值。如果只在外层 lower，sorted 用大写、has_nodes_batch 用大写查不到 lower 化的 GraphML 节点，会产生重复创建。

- [ ] **Step 9: 补全 test_ainsert_custom_kg 测试**

读 lightrag.py `ainsert_custom_kg` 实际代码后，把 Step 2 的 `test_ainsert_custom_kg_entity_name_lowered` 从 `pytest.skip` 改为真实测试。可以用 mock LightRAG 实例，验证传入 "Apple" 后写入 GraphML/vdb 的是 "apple"。

- [ ] **Step 10: 跑测试确认通过**

```bash
cd <lightrag_fork_path>
python -m pytest tests/test_case_insensitive.py -v
```

Expected: PASS（5 个测试全过，含自环丢弃测试）

- [ ] **Step 11: 跑 Fork 已有测试确认无回归**

```bash
cd <lightrag_fork_path>
python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: 全部 PASS。如果有测试因 entity_name 大小写变化失败，更新断言（改成小写）。

- [ ] **Step 12: 提交 Fork 改动**

```bash
cd <lightrag_fork_path>
git add lightrag/operate.py lightrag/lightrag.py tests/test_case_insensitive.py
git commit -m "fix: entity_name/source/target 写入时统一 lower 化（两条路径）

根因：sanitize_and_normalize_extracted_text 不做 .lower()，且
ainsert_custom_kg 路径完全不经 sanitize。导致 entity_name 保留
原始大小写，GraphML node id 被 _normalize_node_id lower 化，但
vdb entity_name 字段和 compute_mdhash_id 用原始大小写，两存储
不一致，check_entity_sync 报 case_mismatch 触发弹窗。

修复（两条路径）：
1. operate.py L401/L490/L493 调完 sanitize 后加 .lower()
   （LLM 提取路径）
2. lightrag.py ainsert_custom_kg 的 entity_name/src_id/tgt_id
   加 .lower()（脑区注入/region_sync 路径）；src/tgt 在 L2465/L2466
   处 lower，让后续 sort/dedup/has_nodes_batch 都用 lower 值

entity_type 和 edge_keywords 已有 lower 逻辑，description 不 lower
（自然语言保留大小写）。自环关系（source==target after lower）
会被丢弃，这是 LightRAG 既有语义（_normalize_node_id 早就 lower
化节点 id），不是本次修复引入的新行为。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 13: 推送 Fork 到远端**

```bash
cd <lightrag_fork_path>
git push origin main
```

---

## Task 2: 清理 site-packages 旧副本 + 重装 Fork

**目标：** 清理 site-packages 里旧的物理 lightrag/ 目录 + .pth + finder + dist-info，确保重装后 import 走新代码，然后重装 Fork（editable 模式）。

**Files:**
- 无源码改动（只清理 + 装包）

- [ ] **Step 1: 临时备份主仓库**

```bash
cd <repo_root>
git add -A && git commit -m "backup: 清理重装 LightRAG Fork 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 清理 site-packages 旧 lightrag 物理目录 + .pth + finder + dist-info**

```bash
# 先看当前 site-packages 里 lightrag 相关文件
ls <repo_root>/python/lib/python3.11/site-packages/ | grep -i lightrag

# 清理物理目录（如果有，editable 模式下不应该有，但历史上可能残留）
rm -rf <repo_root>/python/lib/python3.11/site-packages/lightrag/

# 清理 editable .pth 文件
rm -f <repo_root>/python/lib/python3.11/site-packages/__editable__.lightrag_hku*.pth

# 清理 editable finder
rm -f <repo_root>/python/lib/python3.11/site-packages/__editable___lightrag_hku*_finder.py

# 清理 lightrag_hku*.dist-info（非 editable 安装残留）
rm -rf <repo_root>/python/lib/python3.11/site-packages/lightrag_hku*.dist-info

# 验证清理干净
ls <repo_root>/python/lib/python3.11/site-packages/ | grep -i lightrag
```

Expected: 最后一条 grep 无输出（清理干净）

- [ ] **Step 3: 用项目 python 重装 Fork（editable 模式）**

```bash
cd <lightrag_fork_path>
<repo_root>/python/bin/python -m pip install -e . --no-deps
```

**注意**：
- 用 `<repo_root>/python/bin/python`（项目自带 python，铁律 #6）
- `-e` editable 模式（链接到 Fork 源码，后续 Fork 改动自动生效）
- `--no-deps` 不装依赖（只装 lightrag 本身）
- 不用 `--force-reinstall`（Step 2 已清理干净，不需要强制）

- [ ] **Step 4: 验证安装后的代码含 .lower() + import 走 Fork 源码**

```bash
# 验证 operate.py L401/L490/L493 附近有 .lower()
grep -n "\.lower()" <lightrag_fork_path>/lightrag/operate.py | grep -E "sanitize_and_normalize" | head -5

# 验证 lightrag.py L2465/L2466 附近有 .lower()
grep -n "src_id = relationship_data" <lightrag_fork_path>/lightrag/lightrag.py | head -3

# 验证 import 走 Fork 源码（editable 模式）
<repo_root>/python/bin/python -c "import lightrag; print(lightrag.__file__)"
```

Expected:
- grep operate.py 看到 3 处 `.lower()`（L401/L490/L493 附近）
- grep lightrag.py 看到 `src_id = relationship_data["src_id"].lower()` 和 `tgt_id = relationship_data["tgt_id"].lower()`
- `lightrag.__file__` 指向 `<lightrag_fork_path>/lightrag/__init__.py`（editable 模式，指向 Fork 源码）

- [ ] **Step 5: 跑主仓库的 LightRAG 相关测试确认无回归**

```bash
cd <repo_root>
python -m pytest tests/test_lightrag_entity_sync.py tests/test_lightrag_integrity*.py tests/test_lightrag_resilience*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

- [ ] **Step 6: 权限修复（铁律 #7）**

```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null || true
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

---

## Task 3: 修正 repair_entity_sync 的 __id__ 重算 + 新增 repair_relationship_sync（含 sort）

**目标：** 
1. 修正 `repair_entity_sync` 的 `__id__` 用 `compute_mdhash_id(lower_name, prefix="ent-")` 重算（不是裸 lower_name）
2. 新增 `repair_relationship_sync` 修 vdb_relationships 的 src_id/tgt_id 大写 + 重算 `__id__`（**必须先 sorted(src, tgt) 再拼接**，跟 operate.py L1586-1589 一致），同时 src_id/tgt_id 也存排序后的值；删除 GraphML 无对应边的真孤儿关系；content/description/keywords 字段保留原样不 lower

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（修正 repair_entity_sync L339 + 新增 repair_relationship_sync + repair_all 调用）
- Test: `tests/test_lightrag_entity_sync.py`（追加 repair_entity_sync __id__ 测试）
- Test: `tests/test_lightrag_relationship_sync.py`（新建，含 src>tgt 顺序用例 + 自环用例）

- [ ] **Step 1: 临时备份**

```bash
cd <repo_root>
git add -A && git commit -m "backup: 修正 repair_entity_sync + 新增 repair_relationship_sync 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 写失败测试 — repair_entity_sync 重算 __id__**

在 `tests/test_lightrag_entity_sync.py` 追加：

```python
def test_repair_entity_sync_id_is_hash_not_bare_name(monkeypatch):
    """修复后 vdb 的 __id__ 应该是 compute_mdhash_id(lower_name, prefix='ent-')，不是裸 lower_name。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    from lightrag.utils import compute_mdhash_id
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": compute_mdhash_id("Niu", prefix="ent-"), "entity_name": "Niu", "content": "desc", "source_id": "chunk-1"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc niu", "chunk-1"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        vdb = json.loads((storage / "vdb_entities.json").read_text())
        # __id__ 应该是 hash id 格式 "ent-xxxxx"，不是裸 "niu"
        assert vdb["data"][0]["__id__"].startswith("ent-"), f"__id__ 应是 ent- 前缀，实际: {vdb['data'][0]['__id__']}"
        # __id__ 应该等于 compute_mdhash_id("niu", prefix="ent-")
        expected_id = compute_mdhash_id("niu", prefix="ent-")
        assert vdb["data"][0]["__id__"] == expected_id, f"__id__ 应是 {expected_id}，实际: {vdb['data'][0]['__id__']}"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_id_is_hash_not_bare_name -v
```

Expected: FAIL — 当前 repair_entity_sync L339 用裸 lower_name，`__id__` 是 "niu" 不以 "ent-" 开头

- [ ] **Step 4: 修正 repair_entity_sync L339 — 重算 __id__**

用 Edit 工具改 `niu_api/internal/lightrag_repair.py`，在 repair_entity_sync 函数顶部 import：

```python
from lightrag.utils import compute_mdhash_id
```

改 L339（原 `new_item["__id__"] = lower_name`）：

```python
# 改前：
new_item["__id__"] = lower_name

# 改后：
new_item["__id__"] = compute_mdhash_id(lower_name, prefix="ent-")
```

同样改 L372（重建缺失向量时的 `__id__`）：

```python
# 改前：
new_data.append({
    "__id__": lower_name,
    ...

# 改后：
new_data.append({
    "__id__": compute_mdhash_id(lower_name, prefix="ent-"),
    ...
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_id_is_hash_not_bare_name -v
```

Expected: PASS

- [ ] **Step 6: 写 repair_relationship_sync 失败测试（含 src>tgt 顺序 + 自环 + 孤儿）**

创建 `tests/test_lightrag_relationship_sync.py`：

```python
"""repair_relationship_sync 测试 — 修 vdb_relationships 的 src_id/tgt_id 大写 + 重算 __id__。

关键：LightRAG 写入关系 id 时会先 sorted((src, tgt)) 再 compute_mdhash_id(sorted_src + sorted_tgt, prefix='rel-')。
repair 必须对齐这个逻辑，否则修复后的 id 跟新写入 id 不一致。
"""
import base64
import json
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from lightrag.utils import compute_mdhash_id


def _encode_vector_768(vec_f16) -> str:
    arr = np.array(vec_f16, dtype=np.float16) if not hasattr(vec_f16, 'astype') else vec_f16.astype(np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix_768(matrix_f32) -> str:
    arr = np.array(matrix_f32, dtype=np.float32) if not hasattr(matrix_f32, 'astype') else matrix_f32.astype(np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _write_rel_vdb(path: Path, data_list: list[dict], embedding_dim: int = 768):
    vectors = []
    for item in data_list:
        vec = np.full(embedding_dim, 0.1, dtype=np.float16)
        item = {**item, "vector": _encode_vector_768(vec)}
        vectors.append(vec)
    matrix = np.array(vectors, dtype=np.float32) if vectors else np.zeros((0, embedding_dim), dtype=np.float32)
    path.write_text(json.dumps({
        "embedding_dim": embedding_dim,
        "data": data_list,
        "matrix": _encode_matrix_768(matrix),
    }))


def _write_graphml_with_edges(path: Path, edges: list[tuple[str, str]]):
    """写含边的 GraphML，edges: [(src, tgt), ...]，src/tgt 已 lower。"""
    nodes = set()
    for src, tgt in edges:
        nodes.add(src)
        nodes.add(tgt)
    nodes_xml = "".join(f'<node id="{n}"><data key="d0">{n}</data><data key="d2">desc</data></node>' for n in nodes)
    edges_xml = "".join(f'<edge source="{src}" target="{tgt}"/>' for src, tgt in edges)
    path.write_text(
        f'<?xml version="1.0"?><graphml xmlns="http://graphml.graphdrawing.org/xmlns">'
        f'<key id="d0" for="node" attr.name="entity_id" attr.type="string"/>'
        f'<key id="d2" for="node" attr.name="description" attr.type="string"/>'
        f'<graph>{nodes_xml}{edges_xml}</graph></graphml>'
    )


def test_repair_relationship_sync_src_tgt_lowered_and_sorted(monkeypatch):
    """vdb_relationships 的 src_id='Banana' tgt_id='Apple'（src>tgt 顺序）→ 修复后 lower + sorted + 重算 __id__。
    
    LightRAG 写入时 sorted → src='apple' tgt='banana' → id=compute_mdhash_id('apple'+'banana', prefix='rel-')。
    repair 必须对齐：lower + sorted 后 id=compute_mdhash_id('apple'+'banana', prefix='rel-')。
    如果 repair 只 lower 不 sort，会算成 compute_mdhash_id('banana'+'apple')，跟新写入 id 不一致。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        # 存量关系：大写 src/tgt（src>tgt 顺序），__id__ 用大写算
        old_id = compute_mdhash_id("Banana" + "Apple", prefix="rel-")
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": old_id, "src_id": "Banana", "tgt_id": "Apple", "content": "rel desc"},
        ])
        # GraphML 边是 lower + sorted
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("apple", "banana"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        # src_id/tgt_id 应 lower + sorted（src='apple', tgt='banana'）
        assert vdb["data"][0]["src_id"] == "apple"
        assert vdb["data"][0]["tgt_id"] == "banana"
        # __id__ 应重算为 sorted 后的 lower 化 src+tgt 的 hash
        expected_id = compute_mdhash_id("apple" + "banana", prefix="rel-")
        assert vdb["data"][0]["__id__"] == expected_id, f"__id__ 应是 {expected_id}，实际: {vdb['data'][0]['__id__']}"
        assert vdb["data"][0]["__id__"].startswith("rel-")


def test_repair_relationship_sync_content_preserved(monkeypatch):
    """content/description/keywords 字段保留原样不 lower（自然语言）。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("Apple" + "Banana", prefix="rel-"),
             "src_id": "Apple", "tgt_id": "Banana",
             "content": "Apple founded Banana", "description": "Apple founded Banana",
             "keywords": "founder"},
        ])
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("apple", "banana"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        # content/description/keywords 保留原样
        assert vdb["data"][0]["content"] == "Apple founded Banana"
        assert vdb["data"][0]["description"] == "Apple founded Banana"


def test_repair_relationship_sync_orphan_edge_deleted(monkeypatch):
    """vdb 关系的 src/tgt 在 GraphML 没有对应边 → 删除（真孤儿关系）。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("keep_srckeep_tgt", prefix="rel-"),
             "src_id": "keep_src", "tgt_id": "keep_tgt", "content": "keep"},
            {"__id__": compute_mdhash_id("orphan_srcorphan_tgt", prefix="rel-"),
             "src_id": "orphan_src", "tgt_id": "orphan_tgt", "content": "orphan"},
        ])
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("keep_src", "keep_tgt"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        assert result["removed"] == 1
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        names = [(d["src_id"], d["tgt_id"]) for d in vdb["data"]]
        assert ("keep_src", "keep_tgt") in names
        assert ("orphan_src", "orphan_tgt") not in names


def test_repair_relationship_sync_self_loop_dropped(monkeypatch):
    """vdb 关系 lower 后 src==tgt（自环）→ GraphML 无对应边（LightRAG 不写自环边）→ 删除。
    
    这是 LightRAG 既有语义（_merge_edges_then_upsert L2024 if src_id == tgt_id: return None）。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("Apple" + "apple", prefix="rel-"),
             "src_id": "Apple", "tgt_id": "apple", "content": "self loop"},
        ])
        # GraphML 不写自环边
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "error"  # 修复后无数据
        assert result["removed"] == 1
```

- [ ] **Step 7: 跑测试确认失败**

```bash
python -m pytest tests/test_lightrag_relationship_sync.py -v
```

Expected: FAIL — `repair_relationship_sync` 不存在

- [ ] **Step 8: 写 repair_relationship_sync 实现（含 sorted）**

在 `niu_api/internal/lightrag_repair.py` 的 `repair_entity_sync` 之后插入：

```python
def repair_relationship_sync() -> dict[str, Any]:
    """修复 vdb_relationships 跟 GraphML 的边同步性。

    LightRAG 设计上 GraphML edge src/tgt 全部 lower 化，且写入 vdb 时
    先 sorted((src, tgt)) 再 compute_mdhash_id(sorted_src + sorted_tgt, prefix='rel-')。
    用户铁律：所有写入必须转小写。修复策略（以 GraphML 为真相源，统一小写 + sorted）：
    1. vdb 关系 src/tgt 大写 → 改小写 + sorted + 重算 __id__ = compute_mdhash_id(sorted_lower_src + sorted_lower_tgt, prefix='rel-')
       同时 src_id/tgt_id 也存排序后的值（跟 operate.py L1586-1589 / lightrag.py L2516 一致）
    2. vdb 关系 src/tgt 在 GraphML 没有对应边 → 删除（真孤儿关系，含自环——LightRAG 不写自环边）

    content/description/keywords 字段保留原样不 lower（自然语言）。

    Returns:
        {"status": "ok"|"error", "renamed": int, "removed": int, "message": str}
    """
    import time
    import xml.etree.ElementTree as ET
    import numpy as np
    from lightrag.utils import compute_mdhash_id

    storage_dir = _storage_dir()
    vdb_path = storage_dir / "vdb_relationships.json"
    graphml_path = storage_dir / "graph_chunk_entity_relation.graphml"

    if not vdb_path.exists() or not graphml_path.exists():
        return {"status": "error", "message": "vdb_relationships 或 GraphML 不存在", "renamed": 0, "removed": 0}

    # 1. 读 vdb
    try:
        raw = json.loads(vdb_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "message": f"vdb 读取失败: {e}", "renamed": 0, "removed": 0}

    embedding_dim = raw.get("embedding_dim")
    data_list = raw.get("data", [])
    if not isinstance(embedding_dim, int) or not isinstance(data_list, list):
        return {"status": "error", "message": "vdb 格式异常", "renamed": 0, "removed": 0}

    # 2. 读 GraphML 边集合（src+tgt 都 lower，且已 sorted）
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    graphml_edges: set[tuple[str, str]] = set()
    try:
        tree = ET.parse(graphml_path)
        root = tree.getroot()
        for edge in root.findall(f".//{ns}edge"):
            src = (edge.get("source") or "").lower()
            tgt = (edge.get("target") or "").lower()
            if src and tgt:
                # normalized：sorted 后存，跟 LightRAG 写入一致
                s, t = sorted((src, tgt))
                graphml_edges.add((s, t))
    except Exception as e:
        return {"status": "error", "message": f"GraphML 读取失败: {e}", "renamed": 0, "removed": 0}

    # 3. 分类 vdb 关系
    renamed = 0
    removed = 0
    new_data: list[dict] = []
    new_vectors: list[np.ndarray] = []

    for item in data_list:
        orig_src = item.get("src_id", "")
        orig_tgt = item.get("tgt_id", "")
        if not orig_src or not orig_tgt:
            continue
        lower_src = orig_src.lower()
        lower_tgt = orig_tgt.lower()
        # sorted 后存，跟 LightRAG 写入一致
        sorted_src, sorted_tgt = sorted((lower_src, lower_tgt))

        # 真孤儿关系（lower+sorted 后 GraphML 没有对应边，含自环——LightRAG 不写自环边）→ 删除
        if (sorted_src, sorted_tgt) not in graphml_edges:
            removed += 1
            continue

        # 大写改小写 + sorted + 重算 __id__
        # 保留所有字段（content/description/keywords 等原样），只更新 src_id/tgt_id/__id__
        new_item = {k: v for k, v in item.items() if k not in ("vector", "__id__")}
        new_item["src_id"] = sorted_src
        new_item["tgt_id"] = sorted_tgt
        new_item["__id__"] = compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")
        if orig_src != sorted_src or orig_tgt != sorted_tgt:
            renamed += 1

        # 解码原向量保留
        try:
            vec = _decode_vector(item.get("vector", ""), embedding_dim)
            new_vectors.append(np.array(vec, dtype=np.float16))
        except Exception:
            try:
                vec = _embed_text(item.get("content", ""))
                new_vectors.append(np.array(vec, dtype=np.float16))
            except Exception as e:
                logger.warning(f"[LightRAGRepair] 重建关系 {sorted_src}→{sorted_tgt} 向量失败: {e}，跳过")
                continue
        new_data.append(new_item)

    if not new_data:
        return {"status": "error", "message": "修复后无数据", "renamed": renamed, "removed": removed}

    # 4. 备份（shutil.copy2 + 毫秒时间戳）
    timestamp = int(time.time() * 1000)
    corrupt_bak = storage_dir / f"vdb_relationships.json.corrupt.{timestamp}.bak"
    try:
        shutil.copy2(vdb_path, corrupt_bak)
        logger.info(f"[LightRAGRepair] 损坏 vdb_relationships 备份到: {corrupt_bak}")
    except Exception as e:
        logger.error(f"[LightRAGRepair] 备份失败: {e}，abort")
        return {"status": "error", "message": f"备份失败: {e}", "renamed": renamed, "removed": removed}

    # 5. 原子写新 vdb
    matrix_f32 = np.array(new_vectors, dtype=np.float32)
    for i, item in enumerate(new_data):
        item["vector"] = _encode_vector(new_vectors[i])

    storage = {
        "embedding_dim": embedding_dim,
        "data": new_data,
        "matrix": _encode_matrix(matrix_f32),
    }
    tmp_file = vdb_path.with_name(vdb_path.name + ".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(storage, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, vdb_path)

    logger.info(f"[LightRAGRepair] 关系同步修复完成: 改名 {renamed}, 删除孤儿 {removed}, 总计 {len(new_data)}")
    return {
        "status": "ok",
        "renamed": renamed,
        "removed": removed,
        "message": f"改大小写/排序 {renamed} 条，删除孤儿 {removed} 条",
    }
```

**关键**：
- `sorted_src, sorted_tgt = sorted((lower_src, lower_tgt))` — 跟 operate.py L1586-1589 / lightrag.py L2516 一致
- `new_item["src_id"] = sorted_src; new_item["tgt_id"] = sorted_tgt` — src/tgt 存排序后的值，跟 LightRAG 新写入一致
- `new_item["__id__"] = compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")` — 重算 id 用 sorted 后的值
- `new_item = {k: v for k, v in item.items() if k not in ("vector", "__id__")}` — 保留 content/description/keywords 等字段原样
- 自环（sorted_src == sorted_tgt）不在 graphml_edges 里（LightRAG 不写自环边），会被当孤儿删除——这是正确行为

- [ ] **Step 9: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_relationship_sync.py -v
```

Expected: PASS（4 个测试：src>tgt 顺序 + content 保留 + 孤儿删除 + 自环删除）

- [ ] **Step 10: repair_all 调用 repair_relationship_sync**

改 `niu_api/internal/lightrag_repair.py` 的 `repair_all`：

```python
def repair_all() -> dict[str, Any]:
    """一键修复所有 vdb。"""
    results: dict[str, Any] = {}
    for vdb_file in _VDB_TEXT_FIELD:
        results[vdb_file] = repair_vdb(vdb_file)
    results["entity_sync"] = repair_entity_sync()
    results["relationship_sync"] = repair_relationship_sync()  # 新增
    return results
```

- [ ] **Step 11: 跑全部测试确认无回归**

```bash
python -m pytest tests/test_lightrag_entity_sync.py tests/test_lightrag_relationship_sync.py tests/test_lightrag_repair*.py tests/test_lightrag_resilience*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

- [ ] **Step 12: 提交**

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_entity_sync.py tests/test_lightrag_relationship_sync.py
git commit -m "fix(repair): 修正 repair_entity_sync __id__ 重算 + 新增 repair_relationship_sync

修正：repair_entity_sync L339 把 __id__ 设成裸 lower_name（如 'niu'），
但 LightRAG 写入 vdb 时 __id__ 是 compute_mdhash_id(name, prefix='ent-')
算的 hash id（如 'ent-xxxxx'）。修复后 vdb __id__ 跟新写入不匹配。
改为 compute_mdhash_id(lower_name, prefix='ent-') 重算。

新增 repair_relationship_sync：修 vdb_relationships 的 src_id/tgt_id
大写 + sorted + 重算 __id__（compute_mdhash_id(sorted_src+sorted_tgt,
prefix='rel-')，跟 operate.py L1586-1589 / lightrag.py L2516 一致），
src_id/tgt_id 也存排序后的值；删除 GraphML 无对应边的真孤儿关系
（含自环——LightRAG 不写自环边）；content/description/keywords
字段保留原样不 lower。repair_all 自动调用。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: niu_api region 字典 lower 化

**目标：** `region_injector.py` / `region_activation.py` / `lightrag_adapter.py` 的 region 字典构建时 key lower + 查询时入参 lower（双重保险）。

**Files:**
- Modify: `niu_api/internal/region_injector.py`（L69-72 附近）
- Modify: `niu_api/internal/region_activation.py`（L174-175 附近）
- Modify: `niu_api/internal/lightrag_adapter.py`（L460 附近）
- Test: `tests/test_region_case_insensitive.py`（新建）

- [ ] **Step 1: 临时备份**

```bash
cd <repo_root>
git add -A && git commit -m "backup: region 字典 lower 化前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 读 region_injector.py 的 entity_to_region 构建逻辑**

```bash
grep -n "entity_to_region\|member_set\|activate_for_query" niu_api/internal/region_injector.py | head -20
```

记录 `entity_to_region` 字典在哪构建（L69-72 `activate_for_query` 内联）、查询在哪（L91-99）。

- [ ] **Step 3: 写失败测试 — region 字典大小写不敏感**

创建 `tests/test_region_case_insensitive.py`：

```python
"""region 字典构建时 entity_name key 应 lower 化，查询大小写不敏感。"""
import pytest
from unittest.mock import patch
from niu_api.internal import region_injector


def test_entity_to_region_case_insensitive(monkeypatch):
    """vdb 返回 'Apple'（修复后是 'apple'），region 字典 key 也应 lower，查询能命中。"""
    # 这个测试验证 activate_for_query 内联构建的 entity_to_region 字典 key 是 lower
    # 执行者读 region_injector.py L69-72 实际代码后调整 mock
    # 关键断言：用 'Apple'.lower() 查 entity_to_region 能命中
    # 这里用一个简化的单元测试验证 lower 化逻辑
    entity_to_region = {"apple": "tech_region", "banana": "food_region"}
    # 模拟查询：vdb 返回 'apple'（已 lower），查到 tech_region
    assert entity_to_region.get("apple".lower()) == "tech_region"
    # 模拟查询：如果 vdb 还有大写 'Apple'，查 'Apple'.lower() 也能命中
    assert entity_to_region.get("Apple".lower()) == "tech_region"
```

**注意**：这个测试是验证 lower 化逻辑的示意。执行者读 region_injector.py 实际代码后，可以改成更接近真实场景的集成测试。

- [ ] **Step 4: 跑测试确认**

```bash
python -m pytest tests/test_region_case_insensitive.py -v
```

（这个示意测试可能直接 PASS，因为只是验证 lower 逻辑。真实价值在 Step 5-8 的代码改动 + 回归测试）

- [ ] **Step 5: 改 region_injector.py — 字典构建时 key lower + 查询时入参 lower**

读 `region_injector.py` L69-72 的 `entity_to_region` 构建逻辑和 L91-99 的查询逻辑。用 Edit 工具改：

```python
# 改前（构建，示意）：
entity_to_region[entity_name] = region.name

# 改后：
entity_to_region[entity_name.lower()] = region.name
```

```python
# 改前（查询，示意）：
region = entity_to_region.get(entity_name)

# 改后：
region = entity_to_region.get(entity_name.lower())
```

**双重保险**：构建时 key lower + 查询时入参 lower。

- [ ] **Step 6: 改 region_activation.py — 同样逻辑**

读 L174-175 的 `new_entity_to_region[entity_name] = region.name`，改为：

```python
new_entity_to_region[entity_name.lower()] = region.name
```

查询处也加 `.lower()`（如有）。

- [ ] **Step 7: 改 lightrag_adapter.py — member_set 构建时 lower + 查询时 lower**

读 L460 的 `data.get("entity_name") in member_set`，把 `member_set` 构建时元素 lower 化，查询时 `data.get("entity_name").lower() in member_set`。

**注意**：`data.get("entity_name")` 可能返回 None，需 `(data.get("entity_name") or "").lower()` 防 AttributeError。

- [ ] **Step 8: 跑全部测试确认无回归**

```bash
python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: 全部 PASS。如果有 region 相关测试因大小写变化失败，更新断言。

- [ ] **Step 9: 权限修复（铁律 #7）**

```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null || true
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

- [ ] **Step 10: 提交**

```bash
git add niu_api/internal/region_injector.py niu_api/internal/region_activation.py niu_api/internal/lightrag_adapter.py tests/test_region_case_insensitive.py
git commit -m "fix(region): region 字典 key 统一 lower 化，查询大小写不敏感

region_injector/region_activation/lightrag_adapter 的 entity_to_region
字典构建时 key lower 化，查询时入参也 lower 化（双重保险）。
配合 LightRAG fork 的 entity_name lower 化，整个知识图谱大小写不敏感。

lightrag_adapter 的 data.get('entity_name') 加 (or '') 防 None.lower()。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 存量数据迁移 + 端到端验证（需用户协助）

**目标：** 用修正后的 `repair_entity_sync` + 新增 `repair_relationship_sync` 迁移存量 vdb 数据，启动 `./niu` 验证 `check_all` 报 `ok=True`（case_mismatch 归零）。

**Files:**
- 无源码改动（只跑迁移 + 验证）

**注意**：本 Task 的 Step 5/6 需要用户手动操作（启动 ./niu、拖文档、看日志），子 Agent 做不了。

- [ ] **Step 1: 临时备份真实数据**

```bash
cp ~/.niu/lightrag_storage/vdb_entities.json ~/.niu/lightrag_storage/vdb_entities.json.bak.before-lower-fix
cp ~/.niu/lightrag_storage/vdb_relationships.json ~/.niu/lightrag_storage/vdb_relationships.json.bak.before-lower-fix
cp ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml.bak.before-lower-fix
```

- [ ] **Step 2: 跑 check_all 看当前状态**

```bash
cd <repo_root>
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
r = check_all()
print(f'ok={r[\"ok\"]}, total_errors={r[\"total_errors\"]}')
print(f'entity_sync: {r[\"entity_sync\"][\"stats\"]}')
"
```

- [ ] **Step 3: 跑 repair_all 迁移存量（含 entity_sync + relationship_sync）**

```bash
python3 -c "
from niu_api.internal.lightrag_repair import repair_all
import json
r = repair_all()
print(json.dumps({
    'entity_sync': r.get('entity_sync', {}),
    'relationship_sync': r.get('relationship_sync', {}),
}, indent=2, ensure_ascii=False))
"
```

Expected: 两个 sync 都 `status: ok`（或 `error: 修复后无数据`，如果存量是空），`renamed > 0`（如果有大写）

- [ ] **Step 4: 再跑 check_all 确认 ok=True**

```bash
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
r = check_all()
print(f'ok={r[\"ok\"]}, total_errors={r[\"total_errors\"]}')
print(f'entity_sync: {r[\"entity_sync\"][\"stats\"]}')
"
```

Expected: `ok=True`，case_mismatch 归零

- [ ] **Step 5: 启动 ./niu 验证（需用户操作）**

```bash
./niu
```

用户观察日志：
- Phase 1 检测 `check_ok=True`（不弹窗）
- LightRAG 实例初始化成功
- 触发一次知识库查询（如"查一下知识库"）

**注意**：如果日志仍有 `WARNING: Some nodes are missing`，那是真孤儿节点（不是大小写问题），属于另一类问题，**不要误判为大小写修复失败**。本计划只解决 `case_mismatch`，不解决真孤儿。

- [ ] **Step 6: 入库新文档验证不复发（需用户操作）**

用户拖入一个新文档（如 PDF），等 LightRAG 提取实体后：

```bash
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
r = check_all()
print(f'entity_sync: {r[\"entity_sync\"][\"stats\"]}')
"
```

Expected: `case_mismatch: 0`（新写入的 entity_name 已经是 lower）

- [ ] **Step 7: 验证通过后保留备份 24 小时**

```bash
ls -la ~/.niu/lightrag_storage/*.bak.before-lower-fix
echo "备份保留 24 小时，确认无问题后手动删"
```

**如果 Step 4 `check_all` 仍报 `ok=False` 且 `case_mismatch > 0`**，回退：

```bash
cp ~/.niu/lightrag_storage/vdb_entities.json.bak.before-lower-fix ~/.niu/lightrag_storage/vdb_entities.json
cp ~/.niu/lightrag_storage/vdb_relationships.json.bak.before-lower-fix ~/.niu/lightrag_storage/vdb_relationships.json
cp ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml.bak.before-lower-fix ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
```

报告失败，等用户决定。

- [ ] **Step 8: 提交验证结果**

```bash
git add -A
git commit -m "test(lightrag): 端到端验证大小写不敏感修复走通

- Fork 源码 operate.py + lightrag.py 两条路径加 .lower()
- niu_api region 字典 key lower 化
- repair_entity_sync __id__ 重算修正 + 新增 repair_relationship_sync（含 sorted）
- 存量 vdb 数据迁移（entities + relationships）
- check_all 报 ok=True，case_mismatch 归零
- 入库新文档 case_mismatch 不复发

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Self-Review

### 1. Spec coverage 检查

- ✅ LLM 提取路径 entity_name/source/target lower（Task 1 L401/L490/L493）
- ✅ 脑区注入路径 entity_name/src_id/tgt_id lower（Task 1 lightrag.py ainsert_custom_kg，src/tgt 在 L2465/L2466 处 lower，让后续 sort/dedup/has_nodes_batch 都用 lower）
- ✅ vdb entity_name 字段小写（源头 lower 后，写入 vdb 也是 lower）
- ✅ GraphML node id 小写（已有 _normalize_node_id，不变）
- ✅ vdb id 跟 GraphML 一致（compute_mdhash_id 用 lower 化的 name，存量迁移重算）
- ✅ vdb_relationships src/tgt 迁移（Task 3 新增 repair_relationship_sync，含 sorted）
- ✅ vdb __id__ 重算（Task 3 修正 repair_entity_sync L339 + repair_relationship_sync，关系 id 用 sorted_src+sorted_tgt）
- ✅ 查询时大小写不敏感（vdb 向量检索天然不敏感，GraphML get_node 内部 lower，region 字典也 lower）
- ✅ description 保留大小写（不改 L444/L523，repair_relationship_sync content/description/keywords 字段保留原样）
- ✅ entity_type/edge_keywords 已有 lower（不改）
- ✅ niu_api region 字典联动（Task 4）
- ✅ 存量数据迁移（Task 5 用 repair_all，含 entity_sync + relationship_sync）
- ✅ 端到端验证（Task 5 Step 5/6，需用户协助）
- ✅ Fork 推送 + 清理重装（Task 1 Step 13 + Task 2 含清理 site-packages）
- ✅ 自环关系被丢弃（Task 1 测试显式覆盖 + Task 3 repair_relationship_sync 自环测试覆盖，LightRAG 既有语义）

### 2. Placeholder 检查

- Task 1 Step 9 的 `test_ainsert_custom_kg` 从 `pytest.skip` 改为真实测试——执行者读 lightrag.py 实际代码后补全。这是合理的，因为 ainsert_custom_kg 需要 mock LightRAG 实例，具体 mock 方式依赖实际代码结构。
- Task 4 Step 3 的测试是示意性的，但 Step 5-8 的代码改动有明确改前/改后模式。
- 其他 Step 都有完整代码。

### 3. Type consistency 检查

- `entity_name` / `source` / `target` 三处都加 `.lower()` — 一致
- `compute_mdhash_id(lower_name, prefix="ent-")` for entities, `compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")` for relationships — 跟 LightRAG 写入格式一致（operate.py L1586-1589 / lightrag.py L2467/L2516）
- region 字典构建 key lower + 查询入参 lower — 双重保险
- `repair_entity_sync` 返回 `renamed`/`removed`/`rebuilt`，`repair_relationship_sync` 返回 `renamed`/`removed` — 风格一致

### 4. 已知边界

- **Task 1 Step 7-9 lightrag.py 改动**：`ainsert_custom_kg` 的实际代码结构需要执行者读代码确认具体行号，计划给了改前/改后模式。关键约束（两条原则）：
  - **entity 路径**：必须在 L2423 dedup 循环开头就 lower entity_name，并同步更新 `entity_data["entity_name"]` 字典原值，让 dedup key + 所有下游（entity_nodes/all_entities_data/compute_mdhash_id）全部用 lower
  - **relation 路径**：必须在 L2465/L2466 处 lower src_id/tgt_id（sort 之前），让后续 sort/dedup/has_nodes_batch 都用 lower
- **Task 2 site-packages 清理**：editable 模式重装后，`lightrag.__file__` 应指向 Fork 源码路径。Step 2 清理覆盖了物理目录 + .pth + finder + dist-info。
- **Task 3 repair_relationship_sync sorted**：必须 `sorted((lower_src, lower_tgt))` 后再拼接算 id，跟 operate.py L1586-1589 / lightrag.py L2516 一致。src_id/tgt_id 也存排序后的值。
- **Task 5 Step 5/6 端到端验证**：需要用户手动操作（启动 ./niu、拖文档、看日志），子 Agent 做不了。计划在 Task 5 标题标注"需用户协助"。
- **"nodes are missing" vs case_mismatch**：本计划修复的是 `case_mismatch`（vdb/GraphML 不一致），不是 "nodes are missing"（真孤儿节点）。如果 Task 5 Step 5 仍报 "nodes are missing"，那是另一类问题，不要误判为大小写修复失败。
- **自环关系**：Task 1 改 source/target 后，source="Apple" target="apple" 会 lower 都成 "apple"，L510 `source == target` 触发 `return None`。这是 LightRAG 既有语义（`_normalize_node_id` 早就 lower 化节点 id，自环早就在丢弃），不是本次修复引入的新行为。Task 1 Step 2 和 Task 3 Step 6 都有专门测试覆盖。
- **lightrag_adapter.py None 防护**：`data.get("entity_name")` 可能返回 None，Task 4 Step 7 加 `(or "")` 防 AttributeError。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-08-lightrag-case-insensitive.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
