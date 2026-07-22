# Scheduler 启动信号位置修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复启动时处理过期定时任务的 race condition——signal_scheduler_ready 触发过早导致 scheduler 扫描过期任务时 runner 依赖未就绪，user 消息已写 DB 前端可见但 runner.chat() 抛异常、任务被标 failed。

**Architecture:** 把 `signal_scheduler_ready()` 调用从 `niu_api/__main__.py:218`（Phase 1 gate 之后、LightRAG/BrainGraph/`_SYSTEM_TASKS` 初始化之前）挪到 lifespan 末尾（L401 之后、`yield` 之前），保证 scheduler 收到 ready 信号时所有后台依赖就绪。同时把 `scheduler.py` 的 `_ready_event.wait(timeout=60)` 60s 超时调大到 180s，防止挪到末尾后 lifespan 总耗时接近 60s 触发"超时强行 start"漏洞（提交 `0df739e0` commit message 明确警告过的同型漏洞）。同步更新 5 处文档/注释文案避免文档漂移。

**Tech Stack:** Python 3.11+，FastAPI lifespan，scheduler 后台线程 + `_ready_event`，lifespan 内 Phase 1 韧性 gate。

---

## 历史背景（执行者必读，不要重复考古）

当前 bug 是同型 race 的第三型：

1. **提交 `2e795521`（2026-06-14）** 引入 `signal_scheduler_ready` 机制，解决"scheduler 10s 延迟内 ChatQueue 没起 → 伪造 assistant 消息"的同型症状。signal 当时放在 L156（ChatQueue 之后）。
2. **提交 `867485b0`（2026-07-07）** 引入 LightRAG Phase 1/Phase 2 韧性流程，解决"鸡生蛋"（LightRAG init 失败时 embedding 没加载 → repair_vdb 不可用 → 无法修复 vdb → LightRAG 永远 init 不成功）。signal 位置不动。
3. **提交 `0df739e0`（2026-07-09）** 把 signal 从 L156 挪到 L218（Phase 1 gate 之后），堵"Phase 1 检测到损坏时 scheduler 60s 超时强行 start 撞未就绪 runner"的同型漏洞。L218 是折中点——堵了 Phase 1 漏洞，但没考虑 L255 之后的依赖项。
4. **提交 `1804372b`（2026-07-07）** 删 Phase 2 自动修复（改为只检测不修复，等用户在 rfd 弹窗决策）。signal 位置不动。

**当前 bug 根因**：need_repair=False 时 signal 在 L218 发出，但 L255 之后的 LightRAG eager init / PipelineWatcher / LightRAGSync / BrainGraph / create_default_regions / RegionSync / `_SYSTEM_TASKS` 都还没就绪 → scheduler sleep 2s 后扫描过期任务 → `trigger_callback` → ChatQueue 写 user 消息到 DB（前端可见）→ 调 `runner.chat()` 抛异常 → 跳过 `persist_agent_reply` → 任务被标 failed。

**本计划方案**：沿用提交 `2e795521` 的 signal 机制思路，把 signal 挪到所有依赖项都就绪之后（lifespan 末尾），同时调大 60s 超时到 180s（防止 lifespan L67→末尾耗时接近 60s 触发超时强行 start）。

**全逻辑链审查已通过**（8 个风险点全 LOW/MEDIUM，无 HIGH/CRITICAL）：
1. `cancel_delayed_start` 是全局 flag，need_repair=True 路径完全不受影响
2. L255-401 之间所有初始化块都在 try/except 内，单块失败不中断 lifespan，signal 必然在末尾发出
3. L255-401 无代码依赖 scheduler 已就绪，`_SYSTEM_TASKS` 用 `get_store()` 直接操作 SQLite 不走 Scheduler 类
4. 现有测试不断言 60s 数值或 L218 位置
5. 方案顺带修复了"任务创建中 scheduler 已扫描"的潜在竞态

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `niu_api/__main__.py` | FastAPI lifespan 启动序列 | 删除 L216-223 旧 signal 块，在 L401 之后、`yield` 之前新增 signal 块；更新 L201-202 注释 |
| `niu_api/internal/scheduler/scheduler.py` | Scheduler 后台线程 + `_ready_event` 超时 | L103 `timeout_seconds = 60` → `180`；L106/L121 日志文案 "60s" → "180s"；L128-130 docstring "60s" → "180s" |
| `niu_api/internal/lightrag_manager.py` | Phase 1 gate 函数 | L1400/L1451/L1453/L1455/L1458 docstring/注释 "60s" → "180s"（5 处） |
| `tests/test_lightrag_startup_block.py` | 启动阻断逻辑测试 | L4/L71/L73 docstring "60s" → "180s"（3 处，测试断言不变） |

---

## Task 1：scheduler.py 超时 60s → 180s + 文案同步

**Files:**
- Modify: `niu_api/internal/scheduler/scheduler.py:99-144`

### Step 1.1：改超时数值

修改 `niu_api/internal/scheduler/scheduler.py:103` 的 `timeout_seconds = 60` 改为 `180`：

```python
            # Phase 1: Wait for system ready signal (with timeout fallback)
            timeout_seconds = 180
```

**Edit 前 old_string**（含上下文唯一匹配）：
```python
            # Phase 1: Wait for system ready signal (with timeout fallback)
            timeout_seconds = 60
            signaled = self._ready_event.wait(timeout=timeout_seconds)
            if not signaled:
                logger.warning("[SCHEDULER] Ready signal not received within 60s, forcing start")
```

**Edit 后 new_string**：
```python
            # Phase 1: Wait for system ready signal (with timeout fallback)
            timeout_seconds = 180
            signaled = self._ready_event.wait(timeout=timeout_seconds)
            if not signaled:
                logger.warning("[SCHEDULER] Ready signal not received within 180s, forcing start")
```

- [ ] Step 1.1：用 Edit 工具替换上述代码

### Step 1.2：改启动日志文案

修改 `niu_api/internal/scheduler/scheduler.py:121` 的启动日志文案：

**Edit 前 old_string**：
```python
        threading.Thread(target=_delayed_start, daemon=True).start()
        logger.info("[SCHEDULER] Delayed start: waiting for system_ready signal (60s timeout)")
```

**Edit 后 new_string**：
```python
        threading.Thread(target=_delayed_start, daemon=True).start()
        logger.info("[SCHEDULER] Delayed start: waiting for system_ready signal (180s timeout)")
```

- [ ] Step 1.2：用 Edit 工具替换上述代码

### Step 1.3：改 cancel_delayed_start docstring

修改 `niu_api/internal/scheduler/scheduler.py:126-130` 的 docstring（5 处 "60s"）：

**Edit 前 old_string**（docstring 整段）：
```python
        """取消 delayed start（不 shutdown 整体 scheduler）。

        场景：启动期检测到 LightRAG 损坏（need_repair=True），
        lifespan 不调 signal_scheduler_ready，但 scheduler.start_delayed
        里的 _ready_event.wait(60) 60s 超时后会强行 start（L103-106）。
        此方法设 _delayed_start_cancelled=True，让 _delayed_start 线程
        在 60s 超时后检查到这个 flag 直接 return，不强行 start。
```

**Edit 后 new_string**：
```python
        """取消 delayed start（不 shutdown 整体 scheduler）。

        场景：启动期检测到 LightRAG 损坏（need_repair=True），
        lifespan 不调 signal_scheduler_ready，但 scheduler.start_delayed
        里的 _ready_event.wait(180) 180s 超时后会强行 start（L103-106）。
        此方法设 _delayed_start_cancelled=True，让 _delayed_start 线程
        在 180s 超时后检查到这个 flag 直接 return，不强行 start。
```

- [ ] Step 1.3：用 Edit 工具替换上述代码

### Step 1.4：语法检查

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m py_compile niu_api/internal/scheduler/scheduler.py && echo "OK"
```
Expected: `OK`（无输出）

- [ ] Step 1.4：运行 py_compile，确认无语法错误

### Step 1.5：commit

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/scheduler/scheduler.py && git commit -m "fix(scheduler): start_delayed 超时 60s→180s，给 lifespan 完整初始化留余量

为配合 signal_scheduler_ready 挪到 lifespan 末尾（见下个 commit），
把 _ready_event.wait 超时从 60s 调到 180s，避免 lifespan L67→末尾
耗时接近 60s 时触发'超时强行 start'漏洞（提交 0df739e0 警告的同型 race）。
同步更新日志文案和 cancel_delayed_start docstring 的 60s→180s。"
```

- [ ] Step 1.5：commit

---

## Task 2：__main__.py 挪 signal_scheduler_ready 到 lifespan 末尾 + 文案同步

**Files:**
- Modify: `niu_api/__main__.py:200-223, 399-403`

### Step 2.1：删除 L216-223 旧 signal 块

修改 `niu_api/__main__.py:216-223`，删除原来的 signal 调用块（保留前面的 db_monitor 启动逻辑 L206-214 不动）：

**Edit 前 old_string**：
```python
    # 6.7.3. Signal scheduler that system is ready（need_repair=True 时不 signal）
    from niu_api.internal.lightrag_manager import should_signal_scheduler_ready
    if should_signal_scheduler_ready(phase1_result):
        from niu_api.internal.scheduler.service import signal_scheduler_ready
        signal_scheduler_ready()
        logger.info("Scheduler system_ready signal sent")
    else:
        logger.warning("[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏）")

    # 7. (Removed) Weekly vector cleanup — vectors.db is deprecated,
```

**Edit 后 new_string**：
```python
    # 6.7.3. Signal scheduler 挪到 lifespan 末尾（见 L8.7），保证所有后台依赖就绪后才 signal。
    #        原位置 L218 在 Phase 1 gate 之后但 L255 之后（LightRAG eager init /
    #        BrainGraph / _SYSTEM_TASKS 等）之前，scheduler sleep 2s 后扫描过期任务
    #        会撞未就绪 runner，导致 user 消息已写 DB 但 runner.chat() 抛异常、任务被标 failed。
    #        挪到末尾后 need_repair=True 分支仍由 should_signal_scheduler_ready gate 控制
    #        （cancel_scheduler_delayed_start_if_corrupt 在 L204 已调，flag 持久，行为一致）。

    # 7. (Removed) Weekly vector cleanup — vectors.db is deprecated,
```

注意：保留 `# 7. (Removed)` 起始注释作为下一段的锚点，避免 old_string 不唯一。

- [ ] Step 2.1：用 Edit 工具替换上述代码

### Step 2.2：更新 L201-202 注释

修改 `niu_api/__main__.py:201-202` 的注释（"60s" → "180s"）：

**Edit 前 old_string**：
```python
    # 6.7.1.1 Phase 1 检测到损坏时取消 scheduler delayed start
    #        补 P1 漏洞：scheduler 60s 超时强行 start 的漏洞（_ready_event.wait(60)）
    #        即使不调 signal_scheduler_ready，scheduler 线程 60s 后也会强行 start
```

**Edit 后 new_string**：
```python
    # 6.7.1.1 Phase 1 检测到损坏时取消 scheduler delayed start
    #        补 P1 漏洞：scheduler 180s 超时强行 start 的漏洞（_ready_event.wait(180)）
    #        即使不调 signal_scheduler_ready，scheduler 线程 180s 后也会强行 start
```

- [ ] Step 2.2：用 Edit 工具替换上述代码

### Step 2.3：在 lifespan 末尾 yield 之前新增 signal 块

修改 `niu_api/__main__.py:399-403`，在 `# 8.6.` 的 try/except 结束之后、`yield` 之前插入新的 signal 块。

定位锚点：L401 是 `_SYSTEM_TASKS` try/except 的最后一行 `logger.warning(f"Failed to ensure system tasks: {e}")`，L402 是空行，L403 是 `    yield`。

**Edit 前 old_string**（包含 L359-403 的尾部，确保唯一匹配）：
```python
        try:
            from niu_api.internal.scheduler import get_store

            ts = get_store()

            # Ensure each system task exists (by name, not cron_expr)
            for task_def in _SYSTEM_TASKS:
                existing = ts.find_task_by_name(task_def["name"])

                if existing is None:
                    # Create new task
                    now = datetime.now()
                    hour = task_def.get("hour", 8)
                    next_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                    if task_def.get("dow") is not None:
                        # Calculate next target weekday
                        days_ahead = task_def["dow"] - now.isoweekday()
                        if days_ahead < 0:
                            days_ahead += 7
                        next_time = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
                        if next_time <= now:
                            next_time += timedelta(days=7)
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

    yield
```

**Edit 后 new_string**（在 `except` 后、`yield` 前插入 signal 块）：
```python
        try:
            from niu_api.internal.scheduler import get_store

            ts = get_store()

            # Ensure each system task exists (by name, not cron_expr)
            for task_def in _SYSTEM_TASKS:
                existing = ts.find_task_by_name(task_def["name"])

                if existing is None:
                    # Create new task
                    now = datetime.now()
                    hour = task_def.get("hour", 8)
                    next_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                    if task_def.get("dow") is not None:
                        # Calculate next target weekday
                        days_ahead = task_def["dow"] - now.isoweekday()
                        if days_ahead < 0:
                            days_ahead += 7
                        next_time = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
                        if next_time <= now:
                            next_time += timedelta(days=7)
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

    # 8.7. Signal scheduler that system is ready（need_repair=True 时不 signal）
    #      必须在所有后台依赖（LightRAG eager init / PipelineWatcher / LightRAGSync /
    #      BrainGraph / create_default_regions / RegionSync / _SYSTEM_TASKS）就绪后才 signal。
    #      原位置 L218（Phase 1 gate 之后、L255 依赖项之前）会触发 race：
    #      scheduler sleep 2s 后扫描过期任务撞未就绪 runner，user 消息已写 DB 但
    #      runner.chat() 抛异常、任务被标 failed（见 commit 2e795521/0df739e0 历史背景）。
    #      need_repair=True 分支由 should_signal_scheduler_ready gate 控制（返回 False 跳过），
    #      cancel_scheduler_delayed_start_if_corrupt 在 L204 已调，flag 持久，行为一致。
    from niu_api.internal.lightrag_manager import should_signal_scheduler_ready
    if should_signal_scheduler_ready(phase1_result):
        from niu_api.internal.scheduler.service import signal_scheduler_ready
        signal_scheduler_ready()
        logger.info("Scheduler system_ready signal sent (after all dependencies ready)")
    else:
        logger.warning("[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏）")

    yield
```

**注意**：这段代码在 `if not _lightrag_corrupt_skip_init:` 块之外（缩进 4 空格，与 `if not _lightrag_corrupt_skip_init:` 同级），保证 need_repair=True 跳过依赖初始化时 signal 块仍会执行（被 `should_signal_scheduler_ready` gate 拦截）。

- [ ] Step 2.3：用 Edit 工具替换上述代码

### Step 2.4：语法检查

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m py_compile niu_api/__main__.py && echo "OK"
```
Expected: `OK`（无输出）

- [ ] Step 2.4：运行 py_compile，确认无语法错误

### Step 2.5：跑现有启动阻断测试

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_startup_block.py -v
```
Expected: 全部通过（测试断言 gate 逻辑和 flag 设置，不断言 signal 位置或 60s 数值）

- [ ] Step 2.5：跑测试，确认通过

### Step 2.6：commit

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/__main__.py && git commit -m "fix(__main__): signal_scheduler_ready 挪到 lifespan 末尾

根治启动时处理过期定时任务的 race condition：原位置 L218（Phase 1 gate 之后、
L255 依赖项之前）会触发 scheduler sleep 2s 后扫描过期任务撞未就绪 runner，
user 消息已写 DB 前端可见但 runner.chat() 抛异常、任务被标 failed。

挪到 L8.7（所有依赖项就绪后）：
- need_repair=False 分支：signal 在 LightRAG/BrainGraph/_SYSTEM_TASKS 全就绪后发出，
  scheduler sleep 2s 后扫描过期任务时 runner 依赖已就绪，根治 race。
- need_repair=True 分支：should_signal_scheduler_ready gate 仍返回 False 跳过 signal，
  cancel_scheduler_delayed_start_if_corrupt 在 L204 已调（flag 持久），行为一致。

历史背景：这是同型 race 第三型（提交 2e795521/0df739e0 解决过的同型症状），
沿用 signal 机制思路把 signal 挪到所有依赖项就绪后，配合 scheduler.py 60s→180s
超时调大防止 lifespan 总耗时接近 60s 触发超时强行 start 漏洞。"
```

- [ ] Step 2.6：commit

---

## Task 3：lightrag_manager.py 和 test 文案同步

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1400, 1451, 1453, 1455, 1458`
- Modify: `tests/test_lightrag_startup_block.py:4, 71, 73`

### Step 3.1：lightrag_manager.py L1400 注释

先用 Read 工具读 `niu_api/internal/lightrag_manager.py:1397-1410` 确认上下文。

**Edit 前 old_string**（L1400-1402 docstring 唯一匹配）：
```python
    损坏时不通知，让 scheduler 60s 超时强行扫描的漏洞被堵住
    （配合 scheduler.cancel_delayed_start 让超时后 _delayed_start 线程
    直接 return，不强行 start）。
```

**Edit 后 new_string**：
```python
    损坏时不通知，让 scheduler 180s 超时强行扫描的漏洞被堵住
    （配合 scheduler.cancel_delayed_start 让超时后 _delayed_start 线程
    直接 return，不强行 start）。
```

- [ ] Step 3.1：用 Edit 工具替换上述代码

### Step 3.2：lightrag_manager.py L1451-1458 docstring

先用 Read 工具读 `niu_api/internal/lightrag_manager.py:1448-1461` 确认上下文。

**Edit 前 old_string**（L1451-1458 docstring 整段，含两处 60s 和 60s+120s）：
```python
    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106），即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动 + 阻塞 120 秒（_CALLBACK_TIMEOUT）。
    虽然此期间 ChatQueue 被 pause 阻塞不会触发 runner.chat，但 scheduler
    线程跑起来后 60s+120s 才结束，期间用户决策/退出流程会被拖延。

    调 scheduler.cancel_delayed_start() 设 _delayed_start_cancelled=True，
    _delayed_start 线程 60s 超时后检查到 flag 直接 return。
```

**Edit 后 new_string**：
```python
    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(180) 180s 超时后
    会强行 start（scheduler.py L103-106），即使不调 signal_scheduler_ready，
    scheduler 线程也会在 180s 后启动 + 阻塞 120 秒（_CALLBACK_TIMEOUT）。
    虽然此期间 ChatQueue 被 pause 阻塞不会触发 runner.chat，但 scheduler
    线程跑起来后 180s+120s 才结束，期间用户决策/退出流程会被拖延。

    调 scheduler.cancel_delayed_start() 设 _delayed_start_cancelled=True，
    _delayed_start 线程 180s 超时后检查到 flag 直接 return。
```

- [ ] Step 3.2：用 Edit 工具替换上述代码

### Step 3.3：test_lightrag_startup_block.py L3-4 docstring

先用 Read 工具读 `tests/test_lightrag_startup_block.py:1-12` 确认上下文。

**Edit 前 old_string**（L3-4 docstring 背景段）：
```python
背景：scheduler/ChatQueue/db_monitor 在 Phase 1 检测到损坏后仍跑，
60s 超时强行扫描触发 journal-agent → ChatQueue → runner.chat 报错。
```

**Edit 后 new_string**：
```python
背景：scheduler/ChatQueue/db_monitor 在 Phase 1 检测到损坏后仍跑，
180s 超时强行扫描触发 journal-agent → ChatQueue → runner.chat 报错。
```

- [ ] Step 3.3：用 Edit 工具替换上述代码

### Step 3.4：test_lightrag_startup_block.py L71-74 docstring

先用 Read 工具读 `tests/test_lightrag_startup_block.py:68-75` 确认上下文。

**Edit 前 old_string**（L71-74 docstring 整段，含两处 60s）：
```python
    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106）。即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动。调 cancel_delayed_start 设
    _delayed_start_cancelled=True，_delayed_start 线程超时后检查 flag 直接 return。
```

**Edit 后 new_string**：
```python
    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(180) 180s 超时后
    会强行 start（scheduler.py L103-106）。即使不调 signal_scheduler_ready，
    scheduler 线程也会在 180s 后启动。调 cancel_delayed_start 设
    _delayed_start_cancelled=True，_delayed_start 线程超时后检查 flag 直接 return。
```

- [ ] Step 3.4：用 Edit 工具替换上述代码

### Step 3.5：语法检查

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m py_compile niu_api/internal/lightrag_manager.py && python -m py_compile tests/test_lightrag_startup_block.py && echo "OK"
```
Expected: `OK`（无输出）

- [ ] Step 3.5：运行 py_compile，确认无语法错误

### Step 3.6：跑测试

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_startup_block.py -v
```
Expected: 全部通过（docstring 修改不影响测试断言）

- [ ] Step 3.6：跑测试，确认通过

### Step 3.7：commit

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/lightrag_manager.py tests/test_lightrag_startup_block.py && git commit -m "docs: 同步 lightrag_manager 和 test docstring 的 60s→180s 文案

配合 scheduler.py 超时调大，同步更新 lightrag_manager.py（should_signal_scheduler_ready
和 cancel_scheduler_delayed_start_if_corrupt docstring）以及
test_lightrag_startup_block.py 文件头和 cancel 测试 docstring 的 60s→180s。
仅文案修改，测试断言不变。"
```

- [ ] Step 3.7：commit

---

## Self-Review 检查清单

执行完所有 Task 后，执行者自检：

- [ ] `grep -n "60s\|60 超时\|wait(60)\|timeout=60\b" niu_api/__main__.py niu_api/internal/scheduler/scheduler.py niu_api/internal/lightrag_manager.py tests/test_lightrag_startup_block.py` —— 应该只有 L1488/L1516/L1526 的 `join timeout=60`（无关项，不涉及 scheduler 超时）和 L253 的 `~250s/档`（描述 LLM 探测最坏耗时，不涉及 scheduler 超时）。scheduler 相关的 60s 应全部改为 180s。
- [ ] `python -m py_compile niu_api/__main__.py niu_api/internal/scheduler/scheduler.py niu_api/internal/lightrag_manager.py tests/test_lightrag_startup_block.py` 全部 OK
- [ ] `python -m pytest tests/test_lightrag_startup_block.py -v` 全部通过
- [ ] `git log --oneline -3` 有 3 个新 commit（scheduler / __main__ / docs）

---

## 真实环境验证（用户执行，不在本计划范围内）

执行者完成代码改动后，由用户执行真实环境验证：

1. 修改一个定时任务的 `scheduled_at` 为 1 分钟前（模拟过期任务）
2. 关闭程序
3. `./niu` 启动程序
4. 观察 `logs/api_stderr.log`：
   - 应看到 "LightRAG instance initialized (eager)" / "Brain graph initialized" / "Created system task" 等初始化完成日志
   - 然后才看到 "Scheduler system_ready signal sent (after all dependencies ready)"
   - 然后才看到 scheduler 扫描过期任务并触发 trigger_callback
   - 不应再看到"任务信息显示在 chat 但任务不执行"的症状
5. 如有 Phase 1 检测到损坏场景，应看到 "Scheduler system_ready signal 跳过（LightRAG 损坏）"，scheduler 不扫描
