# KG 实体合并修复 — 照片/人物实体去重 + 命名更新 + 自动同步移除

> **⚠️ 历史文档**：本文档中使用 `brain:Niu`、`brain:region:xxx`、`brain:concept:xxx`、`brain:event:xxx`、`brain:person:xxx`、`brain:session:xxx`、`event:xxx`、`skill:xxx`、`person:xxx` 等冒号前缀实体名的描述已过时。当前系统要求所有实体名必须使用自然语言（如 `Niu`、`编程开发脑区`、`Python`、`海滩日落事件`），禁止冒号前缀格式。详见 `docs/kg-dev-dictionary.md`。

> 修复知识图谱中照片实体重复、人物命名不更新、照片自动同步无效三大问题。
> TDD 实施：先写测试，再写实现，每步提交。

---

## 问题根因

| # | 问题 | 根因 | 修复文件 |
|---|------|------|----------|
| 1 | 照片入库后，内容提取/梦境进化时 LLM 自由提取实体名，产生重复实体 | LightRAG 提取提示词中缺少照片/人物实体命名规则 | `brain_region_prompt.py` |
| 2 | `name_person` 只做 ainsert 文本，不更新 KG 中 `person:{uuid}` 实体的 description | `name_person` 用 `lightrag_insert` 而非 `lightrag_insert_entity` | `photo-server/__init__.py` |
| 3 | `_sync_photos_db` 自动同步照片到 KG，但照片文件路径不存在 | 自动同步无意义，照片入库始终走结构化 `inject_custom_kg` | `lightrag_sync.py` |

---

## Phase 1: 照片/人物实体命名规则注入

**目标**：在 LightRAG 提取提示词中注入照片和人物实体的命名规则，让 LLM 提取时知道：
- 人物实体用 `person:{uuid}` 格式，UUID 是不可变锚点
- 照片实体已由结构化入库创建，提取时不应创建新实体
- 遇到 `person:{uuid}` 和同名自然语言实体时，应合并而非创建新实体

### Step 1.1: RED — 写测试

**文件**: `tests/test_brain_region_prompt.py`（新建）

```python
"""Tests for brain_region_prompt entity naming rules injection."""
import pytest
from niu_api.internal.brain_region_prompt import (
    build_static_brain_region_prompt,
    is_lightrag_extraction_request,
    inject_brain_region_context,
)


class TestStaticPromptContainsEntityRules:
    """Static prompt must include photo/person entity naming rules."""

    def test_prompt_contains_person_uuid_rule(self):
        """人物实体必须使用 person:{uuid} 格式。"""
        prompt = build_static_brain_region_prompt()
        assert "person:" in prompt
        assert "uuid" in prompt.lower() or "UUID" in prompt

    def test_prompt_contains_photo_entity_rule(self):
        """提示词必须说明照片实体由结构化入库创建。"""
        prompt = build_static_brain_region_prompt()
        assert "照片" in prompt
        assert "结构化" in prompt or "inject" in prompt.lower()

    def test_prompt_contains_merge_rule(self):
        """提示词必须说明遇到同名实体时应合并而非创建新实体。"""
        prompt = build_static_brain_region_prompt()
        assert "合并" in prompt

    def test_prompt_still_contains_brain_region_rules(self):
        """原有脑区架构规则不能丢失。"""
        prompt = build_static_brain_region_prompt()
        assert "brain:region" in prompt
        assert "brain:Niu" in prompt
```

### Step 1.2: GREEN — 修改 `_STATIC_BRAIN_REGION_PROMPT`

**文件**: `niu_api/internal/brain_region_prompt.py`

**修改**: 在 `_STATIC_BRAIN_REGION_PROMPT` 末尾追加照片/人物实体规则段落。

**原代码** (L13-23):
```python
_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱中存在脑区节点（`brain:region:*`），实体通过 `belongs_to_region` 边归属于脑区，脑区通过 `brain_region_anchor` 边连接到根节点 `brain:Niu`。

默认脑区：
- `brain:region:聊天历史` — 聊天对话中的实体
- `brain:region:文档库` — 文档内容的实体
- `brain:region:知识体系` — 结构化知识、概念等实体

提取实体时根据语义归入对应脑区，无法判断的归入"知识体系"。"""
```

**新代码**:
```python
_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱中存在脑区节点（`brain:region:*`），实体通过 `belongs_to_region` 边归属于脑区，脑区通过 `brain_region_anchor` 边连接到根节点 `brain:Niu`。

默认脑区：
- `brain:region:聊天历史` — 聊天对话中的实体
- `brain:region:文档库` — 文档内容的实体
- `brain:region:知识体系` — 结构化知识、概念等实体

提取实体时根据语义归入对应脑区，无法判断的归入"知识体系"。

## 照片与人物实体规则

### 人物实体
- 人物实体使用 `person:{uuid}` 格式命名，UUID 是不可变锚点。
- 当你看到文本中提到"人物ID为 xxx 的人"或"人物ID xxx"时，应提取为 `person:xxx` 实体，而非用姓名或"未命名人物"作为实体名。
- 如果图谱中已存在 `person:xxx` 实体，更新其描述即可，不要创建新实体。
- 当 `person:xxx` 实体的描述从"未命名人物"变为真实姓名时，这是同一实体的属性更新，不是新实体。

### 照片实体
- 照片实体已由结构化入库程序预先创建，实体名格式为 `photo:{file_path}`。
- 提取时如果遇到与照片相关的描述，应与已有的 `photo:*` 实体建立关系，不要创建新的照片实体。
- 照片中出现的人物应关联到对应的 `person:{uuid}` 实体，不要用自然语言姓名创建独立的人物实体。

### 合并规则
- 当发现两个实体实际指同一事物时（如 `person:abc123` 和"张三"），应合并为 `person:abc123`，将"张三"作为描述信息，而非保留两个独立实体。
- 同一人物的不同称呼（别名、昵称）应合并到同一 `person:{uuid}` 实体中。"""
```

### Step 1.3: REFACTOR — 清理

- 确认测试通过
- 确认 `inject_brain_region_context` 仍正常工作（新内容只是追加到 static prompt）

### Step 1.4: COMMIT

```
feat: 注入照片/人物实体命名规则到 LightRAG 提取提示词

- 人物实体使用 person:{uuid} 格式，UUID 是不可变锚点
- 照片实体由结构化入库预创建，提取时不应重复创建
- 同名实体应合并而非创建新实体
- 添加测试验证提示词包含所有必要规则
```

---

## Phase 2: 修复 name_person 的 KG 实体更新

**目标**：`name_person` 命名人物时，应使用 `lightrag_insert_entity` 更新 KG 中 `person:{uuid}` 实体的 description，而非仅 ainsert 一段文本让 LLM 自由提取。

**当前行为** (L1828-1840):
```python
# KG: 通过 ainsert 传入命名关联文本，LightRAG 自动建立 UUID-真名关联
try:
    from agent.tool_registry import get_registry
    registry = get_registry()
    insert_fn = registry.get("lightrag-server/lightrag_insert")
    if insert_fn:
        rename_text = f"人物ID为 {person_id} 的人，用户确认其姓名为{name}。"
        insert_fn(content=rename_text)
        logger.info(f"[NAME_PERSON] KG rename ingested: person_id={person_id}, name={name}")
    else:
        logger.warning("[NAME_PERSON] lightrag_insert not available in registry")
except Exception as e:
    logger.warning(f"[NAME_PERSON] LightRAG sync failed: {e}")
```

**问题**：`lightrag_insert` 让 LLM 自由提取，可能创建"张三"实体而非更新 `person:uuid` 实体。

**参考**：`merge_persons` (L2012-2039) 已正确使用 `lightrag_insert_entity` + `lightrag_merge_entities`。

### Step 2.1: RED — 写测试

**文件**: `tests/test_name_person_kg_update.py`（新建）

```python
"""Tests for name_person KG entity update behavior."""
import pytest
from unittest.mock import MagicMock, patch, call


class TestNamePersonKGEntityUpdate:
    """name_person must update KG entity, not just ainsert text."""

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_name_person_calls_insert_entity(self, mock_get_registry, mock_get_conn):
        """name_person 应调用 lightrag_insert_entity 更新 person:{uuid} 实体。"""
        # Setup mock DB
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("test-uuid", "未命名人物1")
        mock_conn.execute.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_get_conn.return_value = mock_conn

        # Setup mock registry
        mock_registry = MagicMock()
        mock_insert_entity = MagicMock(return_value={"status": "ok"})
        mock_registry.get.side_effect = lambda name: {
            "lightrag-server/lightrag_insert_entity": mock_insert_entity,
        }.get(name)
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import name_person
        result = name_person("test-uuid", "张三")

        # 验证调用了 lightrag_insert_entity
        mock_insert_entity.assert_called_once()
        call_args = mock_insert_entity.call_args
        assert call_args.kwargs["name"] == "person:test-uuid"
        assert call_args.kwargs["entity_type"] == "Person"
        assert "张三" in call_args.kwargs["description"]

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_name_person_does_not_use_lightrag_insert(self, mock_get_registry, mock_get_conn):
        """name_person 不应再调用 lightrag_insert（ainsert 自由提取）。"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("test-uuid", "未命名人物1")
        mock_conn.execute.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_get_conn.return_value = mock_conn

        mock_registry = MagicMock()
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_entity = MagicMock(return_value={"status": "ok"})
        mock_registry.get.side_effect = lambda name: {
            "lightrag-server/lightrag_insert": mock_insert,
            "lightrag-server/lightrag_insert_entity": mock_insert_entity,
        }.get(name)
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import name_person
        result = name_person("test-uuid", "张三")

        # lightrag_insert 不应被调用
        mock_insert.assert_not_called()
```

### Step 2.2: GREEN — 修改 `name_person`

**文件**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

**修改**: 将 L1828-1840 的 KG 更新逻辑替换为使用 `lightrag_insert_entity`。

**新代码** (替换 L1828-1840):
```python
        # KG: 更新 person:{uuid} 实体的描述为真实姓名
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            insert_entity_fn = registry.get("lightrag-server/lightrag_insert_entity")
            if insert_entity_fn:
                entity_name = f"person:{person_id}"
                insert_entity_fn(
                    name=entity_name,
                    entity_type="Person",
                    description=name,
                )
                logger.info(f"[NAME_PERSON] KG entity updated: {entity_name} -> {name}")
            else:
                logger.warning("[NAME_PERSON] lightrag_insert_entity not available in registry")
        except Exception as e:
            logger.warning(f"[NAME_PERSON] LightRAG sync failed: {e}")
```

### Step 2.3: REFACTOR — 清理

- 确认测试通过
- 确认 `merge_persons` 的 KG 逻辑未受影响（它已经正确使用 `lightrag_insert_entity` + `lightrag_merge_entities`）

### Step 2.4: COMMIT

```
fix: name_person 使用 lightrag_insert_entity 更新 KG 实体

- 替换 lightrag_insert (ainsert 自由提取) 为 lightrag_insert_entity
- 使用 person:{uuid} 格式确保实体名一致性
- 与 merge_persons 的 KG 更新方式对齐
- 添加测试验证调用正确的 KG 更新方法
```

---

## Phase 3: 移除照片自动同步

**目标**：`_sync_photos_db` 自动同步照片到 KG 是无效的——照片文件路径不存在，照片入库始终走结构化 `inject_custom_kg`。移除此方法及其调用。

### Step 3.1: RED — 写测试

**文件**: `tests/test_lightrag_sync_no_photo.py`（新建）

```python
"""Tests for LightRAGSync — photos auto-sync removed."""
import pytest
from agent.injector.lightrag_sync import LightRAGSync


class TestLightRAGSyncNoPhotoSync:
    """LightRAGSync must not sync photos — photo ingestion is always structured."""

    def test_run_sync_does_not_call_sync_photos_db(self):
        """run_sync 不应调用 _sync_photos_db。"""
        sync = LightRAGSync()
        # _sync_photos_db 方法不应存在
        assert not hasattr(sync, "_sync_photos_db") or \
               getattr(sync, "_sync_photos_db").__is_disabled__

    def test_run_sync_stats_have_no_photo_keys(self):
        """run_sync 返回的 stats 不应包含 photos_synced。"""
        sync = LightRAGSync()
        # Mock _sync_skills_and_tools to avoid real sync
        sync._sync_skills_and_tools = lambda: (0, 0, set(), set())
        stats = sync.run_sync()
        # photos_synced 和 persons_synced 应为 0 或不存在
        assert stats.get("photos_synced", 0) == 0
        assert stats.get("persons_synced", 0) == 0

    def test_status_file_no_photo_ids(self):
        """状态文件不应再跟踪 photo/person IDs。"""
        sync = LightRAGSync()
        # _save_status 不应接受 synced_photo_ids 参数
        # 或者该参数被忽略
        import inspect
        sig = inspect.signature(sync._save_status)
        # synced_photo_ids 参数应被移除或标记为废弃
        assert "synced_photo_ids" not in sig.parameters or \
               sig.parameters["synced_photo_ids"].annotation == "deprecated"
```

### Step 3.2: GREEN — 修改 `lightrag_sync.py`

**文件**: `agent/injector/lightrag_sync.py`

**修改内容**:

1. **删除 `_sync_photos_db` 方法** (L119-273) — 整个方法移除

2. **修改 `run_sync`** — 移除照片同步调用和照片 ID 跟踪：

**原代码** (L48-78 照片相关部分):
```python
        stats = {
            "photos_synced": 0,
            "persons_synced": 0,
            "documents_synced": 0,
            "skills_synced": 0,
            "tools_synced": 0,
            "errors": [],
        }

        # Load previously synced IDs for delta tracking
        prev_state = self._load_status()
        prev_photo_ids = set(prev_state.get("synced_photo_ids", []))
        prev_person_ids = set(prev_state.get("synced_person_ids", []))
        prev_doc_ids = set(prev_state.get("synced_doc_ids", []))
        prev_skill_ids = set(prev_state.get("synced_skill_ids", []))
        prev_tool_ids = set(prev_state.get("synced_tool_ids", []))
        prev_co_occ_ids = set(prev_state.get("synced_co_occ_ids", []))

        # 1. Sync photos from photos.db
        try:
            p, e, new_photo_ids, new_person_ids, new_co_occ_ids = self._sync_photos_db(
                prev_photo_ids, prev_person_ids, prev_co_occ_ids
            )
            stats["photos_synced"] = p
            stats["persons_synced"] = e
        except Exception as e:
            logger.warning(f"[LightRAGSync] photos.db sync failed: {e}")
            stats["errors"].append(f"photos: {e}")
            new_photo_ids = set()
            new_person_ids = set()
            new_co_occ_ids = set()
```

**新代码**:
```python
        stats = {
            "documents_synced": 0,
            "skills_synced": 0,
            "tools_synced": 0,
            "errors": [],
        }

        # Load previously synced IDs for delta tracking
        prev_state = self._load_status()
        prev_doc_ids = set(prev_state.get("synced_doc_ids", []))
        prev_skill_ids = set(prev_state.get("synced_skill_ids", []))
        prev_tool_ids = set(prev_state.get("synced_tool_ids", []))
```

3. **修改 ID 合并和保存部分** (L100-107):

**原代码**:
```python
        # 4. Merge previous + newly synced IDs and save
        all_photo_ids = prev_photo_ids | new_photo_ids
        all_person_ids = prev_person_ids | new_person_ids
        all_doc_ids = prev_doc_ids | new_doc_ids
        all_skill_ids = prev_skill_ids | new_skill_ids
        all_tool_ids = prev_tool_ids | new_tool_ids
        all_co_occ_ids = prev_co_occ_ids | new_co_occ_ids
        self._save_status(stats, all_photo_ids, all_person_ids, all_doc_ids, all_skill_ids, all_tool_ids, all_co_occ_ids)
```

**新代码**:
```python
        # 4. Merge previous + newly synced IDs and save
        all_doc_ids = prev_doc_ids | new_doc_ids
        all_skill_ids = prev_skill_ids | new_skill_ids
        all_tool_ids = prev_tool_ids | new_tool_ids
        self._save_status(stats, all_doc_ids, all_skill_ids, all_tool_ids)
```

4. **修改日志** (L109-116):

**新代码**:
```python
        logger.info(
            f"[LightRAGSync] Sync complete: "
            f"{stats['documents_synced']} documents, "
            f"{stats['skills_synced']} skills, {stats['tools_synced']} tools | "
            f"tracked IDs: {len(all_doc_ids)} docs, {len(all_skill_ids)} skills, {len(all_tool_ids)} tools"
        )
```

5. **简化 `_save_status`** — 移除 `synced_photo_ids`, `synced_person_ids`, `synced_co_occ_ids` 参数:

**新签名**:
```python
    def _save_status(
        self,
        stats: dict,
        synced_doc_ids: set,
        synced_skill_ids: set | None = None,
        synced_tool_ids: set | None = None,
    ):
```

**新保存内容**:
```python
            status = {
                "last_sync": datetime.now().isoformat(),
                "stats": stats,
                "synced_doc_ids": sorted(synced_doc_ids),
                "synced_skill_ids": sorted(synced_skill_ids or set()),
                "synced_tool_ids": sorted(synced_tool_ids or set()),
            }
```

6. **修改 `_load_status`** — 移除照片/人物 ID 字段的 `setdefault`:

**新代码**:
```python
                data.setdefault("synced_doc_ids", [])
                data.setdefault("synced_skill_ids", [])
                data.setdefault("synced_tool_ids", [])
```

7. **更新模块 docstring** (L1-13):

**新 docstring**:
```python
"""
LightRAG Background Sync

Periodic backfill service that syncs documents from local databases
into the LightRAG brain graph.

Architecture:
- Photos are ingested via structured inject_custom_kg at import time (not synced here)
- Skills sync is handled by SkillSync (agent/injector/sync.py)
- Runs in a background daemon thread with configurable interval
"""
```

### Step 3.3: REFACTOR — 清理

- 确认测试通过
- 确认 `run_sync` 不再引用 `_sync_photos_db`
- 确认 `_save_status` 和 `_load_status` 兼容旧状态文件（旧文件有 `synced_photo_ids` 字段，`_load_status` 读取时忽略即可）

### Step 3.4: COMMIT

```
refactor: 移除 LightRAGSync 照片自动同步

- 删除 _sync_photos_db 方法（照片入库始终走结构化 inject_custom_kg）
- 移除 run_sync 中的照片/人物 ID 跟踪
- 简化 _save_status/_load_status 接口
- 照片入库由 photo-server 在导入时直接调用，无需后台同步
- 添加测试验证照片同步已移除
```

---

## Phase 4: 集成验证

### Step 4.1: 端到端验证

1. **验证提示词注入**：启动 API，触发 LightRAG 提取请求，检查日志中注入的提示词是否包含照片/人物规则
2. **验证 name_person**：调用 `name_person` 命名一个人物，检查 KG 中 `person:{uuid}` 实体的 description 是否更新
3. **验证自动同步移除**：检查 `run_sync` 日志不再包含 "photos_synced"

### Step 4.2: COMMIT

```
test: 集成验证 — 照片/人物实体规则注入 + name_person KG 更新 + 照片同步移除
```

---

## 影响范围

| 文件 | 修改类型 | 风险 |
|------|----------|------|
| `niu_api/internal/brain_region_prompt.py` | 追加提示词文本 | LOW — 纯追加，不改现有逻辑 |
| `mcp-servers/photo-server/.../__init__.py` | 替换 name_person KG 更新逻辑 | MEDIUM — 改变 KG 写入方式 |
| `agent/injector/lightrag_sync.py` | 删除方法 + 简化接口 | LOW — 移除无效代码 |

## 不修改的文件

- `agent/injector/dream_writer.py` — 梦境进化写入走 `lightrag_insert`，提示词注入已覆盖
- `merge_persons` — 已正确使用 `lightrag_insert_entity` + `lightrag_merge_entities`
- `lightrag-server` — 接口不变
