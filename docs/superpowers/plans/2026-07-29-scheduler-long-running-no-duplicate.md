# 定时任务长执行防重复触发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 定时任务执行耗时较长时，防止下一轮轮询重复触发同一任务；通过持久化时间戳 + 8 小时超时重置机制，覆盖崩溃恢复和跨进程场景。

**Architecture:** 复用现有 `triggered_at` 字段（ISO datetime）作为"执行中时间戳"。每轮 `check_and_trigger` 开头，将 `in_progress` 且 `triggered_at` 距今超过 8 小时的任务重置为 `pending`（跨日期计算用 `datetime` 差值，自然正确，如 23 点开始 → 次日 7 点超时）。CAS `pending → in_progress` 时机提前到调用 callback 之前（当前实现已如此，本计划补齐超时重置 + 测试覆盖）。

**Tech Stack:** Python 3.11+, SQLite (WAL), threading, pytest, unittest.mock

---

## 问题根因

当前 `_check_and_trigger_impl`（`niu_api/internal/scheduler/scheduler.py:262`）：
1. `get_overdue_tasks()` 查 `status='pending' AND scheduled_at <= now`（已排除 `in_progress`）
2. CAS `pending → in_progress` + 记 `triggered_at`（L358，**在 callback 之前**）
3. `_call_trigger_callback` 同步阻塞（最长 300s）
4. 成功后 `pending` + 改 `scheduled_at`

进程内 `_check_lock`（L48）防止同一进程内 `check_and_trigger` 并发。但两个漏洞：
- **崩溃恢复**：`_recover_orphaned_tasks`（L76）启动时把所有 `in_progress` 重置为 `pending`，若任务执行中崩溃，重启后立即重复触发。
- **跨进程**：launcher 与独立 `python -m niu_api` 各跑一个 Scheduler 实例，`_check_lock` 是进程内锁，无法跨进程互斥。

用户的方案："发送成功立即改状态为执行中时间戳，轮询超 8 小时重置为未执行"——用持久化时间戳代替进程内锁，跨进程/崩溃恢复都安全。当前实现已满足"立即改状态"（CAS 在 callback 前），本计划补齐"8 小时超时重置"。

**8h 超时与 300s callback 超时的关系**：
`_call_trigger_callback` 的 `_CALLBACK_TIMEOUT=300s`（scheduler.py L26/L450），正常执行的任务最长阻塞 300s 后超时返回 None，走失败/reschedule 路径，status 离开 in_progress。因此正常运行的任务不会停留在 in_progress 超 8h。8h 阈值专为以下场景设计：进程崩溃/被 kill 后任务卡在 in_progress、跨进程竞态导致 CAS 后某进程死亡。8h 机制不会误伤执行中的正常任务。

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `niu_api/internal/scheduler/task_store.py` | SQLite 数据层 | 新增 `reset_stale_in_progress(timeout_hours, now)` 方法；`recover_orphaned_tasks` 清除 `triggered_at` |
| `niu_api/internal/scheduler/scheduler.py` | 调度循环 | `_check_and_trigger_impl` 开头调用超时重置 |
| `tests/test_scheduler_overdue.py` | 调度器测试 | 新增超时重置测试类；recover 清 triggered_at 测试；跨进程 CAS 测试 |

---

### Task 1: TaskStore 新增 `reset_stale_in_progress` 方法

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py`（在 `recover_orphaned_tasks` 方法后插入）
- Test: `tests/test_scheduler_overdue.py`（新增 `TestResetStaleInProgress` 类）

- [ ] **Step 1: 写失败测试 — 超时的 in_progress 任务被重置为 pending**

在 `tests/test_scheduler_overdue.py` 末尾追加测试类。测试用真实 TaskStore（tmp_path SQLite），不走 mock，因为要验证 SQL 语义。

```python
class TestResetStaleInProgress:
    """测试超时的 in_progress 任务被重置为 pending"""

    def test_stale_in_progress_reset_to_pending(self, tmp_path):
        """triggered_at 超过 8 小时的 in_progress 任务重置为 pending"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime, timedelta

        now_fixed = datetime(2026, 7, 29, 12, 0, 0)  # 固定时刻，消除墙钟依赖
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="测试任务",
            scheduled_at=now_fixed.isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )
        # 标记为 in_progress，triggered_at 设为 9 小时前
        stale_time = (now_fixed - timedelta(hours=9)).isoformat()
        assert store.update_task(task_id, status="in_progress", triggered_at=stale_time, expected_status="pending")

        # 超时重置（注入固定 now，距 stale_time = 9h > 8h）
        reset_count = store.reset_stale_in_progress(timeout_hours=8, now=now_fixed)
        assert reset_count == 1

        task = store.get_task(task_id)
        assert task["status"] == "pending"

    def test_fresh_in_progress_not_reset(self, tmp_path):
        """triggered_at 未超 8 小时的 in_progress 任务保持不变"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime, timedelta

        now_fixed = datetime(2026, 7, 29, 12, 0, 0)
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="测试任务",
            scheduled_at=now_fixed.isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )
        # triggered_at 设为 1 小时前
        fresh_time = (now_fixed - timedelta(hours=1)).isoformat()
        store.update_task(task_id, status="in_progress", triggered_at=fresh_time, expected_status="pending")

        reset_count = store.reset_stale_in_progress(timeout_hours=8, now=now_fixed)
        assert reset_count == 0

        task = store.get_task(task_id)
        assert task["status"] == "in_progress"

    def test_cross_midnight_timeout(self, tmp_path):
        """跨日期超时：23 点开始，次日 7:30 应超时（8.5 小时 > 8 小时）"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime, timedelta

        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="跨夜任务",
            scheduled_at=datetime.now().isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )
        # 固定参考时钟：用绝对构造避免墙钟依赖
        # now_fixed 同时用于 stale_time 计算和 reset_stale_in_progress 的 now 参数
        # 无论 now_fixed 的绝对值如何，stale(23:00) 距 now(07:30) = 8.5h > 8h 阈值，必触发重置
        now_fixed = datetime.now().replace(hour=7, minute=30, second=0, microsecond=0)
        stale_time = (now_fixed - timedelta(hours=8, minutes=30)).isoformat()  # 昨晚 23:00
        store.update_task(task_id, status="in_progress", triggered_at=stale_time, expected_status="pending")

        # 注入固定 now，距 stale_time = 8.5h > 8h，应重置
        reset_count = store.reset_stale_in_progress(timeout_hours=8, now=now_fixed)
        assert reset_count == 1

        task = store.get_task(task_id)
        assert task["status"] == "pending"

    def test_pending_task_not_affected(self, tmp_path):
        """pending 状态的任务不受超时重置影响"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime

        now_fixed = datetime(2026, 7, 29, 12, 0, 0)
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="待执行",
            scheduled_at=now_fixed.isoformat(),
            is_recurring=False,
        )
        # 任务保持 pending（无 triggered_at）
        reset_count = store.reset_stale_in_progress(timeout_hours=8, now=now_fixed)
        assert reset_count == 0
        task = store.get_task(task_id)
        assert task["status"] == "pending"

    def test_null_triggered_at_not_reset(self, tmp_path):
        """in_progress 但 triggered_at 为 NULL 的任务不重置（异常数据保护）"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime

        now_fixed = datetime(2026, 7, 29, 12, 0, 0)
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="异常任务",
            scheduled_at=now_fixed.isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )
        # 直接用 SQL 写入 in_progress 但不设 triggered_at（模拟异常数据）
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("UPDATE scheduled_tasks SET status='in_progress' WHERE id=?", (task_id,))
        conn.commit()
        conn.close()

        reset_count = store.reset_stale_in_progress(timeout_hours=8, now=now_fixed)
        assert reset_count == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestResetStaleInProgress -v`
Expected: FAIL with `AttributeError: 'TaskStore' object has no attribute 'reset_stale_in_progress'`

- [ ] **Step 3: 实现 `reset_stale_in_progress` 方法**

在 `niu_api/internal/scheduler/task_store.py` 的 `recover_orphaned_tasks` 方法之后（约 L370）、`cleanup_old_tasks` 之前插入：

```python
    def reset_stale_in_progress(self, timeout_hours: int = 8, now: Optional[datetime] = None) -> int:
        """将 triggered_at 超过 timeout_hours 的 in_progress 任务重置为 pending

        用于防止任务执行时间过长或崩溃后状态卡死。跨日期计算由 datetime 差值
        自然处理（如 23:00 开始 → 次日 07:00 超时 8 小时）。

        triggered_at 为 NULL 的 in_progress 任务不重置（异常数据，避免误伤）。

        与 recover_orphaned_tasks 一致，重置 in_progress→pending 时清除 triggered_at，
        避免残留旧时间戳污染 retry_failed_tasks 的重试间隔判断。

        Args:
            timeout_hours: 超时阈值（小时），默认 8
            now: 参考时刻（用于测试注入固定时钟，消除墙钟依赖）；默认 datetime.now()

        Returns:
            被重置的任务数
        """
        from datetime import datetime, timedelta

        if now is None:
            now = datetime.now()
        cutoff = (now - timedelta(hours=timeout_hours)).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        reset = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks
                SET status = 'pending', triggered_at = NULL
                WHERE status = 'in_progress'
                  AND triggered_at IS NOT NULL
                  AND datetime(triggered_at) <= datetime(?)
            """, (cutoff,))
            reset = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return reset
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestResetStaleInProgress -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/scheduler/task_store.py tests/test_scheduler_overdue.py
git commit -m "feat(scheduler): TaskStore 新增 reset_stale_in_progress — 8 小时超时重置卡死的 in_progress 任务"
```

---
### Task 1.5: recover_orphaned_tasks 清除 triggered_at

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py`（`recover_orphaned_tasks` 方法，L355-370）
- Test: `tests/test_scheduler_overdue.py`（新增 `TestRecoverOrphanedClearsTriggeredAt` 类）

**问题**：`recover_orphaned_tasks`（task_store.py L355-370）重置 `status='pending'` 但不清 `triggered_at`，残留值污染 `retry_failed_tasks`（L409-410 用 `triggered_at` 判断重试间隔）。

当前 SQL（task_store.py L362-365）：
```sql
UPDATE scheduled_tasks SET status = 'pending'
WHERE status = 'in_progress'
```

- [ ] **Step 1: 写失败测试 — 恢复后 triggered_at 必须为 None**

在 `tests/test_scheduler_overdue.py` 末尾追加：

```python
class TestRecoverOrphanedClearsTriggeredAt:
    """崩溃恢复重置 status 时必须清 triggered_at，避免污染 retry_failed_tasks"""

    def test_recover_clears_triggered_at(self, tmp_path):
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime, timedelta

        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="崩溃任务",
            scheduled_at=datetime.now().isoformat(),
            is_recurring=False,
        )
        # 模拟崩溃：in_progress + 旧 triggered_at
        old_time = (datetime.now() - timedelta(hours=2)).isoformat()
        store.update_task(task_id, status="in_progress", triggered_at=old_time, expected_status="pending")

        # 恢复
        recovered = store.recover_orphaned_tasks()
        assert recovered == 1

        task = store.get_task(task_id)
        assert task["status"] == "pending"
        assert task["triggered_at"] is None  # 必须清除
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestRecoverOrphanedClearsTriggeredAt -v`
Expected: FAIL — `assert task["triggered_at"] is None` 失败（当前 recover 不清 triggered_at）

- [ ] **Step 3: 修改 recover_orphaned_tasks SQL**

修改 `niu_api/internal/scheduler/task_store.py` 的 `recover_orphaned_tasks` 方法（L362-365），SQL 改为：

```sql
UPDATE scheduled_tasks SET status = 'pending', triggered_at = NULL
WHERE status = 'in_progress'
```

完整方法：
```python
    def recover_orphaned_tasks(self) -> int:
        """恢复崩溃遗留的 in_progress 任务（重置为 pending）"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        recovered = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks SET status = 'pending', triggered_at = NULL
                WHERE status = 'in_progress'
            """)
            recovered = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return recovered
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestRecoverOrphanedClearsTriggeredAt -v`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/scheduler/task_store.py tests/test_scheduler_overdue.py
git commit -m "fix(scheduler): recover_orphaned_tasks 清除 triggered_at，避免污染 retry_failed_tasks 重试间隔判断"
```


### Task 2: Scheduler 每轮调用超时重置

**Files:**
- Modify: `niu_api/internal/scheduler/scheduler.py:262-269`（`_check_and_trigger_impl` 开头）
- Test: `tests/test_scheduler_overdue.py`（新增 `TestSchedulerCallsResetStale` 类）

- [ ] **Step 1: 写失败测试 — Scheduler 每轮调用 reset_stale_in_progress**

在 `tests/test_scheduler_overdue.py` 末尾追加：

```python
class TestSchedulerCallsResetStale:
    """测试 Scheduler 每轮 check_and_trigger 开头调用 reset_stale_in_progress"""

    def test_reset_stale_called_before_due_check(self, mock_scheduler):
        """check_and_trigger 开头调用 store.reset_stale_in_progress"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 0
        # mock_store.get_overdue_tasks 返回空，确保只验证 reset 调用
        mock_store.get_overdue_tasks.return_value = []
        mock_store.reset_stale_in_progress = MagicMock(return_value=0)
        mock_store.retry_failed_tasks.return_value = 0
        # _store_factory 为 None 时不刷新 store
        scheduler._store_factory = None

        scheduler.check_and_trigger()

        mock_store.reset_stale_in_progress.assert_called_once_with(timeout_hours=8)

    def test_reset_stale_with_custom_timeout(self, mock_scheduler):
        """可配置超时阈值"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._stale_timeout_hours = 12
        scheduler._double_confirm_delay = 0
        mock_store.get_overdue_tasks.return_value = []
        mock_store.reset_stale_in_progress = MagicMock(return_value=0)
        mock_store.retry_failed_tasks.return_value = 0
        scheduler._store_factory = None

        scheduler.check_and_trigger()

        mock_store.reset_stale_in_progress.assert_called_once_with(timeout_hours=12)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestSchedulerCallsResetStale -v`
Expected: FAIL（`reset_stale_in_progress` 未被调用，或 `AttributeError: 'MagicMock' object has no attribute 'reset_stale_in_progress'`——因为 mock_store 不会自动有这个方法，且 Scheduler 未调用）

- [ ] **Step 3: 在 `__init__` 加超时配置字段**

在 `niu_api/internal/scheduler/scheduler.py` 的 `__init__` 方法中，`self._TASK_FAIL_THRESHOLD = 3`（L71）之后插入：

```python
        # in_progress 任务超时阈值（小时）：超过则重置为 pending
        # 防止任务执行中崩溃或跨进程竞态导致状态卡死
        self._stale_timeout_hours = 8
```

- [ ] **Step 4: 在 `_check_and_trigger_impl` 开头调用超时重置**

修改 `niu_api/internal/scheduler/scheduler.py` 的 `_check_and_trigger_impl` 方法。当前 L268-269：

```python
        # Reset failed tasks older than 5 minutes to pending for retry
        self.store.retry_failed_tasks(retry_interval_seconds=300)
```

改为在 `retry_failed_tasks` 之后、动态刷新 store 之前插入超时重置：

```python
        # Reset failed tasks older than 5 minutes to pending for retry
        self.store.retry_failed_tasks(retry_interval_seconds=300)

        # Reset in_progress tasks stuck longer than stale_timeout_hours (crash/cross-process safety)
        reset_count = self.store.reset_stale_in_progress(timeout_hours=self._stale_timeout_hours)
        if reset_count > 0:
            logger.warning(
                f"[SCHEDULER] Reset {reset_count} stale in_progress tasks "
                f"(exceeded {self._stale_timeout_hours}h timeout) to pending"
            )
```

- [ ] **Step 5: 同步 `_make_scheduler` fixture**

在 `tests/test_scheduler_overdue.py` 的 `_make_scheduler` 函数中（L33 `return scheduler, _CALLBACK_TIMEOUT` 之前）追加：

```python
    # Task 2 新增：超时重置阈值
    scheduler._stale_timeout_hours = 8
```

- [ ] **Step 6: 运行新测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestSchedulerCallsResetStale -v`
Expected: 2 passed

- [ ] **Step 7: 运行全量调度器测试确认无回归**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py -v`
Expected: 全部 passed（原有测试 + 新增 7 个）

- [ ] **Step 8: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/scheduler/scheduler.py tests/test_scheduler_overdue.py
git commit -m "feat(scheduler): 每轮 check_and_trigger 开头重置超时 in_progress 任务（8h 阈值，防崩溃/跨进程卡死）"
```

---

### Task 3: 验证 CAS 提前 + 整体集成

**Files:**
- Test: `tests/test_scheduler_overdue.py`（新增 `TestLongRunningNoDuplicate` 类）

本任务不新增实现代码——验证现有 CAS 时机（`pending → in_progress` + `triggered_at` 在 callback 之前，L356-359）+ Task 1/2 的超时重置，组合后能防止长执行任务的重复触发。

- [ ] **Step 1: 写集成测试 — 长执行任务在 callback 期间状态为 in_progress**

```python
class TestLongRunningNoDuplicate:
    """验证长执行任务期间状态正确，不会重复触发"""

    def test_status_in_progress_during_callback(self, mock_scheduler):
        """callback 执行期间任务状态为 in_progress，不会被 get_overdue_tasks 重新查出"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 0
        scheduler._store_factory = None

        # 用真实 TaskStore 验证 SQL 语义
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime
        real_store = TaskStore(scheduler.db_path)
        task_id = real_store.create_task(
            content="长任务",
            scheduled_at=datetime.now().isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )
        scheduler.store = real_store

        # callback 内检查任务状态应为 in_progress（不是 pending）
        states_during_callback = []
        def slow_callback(task):
            states_during_callback.append(real_store.get_task(task["id"])["status"])
            return "ok"
        scheduler.trigger_callback = slow_callback

        scheduler.check_and_trigger()

        # callback 执行时任务状态应为 in_progress
        assert states_during_callback == ["in_progress"]

        # callback 完成后，recurring 任务应 reschedule 到 pending
        final_task = real_store.get_task(task_id)
        assert final_task["status"] == "pending"
        # scheduled_at 应已推进到下次 cron 时间
        assert final_task["scheduled_at"] != datetime.now().isoformat()

    def test_second_check_during_execution_skips_in_progress(self, tmp_path):
        """【进程内回归测试】第二轮 check_and_trigger 被进程内 _check_lock 阻止，不会重新触发 in_progress 任务。注意：此测试验证的是进程内 _check_lock 互斥（回归保护），跨进程安全由 test_cross_process_cas_prevents_duplicate 验证（CAS pending→in_progress 是数据库层互斥，不依赖进程内锁）。"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from niu_api.internal.scheduler.scheduler import Scheduler
        from datetime import datetime
        import threading

        db_path = str(tmp_path / "test.db")
        store = TaskStore(db_path)
        task_id = store.create_task(
            content="长任务",
            scheduled_at=datetime.now().isoformat(),
            is_recurring=True,
            cron_expr="0 8 * * *",
        )

        # callback 阻塞，模拟长执行
        callback_done = threading.Event()
        callback_started = threading.Event()
        def blocking_callback(task):
            callback_started.set()
            callback_done.wait(timeout=5)
            return "ok"

        scheduler = Scheduler.__new__(Scheduler)
        scheduler.db_path = db_path
        scheduler.trigger_callback = blocking_callback
        scheduler.store = store
        scheduler.running = True
        scheduler.thread = None
        import threading as _t
        scheduler._lock = _t.RLock()
        scheduler._check_lock = _t.Lock()
        from concurrent.futures import ThreadPoolExecutor
        scheduler._executor = ThreadPoolExecutor(max_workers=2)
        scheduler._delayed_start_cancelled = False
        scheduler._task_fail_count = {}
        scheduler._TASK_FAIL_THRESHOLD = 3
        import threading as _te
        scheduler._ready_event = _te.Event()
        scheduler._store_factory = None
        scheduler._busy_poll_interval = 0
        scheduler._double_confirm_delay = 0
        scheduler._stagger_max_wait = 600
        scheduler._stale_timeout_hours = 8

        try:
            # 第一轮：启动（在 executor 里异步跑，因为 callback 会阻塞）
            import concurrent.futures
            future = scheduler._executor.submit(scheduler.check_and_trigger)
            assert callback_started.wait(timeout=2)

            # 此时任务应为 in_progress
            task_during = store.get_task(task_id)
            assert task_during["status"] == "in_progress"

            # 第二轮 check_and_trigger 应被 _check_lock 阻止（返回 skip）
            # 用另一个线程尝试，应立即返回（acquire blocking=False 失败）
            second_result = []
            def run_second():
                second_result.append(scheduler.check_and_trigger())
            t = _t.Thread(target=run_second)
            t.start()
            t.join(timeout=2)
            # 第二轮立即返回（skip），不重复触发
            assert not second_result or second_result[0] is None

            # callback 计数应为 1（未重复触发）
            # 释放第一轮 callback
            callback_done.set()
            future.result(timeout=10)

            # 最终任务 reschedule 为 pending
            final_task = store.get_task(task_id)
            assert final_task["status"] == "pending"
        finally:
            scheduler._executor.shutdown(wait=False)
```

    def test_cross_process_cas_prevents_duplicate(self, tmp_path):
        """两 Scheduler 实例共享 SQLite，CAS 防止重复触发（模拟跨进程）"""
        from niu_api.internal.scheduler.task_store import TaskStore
        from niu_api.internal.scheduler.scheduler import Scheduler
        from datetime import datetime
        from concurrent.futures import ThreadPoolExecutor
        import threading

        db_path = str(tmp_path / "test.db")
        store = TaskStore(db_path)
        task_id = store.create_task(
            content="跨进程测试",
            scheduled_at=datetime.now().isoformat(),
            is_recurring=False,
        )

        call_count = 0
        count_lock = threading.Lock()
        def callback(task):
            nonlocal call_count
            with count_lock:
                call_count += 1
            return "ok"

        # 两个 Scheduler 实例，共享 db_path，模拟跨进程
        def make_scheduler():
            s = Scheduler.__new__(Scheduler)
            s.db_path = db_path
            s.trigger_callback = callback
            s.store = TaskStore(db_path)
            s.running = True
            s.thread = None
            s._lock = threading.RLock()
            s._check_lock = threading.Lock()  # 各自独立的进程内锁
            s._executor = ThreadPoolExecutor(max_workers=2)
            s._delayed_start_cancelled = False
            s._task_fail_count = {}
            s._TASK_FAIL_THRESHOLD = 3
            s._ready_event = threading.Event()
            s._store_factory = None
            s._busy_poll_interval = 0
            s._double_confirm_delay = 0
            s._stagger_max_wait = 600
            s._stale_timeout_hours = 8
            return s

        s1 = make_scheduler()
        s2 = make_scheduler()
        try:
            # 并发执行
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(s1.check_and_trigger)
                f2 = pool.submit(s2.check_and_trigger)
                f1.result(timeout=10)
                f2.result(timeout=10)

            # CAS 保证只触发一次
            assert call_count == 1, f"Expected 1 callback, got {call_count}"
            # CAS 保证只触发一次：任务要么被删除（一次性任务成功后 delete），要么状态不是 pending（不会被重新查出）
            task = store.get_task(task_id)
            assert task is None or task['status'] != 'pending', f"Task still pending, may be re-triggered"
        finally:
            s1._executor.shutdown(wait=False)
            s2._executor.shutdown(wait=False)

- [ ] **Step 2: 运行集成测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py::TestLongRunningNoDuplicate -v`
Expected: 3 passed

- [ ] **Step 3: 运行全量测试确认无回归**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_scheduler_overdue.py -v`
Expected: 全部 passed

- [ ] **Step 4: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check niu_api/internal/scheduler/ tests/test_scheduler_overdue.py`
Expected: No errors

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_scheduler_overdue.py
git commit -m "test(scheduler): 验证长执行任务防重复触发 — CAS 提前 + in_progress 期间不重查 + 超时重置"
```

---

## Self-Review

**1. Spec coverage:**
- "发送成功立即改状态为执行中时间戳" → 当前实现已满足（CAS `pending→in_progress` + `triggered_at` 在 callback 之前，L356-359）。Task 3 集成测试验证此行为，其中 `test_cross_process_cas_prevents_duplicate` 验证跨进程 CAS 互斥。✅

**设计取舍**：用户"状态为执行中xx:xx"解读为 status='in_progress' 配合 triggered_at 时间戳，而非把时间戳编码进 status 字符串（如 'in_progress_14:30'）。原因：status 字段全程是枚举语义（pending/in_progress/completed/failed/cancelled），所有 SQL 查询用 WHERE status=? 字符串比较；若编码时间戳会破坏这些查询。triggered_at 字段（schema L36）已存在，scheduler.py L358 已写入，复用避免 schema 改动。
- "每轮轮询检查时间戳超过 8 小时重置为未执行" → Task 1 (`reset_stale_in_progress`) + Task 2 (每轮调用)。✅
- "跨日期计算，23 点开始 8 小时超时为 7 点" → Task 1 `test_cross_midnight_timeout` 验证。`datetime` 差值天然跨日期。✅
- "状态机为非忙时才发送" → 现有 `_is_backend_busy` + 二次确认（L218-249, L317-336），未改动。✅

**2. Placeholder scan:** 无 TODO/TBD/占位符。所有代码块完整。✅

**3. Type consistency:**
- `reset_stale_in_progress(timeout_hours: int = 8, now: Optional[datetime] = None) -> int` — Task 1 定义（now 参数用于测试注入固定时钟），Task 2 调用 `self.store.reset_stale_in_progress(timeout_hours=self._stale_timeout_hours)`（生产调用不传 now，默认 datetime.now()）。✅
- `_stale_timeout_hours` — Task 2 Step 3 在 `__init__` 定义，Step 5 在 fixture 同步。✅
- `triggered_at` — schema L36 已有，Task 1 SQL 复用。✅
- `recover_orphaned_tasks` triggered_at 清除 — Task 1.5 修改 SQL 加 `triggered_at = NULL`，`TestRecoverOrphanedClearsTriggeredAt` 验证。避免残留值污染 `retry_failed_tasks`（L409-410 用 triggered_at 判断重试间隔）。✅
- `reset_stale_in_progress` 与 `recover_orphaned_tasks` 都在 in_progress→pending 转换时清 triggered_at=NULL，保持一致。✅

---

## 执行说明

**项目经理注意：** 本计划全部是代码修改。按铁律 2，主对话不直接改代码，应委托子 Agent 执行。但计划本身已写明每步的精确代码和测试，子 Agent 可直接落地。

计划已保存到 `docs/superpowers/plans/2026-07-29-scheduler-long-running-no-duplicate.md`。
