# Skill 来源标识修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 `file_path="skill_sync"` 标记 SkillSync 注入的 skill 实体，修复 ghost 清理逻辑（当前因 `source_id` 被 hash 化而失效），并修正 runner.py 的 skill 注入校验和测试 mock 数据。

**Architecture:** LightRAG 的 `ainsert_custom_kg` 入库时把 `source_id` 当作 chunk 逻辑引用键，通过 `chunk_to_source_map` 翻译为物理 chunk hash（`chunk-xxx`），原始的 `skill://` 标记入库后永久丢失。但 `file_path` 字段不被 hash 化，会被原样持久化到 graph node（`ainsert_custom_kg` → `upsert_nodes_batch` → `graph.add_node` 覆盖写入）。方案：sync.py 入库时设置 `file_path="skill_sync"`，ghost 清理和 runner.py 注入时检查 `file_path` 字段区分来源。`file_path` 的 `<SEP>` 合并只发生在文档提取路径（`ainsert` → `_merge_nodes_then_upsert`），SkillSync 自身重新入库时是覆盖写入，不会合并。

**Tech Stack:** Python 3.11, LightRAG (Fork: github.com/abc-lee/LightRAG), NetworkX

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `agent/injector/sync.py` | 入库时设置 `file_path="skill_sync"`；ghost 清理从 `source_id` 判断改为 `file_path` 判断 |
| `niu_api/internal/lightrag_adapter.py` | `list_entities` 返回 `file_path` 字段 |
| `agent/runner.py` | skill 注入校验改为 `file_path` 检查 + 文件存在性 fallback（两条路径一致） |
| `tests/test_skill_sync_no_write_when_unchanged.py` | 修正 mock 数据，用 `file_path` 代替 `source_id` 判断 |

---

### Task 0: 旧 skill 迁移——删除状态文件强制全量重新入库

- [ ] **Step 1: 停止 API 进程后删除 skill 同步状态文件**

先停止 API 进程（避免运行中的 scan_and_sync 重建状态文件），再删除：

```bash
# 先停止 API 进程
rm -f ~/.niu/skill_sync_state.json
```


删除状态文件后，`scan_and_sync` 的 `known_skills` 为空，所有磁盘上的 `.md` 文件会被当作"新增"处理，触发 `_inject_skill_to_lightrag` 重新入库，新入库的 entity dict 包含 `file_path="skill_sync"`。

- [ ] **Step 2: 验证状态文件已删除**

```bash
ls ~/.niu/skill_sync_state.json 2>&1
```

Expected: `No such file or directory`

- [ ] **Step 3: 不提交**

这是运行时操作，不涉及代码改动。在 Task 1-5 实施完成后、重启 API 前执行。

---

### Task 1: sync.py 入库时设置 `file_path="skill_sync"`

**Files:**
- Modify: `agent/injector/sync.py:570-575`（entities 列表构建）

- [ ] **Step 1: 修改 entities 列表，加 `file_path` 字段**

在 `agent/injector/sync.py` 的 `_inject_skill_to_lightrag` 方法中（约 570 行），entities 列表的每个 entity dict 加 `"file_path": "skill_sync"`：

```python
            entities = [{
                "entity_name": entity_name,
                "entity_type": "Skill",
                "description": full_description,
                "source_id": source_id,
                "file_path": "skill_sync",
            }]
```

- [ ] **Step 2: 验证入库后图节点保留了 file_path**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.injector.sync import SkillSync
# 不实际入库，只验证 entity dict 构建正确
import inspect
source = inspect.getsource(SkillSync._inject_skill_to_lightrag)
assert 'file_path' in source and 'skill_sync' in source, 'file_path not set'
print('OK: file_path=skill_sync in entity dict')
"
```

Expected: `OK: file_path=skill_sync in entity dict`

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/injector/sync.py
git commit -m "feat: sync.py 入库时设置 file_path=skill_sync 标记来源"
```

---

### Task 2: lightrag_adapter.py list_entities 返回 file_path 字段

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:1350-1355`（entities 分支返回 dict）
- Modify: `niu_api/internal/lightrag_adapter.py:1373-1378`（无过滤分支返回 dict）

- [ ] **Step 1: 修改 entities 分支，加 `file_path` 到返回 dict**

在 `niu_api/internal/lightrag_adapter.py` 第 1350-1355 行，按 entity_type 过滤的分支，返回 dict 加 `"file_path"`：

```python
                    for node_id, node_data in snapshot.nodes(data=True):
                        nt = node_data.get("entity_type", "other")
                        if nt.lower() == entity_type.lower():
                            nodes.append({
                                "entity_name": node_id,
                                "entity_type": nt,
                                "description": node_data.get("description", ""),
                                "source_id": node_data.get("source_id", ""),
                                "file_path": node_data.get("file_path", ""),
                            })
                            if len(nodes) >= limit:
                                break
```

- [ ] **Step 2: 修改无过滤分支，加 `file_path` 到返回 dict**

在第 1373-1378 行，无过滤分支同样加 `"file_path"`：

```python
                    for node in kg.nodes:
                        nodes.append({
                            "entity_name": node.id,
                            "entity_type": node.properties.get("entity_type", "other"),
                            "description": node.properties.get("description", ""),
                            "source_id": node.properties.get("source_id", ""),
                            "file_path": node.properties.get("file_path", ""),
                        })
```

- [ ] **Step 3: 验证 list_entities 返回 file_path**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
result = adapter.list_entities(list_type='entities', entity_type='skill', limit=3)
if isinstance(result, dict) and result.get('status') == 'ok':
    for e in result.get('data', [])[:3]:
        fp = e.get('file_path', '(MISSING)')
        print(f'{e.get(\"entity_name\")}: file_path={fp}')
        assert 'file_path' in e, 'file_path missing from list_entities result'
    print('OK: file_path returned')
"
```

Expected: 每个 entity 有 `file_path` 字段（迁移前旧 skill 的值是 `custom_kg`，Task 0 迁移后新入库的将是 `skill_sync`）。

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/lightrag_adapter.py
git commit -m "feat: list_entities 返回 file_path 字段"
```

---

### Task 3: sync.py ghost 清理逻辑改为 file_path 判断

**Files:**
- Modify: `agent/injector/sync.py:382-405`（ghost 清理循环）

- [ ] **Step 1: 替换 ghost 清理判断逻辑**

将 `agent/injector/sync.py` 第 382-405 行的 ghost 清理循环替换为：

```python
                for entity in kg_skills:
                    entity_name = entity.get("entity_name", "")
                    entity_file_path = entity.get("file_path", "")
                    # 只清理 SkillSync 自己注入的 skill 实体（file_path 含 "skill_sync" 段）
                    # 不碰从其他路径（文档入库/手动创建/MCP 工具）入库的 skill 实体
                    # 否则会误删用户知识图谱中合法记录的技能实体
                    # file_path 在 ainsert_custom_kg 路径中是覆盖写入，不会 <SEP> 合并
                    # <SEP> 合并只发生在文档提取路径（_merge_nodes_then_upsert），
                    # 当文档提取创建同名实体时 file_path 可能变为 "skill_sync<SEP>some_doc.md"
                    # 行为变更：旧代码用 source_id 判断（失效，永不为 True），合并实体不会被清理。
                    # 新代码用 any() 判断 file_path，合并形式含 skill_sync 段会被识别为 SkillSync owned。
                    # 如果 skill 文件从磁盘删除，ghost 清理会删除整个实体节点。chunk 数据保留在 chunks_vdb
                    # 不受影响。此场景罕见（skill 名与文档提取实体同名 + skill 被删除），设计上可接受。
                    is_skill_sync_owned = any(
                        seg.strip() == "skill_sync"
                        for seg in entity_file_path.split("<SEP>")
                    )
                    if (entity_name
                            and is_skill_sync_owned
                            and entity_name.lower() not in current_hashes_lower
                            and entity_name not in step3_deleted):
                        # SkillSync 注入但磁盘上不存在 → 幽灵 skill，删除
                        if self._delete_skill_from_lightrag(entity_name):
                            logger.info(f"[SkillSync] Cleaned ghost skill '{entity_name}' from KG (not on disk, file_path=skill_sync)")
                            next_scan.pop(entity_name, None)
                            deleted += 1
                        else:
                            logger.warning(f"[SkillSync] Failed to delete ghost skill '{entity_name}' from KG")
                            next_scan[entity_name] = next_scan.get(entity_name, "")
```

关键变化：
- `entity_source_id = entity.get("source_id", "")` → `entity_file_path = entity.get("file_path", "")`
- `is_skill_sync_owned = any(seg.strip().startswith("skill://") ...)` → `is_skill_sync_owned = any(seg.strip() == "skill_sync" for seg in entity_file_path.split("<SEP>"))`
- 用 `<SEP>` 拆分判断（与旧代码的 `source_id` 判断模式一致），处理文档提取路径的 `file_path` 合并形式 `skill_sync<SEP>some_doc.md`

- [ ] **Step 2: 删除过时注释**

第 385-389 行的旧注释（关于 `source_id` 含 `skill://` 段、合并形式 `<SEP>` 拆分等）已被上面的新注释替代，确认删除。

- [ ] **Step 3: 验证 ghost 清理逻辑**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
import inspect
from agent.injector.sync import SkillSync
source = inspect.getsource(SkillSync.scan_and_sync)
assert 'skill_sync' in source, 'file_path=skill_sync check missing'
assert 'skill://' not in source, 'old skill:// check still present in scan_and_sync'
print('OK: ghost cleanup uses file_path=skill_sync')
"
```

Expected: `OK: ghost cleanup uses file_path=skill_sync`

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/injector/sync.py
git commit -m "fix: ghost 清理从 source_id 判断改为 file_path=skill_sync"
```

---

### Task 4: runner.py skill 注入校验改为 file_path 检查

**Files:**
- Modify: `agent/runner.py:2065-2071`（向量检索路径）
- Modify: `agent/runner.py:2125-2129`（图遍历路径）

- [ ] **Step 1: 修改向量检索路径的 skill 校验**

将 `agent/runner.py` 第 2065-2071 行替换为：

```python
                # 真实 skill 检查：file_path 含 "skill_sync" 段（由 sync.py 标记）
                # 非 SkillSync 来源的 skill（文档入库 LLM 提取）降级为 knowledge
                # fallback: file_path 不含 skill_sync 时，检查磁盘文件是否存在（兼容旧 skill）
                inject_category = category
                if category == "skill":
                    entity_file_path = entity.get("file_path", "")
                    is_real_skill = any(
                        seg.strip() == "skill_sync"
                        for seg in entity_file_path.split("<SEP>")
                    )
                    if not is_real_skill:
                        skill_path = Path.home() / ".niu" / "skills" / f"{name}.md"
                        inject_category = "knowledge" if not skill_path.exists() else "skill"
```

说明：`convert_to_user_format` 给 `file_path` 默认值 `unknown_source`，所以 `entity.get("file_path", "")` 永远非空。当 `file_path` 不含 `skill_sync` 段时（旧 skill 的 `custom_kg`、文档提取的 `unknown_source` 等），统一用文件存在性检查作为 fallback。这保证了旧 skill（`file_path=custom_kg` 但磁盘文件存在）不会被误降级。

- [ ] **Step 2: 修改图遍历路径的 skill 校验**

将第 2125-2129 行替换为（与向量检索路径一致）：

```python
            # 真实 skill 检查：file_path 含 "skill_sync" 段（与向量检索路径一致）
            if category == "skill":
                entity_file_path = node_data.get("file_path", "")
                is_real_skill = any(
                    seg.strip() == "skill_sync"
                    for seg in entity_file_path.split("<SEP>")
                )
                if not is_real_skill:
                    skill_path = Path.home() / ".niu" / "skills" / f"{entity_name}.md"
                    category = "knowledge" if not skill_path.exists() else "skill"
```

- [ ] **Step 3: 验证 skill 注入校验**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
import inspect
from agent.runner import NiuRunner
source = inspect.getsource(NiuRunner._inject_dynamic_resources)
assert 'skill_sync' in source, 'file_path=skill_sync check missing in inject'
assert 'Path.home' in source, 'file existence fallback missing'
print('OK: skill check uses file_path=skill_sync with file existence fallback')
"
```

Expected: `OK: skill check uses file_path=skill_sync with file existence fallback`

- [ ] **Step 4: 跑相关测试**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_on_before_llm_method.py tests/test_skill_inject_integration.py -x -q
```

Expected: 5 passed, 2 skipped

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "fix: runner.py skill 校验改为 file_path=skill_sync + 文件存在性 fallback"
```

---

### Task 5: 修正测试 mock 数据

**Files:**
- Modify: `tests/test_skill_sync_no_write_when_unchanged.py:181-239`

- [ ] **Step 1: 修改 ghost cleanup failure 测试的 mock 数据**

在 `tests/test_skill_sync_no_write_when_unchanged.py` 第 181-186 行，将 mock 数据从 `source_id="skill://ghost-skill"` 改为 `file_path="skill_sync"`，并补全 `entity_type` 和 `description` 字段：

```python
        # KG 里有 1 个 ghost skill（磁盘上不存在）
        # file_path 含 "skill_sync" 段才会被 SkillSync ghost 清理识别为自身注入实体
        mock_adapter.return_value.list_entities.return_value = {
            "status": "ok",
            "data": [{"entity_name": "ghost-skill", "entity_type": "Skill", "description": "test ghost", "file_path": "skill_sync", "source_id": "chunk-abc123"}]
        }
```

- [ ] **Step 2: 修改 external entity not cleaned 测试的 mock 数据**

在第 216-227 行，将 mock 数据从 `source_id` 判断改为 `file_path` 判断，补全 `entity_type` 和 `description`，并增加合并形式测试 case：

```python
        # KG 里有 4 个外部入库的 skill 实体（磁盘上无同名 .md，但 file_path 不含 skill_sync 段）
        # 1. file_path 形式（文档解析入库，file_path=custom_kg）
        # 2. 手动创建形式（file_path=manual_creation）
        # 3. file_path 为空（旧版本入库，无标记）
        # 4. 合并形式（file_path=custom_kg<SEP>some_doc.md，不含 skill_sync 段）
        mock_adapter.return_value.list_entities.return_value = {
            "status": "ok",
            "data": [
                {"entity_name": "external-doc-skill", "entity_type": "Skill", "description": "external", "file_path": "custom_kg", "source_id": "chunk-def456"},
                {"entity_name": "manual-skill", "entity_type": "Skill", "description": "manual", "file_path": "manual_creation", "source_id": "chunk-ghi789"},
                {"entity_name": "legacy-skill", "entity_type": "Skill", "description": "legacy", "file_path": "", "source_id": "chunk-jkl012"},
                {"entity_name": "merged-external-skill", "entity_type": "Skill", "description": "merged", "file_path": "custom_kg<SEP>some_doc.md", "source_id": "chunk-mno345"},
            ]
        }
```

- [ ] **Step 3: 更新测试注释和断言**

第 201-207 行的 docstring 更新：

```python
def test_scan_and_sync_external_entity_not_cleaned(fake_skill_sync):
    """核心修复目标：外部入库的 skill 实体（file_path 不含 skill_sync 段）不应被 ghost 清理误删

    覆盖 SkillSync ghost 清理的 file_path 守卫逻辑（sync.py ghost 清理段）：
    is_skill_sync_owned = any(seg.strip() == "skill_sync" for seg in entity_file_path.split("<SEP>"))
    只有 file_path 含 skill_sync 段的实体才会被当 ghost 候选清理。
    外部入库（文档解析 / 手动创建 / MCP 工具）的 skill 实体 file_path 不含 skill_sync 段，
    即使磁盘上无同名 .md，也不应被 SkillSync 误删。
    """
```

第 231-232 行的断言注释更新：

```python
    # 关键断言 1：外部实体不被当 ghost 删除，_delete_skill_from_lightrag 一次都没被调
    mock_delete.assert_not_called()
```

- [ ] **Step 4: 跑测试验证**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_skill_sync_no_write_when_unchanged.py -x -v
```

Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_skill_sync_no_write_when_unchanged.py
git commit -m "fix: 测试 mock 数据从 source_id=skill:// 改为 file_path=skill_sync"
```

---

### Task 6: 全量验证

- [ ] **Step 1: 跑所有修改相关的测试**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_skill_sync_no_write_when_unchanged.py tests/test_on_before_llm_method.py tests/test_skill_inject_integration.py -x -q
```

Expected: 全部 PASS

- [ ] **Step 2: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m py_compile agent/injector/sync.py agent/runner.py niu_api/internal/lightrag_adapter.py && echo "SYNTAX OK"
```

Expected: `SYNTAX OK`

- [ ] **Step 3: 执行旧 skill 迁移（Task 0）**

先停止 API 进程（避免运行中的 scan_and_sync 重建状态文件），再删除：

```bash
# 先停止 API 进程
rm -f ~/.niu/skill_sync_state.json
```

删除状态文件后，下次 `scan_and_sync` 会将所有磁盘 skill 当作新增重新入库，新入库的 `file_path` 为 `skill_sync`。

- [ ] **Step 4: 验证迁移后 skill 的 file_path**

重启 API 后，等待 `scan_and_sync` 执行（约 60 秒），然后检查：

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
result = adapter.list_entities(list_type='entities', entity_type='skill', limit=5)
if isinstance(result, dict) and result.get('status') == 'ok':
    for e in result.get('data', [])[:5]:
        name = e.get('entity_name', '?')
        fp = e.get('file_path', '(MISSING)')
        print(f'{name}: file_path={fp}')
"
```

Expected: 真实 skill 的 `file_path` 为 `skill_sync`，虚假 skill（如"测试验证方法论"）的 `file_path` 为其他值。

---

## Self-Review

### Spec coverage
- ✅ 旧 skill 迁移 — Task 0 + Task 6 Step 3
- ✅ sync.py 入库设置 file_path=skill_sync — Task 1
- ✅ list_entities 返回 file_path — Task 2
- ✅ sync.py ghost 清理改为 file_path 判断（含 <SEP> 拆分） — Task 3
- ✅ runner.py skill 注入校验改为 file_path + 文件存在性 fallback（两条路径一致） — Task 4
- ✅ 测试 mock 数据修正（含 entity_type/description 补全 + 合并形式 case） — Task 5
- ✅ 全量验证（含迁移执行） — Task 6

### Placeholder scan
- 无 TBD/TODO
- 每步都有完整代码
- 每步都有验证命令和 expected

### Type consistency
- `file_path="skill_sync"` 在 sync.py 入库、ghost 清理、runner.py 注入校验中一致
- `is_skill_sync_owned = any(seg.strip() == "skill_sync" for seg in entity_file_path.split("<SEP>"))` 在 ghost 清理（Task 3）和 runner.py（Task 4）两条路径中一致
- `list_entities` 返回字段名 `file_path` 与图节点 `node_data.get("file_path")` 一致
- runner.py 两条路径（向量检索 + 图遍历）校验逻辑一致

### 旧 skill 迁移
- Task 0 删除 `skill_sync_state.json`，强制 `scan_and_sync` 全量重新入库
- Task 4 的 fallback 逻辑用文件存在性检查处理迁移前的过渡期（旧 skill 的 `file_path` 是 `custom_kg` 但磁盘文件存在 → 仍注入 skill 段）
- Task 6 Step 3 执行迁移，Step 4 验证迁移结果

### 遗留问题
- **scripts/ 下的测试脚本**：`test_kg_real_photo_ingest.py`、`test_kg_3agent_e2e_v2.py`、`test_kg_merge_tdd.py` 中有断言入库后 source_id 含 `photo:` 前缀的代码。这些是 photo 入库路径的测试，与 skill 无直接关系，但同样存在 source_id 误用问题。由于这些是 scripts/ 下的手动测试脚本（不在 pytest 自动收集范围），暂不修改，后续统一处理。
