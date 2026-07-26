# SkillSync 无变化不写盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `SkillSync.scan_and_sync` 末尾无条件 `_save_state()` 写盘的 bug——每分钟重写 `~/.niu/skill_sync_state.json` 即使 skills 和 notes 都无变化。改为"snapshot 对比"模式：入口对 `_last_scan` + `_last_notes_scan` 做深拷贝快照，出口对比是否变化，无变化跳过写盘。

**Architecture:** 4 处改动：(1) `scan_and_sync` 入口快照 + 出口对比，无变化跳过 `_save_state`；(2) 删除 `_scan_notes` 末尾的 `_save_state`（L711-713），统一到 L397 出口处理，避免双重写盘；(3) L393-395 watchdog 合并逻辑同时覆盖已有 key 的 hash 修改（避免 watchdog 在 scan 进行中改的 hash 被 next_scan 覆盖回旧值）；(4) 把 `self._last_scan = next_scan` 移到 `_save_state()` 之后，确保"写盘成功才更新内存"（避免 `_save_state` 失败时内存与磁盘不一致）。保持 `SkillFileHandler._execute`（L124/L129）的 `_save_state` 不变——watchdog 是文件变化触发的，本身就是"有变化"，写盘正确。

**Tech Stack:** Python 3.11, threading.Lock, pytest, MagicMock

---

## 关键背景知识

### 当前写盘机制（`agent/injector/sync.py`）

#### 三个 `_save_state` 调用点

1. **L124 `SkillFileHandler._execute` (sync 路径)**——watchdog 检测到 .md 文件创建/修改后，防抖 1 秒触发 `_sync_skill` 注入 KG，成功后更新 `_last_scan[name] = content_hash` 并写盘。**正确写盘**：文件变化触发的，本身就是"有变化"。

2. **L129 `SkillFileHandler._execute` (delete 路径)**——watchdog 检测到 .md 删除，调 `_delete_skill_from_lightrag` 成功后 `_last_scan.pop(name)` 并写盘。**正确写盘**。

3. **L397 `scan_and_sync` 末尾**——后台定时扫描（60 秒一次）跑完所有 skills + notes 同步逻辑后，**无条件** `_save_state()` 写盘。**这是 bug**：即使 skills 和 notes 都无变化，也重写一次 648 字节的状态文件。

4. **L713 `_scan_notes` 末尾**——notes 部分有自己的判断 `if added > 0 or updated > 0 or deleted_ids: _save_state()`，跟 L397 重复写盘。修复时删除，统一到 L397 出口。

#### 状态文件结构（`~/.niu/skill_sync_state.json`）

```json
{
  "brain-region-management": "832d84e6...",
  "knowledge-graph-query": "c832e1ef...",
  ...7 个 skill hash...
  "_notes": {}
}
```

`_save_state` (L250-261) 实现：`data = {**self._last_scan, "_notes": self._last_notes_scan}` + `json.dumps(data, ensure_ascii=False, indent=2)` + `write_text`。每次都重新序列化整个 dict 重写整个文件。

#### 后台线程循环（`start()` L848-870）

每 `scan_interval` 秒（默认 60）调 `scan_and_sync()`。一年累计 60*24*365 = 525600 次无意义写盘。

### 风险点（审查 Agent 指出）

#### C1: watchdog 并发新增条目

`scan_and_sync` 期间 watchdog 可能触发 `_execute` 往 `_last_scan` 塞新条目（L123）。L391-396 把"不在 known_skills 快照中的"条目合并到 `next_scan`。若只在 `added/updated/deleted > 0` 时写盘（方案 A），watchdog 新增条目未同步成功时 `added=0`，跳过写盘 → 进程重启后丢状态，下次扫描把已注入 KG 的 skill 当"新增"重注。

**方案 C 的 snapshot 对比能覆盖**：入口快照 `_last_scan`，出口对比 `_last_scan`（已被 L396 改成 `next_scan`）跟快照不同 → 写盘。

#### C2: KG ghost cleanup 失败

L370-378 KG ghost skill 删除失败时塞空字符串：`next_scan[entity_name] = next_scan.get(entity_name, "")`。此时 `added/updated/deleted` 可能全 0（ghost 不在 `known_skills` 不走 L347 的 deleted 分支；`deleted += 1` 只在成功分支 L375）。方案 A 跳过写盘 → 空值不落盘 → 下次扫描还把 ghost 当新发现 → 无限重试。

**方案 C 的 snapshot 对比能覆盖**：`next_scan[entity_name] = ""` 让 `next_scan` 跟入口快照不同 → 写盘，空值落盘，下次扫描读到空值不会再当 ghost 重新发现（除非 KG 里还有这个实体，那时再删一次）。

### 不动 `_save_state` 本身

方案 C 只在 L397 前加"是否变化的判断"，不改 `_save_state` 实现。状态文件格式（`{**skills, "_notes": {...}}`）保持不变，向后兼容。`_load_state` (L216-248) 和 `_load_notes_state` 不动。

### 不影响 lightrag_manager 清理逻辑

`_clear_sync_state_if_storage_empty` (lightrag_manager.py L840-876) 在 KG 存储为空时删除 state 文件让 SkillSync 重载。这个逻辑跟"频繁写盘"无关——它依赖的是"state 文件存在"，不依赖"L397 每次写盘"。方案 C 不影响。

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `agent/injector/sync.py` | (1) `scan_and_sync` 入口快照 + 出口对比<br>(2) 删除 `_scan_notes` 末尾的 `_save_state`<>(3) L393-395 watchdog 合并覆盖已有 key 的 hash 修改<br>(4) `self._last_scan = next_scan` 移到 `_save_state()` 之后 | 修改逻辑 |
| `tests/test_skill_sync_no_write_when_unchanged.py` | 新增 3 个测试覆盖无变化/watchdog 并发/ghost 失败 | 新增测试 |

---

## Task 1: 入口快照 + 出口对比，无变化跳过写盘

**Files:**
- Modify: `agent/injector/sync.py:263-399`（`scan_and_sync` 函数）
- Test: `tests/test_skill_sync_no_write_when_unchanged.py`

- [ ] **Step 1: 写失败测试 1 — 无变化不写盘**

创建 `tests/test_skill_sync_no_write_when_unchanged.py`：

```python
"""SkillSync 无变化不写盘测试。

验证 scan_and_sync 在 skills + notes 都无变化时，不调用 _save_state，
避免每分钟无意义重写 ~/.niu/skill_sync_state.json。
"""
import json
from unittest import mock

import pytest


@pytest.fixture
def fake_skill_sync(tmp_path):
    """构造一个轻量 SkillSync 实例，绕过 LightRAG 真实初始化。

    skills 目录有 1 个 skill 文件，state 文件已记录其 hash。
    """
    # 准备 skills 目录
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test-skill.md"
    skill_file.write_text("# Test Skill\n", encoding="utf-8")

    # 计算 hash
    import hashlib
    content_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()

    # 准备 state 文件（已记录 hash，模拟"已同步"状态）
    # 注意：SkillSync._state_file = Path.home() / ".niu" / "skill_sync_state.json"
    # Patch Path.home() 返回 tmp_path 后，_state_file = tmp_path / ".niu" / "skill_sync_state.json"
    # 所以 fixture 必须写到 tmp_path / ".niu" / "skill_sync_state.json"，先建 .niu 子目录
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    state_file = niu_dir / "skill_sync_state.json"
    state_file.write_text(
        json.dumps({"test-skill": content_hash, "_notes": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Patch Path.home() 让 SkillSync 找到 tmp_path 下的 state_file
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)

    return sync, content_hash


def test_scan_and_sync_no_write_when_unchanged(fake_skill_sync):
    """skills 和 notes 都无变化时，scan_and_sync 不调用 _save_state"""
    sync, _ = fake_skill_sync

    # Patch get_lightrag 返回非 None，绕过 LightRAG 不可用的提前返回
    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True) as mock_sync, \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        # list_entities 返回空 list（没有 KG ghost）
        MockAdapter.return_value.list_entities.return_value = {
            "status": "ok", "data": []
        }

        added, updated, deleted = sync.scan_and_sync()

    # 无变化：added=0, updated=0, deleted=0
    assert added == 0 and updated == 0 and deleted == 0
    # _sync_skill 不应被调用（skill 已知且 hash 未变）
    mock_sync.assert_not_called()
    # 关键断言：_save_state 不应被调用
    assert not mock_save.called, \
        "skills 和 notes 都无变化时不应调 _save_state，但被调用了"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd <repo_root>
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py::test_scan_and_sync_no_write_when_unchanged -v 2>&1 | tail -15
```

预期：FAIL，断言 `_save_state` 被调用了（当前代码 L397 无条件调用）。

- [ ] **Step 3: 修改 `scan_and_sync` 加入口快照 + 出口对比**

读 `agent/injector/sync.py:263-399`。用 Edit 工具替换：

**old_string**（实际从代码读，下面是预期形式，必须跟实际逐字符一致）：

```python
    def scan_and_sync(self) -> tuple[int, int, int]:
        """
        扫描目录，同步变化的 skills 到 LightRAG 知识图谱

        变化检测基于文件内容哈希（SHA256），状态持久化到磁盘文件，
        进程重启后不会误判已有 skill 为"新增"。

        仅在注入/删除成功时才更新状态文件中的 hash，
        失败的 skill 保留旧 hash（或新增失败的不写入），
        下次扫描时会重试。

        Returns:
            (added, updated, deleted) 计数；若 LightRAG 不可用返回 (-1, -1, -1)
            表示"未真正扫描"，调用方据此不触发 _first_scan_complete。
        """
        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return 0, 0, 0
```

**new_string**：

```python
    def scan_and_sync(self) -> tuple[int, int, int]:
        """
        扫描目录，同步变化的 skills 到 LightRAG 知识图谱

        变化检测基于文件内容哈希（SHA256），状态持久化到磁盘文件，
        进程重启后不会误判已有 skill 为"新增"。

        仅在注入/删除成功时才更新状态文件中的 hash，
        失败的 skill 保留旧 hash（或新增失败的不写入），
        下次扫描时会重试。

        无变化不写盘：入口对 _last_scan + _last_notes_scan 做深拷贝快照，
        出口对比是否变化，无变化跳过 _save_state（避免每分钟无意义重写）。

        Returns:
            (added, updated, deleted) 计数；若 LightRAG 不可用返回 (-1, -1, -1)
            表示"未真正扫描"，调用方据此不触发 _first_scan_complete。
        """
        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return 0, 0, 0

        # 入口快照：scan 之前的 _last_scan + _last_notes_scan 状态
        # 位置：在 skills_dir 检查之后、LightRAG 可用性检查（L286-293）之前
        # 出口对比是否变化，无变化跳过 _save_state
        # 注意：LightRAG 不可用提前 return -1 时不做快照也无副作用（不写盘就行）
        with self._lock:
            skills_snapshot_before: dict[str, str] = dict(self._last_scan)
            notes_snapshot_before: dict[str, str] = dict(self._last_notes_scan)
```

- [ ] **Step 4: 修改 L397 末尾的 `_save_state` 调用，加出口对比 + watchdog 合并覆盖 + 写盘前更新内存**

读 `agent/injector/sync.py:390-398`。用 Edit 工具替换：

**old_string**：

```python
        # 4. 将 next_scan 写入状态文件（合并 watchdog 并发修改）
        with self._lock:
            # 保留 scan 期间 watchdog 新增/修改的条目（不在 known_keys 快照中的）
            for name, hash_val in self._last_scan.items():
                if name not in known_skills:
                    next_scan[name] = hash_val
            self._last_scan = next_scan
        self._save_state()

        return added, updated, deleted
```

**new_string**：

```python
        # 4. 合并 watchdog 并发修改 + 出口对比 + 写盘成功才更新内存
        with self._lock:
            # 合并 scan 期间 watchdog 新增/修改的条目
            # 1. 不在 known_skills 快照中的 → watchdog 新增的，合并进 next_scan
            # 2. 在 known_skills 中但 hash 跟 _last_scan 不同 → watchdog 改了已有 key 的 hash
            #    （覆盖 next_scan 里 scan 算出的旧 hash，避免被覆盖回旧值）
            for name, hash_val in self._last_scan.items():
                if name not in known_skills or next_scan.get(name) != hash_val:
                    next_scan[name] = hash_val

            # 出口对比：_last_scan 或 _last_notes_scan 跟入口快照不同才写盘
            # 覆盖以下场景（added/updated/deleted 可能全 0 但状态确实变了）：
            # - watchdog 并发往 _last_scan 塞新条目或改已有 key 的 hash
            # - KG ghost cleanup 失败时往 next_scan 塞空字符串（L378）
            # - _scan_notes 修改了 _last_notes_scan
            skills_changed = next_scan != skills_snapshot_before
            notes_changed = self._last_notes_scan != notes_snapshot_before

        if skills_changed or notes_changed:
            # 写盘成功后才更新内存，避免 _save_state 抛 OSError 时
            # 内存已改但磁盘未改，下次 scan 入口快照读到新内存状态，
            # 出口对比"无变化"不写盘，磁盘永远旧状态
            self._save_state()
            with self._lock:
                self._last_scan = next_scan

        return added, updated, deleted
```

**关键改动说明**：
1. `skills_snapshot_before` / `notes_snapshot_before` 在函数入口（L295 之前，紧跟 `if not self.skills_dir.exists()` 后、LightRAG 可用性检查前）做深拷贝
2. L393 合并 watchdog 修改时**同时覆盖已有 key 的 hash**（`if name not in known_skills or next_scan.get(name) != hash_val`）——避免 watchdog 在 scan 进行中改的 hash 被 `self._last_scan = next_scan` 覆盖回旧值
3. L397 改为对比 `next_scan != skills_snapshot_before` 或 `self._last_notes_scan != notes_snapshot_before`，任一变化才写盘
4. **`self._last_scan = next_scan` 移到 `_save_state()` 之后**，确保"写盘成功才更新内存"。`_save_state` 抛 OSError 时内存未改，下次 scan 重新对比 → 重新写盘
5. **dict 深拷贝用 `dict(self._last_scan)`**——浅拷贝足够，因为值是 str 不可变；键值对增删改都会让 dict 不等
6. **对比在 `with self._lock` 块内做，但 `_save_state` 必须在锁外调**——`_lock` 是普通 `threading.Lock()` 不可重入（L157），`_save_state` L254 自己 `with self._lock` 会嵌套死锁

- [ ] **Step 5: 跑测试确认通过**

```bash
cd <repo_root>
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py::test_scan_and_sync_no_write_when_unchanged -v 2>&1 | tail -15
```

预期：PASS

- [ ] **Step 6: 跑语法检查**

```bash
cd <repo_root>
python -c "import ast; ast.parse(open('agent/injector/sync.py').read()); print('SYNTAX_OK')"
```

预期：`SYNTAX_OK`

- [ ] **Step 7: 跑回归测试**

```bash
cd <repo_root>
python -m pytest tests/test_notes_json.py tests/test_lightrag_unified.py tests/test_lightrag_skillsync_vdb_preservation.py tests/test_lightrag_repair_e2e_skillsync.py -v 2>&1 | tail -30
```

预期：所有现有 SkillSync 相关测试通过（如果失败，记录测试名+断言信息+实际 vs 期望）

- [ ] **Step 8: Commit**

```bash
cd <repo_root>
git add tests/test_skill_sync_no_write_when_unchanged.py agent/injector/sync.py
git commit -m "fix(skill_sync): 无变化不写盘，避免每分钟无意义重写 state 文件

scan_and_sync 末尾原本无条件调 _save_state，导致后台定时扫描（默认 60s）
即使 skills 和 notes 都无变化也重写 ~/.niu/skill_sync_state.json。

改为 snapshot 对比模式：
- 入口对 _last_scan + _last_notes_scan 做深拷贝快照
- 出口对比两个 dict 是否变化，无变化跳过 _save_state
- 覆盖两个边界场景：
  1. watchdog 并发往 _last_scan 塞新条目（L393 合并到 next_scan）
  2. KG ghost cleanup 失败时往 next_scan 塞空字符串（L378）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 删除 `_scan_notes` 末尾的 `_save_state`，避免双重写盘

**Files:**
- Modify: `agent/injector/sync.py:711-713`（`_scan_notes` 函数末尾）
- Test: `tests/test_skill_sync_no_write_when_unchanged.py`

- [ ] **Step 1: 写失败测试 2 — notes 变化时只写一次盘**

在 `tests/test_skill_sync_no_write_when_unchanged.py` 末尾追加：

```python
def test_scan_and_sync_notes_changed_writes_once(fake_skill_sync, tmp_path, monkeypatch):
    """notes 有变化时，scan_and_sync 只调用一次 _save_state（不双重写盘）"""
    sync, _ = fake_skill_sync

    # _scan_notes 读 WORKSPACE_PATH/notes/notes.json（sync.py L615-618）
    # 必须设 WORKSPACE_PATH 环境变量 + 写到对应路径，否则 _scan_notes 返回 (0, 0)
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes_file = notes_dir / "notes.json"
    notes_file.write_text(
        json.dumps([{"id": "note1", "content": "test content", "tags": []}], ensure_ascii=False),
        encoding="utf-8",
    )

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject_note, \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        MockAdapter.return_value.list_entities.return_value = {"status": "ok", "data": []}
        MockAdapter.return_value.delete_document.return_value = {"status": "ok"}

        added, updated, deleted = sync.scan_and_sync()

    # notes 新增了 1 条
    assert added >= 1, f"应至少有 1 条 notes 新增，实际 added={added}"
    # 关键断言：_save_state 只调用一次（L713 删除后不再双重写）
    assert mock_save.call_count == 1, \
        f"notes 变化时应只写一次盘，实际写了 {mock_save.call_count} 次"
    # _inject_note_to_lightrag 被调用
    mock_inject_note.assert_called_once()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd <repo_root>
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py::test_scan_and_sync_notes_changed_writes_once -v 2>&1 | tail -15
```

预期：FAIL，断言 `_save_state.call_count == 1` 失败，实际 2 次（L713 + L397 都写）。

- [ ] **Step 3: 删除 `_scan_notes` 末尾的 `_save_state`**

读 `agent/injector/sync.py:708-715`。用 Edit 工具替换：

**old_string**：

```python
        # 持久化 notes 状态
        if added > 0 or updated > 0 or deleted_ids:
            self._save_state()

        return added, updated
```

**new_string**：

```python
        # notes 状态持久化统一在 scan_and_sync 末尾出口对比时处理
        # （对比 _last_notes_scan 跟入口快照，无变化不写盘）
        return added, updated
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd <repo_root>
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py::test_scan_and_sync_notes_changed_writes_once -v 2>&1 | tail -15
```

预期：PASS

- [ ] **Step 5: 跑回归测试**

```bash
cd <repo_root>
python -m pytest tests/test_notes_json.py tests/test_lightrag_unified.py tests/test_lightrag_skillsync_vdb_preservation.py tests/test_lightrag_repair_e2e_skillsync.py tests/test_skill_sync_no_write_when_unchanged.py -v 2>&1 | tail -30
```

预期：所有测试通过

- [ ] **Step 6: Commit**

```bash
cd <repo_root>
git add tests/test_skill_sync_no_write_when_unchanged.py agent/injector/sync.py
git commit -m "fix(skill_sync): 删除 _scan_notes 末尾的 _save_state，避免双重写盘

_scan_notes 原本末尾有自己的判断 'if added/updated/deleted: _save_state()'，
但 scan_and_sync 末尾的出口对比也会写一次。notes 变化时双重写盘。

删除 _scan_notes 的 _save_state，统一在 scan_and_sync 出口对比处理。
notes 内存状态在 _scan_notes 期间已被修改（L654/660/667），
出口对比 _last_notes_scan 跟入口快照不同 → 写盘，不丢状态。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 补 watchdog 并发 + ghost 失败写盘测试

**Files:**
- Test: `tests/test_skill_sync_no_write_when_unchanged.py`

- [ ] **Step 1: 写测试 3 — watchdog 并发新增条目时写盘**

在 `tests/test_skill_sync_no_write_when_unchanged.py` 末尾追加。**注意**：这个测试需要 **state 为空**的 fixture（让 scan 走"新增"路径调 `_sync_skill`，side_effect 才能触发），不能用默认的 `fake_skill_sync` fixture（state 含 `test-skill` hash，scan 走 unchanged 路径不调 `_sync_skill`）。所以测试内自己构造空 state fixture。

```python
def test_scan_and_sync_watchdog_concurrent_write(tmp_path):
    """watchdog 在 scan 期间往 _last_scan 塞新条目时，scan_and_sync 出口写盘

    覆盖审查 Agent 指出的 C1 风险：watchdog 并发新增条目未同步成功时
    added=0，但 _last_scan 已被 watchdog 修改，出口对比应捕获并写盘。

    关键：fixture state 必须为空，让 scan 走"新增"路径调 _sync_skill，
    side_effect 才能真正触发（如果 state 含 test-skill hash，scan 走
    unchanged 路径不调 _sync_skill，side_effect 永不触发）。
    """

    # 准备 skills 目录（state 为空 → scan 走新增路径调 _sync_skill）
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test-skill.md"
    skill_file.write_text("# Test Skill\n", encoding="utf-8")

    # state 文件：空（只含 _notes 键）
    # SkillSync._state_file = Path.home() / ".niu" / "skill_sync_state.json"
    # 必须写到 tmp_path / ".niu" / "skill_sync_state.json"，先建 .niu 子目录
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    state_file = niu_dir / "skill_sync_state.json"
    state_file.write_text(json.dumps({"_notes": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)

    # 模拟 watchdog 在 scan 期间往 _last_scan 塞新条目
    # （实际场景：watchdog 触发 _execute 往 _last_scan[name] 塞 hash）
    def fake_sync_skill(name, skill_file):
        with sync._lock:
            sync._last_scan["watchdog-concurrent-skill"] = "fake_watchdog_hash"
        return True

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", side_effect=fake_sync_skill), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        MockAdapter.return_value.list_entities.return_value = {"status": "ok", "data": []}

        added, updated, deleted = sync.scan_and_sync()

    # 关键断言 1：scan 走新增路径调了 _sync_skill（fixture state 空导致 test-skill 是新增）
    # fake_sync_skill 往 _last_scan 塞了 watchdog-concurrent-skill
    assert "watchdog-concurrent-skill" in sync._last_scan, \
        "watchdog 并发塞的条目应保留在 _last_scan"
    # 关键断言 2：出口对比捕获 _last_scan 变化（多了 watchdog-concurrent-skill），写盘
    assert mock_save.call_count >= 1, \
        f"watchdog 并发修改 _last_scan 时应写盘，实际 _save_state 调用 {mock_save.call_count} 次"
```

- [ ] **Step 2: 写测试 4 — KG ghost cleanup 失败时写盘**

继续追加。这个测试用默认 `fake_skill_sync` fixture（state 含 `test-skill` hash，scan 走 unchanged 路径，只触发 L372 ghost cleanup 路径）：

```python
def test_scan_and_sync_ghost_cleanup_failure_writes(fake_skill_sync):
    """KG ghost skill 删除失败时往 next_scan 塞空字符串，scan_and_sync 写盘

    覆盖审查 Agent 指出的 C2 风险：ghost 删除失败时 next_scan[entity_name]=''，
    added/updated/deleted 可能全 0，但 next_scan 跟入口快照不同，出口对比应写盘。
    否则下次扫描还会再扫到 ghost 无限重试。

    fixture 用 fake_skill_sync（state 含 test-skill hash），scan 走 unchanged
    路径不调 _sync_skill/_delete_skill_from_lightrag，只触发 L372 ghost 路径。
    """
    sync, _ = fake_skill_sync

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=False), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        # KG 里有 1 个 ghost skill（磁盘上不存在）
        MockAdapter.return_value.list_entities.return_value = {
            "status": "ok",
            "data": [{"entity_name": "ghost-skill"}]
        }

        added, updated, deleted = sync.scan_and_sync()

    # 关键断言 1：ghost 删除失败时写盘（出口对比捕获 next_scan 塞了空值）
    assert mock_save.call_count >= 1, \
        f"ghost cleanup 失败塞空值时应写盘，实际 _save_state 调用 {mock_save.call_count} 次"
    # 关键断言 2：_last_scan 含 ghost-skill 空值条目
    assert "ghost-skill" in sync._last_scan, \
        "ghost 删除失败应塞空值到 _last_scan，下次扫描不再当新发现"
    assert sync._last_scan["ghost-skill"] == "", \
        f"ghost-skill 应是空字符串，实际 {sync._last_scan['ghost-skill']!r}"
```

- [ ] **Step 3: 跑测试确认通过**

```bash
cd <repo_root>
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py -v 2>&1 | tail -20
```

预期：4 个测试全部 PASS（Task 1 写的 1 个 + Task 2 写的 1 个 + Task 3 写的 2 个）

- [ ] **Step 4: 跑全量 SkillSync 相关回归测试**

```bash
cd <repo_root>
python -m pytest tests/test_notes_json.py tests/test_lightrag_unified.py tests/test_lightrag_skillsync_vdb_preservation.py tests/test_lightrag_repair_e2e_skillsync.py tests/test_skill_sync_no_write_when_unchanged.py -v 2>&1 | tail -30
```

预期：所有测试通过

- [ ] **Step 5: Commit**

```bash
cd <repo_root>
git add tests/test_skill_sync_no_write_when_unchanged.py
git commit -m "test(skill_sync): 补 watchdog 并发和 ghost 失败写盘测试

覆盖审查 Agent 指出的两个 Critical 边界场景：
1. watchdog 在 scan 期间往 _last_scan 塞新条目时写盘（C1）
2. KG ghost cleanup 失败时塞空字符串到 next_scan 时写盘（C2）

验证 snapshot 对比模式能正确捕获 added=0 但状态实际变化的场景。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 真实程序验证 + 文档更新

**Files:**
- Modify: `docs/manual-vector-store.md`（补充 SkillSync 写盘策略说明，如有相关章节）

- [ ] **Step 1: 真实程序验证**

```bash
cd <repo_root>

# 备份当前 state 文件 mtime
stat -f "before: %Sm" ~/.niu/skill_sync_state.json

# 等待 2 分钟（让后台 scan 跑 2 轮）
sleep 120

# 检查 mtime 是否变化（无 skills 变化时 mtime 应保持不变）
stat -f "after: %Sm" ~/.niu/skill_sync_state.json
```

预期：`before` 和 `after` 的 mtime **相同**（之前每分钟变一次，现在无变化不变）。

**重要**：这个测试需要真实启动程序（`./niu`），不是 pytest 能覆盖的。如果环境不方便启动真实程序，可以跳过这一步，依赖 Task 1-3 的 pytest 覆盖。

- [ ] **Step 2: 文档更新（可选）**

```bash
cd <repo_root>
grep -n "skill_sync_state\|SkillSync\|skill.*sync" docs/manual-vector-store.md | head -10
```

如果有相关章节，补充一段说明 SkillSync 的写盘策略：

```markdown
**SkillSync 写盘策略**（2026-07-19 修复）：

`scan_and_sync` 每 60 秒跑一次，但只在以下情况写 `~/.niu/skill_sync_state.json`：
- skills 新增/修改/删除成功（hash 变化）
- notes 新增/修改/删除成功
- watchdog 在 scan 期间并发修改了 `_last_scan`（如新文件防抖后注入）
- KG ghost skill 删除失败时塞空值到 next_scan（避免无限重试）

无变化时不写盘，避免每分钟无意义重写。
```

如果没有相关章节，跳过这一步。

- [ ] **Step 3: Commit（如有文档改动）**

```bash
cd <repo_root>
git add docs/manual-vector-store.md
git commit -m "docs: 补充 SkillSync 写盘策略说明

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 验证清单（所有 Task 完成后跑）

```bash
cd <repo_root>

# 1. 新增测试全部通过
python -m pytest tests/test_skill_sync_no_write_when_unchanged.py -v

# 2. 全量 SkillSync 相关测试无回归
python -m pytest tests/test_notes_json.py tests/test_lightrag_unified.py tests/test_lightrag_skillsync_vdb_preservation.py tests/test_lightrag_repair_e2e_skillsync.py tests/test_skill_sync_no_write_when_unchanged.py -v

# 3. Python 语法检查
python -c "import ast; ast.parse(open('agent/injector/sync.py').read()); print('SYNTAX_OK')"

# 4. 真实程序验证（可选，需要启动 ./niu）
stat -f "before: %Sm" ~/.niu/skill_sync_state.json
sleep 120
stat -f "after: %Sm" ~/.niu/skill_sync_state.json
# 预期：before 和 after 相同（无变化时不写盘）
```

---

## Self-Review

### 1. Spec coverage 检查

- ✅ 无变化不写盘（用户核心诉求）— Task 1 Step 3-4 入口快照 + 出口对比
- ✅ 覆盖 watchdog 并发新增条目场景（C1）— Task 3 测试 3
- ✅ 覆盖 KG ghost cleanup 失败塞空值场景（C2）— Task 3 测试 4
- ✅ 删除 _scan_notes 末尾重复 _save_state（M2）— Task 2
- ✅ 保持 SkillFileHandler._execute 的 _save_state 不变（I2）— Task 1 不动 L124/L129
- ✅ 不改 _save_state 本身，状态文件格式向后兼容 — Task 1 只加判断

### 2. Placeholder 检查

无 TBD/TODO。所有代码段完整可执行。

### 3. 类型一致性检查

- `skills_snapshot_before: dict[str, str]` / `notes_snapshot_before: dict[str, str]` — 类型跟 `_last_scan` / `_last_notes_scan` 一致（L154/L156 都是 `dict[str, str]`）
- Task 1 入口快照变量名 `skills_snapshot_before` / `notes_snapshot_before` 在 Task 1 Step 4 出口对比时复用 — 一致

### 4. 风险点

- **dict 浅拷贝足够**：`dict(self._last_scan)` 是浅拷贝，但 `_last_scan` 的值是 str（不可变），键值对增删改都会让新 dict 跟旧 dict 不等。不需要 deepcopy。
- **锁内对比 + 锁外写盘**：`skills_changed = next_scan != skills_snapshot_before` 在 `with self._lock` 块内做，保证读到一致快照。`_save_state` **必须在锁外调**——`_lock` 是普通 `threading.Lock()` 不可重入（L157），`_save_state` L254 自己 `with self._lock` 会嵌套死锁。方案 Task 1 Step 4 的 `if skills_changed or notes_changed: self._save_state()` 在 `with self._lock` 块外调用，正确。
- **写盘成功才更新内存**（审查 Agent 指出的 L396 顺序问题）：`self._last_scan = next_scan` 移到 `_save_state()` 之后。如果 `_save_state` 抛 OSError（磁盘满/权限），内存未改，下次 scan 重新对比 → 重新写盘。否则内存已改但磁盘未改，下次 scan 入口快照读到新内存，出口对比"无变化"不写盘，磁盘永远旧状态。
- **watchdog 在 scan 进行中改已有 key 的 hash**（审查 Agent 指出的 I1）：L393 合并逻辑同时覆盖已有 key 的 hash（`if name not in known_skills or next_scan.get(name) != hash_val`），避免 watchdog 改过的 hash 被 `self._last_scan = next_scan` 覆盖回旧值。否则下次 scan 把该 skill 当"内容变化"重新注入一次（LightRAG upsert 不致命，但浪费一次调用 + 日志噪音）。
- **_first_scan_complete 不受影响**：L857-863 看 `scan_result[0] != -1`，不看是否写盘。首次扫描无变化时不写盘，但信号照常 set。
- **_clear_sync_state_if_storage_empty 不受影响**：lightrag_manager.py L840-876 依赖"state 文件存在"不依赖"L397 每次写盘"。方案 C 只让"无变化不写"，state 文件还在。
- **watchdog `_execute` 的 _save_state 保留**：L124/L129 不动。watchdog 是文件变化触发的，本身就是"有变化"，写盘正确。下次 `scan_and_sync` 入口快照会捕获 watchdog 写的新状态，出口对比发现"无变化"跳过写盘——不丢状态。watchdog `_execute` 自己写的盘 + scan 出口对比写的盘可能双重写盘（如果 scan 进行中 watchdog 触发），但都是真实变化，写盘正确，重复写不致命。
- **_scan_notes 部分失败**：L639-668 循环中途改 `_last_notes_scan`，若抛异常被 L387-388 catch。L713 原本的 `_save_state` 删除后，notes 内存状态靠 L397 出口对比写盘。**两种情况**：(1) 成功注入的 note 已修改 `_last_notes_scan` → 出口对比 `notes_changed=True` → 写盘 ✓；(2) 全部失败回滚后 `_last_notes_scan` 与入口一致 → 出口对比 `notes_changed=False` → 不写盘 ✓（正确，因为状态确实没变）。
- **ghost cleanup 成功路径**：L374 `next_scan.pop(entity_name)` 让 `next_scan` 跟入口快照不同（少了一个 key），同时 `deleted += 1`。L397 出口对比 `next_scan != skills_snapshot_before` → 写盘。覆盖。
- **scan_and_sync 抛异常**：L848-853 try/except 捕获 scan_and_sync 异常，此时不写盘。如果异常发生在 L396 之前：`_last_scan` 未改，下次 scan 入口快照跟磁盘一致 → 不丢状态。如果异常发生在 L396 之后、`_save_state` 之前：方案 Step 4 把 `self._last_scan = next_scan` 移到 `_save_state` 之后，异常中断 `_save_state` 时内存未改，下次 scan 入口快照读到磁盘一致状态 → 不丢状态 ✓。

### 5. 测试覆盖度统计

| 文件 | 测试数 | 覆盖场景 |
|------|--------|----------|
| `tests/test_skill_sync_no_write_when_unchanged.py` | 4 | 无变化不写盘 / notes 变化写一次 / watchdog 并发写盘 / ghost 失败写盘 |

### 6. 测试 fixture 路径对齐（审查 Agent 第 3 轮指出）

- **state 文件路径**：`SkillSync._state_file = Path.home() / ".niu" / "skill_sync_state.json"`（sync.py L151）。fixture Patch `Path.home()` 返回 `tmp_path` 后，必须把 state 写到 `tmp_path / ".niu" / "skill_sync_state.json"`，先 `mkdir(tmp_path / ".niu")`。
- **notes.json 路径**：`_scan_notes` 读 `os.environ["WORKSPACE_PATH"] / "notes" / "notes.json"`（sync.py L615-618）。测试必须 `monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))` + 把 notes.json 写到 `tmp_path / "notes" / "notes.json"`。
- **未对齐后果**：fixture 写错路径 → `_load_state` 返回空 dict → known_skills 空 → scan 走新增路径调 `_sync_skill` → 测试 1 断言 `mock_sync.assert_not_called()` 永远失败；测试 2 因 `WORKSPACE_PATH` 未设 → `_scan_notes` 返回 (0, 0) → `assert added >= 1` 失败。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-19-skill-sync-no-write-when-unchanged.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
