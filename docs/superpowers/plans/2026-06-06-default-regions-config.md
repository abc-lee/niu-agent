# 缺省脑区配置化与保护机制重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将缺省脑区定义从硬编码改为配置驱动（preferences.json），保护机制从"靠 community_id 是否为空推断"改为"查配置声明"。

**Architecture:** 在 preferences.json 增加 `brain_regions` 配置段，列出缺省脑区的名称、描述和优先级。程序启动时读配置 → 查图谱 → 缺啥补啥。清理/解散/合并脑区时，通过配置列表判断是否缺省脑区，而非依赖 description 中的 community_id 是否为空。删除代码中的 DEFAULT_REGIONS 硬编码字典。

**Tech Stack:** Python, JSON 配置, LightRAG (fork), NetworkX

---

## 核心原则

1. **声明式保护** — 配置文件里声明的就是缺省脑区，程序不靠推断
2. **配置驱动创建** — 缺省脑区的名称、描述、优先级都从 preferences.json 读取
3. **向后兼容** — 旧版 preferences.json 没有 `brain_regions` 段时，使用代码中的默认值（与当前硬编码一致）
4. **幂等创建** — 已存在的脑区跳过，不重复创建

---

## 执行顺序

1. **提交1**：Task 1（preferences.json 加 brain_regions 配置段 + 读取函数）
2. **提交2**：Task 2（create_default_regions 改为读配置 + 删除 DEFAULT_REGIONS 硬编码）
3. **提交3**：Task 3（保护机制改为查配置 + 删除 community_id 判断）
4. **提交4**：Task 4（验证）

---

### Task 1: preferences.json 加 brain_regions 配置段 + 读取函数

**Files:**
- Modify: `~/.niu/preferences.json` — 增加 brain_regions 段
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py` — 增加 `get_default_regions_config()` 函数

- [ ] **Step 1: 在 preferences.json 增加 brain_regions 配置段**

在 `~/.niu/preferences.json` 的顶级增加 `brain_regions` 键：

```json
{
  "brain_regions": {
    "defaults": [
      {
        "label": "聊天历史",
        "description": "日常对话中提炼的偏好、技能和经验记忆",
        "priority": "core"
      },
      {
        "label": "文档库",
        "description": "用户导入的文档和资料，经解析后入库的知识",
        "priority": "core"
      },
      {
        "label": "知识体系",
        "description": "系统化组织的概念、关系和理论体系",
        "priority": "core"
      },
      {
        "label": "人际关系",
        "description": "人物实体、关系网络、社交图谱",
        "priority": "category"
      },
      {
        "label": "工作事务",
        "description": "工作相关的项目、任务、决策记录",
        "priority": "category"
      },
      {
        "label": "生活事务",
        "description": "日常生活相关的日程、健康、财务",
        "priority": "category"
      }
    ]
  }
}
```

注：值与当前硬编码的 DEFAULT_REGIONS 完全一致，保证向后兼容。

- [ ] **Step 1b: 同步修改初始模板文件**

Go 启动器在 `~/.niu/preferences.json` 不存在时，从 `memory/preferences.json` 复制初始模板（见 `main.go:135`）。另外 `config/user-data/preferences.json` 是手动安装用户的参考模板。两个文件都需要同步添加 `brain_regions` 段。

在以下两个文件的顶级增加同样的 `brain_regions` 键（内容与 Step 1 完全相同）：
- `REDACTED_USER_PATH/tools/ai-bot/memory/preferences.json`
- `REDACTED_USER_PATH/tools/ai-bot/config/user-data/preferences.json`

- [ ] **Step 2: 添加 `json` 和 `os` 导入**

在 `region_manager.py` 文件顶部（约 line 16-24 的 import 区域）添加：

```python
import json
import os
```

- [ ] **Step 3: 在 region_manager.py 增加 `get_default_regions_config()` 函数**

在 `DEFAULT_REGIONS` 定义之前（约 line 1005 附近）添加：

```python
def get_default_regions_config() -> list[dict]:
    """Read default brain region definitions from preferences.json.

    Returns list of dicts with keys: label, description, priority.
    Falls back to hardcoded defaults ONLY when preferences.json has no
    brain_regions section at all. If the section exists (even with empty
    defaults list), that configuration is respected.
    """
    try:
        prefs_path = os.path.expanduser("~/.niu/preferences.json")
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        # Respect explicit configuration — even empty defaults list
        if "brain_regions" in prefs:
            return prefs["brain_regions"].get("defaults", [])
    except Exception:
        pass
    # Fallback ONLY when preferences.json has no brain_regions section
    return [
        {"label": "聊天历史", "description": "日常对话中提炼的偏好、技能和经验记忆", "priority": "core"},
        {"label": "文档库", "description": "用户导入的文档和资料，经解析后入库的知识", "priority": "core"},
        {"label": "知识体系", "description": "系统化组织的概念、关系和理论体系", "priority": "core"},
        {"label": "人际关系", "description": "人物实体、关系网络、社交图谱", "priority": "category"},
        {"label": "工作事务", "description": "工作相关的项目、任务、决策记录", "priority": "category"},
        {"label": "生活事务", "description": "日常生活相关的日程、健康、财务", "priority": "category"},
    ]
```

同时增加 `is_default_region()` 辅助函数：

```python
def is_default_region(region_name: str) -> bool:
    """Check if a region name is a default region defined in preferences.

    Uses the configured default regions list, not community_id.
    """
    defaults = get_default_regions_config()
    for d in defaults:
        if region_name == f"{d['label']}{REGION_SUFFIX}":
            return True
    return False
```

注：需要确认 `json` 和 `os` 已在 region_manager.py 顶部导入。

- [ ] **Step 4: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py`
Expected: 无输出

- [ ] **Step 5: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py
git commit -m "feat: add brain_regions config to preferences.json and reader functions"
```

---

### Task 2: create_default_regions 改为读配置 + 删除 DEFAULT_REGIONS 硬编码

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py` — create_default_regions() 改用 get_default_regions_config()

- [ ] **Step 1: 修改 create_default_regions() 使用配置**

```python
# 修改前 (line 1068):
for region_label, config in DEFAULT_REGIONS.items():
    # Skip category regions unless explicitly requested
    if config.get("priority") == "category" and not include_category:
        continue

    region_name = f"{region_label}{REGION_SUFFIX}"

    # Check if region already exists (direct graph read, no LLM)
    if region_name in existing_regions:
        existing += 1
        continue

    # Collect region entity and anchor relation for batch inject
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

# 修改后:
for region_def in get_default_regions_config():
    region_label = region_def["label"]
    # Skip category regions unless explicitly requested
    if region_def.get("priority") == "category" and not include_category:
        continue

    region_name = f"{region_label}{REGION_SUFFIX}"

    # Check if region already exists (direct graph read, no LLM)
    if region_name in existing_regions:
        existing += 1
        continue

    # Collect region entity and anchor relation for batch inject
    all_entities.append({
        "entity_name": region_name,
        "entity_type": REGION_ENTITY_TYPE,
        "description": region_def["description"],
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
```

- [ ] **Step 2: 删除 DEFAULT_REGIONS 硬编码字典**

删除 `region_manager.py` 中的 `DEFAULT_REGIONS` 字典定义（约 line 1010-1037）。`get_default_regions_config()` 的 fallback 已在 Task 1 Step 3 中改为内联硬编码（不引用 DEFAULT_REGIONS），所以可以直接删除。

- [ ] **Step 3: 重写测试文件**

`tests/test_default_regions.py` 的4个测试方法都有问题：`test_default_regions_constant` 导入 `DEFAULT_REGIONS`（删除后 ImportError），其他3个 mock 了错误的旧API（`query_data`/`inject_entity` 而非 `get_brain_regions()`/`inject_custom_kg()`），且期望 `created == 3`（只有 core 脑区）。重写整个文件：

```python
"""Tests for default brain region creation."""
from unittest.mock import MagicMock, patch
import pytest


class TestDefaultRegions:
    def test_default_regions_config(self):
        """get_default_regions_config() 应返回6个缺省脑区"""
        from niu_api.internal.region_manager import get_default_regions_config
        # Mock preferences.json not having brain_regions section — fallback to hardcoded
        with patch("builtins.open", side_effect=FileNotFoundError):
            defaults = get_default_regions_config()
        labels = [d["label"] for d in defaults]
        assert "聊天历史" in labels
        assert "文档库" in labels
        assert "知识体系" in labels
        assert len(defaults) == 6

    def test_create_default_regions_creates_new(self):
        """脑区不存在时应创建6个"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        with patch("niu_api.internal.region_manager.get_brain_regions", return_value=[]):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 6
        assert result["existing"] == 0
        assert mock_ingester.inject_custom_kg.call_count == 1

    def test_create_default_regions_skips_existing(self):
        """脑区已存在时应全部跳过"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        all_regions = ["聊天历史脑区", "文档库脑区", "知识体系脑区",
                       "人际关系脑区", "工作事务脑区", "生活事务脑区"]
        with patch("niu_api.internal.region_manager.get_brain_regions",
                    return_value=all_regions):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 0
        assert result["existing"] == 6
        assert mock_ingester.inject_custom_kg.call_count == 0

    def test_create_default_regions_partial_existing(self):
        """部分脑区已存在时只创建缺失的"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        with patch("niu_api.internal.region_manager.get_brain_regions",
                    return_value=["聊天历史脑区", "文档库脑区"]):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 4
        assert result["existing"] == 2
        assert mock_ingester.inject_custom_kg.call_count == 1
```

- [ ] **Step 4: 修改 brain_region_prompt.py 的 FALLBACK_REGIONS**

`brain_region_prompt.py` 中的 `FALLBACK_REGIONS` 硬编码了脑区名称列表。改为从 `get_default_regions_config()` 读取：

```python
# 修改前 (brain_region_prompt.py line 90):
FALLBACK_REGIONS = "聊天历史脑区、文档库脑区、知识体系脑区、人际关系脑区、工作事务脑区、生活事务脑区"

# 修改后:
def _get_fallback_regions_text() -> str:
    from niu_api.internal.region_manager import get_default_regions_config, REGION_SUFFIX
    defaults = get_default_regions_config()
    names = [f"{d['label']}{REGION_SUFFIX}" for d in defaults]
    return "、".join(names)

FALLBACK_REGIONS = _get_fallback_regions_text()
```

- [ ] **Step 5: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py && python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/brain_region_prompt.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py niu_api/internal/brain_region_prompt.py tests/test_default_regions.py
git commit -m "refactor: create_default_regions reads from preferences.json, remove DEFAULT_REGIONS hardcode"
```

---

### Task 3: 保护机制改为查配置 + 删除 community_id 判断

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py` — cleanup_stale_regions, dissolve_shrunk_regions, _find_most_similar_neighbor
- Modify: `REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py` — _merge_and_dissolve

- [ ] **Step 1: cleanup_stale_regions() 保护改为 is_default_region()**

```python
# 修改前 (line 521-525):
for region in existing_regions:
    # Protect default regions (no community_id = created by create_default_regions)
    if not region.community_id:
        logger.debug("保护默认脑区: %s", region.name)
        continue
    if region.community_id not in current_community_ids:

# 修改后:
for region in existing_regions:
    # Protect default regions (defined in preferences.json)
    if is_default_region(region.name):
        logger.debug("保护默认脑区: %s", region.name)
        continue
    if region.community_id not in current_community_ids:
```

- [ ] **Step 2: dissolve_shrunk_regions() 保护改为 is_default_region()**

```python
# 修改前 (line 585-588):
for region in existing_regions:
    # Protect default regions (no community_id)
    if not region.community_id:
        continue

    members = self.get_region_members(region.name)

# 修改后:
for region in existing_regions:
    # Protect default regions (defined in preferences.json)
    if is_default_region(region.name):
        continue

    members = self.get_region_members(region.name)
```

- [ ] **Step 3: _find_most_similar_neighbor() 保护改为 is_default_region()**

```python
# 修改前 (line 723):
if not other.community_id:
    continue

# 修改后:
if is_default_region(other.name):
    continue
```

- [ ] **Step 4: region_sync.py _merge_and_dissolve() 保护改为 is_default_region()**

```python
# 修改前 (line 357-363):
if not source_state.community_id:
    logger.debug("[RegionSync] 跳过预置脑区合并: %s", source_state.label)
    continue
if not target_state.community_id:
    logger.debug("[RegionSync] 跳过预置脑区作为合并目标: %s", target_state.label)
    continue

# 修改后:
if is_default_region(source_state.region_id):
    logger.debug("[RegionSync] 跳过预置脑区合并: %s", source_state.label)
    continue
if is_default_region(target_state.region_id):
    logger.debug("[RegionSync] 跳过预置脑区作为合并目标: %s", target_state.label)
    continue
```

同时需要在 `region_sync.py` 文件顶部添加导入：

```python
from niu_api.internal.region_manager import is_default_region
```

注：`BrainRegionState.region_id` 是脑区完整名称（如"文档库脑区"），与 `is_default_region()` 的参数格式匹配。`source_state.label` 是去掉"脑区"后缀的名称（如"文档库"），仅用于日志显示。

- [ ] **Step 5: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py && python -m py_compile REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py agent/injector/region_sync.py
git commit -m "refactor: protect default regions by config lookup, not community_id check"
```

---

### Task 4: 验证

- [ ] **Step 1: 派审查Agent检查所有修改**

审查要点：
1. `get_default_regions_config()` 读取 preferences.json 是否正确，fallback 是否完整
2. `is_default_region()` 是否在所有需要保护的地方被使用
3. 旧的 `not region.community_id` 判断是否全部被替换，没有遗漏
4. region_sync.py 中 `BrainRegionState.region_id` 是脑区完整名称（已确认匹配）
5. brain_region_prompt.py 的 FALLBACK_REGIONS 是否正确从配置读取
6. 向后兼容：旧 preferences.json 没有 brain_regions 段时是否正确 fallback

- [ ] **Step 2: 修复审查发现的问题（如有）**

- [ ] **Step 3: 最终提交**

```bash
git add -A
git commit -m "fix: address review findings for default regions config"
```
