# 调度器错峰等待改为"等后端非忙" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把定时任务调度器对"启动时错过的多个任务"的固定 600 秒错峰等待，改成轮询后端 `_chat_lock.locked()`，后端非忙且二次确认仍非忙即执行下一条。

**Architecture:** scheduler 线程在错峰等待处，用 `asyncio.run_coroutine_threadsafe` 桥接到主事件循环读取 `_chat_lock.locked()`（复用项目既有模式，不绕道 HTTP）。`busy=false` 后等 3 秒再查一次，仍为 `false` 才执行下一条（二次确认防抖，避免与异步子 Agent 抢占冲突）。错峰等待期间**持锁**（不 release `_check_lock`），避免多批次并发轮询破坏串行性。

**Tech Stack:** Python 3.11, threading, asyncio（`run_coroutine_threadsafe` 桥接，无新依赖）, pytest

## Global Constraints

- 调度器是纯 `threading.Thread`，**不能**直接用 asyncio。从 scheduler 线程访问主 loop 的 async 对象（`_chat_lock`），必须用 `asyncio.run_coroutine_threadsafe(coro, _main_loop).result(timeout=...)` 桥接——这是项目既有模式（service.py:100、watcher.py:211、lightrag_manager.py:904 等十几处）。**禁止**用 urllib/requests HTTP 自请求 `127.0.0.1`（会引入死锁风险 + 与架构模式相悖）。
- 信号源是后端 `_chat_lock.locked()`，**不绕道前端小女孩状态机**——后端信号更早更准（runner.chat 返回即 release 锁，前端 SSE+动画有秒级延迟）。
- **本次一并修复超时不一致**：三层超时实际链路是 ChatQueue 内层 `enqueue_and_wait` 默认 120s → service 层 `future.result(timeout=300)` 但因内层 120s 先返回空串，service 最坏 2×120s+10s=250s → scheduler 外层 `_CALLBACK_TIMEOUT`。原值 120s < service 最坏 250s，外层会先超时但底层仍持 `_chat_lock`。改为 `_CALLBACK_TIMEOUT=300`（覆盖 250s + 余量）。
- **错峰等待期间持锁**：新逻辑下不再 `_check_lock.release()`（原设计为让新到期任务可触发，但新逻辑轮询非忙会快速推进，持锁不会让新任务卡太久；且持锁避免多批次并发轮询破坏串行性）。
- 遵循现有代码风格：loguru 日志、`[SCHEDULER]` 前缀、中文注释。
- 改前 git 备份 + gitnexus impact 分析；改后跑 `tests/test_scheduler_overdue.py` 全套测试。

## 设计决策（调研结论 + 第一轮审查修正）

### 为什么用 `_chat_lock.locked()` 而非前端状态机

| 对比项 | 后端 `_chat_lock.locked()`（采用） | 前端 spirit 状态机（不采用） |
|--------|-------------------------------|---------------------------|
| 信号源 | `_chat_lock`，后端串行化锁 | `busyCount`，前端引用计数 |
| 可达性 | `run_coroutine_threadsafe` 桥接直接读，无需端口/HTTP | 前端状态只在 main.js 转发，后端无回读接口 |
| 延迟 | 零延迟（release 锁即 `busy:false`） | 秒级延迟（SSE 推送 + 动画过渡） |
| 准确性 | 覆盖 `/chat` 和 ChatQueue 两条路径，是后端忙碌的唯一真相源 | 只反映动画状态，可能滞后于真实处理完成 |

### 为什么用 run_coroutine_threadsafe 而非 HTTP 自请求（第一轮审查 C2 修正）

- 项目所有"非 loop 线程访问主 loop async 对象"都用 `run_coroutine_threadsafe` 桥接（service.py:100 等）。引入 urllib HTTP 自请求同进程 uvicorn 会：1) 与架构模式不一致；2) urlopen 3s 超时返回 False 误判"空闲"会直接破坏串行性（本计划要保证的核心性质）。
- `run_coroutine_threadsafe` 直接读 `_chat_lock.locked()`，无网络栈开销、无端口依赖、无 uvicorn 就绪竞态。

### 二次确认防抖（替代固定冷却）

任务执行完 `_chat_lock` release，轮询到 `busy=false` 后**不能立即执行下一条**——因为异步调用的子 Agent 也会查这个状态，一旦发现非忙就立即执行下一个动作，两者会冲突。所以采用**二次确认**：

1. 轮询到 `busy=false` → 等 3 秒（分块检查 running，可中断）
2. 再查一次 `_is_backend_busy()`
3. 仍为 `false` → 执行下一条；若变回 `true`（子 Agent 抢占了）→ 继续轮询

这样 scheduler 和异步子 Agent 的动作天然错开：谁先拿到非忙状态谁先动，另一个的二次确认会失败、退回等待。

### 为什么错峰等待期间持锁（第一轮审查 C3 修正）

原代码 `_check_lock.release()` 是为了让"准时任务"不被错峰队列阻塞。但原逻辑是固定 600s sleep，持锁会让准时任务卡 600s。新逻辑轮询非忙、快速推进（任务完成即触发下一条），持锁不会让新任务卡太久（最多多等一个"任务耗时+3s"）。且持锁避免多批次并发轮询互相观测、乒乓等待破坏串行性。**新逻辑持锁是安全的。**

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|---------|
| `niu_api/internal/scheduler/scheduler.py` | 调度器核心 | 修改：`_CALLBACK_TIMEOUT` + `__init__` 常量 + `_is_backend_busy` 新增 + 错峰等待逻辑替换 |
| `tests/test_scheduler_overdue.py` | 测试 | 修改：新增测试 + 适配原有测试 + fixture 同步 |

**不动的文件**：`niu_api/chat.py`、`niu_api/compat.py`、`service.py`、前端任何文件。

---

### Task 1: 新增 `_is_backend_busy`（run_coroutine_threadsafe 桥接）

**Files:**
- Modify: `niu_api/internal/scheduler/scheduler.py`（在 `Scheduler` 类内新增私有方法）
- Test: `tests/test_scheduler_overdue.py`

**Interfaces:**
- Consumes: `niu_api.chat._main_loop`（主事件循环引用）、`niu_api.compat._chat_lock`
- Produces: `Scheduler._is_backend_busy(self) -> bool` —— 通过 `run_coroutine_threadsafe` 调一个读 `_chat_lock.locked()` 的协程，返回 `True` 表示后端忙；loop 不可用或查询超时返回 `False`（不阻塞调度，但记 warning）

- [ ] **Step 1: 写失败测试**

在 `tests/test_scheduler_overdue.py` 顶部 import 区确认有 `import asyncio`（没有则加），然后新增测试类（放现有测试之后）：

```python
class TestIsBackendBusy:
    """测试 _is_backend_busy 通过 run_coroutine_threadsafe 桥接读取"""

    def test_returns_false_when_chat_lock_free(self, mock_scheduler):
        """_chat_lock 空闲时返回 False"""
        scheduler, _, _, _ = mock_scheduler
        import asyncio
        from unittest.mock import patch, MagicMock

        fake_loop = MagicMock()
        fake_future = MagicMock()
        fake_future.result.return_value = False  # _chat_lock.locked() = False

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is False

    def test_returns_true_when_chat_lock_held(self, mock_scheduler):
        """_chat_lock 被持有时返回 True"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch, MagicMock

        fake_loop = MagicMock()
        fake_future = MagicMock()
        fake_future.result.return_value = True

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is True

    def test_returns_false_when_loop_unavailable(self, mock_scheduler):
        """主 loop 为 None 时返回 False（不阻塞调度）"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch

        with patch('niu_api.chat._main_loop', None):
            assert scheduler._is_backend_busy() is False

    def test_returns_false_on_query_timeout(self, mock_scheduler):
        """桥接 future.result 超时返回 False（不阻塞调度）"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch, MagicMock
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        fake_loop = MagicMock()
        fake_future = MagicMock()
        fake_future.result.side_effect = FuturesTimeoutError()

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_scheduler_overdue.py::TestIsBackendBusy -v`
Expected: 4 个 FAIL with "AttributeError: 'Scheduler' object has no attribute '_is_backend_busy'"

- [ ] **Step 3: 实现 `_is_backend_busy`**

在 `niu_api/internal/scheduler/scheduler.py` 顶部 import 区确认有 `import asyncio`（没有则加）。在 `Scheduler` 类内、`_check_and_trigger_impl` 方法之前新增：

```python
    def _is_backend_busy(self) -> bool:
        """通过 run_coroutine_threadsafe 桥接读取后端 _chat_lock.locked()。

        复用项目既有桥接模式（service.py:100 等），不绕道 HTTP 自请求。
        - True：后端正在处理 chat 请求或 scheduler 任务，应等待
        - False：后端空闲，可执行下一条错过的任务
        - 主 loop 不可用或查询超时：返回 False（不阻塞调度，记 warning）

        从 scheduler 工作线程调用，桥接到主事件循环读取 asyncio.Lock 状态。
        """
        from niu_api.chat import _main_loop
        from niu_api.compat import _chat_lock

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[SCHEDULER] Main loop not available, _is_backend_busy assuming idle")
            return False

        async def _check():
            return _chat_lock.locked()

        try:
            future = asyncio.run_coroutine_threadsafe(_check(), loop)
            return future.result(timeout=3)
        except Exception as e:
            logger.warning(f"[SCHEDULER] _is_backend_busy query failed: {e}, assuming idle")
            return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_scheduler_overdue.py::TestIsBackendBusy -v`
Expected: 4 个 PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/scheduler/scheduler.py tests/test_scheduler_overdue.py
git commit -m "feat(scheduler): 新增 _is_backend_busy 通过 run_coroutine_threadsafe 读取 _chat_lock

复用项目既有桥接模式（service.py:100 等），不绕道 HTTP 自请求。
主 loop 不可用或查询超时返回 False 不阻塞调度。"
```

---

### Task 2: 替换错峰等待为"轮询非忙 + 二次确认防抖（持锁）" + 修复超时

**Files:**
- Modify: `niu_api/internal/scheduler/scheduler.py`（`_CALLBACK_TIMEOUT` + `__init__` 常量 + `_check_and_trigger_impl` 的 `if i > 0:` 块）
- Test: `tests/test_scheduler_overdue.py`

**Interfaces:**
- Consumes: `Scheduler._is_backend_busy()`（Task 1 产出）
- Produces: 改造后的错峰等待逻辑——轮询非忙 + 二次确认防抖（持锁）+ 修复后的超时

**关键设计**：
- **超时修复**：`_CALLBACK_TIMEOUT` 从 120 改 300（覆盖 service 最坏 2×120s+10s=250s + 余量）。三层超时关系：ChatQueue 内层 120s 先触发 → service 最坏 250s → scheduler 外层 300s 最后兜底。
- **二次确认防抖**：轮询到 `busy=false` → 等 3 秒（分块检查 running）→ 再查一次，仍为 `false` 才执行下一条。
- **持锁**：错峰等待期间**不释放** `_check_lock`（原 `release()` 删除），避免多批次并发轮询破坏串行性。
- 轮询间隔 2 秒，二次确认间隔 3 秒，总超时上限 600 秒（后端一直忙或 loop 异常时强制执行下一条）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scheduler_overdue.py` 末尾新增测试类（断言用 `call_count` 验证语义，避免 flaky 时间断言）：

```python
class TestStaggerWaitBackendIdle:
    """测试错峰等待改为等后端非忙+二次确认（持锁）"""

    def test_executes_next_after_double_confirm_idle(self, mock_scheduler):
        """后端空闲→等3s→仍空闲→执行下一条；验证 _is_backend_busy 调用次数"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        with patch.object(scheduler, '_is_backend_busy', return_value=False) as mock_busy:
            scheduler.check_and_trigger()

        # i=1 错峰等待：首次查询(False) + 二次确认查询(False) = 2 次
        assert callback.call_count == 2
        assert mock_busy.call_count == 2

    def test_rechecks_when_subagent_takes_lock_during_confirm(self, mock_scheduler):
        """二次确认时若后端又忙（子Agent抢占）→ 继续等；验证调用序列"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 首次 False→二次确认 True（被抢占）→轮询 False→二次确认 False
        busy_sequence = [False, True, False, False]
        with patch.object(scheduler, '_is_backend_busy', side_effect=busy_sequence) as mock_busy:
            scheduler.check_and_trigger()

        assert callback.call_count == 2
        assert mock_busy.call_count == 4  # 首次+确认(失败) + 轮询 + 首次+确认(成功)

    def test_waits_while_backend_busy(self, mock_scheduler):
        """后端忙碌时轮询等待，变空闲后二次确认执行"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 2次忙→False→二次确认False
        busy_sequence = [True, True, False, False]
        with patch.object(scheduler, '_is_backend_busy', side_effect=busy_sequence) as mock_busy:
            scheduler.check_and_trigger()

        assert callback.call_count == 2
        assert mock_busy.call_count == 4

    def test_stagger_wait_interruptible_during_double_confirm(self, mock_scheduler):
        """二次确认 sleep 期间 stop() 能快速中断（<2s）"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 3  # 生产值，测试中断响应
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 后端空闲（进入二次确认），0.5s 后 stop
        import threading
        def stop_after_delay():
            time.sleep(0.5)
            scheduler.running = False
        threading.Thread(target=stop_after_delay, daemon=True).start()

        with patch.object(scheduler, '_is_backend_busy', return_value=False):
            scheduler.check_and_trigger()

        # 只执行第一个任务（i=0），第二个在二次确认 sleep 期间被中断
        assert callback.call_count == 1

    def test_fallback_timeout_forces_next(self, mock_scheduler):
        """后端一直忙超过总超时上限，强制执行下一条"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1
        scheduler._stagger_max_wait = 2

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        with patch.object(scheduler, '_is_backend_busy', return_value=True):
            start = time.time()
            scheduler.check_and_trigger()
            elapsed = time.time() - start

        assert callback.call_count == 2
        assert elapsed >= 2  # 至少等了总超时
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_scheduler_overdue.py::TestStaggerWaitBackendIdle -v`
Expected: 5 个 FAIL（`_double_confirm_delay`/`_stagger_max_wait` 属性不存在 + 错峰逻辑还是旧的固定等待）

- [ ] **Step 3: 修复超时常量 + 新增配置常量**

在 `niu_api/internal/scheduler/scheduler.py`：

1. **修复超时**（约 23 行）：把 `_CALLBACK_TIMEOUT = 120  # 2 minutes` 改为：

```python
_CALLBACK_TIMEOUT = 300  # 覆盖 service 最坏 2×120s+10s=250s + 余量；原 120s 小于 service 最坏耗时会导致外层先超时但 _chat_lock 仍被持有
```

2. **新增配置常量**：在 `__init__` 方法内，找到 `self._overdue_stagger_interval = 600  # 10 分钟`（约 39 行），**替换为**（删除 `_overdue_stagger_interval`，新逻辑不再用它）：

```python
        # 改造：错峰等待改为轮询后端非忙 + 二次确认防抖（持锁）
        self._busy_poll_interval = 2  # 轮询后端忙碌状态的间隔（秒）
        self._double_confirm_delay = 3  # 二次确认间隔：查到非忙→等3s→再查，仍非忙才执行
        self._stagger_max_wait = 600  # 错峰等待总超时上限（秒），防止后端一直忙导致永远等不到
```

3. **更新 `_make_scheduler` fixture**（`tests/test_scheduler_overdue.py` 约 12-28 行）：
   - 删除 `scheduler._overdue_stagger_interval = 2`（约 18 行）
   - 在 `scheduler._store_factory = None` 之后补：

```python
    scheduler._busy_poll_interval = 2
    scheduler._double_confirm_delay = 3
    scheduler._stagger_max_wait = 600
```

- [ ] **Step 4: 替换错峰等待逻辑**

在 `niu_api/internal/scheduler/scheduler.py` 的 `_check_and_trigger_impl` 方法内，找到 `if i > 0:` 块（约 245-269 行，包含 `self._check_lock.release()` 和 `while remaining > 0` 循环），整块替换为：

```python
            # 间隔等待（第一个任务不等待）
            # 持锁（不 release _check_lock）：新逻辑轮询非忙快速推进，
            # 持锁避免多批次并发轮询破坏串行性（原 release 设计是为让
            # 准时任务不被固定 600s sleep 阻塞，新逻辑无需此妥协）
            if i > 0:
                stopped = False
                logger.info(
                    f"[SCHEDULER] Waiting for backend idle before next due task "
                    f"({i+1}/{len(due_tasks)})"
                )
                wait_start = time.time()
                while True:
                    with self._lock:
                        if not self.running:
                            logger.info("[SCHEDULER] Stopped during stagger wait")
                            stopped = True
                            break

                    # 总超时兜底：后端一直忙或 loop 异常时，强制执行下一条
                    if time.time() - wait_start >= self._stagger_max_wait:
                        logger.warning(
                            f"[SCHEDULER] Stagger wait exceeded {self._stagger_max_wait}s "
                            f"timeout, forcing next task"
                        )
                        break

                    # 二次确认防抖：查非忙→分块等3s（可中断）→再查，仍非忙才执行
                    # 原因：异步子 Agent 也会查这个状态抢着执行，
                    # 二次确认让两者动作错开（谁先拿到非忙谁先动，
                    # 另一个的二次确认会失败、退回等待）
                    if not self._is_backend_busy():
                        # 分块等待二次确认间隔，每秒检查 running
                        confirm_remaining = self._double_confirm_delay
                        while confirm_remaining > 0:
                            with self._lock:
                                if not self.running:
                                    logger.info("[SCHEDULER] Stopped during double-confirm")
                                    stopped = True
                                    break
                            chunk = min(confirm_remaining, 1)
                            time.sleep(chunk)
                            confirm_remaining -= chunk
                        if stopped:
                            break
                        # 再次查后端状态
                        if not self._is_backend_busy():
                            break  # 二次确认成功，执行下一条
                        logger.debug("[SCHEDULER] Backend became busy during double-confirm, rewaiting")
                        time.sleep(self._busy_poll_interval)  # rewait 也退避，避免紧循环
                        continue

                    # 后端忙，轮询等待
                    time.sleep(self._busy_poll_interval)
                if stopped:
                    return
```

注意：去掉了 `self._check_lock.release()` 和 finally 里的 `self._check_lock.acquire()`——整个错峰等待期间持锁。

- [ ] **Step 5: 跑新测试确认通过**

Run: `python3 -m pytest tests/test_scheduler_overdue.py::TestStaggerWaitBackendIdle -v`
Expected: 5 个 PASS

- [ ] **Step 6: 跑全部调度器测试看哪些原有测试 break**

Run: `python3 -m pytest tests/test_scheduler_overdue.py -v 2>&1 | tail -30`
Expected: 记录所有 FAIL 的测试名（进入 Task 3 修复）

- [ ] **Step 7: Commit**

```bash
git add niu_api/internal/scheduler/scheduler.py tests/test_scheduler_overdue.py
git commit -m "feat(scheduler): 错峰等待改为轮询后端非忙+二次确认防抖+修复超时

三处改动：
1. 错峰等待从固定 600s 改为轮询 _chat_lock.locked()，后端非忙→等3s→
   再查仍非忙才执行下一条（二次确认防抖，避免与异步子Agent冲突）
2. _CALLBACK_TIMEOUT 从 120 提到 300，覆盖 service 最坏 250s，
   修复外层先于内层超时导致 _chat_lock 仍被持有的问题
3. 错峰等待期间持锁（不 release _check_lock），避免多批次并发轮询
   破坏串行性；保留 600s 总超时兜底

语义从'每10分钟执行一条'变成'后端空闲就执行下一条'，错过的任务补
完速度从最坏 N*10分钟 降到 N*(任务耗时+3s)。"
```

---

### Task 3: 修复原有测试 + 更新注释

**Files:**
- Modify: `tests/test_scheduler_overdue.py`（原有依赖 `_overdue_stagger_interval` 的测试）
- Modify: `niu_api/internal/scheduler/scheduler.py`（文件头注释）

**Interfaces:**
- Consumes: Task 2 改造后逻辑
- Produces: 全套测试通过 + 注释准确

**预期会 break / 需清理的测试清单（第一轮审查 I5 + 第二轮 Imp-3 明确）**：

1. `test_multiple_due_tasks_execute_sequentially`（约 42-76 行）：断言 `elapsed >= scheduler._overdue_stagger_interval * 3 - 1`。fixture 删了 `_overdue_stagger_interval` 会 AttributeError；即使保留也会因新逻辑 mock `_is_backend_busy` 缺失而 urlopen 失败。
2. `test_callback_timeout_is_120s`（约 200-203 行）：直接 `assert timeout == 120`，改 300 后必 fail。
3. `test_single_due_task_no_stagger_wait`（约 91-103 行）：断言 `elapsed < scheduler._overdue_stagger_interval`，fixture 删了属性会 AttributeError。
4. `test_stagger_wait_interruptible_by_stop`（约 105-145 行，旧版）：依赖原 stagger 循环每 10s 检查 running，新逻辑行为变了。
5. `test_start_and_stop_with_lock_protection`（约 206-209 行）：直接 `scheduler._overdue_stagger_interval = 600` 赋值。不会 AttributeError（Python 允许动态赋属性），但残留死属性误导维护者，需删除该行。

- [ ] **Step 1: 逐个修复 break 的测试**

对上述 4 个测试，按以下原则适配（具体代码在执行时根据实际失败情况写）：

- **`test_callback_timeout_is_120s`**：把断言从 `assert timeout == 120` 改为 `assert timeout == 300`（读 `scheduler.py` 的 `_CALLBACK_TIMEOUT` 常量确认）。
- **依赖 `_overdue_stagger_interval` 的测试**（`test_multiple_due_tasks_execute_sequentially`、`test_single_due_task_no_stagger_wait`）：
  - 删除对 `_overdue_stagger_interval` 的引用
  - mock `_is_backend_busy` 返回 `False`（让它走"非忙立即执行"路径）+ 设 `_double_confirm_delay=0`（跳过二次确认等待）
  - 时间断言改为 `callback.call_count == N`（验证执行了 N 个任务），不再依赖固定时间
- **旧 `test_stagger_wait_interruptible_by_stop`**：已被 Task 2 的新测试 `test_stagger_wait_interruptible_during_double_confirm` 覆盖，可直接删除旧版，或改写为 mock `_is_backend_busy` 返回 True + 验证只执行第一个任务。
- **`test_start_and_stop_with_lock_protection`**：删除第 209 行的 `scheduler._overdue_stagger_interval = 600`（已无语义，`_overdue_stagger_interval` 属性已从 `__init__` 删除）。

每个测试修复后立即跑 `python3 -m pytest tests/test_scheduler_overdue.py::<TestName> -v` 确认通过。

- [ ] **Step 2: 更新文件头注释**

`scheduler.py` 文件头注释（约 1-5 行）从：

```python
"""
Task Scheduler - Single-loop architecture

Periodically scans for due tasks and executes them via trigger_callback.
Overdue tasks are handled by the same loop with stagger intervals to prevent
simultaneous execution on startup.
"""
```

改为：

```python
"""
Task Scheduler - Single-loop architecture

Periodically scans for due tasks and executes them via trigger_callback.
Overdue tasks are handled by the same loop: each waits for the backend idle
signal (_chat_lock.locked() polled via run_coroutine_threadsafe) with
double-confirm debounce, bounded by a max-wait timeout to prevent indefinite
blocking when the backend stays busy.
"""
```

- [ ] **Step 3: 跑全套调度器测试确认全过**

Run: `python3 -m pytest tests/test_scheduler_overdue.py tests/test_scheduler_service.py tests/test_scheduler_group_push.py tests/test_scheduler_frontend_ready.py tests/test_scheduler_message_sse.py -v`
Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_scheduler_overdue.py niu_api/internal/scheduler/scheduler.py
git commit -m "test(scheduler): 适配错峰等待改造+更新注释

- test_callback_timeout_is_120s 断言改 300
- 依赖 _overdue_stagger_interval 的测试改 mock _is_backend_busy 走非忙路径
- 旧 test_stagger_wait_interruptible_by_stop 被 Task2 新测试覆盖，删除/改写
- 更新文件头注释反映新语义"
```

---

## 风险与遗留问题

1. **超时已修复**：`_CALLBACK_TIMEOUT` 从 120 提到 300。三层超时关系正确：ChatQueue 内层 120s 先触发 → service 最坏 250s（2×120+10）→ scheduler 外层 300s 最后兜底。**遗留**：service.py:108 的 `future.result(timeout=300)` 实际是死代码（内层 120s 先返回空串，service 永远等不到 300s），本次不改它，仅记录。

2. **总超时强制执行的副作用**：总超时强制执行下一条时，若后端真卡死（`_chat_lock` 长期被持有），下一条任务的 `trigger_callback` 会入队 ChatQueue 等 `_chat_lock`，scheduler 线程被 `_CALLBACK_TIMEOUT=300s` 阻塞期间 `_run_loop` 饿死。可接受（真卡死是异常状态，需人工介入），但需记录。

3. **ALERT 视觉叠加**：任务执行完会 `add_pending_alert("⏰")`（service.py:135）触发小女孩蹦高 ALERT。若上一个任务的 ALERT 还没被用户点掉，下一条任务又执行，spirit 会 ALERT→BUSY 切换。不影响后端逻辑，仅视觉，可接受。

4. **持锁代价**：错峰等待期间持锁，新到期任务（准时的）会等当前错峰队列跑完。但新逻辑快速推进（任务完成+3s 即触发下一条），最多多等一个"任务耗时+3s"，可接受。原固定 600s sleep 持锁会卡死，新逻辑不会。

## Self-Review

**1. Spec coverage:**
- "检查状态机不等于忙就执行下一条" → Task 2 轮询 `_is_backend_busy`，busy=false 即执行 ✓
- "为什么只触发一个" → 根因（固定 600s）已分析，改造为非忙即执行 ✓
- "10 分钟才触发第二个" → 根因（`_overdue_stagger_interval=600`）已分析，改造为轮询+二次确认防抖 ✓
- 信号源选择（后端 vs 前端）→ 调研报告论证选后端 ✓
- 二次确认防抖 → Task 2 `_double_confirm_delay=3` ✓
- 超时不一致一并修复 → Task 2 `_CALLBACK_TIMEOUT` 120→300，核算正确（ChatQueue 120s 内层 → service 250s → scheduler 300s）✓
- 总超时兜底 → Task 2 `_stagger_max_wait=600` ✓
- 串行性保证 → Task 2 持锁（不 release _check_lock）✓
- 可中断 → Task 2 二次确认 sleep 分块检查 running ✓
- 桥接模式复用 → Task 1 用 `run_coroutine_threadsafe` 非 HTTP 自请求 ✓

**2. Placeholder scan:**
- Task 3 Step 1 "按以下原则适配"——给了明确的 4 个测试清单 + 每个的改法原则，具体代码在执行时根据实际失败写（合理，因 fixture/属性改动后失败表现需实测）。不算 placeholder。
- 所有代码步骤都有完整代码块 ✓
- 无 "TBD" / "TODO" / "类似 Task N" ✓

**3. Type consistency:**
- `_is_backend_busy() -> bool`：Task 1 定义，Task 2 调用 ✓
- `_busy_poll_interval` / `_double_confirm_delay` / `_stagger_max_wait`：Task 2 Step 3 定义，Step 4 使用，测试 fixture 同步 ✓
- `_CALLBACK_TIMEOUT`：Task 2 Step 3 改 300，Task 3 Step 1 测试断言改 300 ✓
- 删除 `_overdue_stagger_interval`：Task 2 Step 3 删定义 + fixture 删，Task 3 Step 1 处理依赖它的测试 ✓

**4. 第一轮审查 Critical 修复情况:**
- C1（超时数学错误）→ 650 改 300，核算修正为 250s 最坏 ✓
- C2（HTTP 自请求死锁）→ 改用 `run_coroutine_threadsafe` 桥接 ✓
- C3（批次并发破坏串行性）→ 错峰等待持锁 ✓
- C4（二次确认 sleep 不检查 running）→ 改分块循环每秒检查 ✓
- I1-I3（测试 flaky）→ 断言改 `call_count` ✓
- I5（Task 3 低估工作量）→ 明确列出 4 个 break 测试 + 改法 ✓
- I6（fixture 属性同步）→ Step 3 明确删 `_overdue_stagger_interval` + 加新属性 ✓
