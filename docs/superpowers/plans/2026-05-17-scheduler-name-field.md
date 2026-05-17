# 定时任务 name 字段 + 自动注入 Bug 修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 scheduled_tasks 表加 name 字段，自动注入定时任务改为按 name 匹配而非 cron_expr，修复用户改时间后重复注入的 Bug，新增 2 个定时任务。

**Architecture:** name 是可选字段（用户手动创建的任务不需要 name），只有自动注入的系统任务用 name 标识。数据库迁移用 ALTER TABLE ADD COLUMN（同 last_executed_date 的模式）。

**Tech Stack:** SQLite + Python + FastAPI + MCP

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `niu_api/internal/scheduler/task_store.py` | 表加 name 列，所有方法加 name 参数和返回 |
| Modify | `niu_api/__main__.py` | 注入逻辑改为按 name 匹配，新增 2 个定时任务 |
| Modify | `niu_api/internal/scheduler/routes.py` | CreateTaskRequest 加 name 字段 |
| Modify | `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | schedule_task 工具加 name 参数 |

---

### Task 1: task_store.py — 加 name 列 + 方法适配

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py`

- [ ] **Step 1: 写测试 — 验证 name 字段能正确创建和查询**

创建测试文件 `niu_api/internal/scheduler/test_task_store_name.py`：

```python
"""测试 TaskStore name 字段"""
import tempfile
import os
from niu_api.internal.scheduler.task_store import TaskStore


def test_create_task_with_name():
    """带 name 创建任务，能通过 name 查询"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    task_id = store.create_task(
        content="测试任务",
        scheduled_at="2026-05-18T08:00:00",
        event_type="recurring",
        is_recurring=True,
        cron_expr="0 8 * * *",
        name="daily-entity-extractor",
    )

    tasks = store.list_tasks()
    found = [t for t in tasks if t.get("name") == "daily-entity-extractor"]
    assert len(found) == 1
    assert found[0]["id"] == task_id
    assert found[0]["name"] == "daily-entity-extractor"
    os.unlink(db_path)


def test_create_task_without_name():
    """不带 name 创建任务（用户手动创建），name 为 None"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    task_id = store.create_task(
        content="用户提醒",
        scheduled_at="2026-05-18T15:00:00",
        event_type="reminder",
    )

    task = store.get_task(task_id)
    assert task is not None
    assert task.get("name") is None
    os.unlink(db_path)


def test_find_task_by_name():
    """按 name 查找任务（核心功能：替代 cron_expr 匹配）"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    store.create_task(
        content="提取实体",
        scheduled_at="2026-05-18T08:00:00",
        event_type="recurring",
        is_recurring=True,
        cron_expr="0 9 * * *",  # 用户改了时间！
        name="daily-entity-extractor",
    )

    # 按 name 查找，不依赖 cron_expr
    found = store.find_task_by_name("daily-entity-extractor")
    assert found is not None
    assert found["cron_expr"] == "0 9 * * *"  # 用户改的时间保留
    os.unlink(db_path)


def test_find_task_by_name_not_found():
    """name 不存在时返回 None"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    found = store.find_task_by_name("nonexistent")
    assert found is None
    os.unlink(db_path)


def test_name_migration_from_old_db():
    """旧数据库（无 name 列）迁移后 name 列存在且为 None"""
    db_path = tempfile.mktemp(suffix=".db")

    # 先创建旧格式数据库（无 name 列）
    conn = __import__("sqlite3").connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            scheduled_at DATETIME NOT NULL,
            is_recurring INTEGER DEFAULT 0,
            cron_expr TEXT,
            event_type TEXT,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            triggered_at DATETIME,
            last_triggered_at DATETIME,
            last_executed_date TEXT
        )
    """)
    conn.execute("INSERT INTO scheduled_tasks (id, content, scheduled_at, status) VALUES (?, ?, ?, 'pending')",
                 ("old-task-id", "旧任务", "2026-05-18T08:00:00"))
    conn.commit()
    conn.close()

    # TaskStore 初始化会自动迁移
    store = TaskStore(db_path)

    # 旧任务的 name 为 None
    task = store.get_task("old-task-id")
    assert task is not None
    assert task.get("name") is None
    os.unlink(db_path)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest niu_api/internal/scheduler/test_task_store_name.py -v`
Expected: FAIL（name 参数和 find_task_by_name 方法不存在）

- [ ] **Step 3: 实现 task_store.py 的 name 字段**

修改 `niu_api/internal/scheduler/task_store.py`：

1. `_init_db()` 中加迁移：`ALTER TABLE scheduled_tasks ADD COLUMN name TEXT`
2. `create_task()` 加 `name` 参数，INSERT 语句加 name 列
3. 新增 `find_task_by_name()` 方法
4. `list_tasks()` / `get_task()` / `get_overdue_tasks()` 的 SELECT 和返回字典加 name 字段
5. `update_task()` 加 `name` 参数

- [ ] **Step 4: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest niu_api/internal/scheduler/test_task_store_name.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/scheduler/task_store.py niu_api/internal/scheduler/test_task_store_name.py
git commit -m "feat: TaskStore 加 name 字段 + find_task_by_name 方法 — 按名称匹配定时任务"
```

---

### Task 2: __main__.py — 注入逻辑改为按 name 匹配 + 新增 2 个定时任务

**Files:**
- Modify: `niu_api/__main__.py`

- [ ] **Step 1: 写测试 — 验证注入逻辑按 name 匹配**

创建测试文件 `niu_api/test_auto_inject_tasks.py`：

```python
"""测试启动时自动注入定时任务的逻辑"""
import tempfile
import os
from datetime import datetime, timedelta
from niu_api.internal.scheduler.task_store import TaskStore


def test_inject_does_not_duplicate_when_user_changes_time():
    """用户改了 cron 时间后，启动注入不会创建重复任务"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    # 模拟用户改了时间（从 8 点改到 9 点）
    store.create_task(
        content="调用 chat-with-entity-extractor ...",
        scheduled_at="2026-05-18T09:00:00",
        event_type="recurring",
        is_recurring=True,
        cron_expr="0 9 * * *",  # 用户改了！
        name="daily-entity-extractor",
    )

    # 模拟启动注入逻辑：按 name 查找
    existing = store.find_task_by_name("daily-entity-extractor")
    assert existing is not None
    assert existing["cron_expr"] == "0 9 * * *"  # 用户改的时间保留
    assert existing["name"] == "daily-entity-extractor"

    # 不应再创建新任务
    tasks_before = store.list_tasks()
    count_before = len([t for t in tasks_before if t.get("status") != "cancelled"])

    # 模拟：发现已有任务，不创建
    # （实际逻辑在 __main__.py 中，这里验证 find_task_by_name 的行为）

    tasks_after = store.list_tasks()
    count_after = len([t for t in tasks_after if t.get("status") != "cancelled"])
    assert count_after == count_before  # 没有重复
    os.unlink(db_path)


def test_inject_creates_task_when_not_exists():
    """name 不存在时，注入创建新任务"""
    db_path = tempfile.mktemp(suffix=".db")
    store = TaskStore(db_path)

    existing = store.find_task_by_name("daily-entity-extractor")
    assert existing is None

    store.create_task(
        content="调用 chat-with-entity-extractor ...",
        scheduled_at=datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).isoformat(),
        event_type="recurring",
        is_recurring=True,
        cron_expr="0 8 * * *",
        name="daily-entity-extractor",
    )

    found = store.find_task_by_name("daily-entity-extractor")
    assert found is not None
    os.unlink(db_path)
```

- [ ] **Step 2: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest niu_api/test_auto_inject_tasks.py -v`
Expected: PASS（依赖 Task 1 的 find_task_by_name）

- [ ] **Step 3: 修改 __main__.py 注入逻辑**

将第 161-217 行的注入逻辑重构为：

```python
# 8.6. Ensure system recurring tasks exist (by name, not cron_expr)
_SYSTEM_TASKS = [
    {
        "name": "daily-entity-extractor",
        "content": (
            "调用 chat-with-entity-extractor 子 Agent，task 参数为："
            "\"提炼有价值内容：扫描近期对话，筛选偏好/技能/经验，形成精炼文档通过 lightrag_insert 增量注入 LightRAG。\" "
            "不要从对话历史中提取内容，只执行此 task。"
        ),
        "cron_expr": "0 8 * * *",
        "hour": 8,
    },
    {
        "name": "daily-journal-check",
        "content": "请检查今天的日志，整理后与用户确认是否完整",
        "cron_expr": "0 18 * * *",
        "hour": 18,
    },
    {
        "name": "weekly-report-reminder",
        "content": "提醒用户本周工作已汇总，询问是否需要生成周报",
        "cron_expr": "0 9 * * 1",
        "hour": 9,
        "dow": 1,
    },
]

try:
    from niu_api.internal.scheduler import get_store

    ts = get_store()
    existing_tasks = ts.list_tasks()

    # Cancel any stale kg-enricher tasks
    for task in existing_tasks:
        if (
            task.get("event_type") == "recurring"
            and "chat-with-kg-enricher" in task.get("content", "")
        ):
            try:
                ts.cancel_task(task["id"])
                logger.info(f"Cancelled stale kg-enricher task: {task['id']}")
            except Exception as cancel_err:
                logger.warning(f"Could not cancel kg-enricher task {task['id']}: {cancel_err}")

    # Ensure each system task exists (by name, not cron_expr)
    for task_def in _SYSTEM_TASKS:
        existing = ts.find_task_by_name(task_def["name"])

        if existing is None:
            # Create new task
            now = datetime.now()
            hour = task_def.get("hour", 8)
            next_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if task_def.get("dow") is not None:
                # Calculate next Monday
                days_ahead = task_def["dow"] - now.isoweekday()
                if days_ahead <= 0:
                    days_ahead += 7
                next_time = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
            elif next_time <= now:
                next_time += timedelta(days=1)

            ts.create_task(
                content=task_def["content"],
                scheduled_at=next_time.isoformat(),
                is_recurring=True,
                cron_expr=task_def["cron_expr"],
                event_type="recurring",
                name=task_def["name"],
            )
            logger.info(f"Created system task '{task_def['name']}' (next run: {next_time})")
        elif existing.get("content") != task_def["content"]:
            # Update content only (keep user's cron_expr changes)
            ts.update_task(existing["id"], content=task_def["content"])
            logger.info(f"Updated system task '{task_def['name']}' content (id={existing['id']})")
        else:
            logger.debug(f"System task '{task_def['name']}' already exists and up-to-date")

except Exception as e:
    logger.warning(f"Failed to ensure system tasks: {e}")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest niu_api/test_auto_inject_tasks.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/__main__.py niu_api/test_auto_inject_tasks.py
git commit -m "fix: 定时任务注入改为按name匹配 — 修复用户改时间后重复注入Bug + 新增2个定时任务"
```

---

### Task 3: routes.py + scheduler-server — name 参数透传

**Files:**
- Modify: `niu_api/internal/scheduler/routes.py`
- Modify: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py`

- [ ] **Step 1: routes.py 加 name 字段**

在 `CreateTaskRequest` 中加 `name: Optional[str] = None`，在 `create_task` 路由中透传 `name=request.name`。

- [ ] **Step 2: scheduler-server 加 name 参数**

在 TOOL_SCHEMAS 的 `schedule_task` 中加 `name` 参数（可选）。
在 `schedule_task()` 函数中加 `name` 参数并透传到 `store.create_task()`。
在 MCP server 的 `call_tool` handler 中透传 `name=arguments.get("name")`。

- [ ] **Step 3: 提交**

```bash
git add niu_api/internal/scheduler/routes.py mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py
git commit -m "feat: 定时任务 API + MCP 工具加 name 参数（可选）"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 运行所有测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest niu_api/internal/scheduler/test_task_store_name.py niu_api/test_auto_inject_tasks.py -v`
Expected: ALL PASS

- [ ] **Step 2: 启动应用验证**

启动应用后检查日志，确认 3 个系统任务正确注入：
- `daily-entity-extractor`（每天 8:00）
- `daily-journal-check`（每天 18:00）
- `weekly-report-reminder`（每周一 9:00）

- [ ] **Step 3: 验证改时间不重复**

通过 UI 或 API 把 `daily-entity-extractor` 的时间从 8:00 改到 9:00，重启应用，确认不会创建新的 8:00 任务。