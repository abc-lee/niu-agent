# tmp 目录子目录递归清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `cleanup_old_tmp()` 递归清理 `~/.niu/tmp/` 下的子目录——子目录中超过 24 小时的文件删除，空目录删除。

**Architecture:** 重写 `cleanup_old_tmp()` 用 `os.walk` 自底向上遍历，对每个文件按 mtime 判断是否超过 24 小时，删除过期文件；`os.walk` 遍历完成后空目录自动删除。时间判断从"非当天"（`mtime.date < today`）改为"超过 24 小时"（`now - mtime > 24h`），更精确。

**Tech Stack:** Python 3.11，`os.walk`，`pathlib`

---

## File Structure

| File | Responsibility |
|---|---|
| `agent/tmp_dir.py` | 临时目录管理。重写 `cleanup_old_tmp()` 支持子目录递归 |
| `tests/test_tmp_dir.py` | 测试。新增 `TestCleanupOldTmp` 测试类 |

---

## 当前逻辑 vs 目标逻辑

### 当前（tmp_dir.py L51-72）
```python
def cleanup_old_tmp() -> int:
    tmp_dir = get_tmp_dir()
    today = datetime.date.today()
    for f in tmp_dir.iterdir():        # 只遍历顶层
        if not f.is_file():            # 跳过子目录 ← 问题所在
            continue
        mtime = datetime.date.fromtimestamp(f.stat().st_mtime)
        if mtime < today:              # 按日期比较（非24小时）
            f.unlink()
```

### 目标
```python
def cleanup_old_tmp() -> int:
    tmp_dir = get_tmp_dir()
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
    # os.walk(topdown=False) 自底向上，先处理文件再处理目录
    for root, dirs, files in os.walk(tmp_dir, topdown=False):
        for filename in files:
            filepath = os.path.join(root, filename)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            if mtime < cutoff:
                os.remove(filepath)
                deleted += 1
        for dirname in dirs:
            dirpath = os.path.join(root, dirname)
            try:
                os.rmdir(dirpath)  # 只删空目录，非空报 OSError
            except OSError:
                pass  # 目录非空，跳过
    return deleted
```

关键变化：
1. `iterdir()` → `os.walk(topdown=False)` — 递归遍历子目录
2. `date < today` → `datetime < cutoff(24h)` — 精确按 24 小时判断
3. `f.is_file()` 跳过目录 → `os.rmdir()` 删空目录

---

### Task 1: 新增 `TestCleanupOldTmp` 测试类

**Files:**
- Test: `tests/test_tmp_dir.py`（在文件末尾追加）

- [ ] **Step 1: 写测试用例**

在 `tests/test_tmp_dir.py` 末尾追加：

```python
class TestCleanupOldTmp:
    def test_deletes_old_files_in_root(self, tmp_dir_fixture):
        """根目录超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        # 创建一个旧文件（修改时间设为2天前）
        old_file = tmp_dir_fixture / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)  # 2天前
        os.utime(old_file, (old_time, old_time))
        # 创建一个新文件
        new_file = tmp_dir_fixture / "new.txt"
        new_file.write_text("new")
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_deletes_old_files_in_subdirectory(self, tmp_dir_fixture):
        """子目录中超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "subdir"
        subdir.mkdir()
        old_file = subdir / "old_sub.txt"
        old_file.write_text("old in subdir")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_old_files_in_nested_subdirectory(self, tmp_dir_fixture):
        """多级子目录中超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        deep_dir = tmp_dir_fixture / "a" / "b" / "c"
        deep_dir.mkdir(parents=True)
        old_file = deep_dir / "deep_old.txt"
        old_file.write_text("deep old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_empty_directories(self, tmp_dir_fixture):
        """文件被删除后空目录被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "empty_after_cleanup"
        subdir.mkdir()
        old_file = subdir / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not subdir.exists(), "空目录应被删除"

    def test_keeps_nonempty_directories(self, tmp_dir_fixture):
        """子目录中有新文件时不删除目录"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "has_new_file"
        subdir.mkdir()
        # 旧文件会被删除
        old_file = subdir / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        # 新文件保留
        new_file = subdir / "new.txt"
        new_file.write_text("new")
        cleanup_old_tmp()
        assert subdir.exists(), "有新文件的目录不应被删除"
        assert new_file.exists()

    def test_keeps_recent_files(self, tmp_dir_fixture):
        """24小时内的文件不被删除"""
        from agent.tmp_dir import cleanup_old_tmp
        recent_file = tmp_dir_fixture / "recent.txt"
        recent_file.write_text("recent")
        deleted = cleanup_old_tmp()
        assert deleted == 0
        assert recent_file.exists()

    def test_returns_zero_for_empty_dir(self, tmp_dir_fixture):
        """空目录返回0"""
        from agent.tmp_dir import cleanup_old_tmp
        deleted = cleanup_old_tmp()
        assert deleted == 0

    def test_keeps_files_under_24_hours(self, tmp_dir_fixture):
        """23小时前的文件不被删除（超过24小时才删除，用 mtime < cutoff 严格小于判断）"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        # 23小时前的文件 — 不应删除
        recent_file = tmp_dir_fixture / "23h.txt"
        recent_file.write_text("recent")
        recent_time = time.time() - (23 * 3600)
        os.utime(recent_file, (recent_time, recent_time))
        deleted = cleanup_old_tmp()
        assert deleted == 0
        assert recent_file.exists()
```

- [ ] **Step 2: 运行测试验证失败**
Run: `python/bin/python -m pytest tests/test_tmp_dir.py::TestCleanupOldTmp -v`
Expected: 4 个测试 FAIL（子目录递归 + 空目录删除相关），4 个 PASS（根目录行为在旧实现下已正确）

- [ ] **Step 3: Commit**

```bash
git add tests/test_tmp_dir.py
git commit -m "test: add TestCleanupOldTmp for subdirectory recursive cleanup"
```

---

### Task 2: 重写 `cleanup_old_tmp()` 支持子目录递归

**Files:**
- Modify: `agent/tmp_dir.py:51-72`

- [ ] **Step 1: 重写 `cleanup_old_tmp` 函数**

将 `agent/tmp_dir.py` L51-72 的：

```python
def cleanup_old_tmp() -> int:
    """清理非当天的临时文件，返回删除数量"""
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return 0
    today = datetime.date.today()
    deleted = 0
    for f in tmp_dir.iterdir():
        if not f.is_file():
            continue
        # 按修改时间判断是否为当天文件
        try:
            mtime = datetime.date.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime < today:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted
```

替换为：

```python
def cleanup_old_tmp() -> int:
    """清理超过24小时的临时文件（含子目录），空目录自动删除，返回删除文件数量"""
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return 0
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
    deleted = 0
    # topdown=False: 自底向上遍历，先处理文件再处理子目录
    for root, dirs, files in os.walk(tmp_dir, topdown=False):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    os.remove(filepath)
                    deleted += 1
                except OSError:
                    pass
        for dirname in dirs:
            dirpath = os.path.join(root, dirname)
            try:
                os.rmdir(dirpath)  # 只删除空目录，非空会抛 OSError
            except OSError:
                pass
    return deleted
```

- [ ] **Step 2: 运行全部 tmp_dir 测试验证通过**

Run: `python/bin/python -m pytest tests/test_tmp_dir.py -v`
Expected: PASS — 全部测试通过（原有测试 + 新增 TestCleanupOldTmp 8 个）

- [ ] **Step 3: Commit**

```bash
git add agent/tmp_dir.py
git commit -m "fix: cleanup_old_tmp now recursively cleans subdirectories and empty dirs"
```

---

## Self-Review

### 1. Spec coverage

- ✅ "子目录下的文档逐级检查" — `os.walk(topdown=False)` 递归遍历所有层级
- ✅ "超过24小时的一样删除" — `mtime < cutoff`（24小时前）
- ✅ "空目录直接删除" — `os.rmdir(dirpath)` 删空目录，非空 OSError 跳过
- ✅ 时间判断从"非当天"改为"超过24小时" — 更精确，符合用户需求

### 2. Placeholder scan

- 无 TBD/TODO
- 所有代码步骤都有完整代码
- 测试用例有具体断言

### 3. Type consistency

- `cleanup_old_tmp() -> int` 返回值类型不变
- `get_tmp_dir() -> Path` 不变
- `os.walk` 返回 `(root, dirs, files)` 三元组，`root` 是 str，`os.path.join(root, filename)` 返回 str — 与 `os.remove` / `os.rmdir` 参数类型一致
- `datetime.datetime.now() - datetime.timedelta(hours=24)` 返回 `datetime.datetime`，与 `datetime.datetime.fromtimestamp()` 返回值类型一致，可以用 `<` 比较 ✅
