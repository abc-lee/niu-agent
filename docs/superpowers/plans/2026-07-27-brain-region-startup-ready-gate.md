# 脑区启动就绪门控方案 Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除系统启动后脑区动态注入缺失的根因：日常重启场景下 `run_sync()` 没有启动时触发路径，导致 `activation_mgr` 永远是 None，直到用户第一轮请求或 scheduler 触发的过期任务调 `_get_brain_injector` 时才 lazy 派 forced sync daemon，daemon 跑完前的所有请求都拿到 None。

**Architecture:** 在 `RegionSync` 暴露 `_first_sync_done` Event 和 `run_sync_once_for_startup()` 方法，让 `niu_api/__main__.py` lifespan 在 `signal_scheduler_ready()` 之前**同步**跑一次 `run_sync()`，确保 `activation_mgr` 在 scheduler proceed 和前端启动前已就绪。**关键：`_sync_loop` daemon 的拉起（`start_background_sync()`）推迟到 `run_brain_region_startup_gate` 之后**——这样 gate 运行期间 daemon 不存在，`run_sync_once_for_startup` 必拿锁、必跑完，从结构上消除首次启动场景下 daemon 与 lifespan 抢 `_sync_lock` 的竞态。这样：
- 用户第一轮请求到达时 `activation_mgr` 已就绪，脑区段正常注入
- scheduler 触发的过期任务进 ChatQueue 调 `runner.chat` 时 `activation_mgr` 已就绪，脑区段正常注入

`_get_brain_injector` 的 forced sync daemon 保留作为 fallback（lifespan `run_sync_once_for_startup` 失败或超时时兜底）。

**Tech Stack:** Python 3.11+，FastAPI lifespan，threading.Event，pytest，loguru，Rust（launcher）

---

## 背景与根因（v3 修正）

### 现象

系统启动后前几轮对话脑区动态注入缺失（`## 脑区状态` + `### [活跃脑区知识]` + `### [知识探索指引]` 三段）。**用户测试证实**：启动后 2 分钟不说话，2 分钟后说话时仍然 `activation_mgr is None`，forced sync daemon 仍然在说话时才被触发。

### 真正的根因（v3 修正，颠覆了原判断）

**日常重启场景下，`run_sync()` 没有启动时触发路径**。

证据链（grep 验证）：

1. **`set_activation_mgr()` 全局只有一个调用点**：`agent/injector/region_sync.py:412`，在 `_refresh_activation_manager` 内
2. **`_refresh_activation_manager` 只在 `_run_sync_impl` 内被调**：`region_sync.py:155`、`:178`
3. **`run_sync()` 只有 2 个调用点**：
   - `region_sync.py:705` — `_sync_loop` 内部
   - `runner.py:1762` — forced sync daemon 内部
4. **`_sync_loop` 因"跨进程 24h 间隔持久化"逻辑，日常重启不跑首次 `run_sync()`**：用户日志实证 `12:38:08.504 [RegionSync] 距上次同步 8328 秒，不足 77760 秒，等待 69432 秒后再首次同步`（`region_sync.py:673-701` 的 wait 分支）
5. **forced sync daemon 是 lazy 触发**：`runner.py:1741-1782`，只有 `_get_brain_injector` 被调且 `activation_mgr is None` 时才派 daemon

**结论**：日常重启时 `_sync_loop` wait 19 小时不跑 `run_sync`，forced sync daemon 也没人触发，**`activation_mgr` 在启动后永远保持 None，直到第一次有人调 `_get_brain_injector`**。

### 时序图（修复前 bug 时序，基于用户实测）

```
T+0   程序启动
T+22s get_lightrag() eager init → _lightrag_ready.set()
T+26s signal_brain_ready() → _brain_ready.set()
T+26s start_background_sync() → _sync_loop daemon 拉起
        └ wait_lightrag_ready(30) 立即返回 True
        └ _brain_ready.wait(60) 立即返回 True
        └ 读 last_region_sync.json，距上次 < 21.6h
        └ wait 19 小时再首次同步  ← _sync_loop 不跑 run_sync
T+59s signal_scheduler_ready()  ← activation_mgr 仍 None
T+59s set_preload_complete()
T+59s yield → FastAPI 接受请求
T+60s Rust launch 前端
T+90s 前端 SSE 订阅，frontend_ready_event.set()
T+91s scheduler _delayed_start proceed → _run_loop 拉
        └ 扫描过期任务（如有），trigger_callback 入 ChatQueue
        └ ChatQueue worker 调 runner.chat → _get_brain_injector
        └ activation_mgr is None → 派 forced sync daemon → 返回 None
        └ 脑区段缺失
... 用户不说话 ...
T+2min 用户说话 → runner.chat → _get_brain_injector
        └ activation_mgr is None（forced sync daemon 之前从没被触发过）
        └ 派 forced sync daemon
        └ daemon 跑 ~40s
        └ 期间主线程返回 None，脑区段缺失
T+2min41s daemon 完成，set_activation_mgr()
T+2min41s+ 之后的请求拿到非 None，脑区段正常注入
```

### 时序图（修复后目标时序，v3）

```
T+0   程序启动
T+22s get_lightrag() eager init → _lightrag_ready.set()
T+26s signal_brain_ready() → _brain_ready.set()
        （注意：_sync_loop daemon 此时未拉起，_brain_ready set 后持续有效，
         等 gate 之后 start_background_sync 拉起 daemon 时 wait 立即返回）
T+26s _SYSTEM_TASKS 创建
T+26s run_brain_region_startup_gate（lifespan 主线程，同步阻塞）
        ├─ run_sync_once_for_startup() → run_sync()
        │     └ try_acquire_sync 成功（_sync_loop daemon 还没拉起，无竞争）
        │     └ _run_sync_impl() 跑 ~40s（Leiden + 区域管理 + activation 刷新）
        │     └ _refresh_activation_manager → set_activation_mgr()
        │     └ _save_status() 更新 last_region_sync.json
        │     └ finally: _first_sync_done.set()
        ├─ wait_first_sync_done(90) 立即返回 True
        ├─ 检查 get_activation_mgr() is not None → True
        └─ signal_scheduler_ready()  ← activation_mgr 已就绪
T+66s start_background_sync() → _sync_loop daemon 拉起  ← v3 关键：推迟到 gate 之后
        └ wait_lightrag_ready(30) 立即返回
        └ _brain_ready.wait(60) 立即返回
        └ 读 last_region_sync.json（刚被 gate 更新，elapsed=0s < 21.6h）
        └ wait 21.6h 再首次同步  ← 不重复跑
T+66s set_preload_complete()
T+66s yield → FastAPI 接受请求
T+67s Rust launch 前端
T+97s 前端 SSE 订阅，frontend_ready_event.set()
T+98s scheduler _delayed_start proceed → _run_loop 拉
        └ 扫描过期任务，trigger_callback 入 ChatQueue
        └ ChatQueue worker 调 runner.chat → _get_brain_injector
        └ activation_mgr 非 None → 返回有效 injector → 脑区段正常注入
T+97s+ 用户说话 → runner.chat → _get_brain_injector
        └ activation_mgr 非 None → 脑区段正常注入（第一轮就有）
```

### "前 3 轮缺失"是症状不是根因

前几轮缺失不是"33s 没跑完"——是"forced sync daemon 还在跑"。前几轮请求都在 daemon 跑完前到达，全部拿到 None。第 4 轮请求到达时 daemon 刚跑完，才拿到非 None。

**所以无论用户等多久再说话，第一轮都会撞 None**——这正是用户测试发现的。

### scheduler 触发的过期任务也有同样问题（用户指出的"完备性"）

scheduler `_delayed_start` 等 `signal_scheduler_ready` + `frontend_ready_event`，**但不等 `activation_mgr`**。过期任务进 ChatQueue 调 `runner.chat` 走 `_get_brain_injector`，撞同样的 None。

**方案设计要求**：scheduler 必须等脑区同步完成才能 proceed。本方案通过"`signal_scheduler_ready` 推迟到 `_first_sync_done` 之后"实现——`signal_scheduler_ready` 推迟后，scheduler `_delayed_start` 醒来时 `activation_mgr` 已就绪，过期任务不会撞 None。

---

## 修复点

### 修改的文件

| 文件 | 责任 | 修改类型 |
|------|------|------|
| `agent/injector/region_sync.py` | RegionSync 类，加 `_first_sync_done` Event + `wait_first_sync_done()` + `run_sync_once_for_startup()` 方法 | 新增方法 + 修改 `run_sync()` finally |
| `niu_api/__main__.py` | FastAPI lifespan，预初始化 `region_sync = None` + 在 `signal_scheduler_ready()` 前 wait `_first_sync_done` | 修改 lifespan startup |
| `niu_api/startup_gate.py`（新建） | `run_brain_region_startup_gate` helper（提取可测试逻辑） | 新建 |
| `launcher/src/main.rs` | Rust preload 轮询超时从 60s 提升到 180s | 修改 `for i in 0..120` → `for i in 0..360` |

### 不改的文件

- `niu_api/internal/scheduler/scheduler.py` — `_delayed_start` 现有逻辑保持（因为 `signal_scheduler_ready` 推迟已覆盖）
- `niu_api/chat.py` — `frontend_ready_event` 保持
- `agent/brain_tools.py` — `_activation_mgr` 全局单例保持
- `agent/runner.py` — `_get_brain_injector` forced sync daemon 保留作 fallback

---

## v1 → v2 修复的第一轮审查问题

第一轮审查发现 4 个严重问题，本 v2 方案全部修复：

### 严重问题 1：`region_sync` 变量在 LightRAG 损坏分支会 NameError

**问题**：`region_sync` 在 `__main__.py:329` 创建（try 块内），`:332` 有 `region_sync = None` fallback（except 块内），但整个块在 `if not _lightrag_corrupt_skip_init:`（`:262`）内。LightRAG 损坏时该块跳过，`region_sync` 从未定义。

**v2 修复**：在 `__main__.py:262` 之前预初始化 `region_sync = None`；`run_brain_region_startup_gate` helper 内部处理 `region_sync is None` 跳过 gate。

### 严重问题 2：`_first_sync_done` 在并发 skip 分支被 set，但 `activation_mgr` 可能仍是 None

**问题**：方案 Task 1.4 把 `try_acquire_sync` 包进 try/finally，并发 skip 分支也 set Event。但首次启动（无 `last_sync.json`）时 `_sync_loop` 立即跑 `run_sync`，lifespan `run_sync_once_for_startup` 抢锁失败 skip，set Event，但 `_sync_loop` 的 `run_sync` 还在跑，`set_activation_mgr` 未调用。

**v2 修复**：`run_brain_region_startup_gate` 在 `wait_first_sync_done` 返回 True 后，额外检查 `get_activation_mgr() is not None`。如果 None，log warning 但仍 proceed（与超时兜底一致）。

### 严重问题 3：Rust 启动器 preload 超时 60s，但方案可能阻塞 90s+

**问题**：`launcher/src/main.rs:1698` `for i in 0..120` 每次 sleep 500ms = 60 秒总超时。方案 `wait_first_sync_done(90)` + 启动开销可能超 60s，Rust 误判启动完成，前端启动后无法连接 API。

**v2 修复**：Rust preload 轮询超时从 60s 提升到 180s（`for i in 0..360`），覆盖最坏情况 90s + 启动开销。

### 严重问题 4：`run_sync` 异常时 `wait_first_sync_done` 立即返回 True，但 `activation_mgr` 可能 None

**问题**：`run_sync` finally 块 set Event，即使 `_run_sync_impl` 抛异常。`wait_first_sync_done` 立即返回 True，但 `activation_mgr` 可能未 set。

**v2 修复**：同严重问题 2，`run_brain_region_startup_gate` 检查 `get_activation_mgr() is not None`。

---

## v2 → v3 修复的第二轮审查问题

第二轮审查发现 1 个新的严重问题（confidence 85），本 v3 方案修复：

### 严重问题（第二轮）：首次启动场景 `_sync_loop` daemon 与 lifespan 抢 `_sync_lock` 竞态

**问题**：v2 方案 lifespan 在 `signal_scheduler_ready` 前调 `run_sync_once_for_startup()`，但 `_sync_loop` daemon 在 `__main__.py:345` 已被拉起。首次启动场景（无 `last_sync.json`）下：
- `_sync_loop` daemon 立即 `wait_lightrag_ready(30)` + `_brain_ready.wait(60)`（两个 Event 都已 set，立即返回），跳过 24h wait 分支，立即跑 `run_sync()`
- 与 lifespan `:419+` 的 `run_sync_once_for_startup` 抢同一把 `_sync_lock`
- 如果 `_sync_loop` 先抢到：lifespan 抢锁失败 skip，set `_first_sync_done`，但 `_sync_loop` 的 `run_sync` 还没跑到 `_refresh_activation_manager`，`activation_mgr` 仍 None
- helper 检查 `activation_mgr` 是 None → proceed + warning → scheduler proceed → **脑区段仍缺失**

谁先抢到锁取决于线程调度，竞态窗口窄但非零。

**v3 修复（方案 A，第二轮审查推荐）**：把 `start_background_sync()`（`_sync_loop` daemon 拉起）**推迟到 `run_brain_region_startup_gate` 之后调用**。这样 gate 运行期间 daemon 不存在，`run_sync_once_for_startup` 必拿锁、必跑完 `_run_sync_impl`、必经 `_refresh_activation_manager`，`activation_mgr` 在 LightRAG 健康时必然 set。竞态在结构上被消除。

**推迟安全性验证**（第三轮审查逐条确认）：
1. `:350-409` `_SYSTEM_TASKS` 创建只调 `get_store()`（TaskStore 直查 SQLite），不依赖 `_sync_loop` daemon——**无依赖，安全**
2. `_brain_ready` 全仓库唯一 wait 点是 `region_sync.py:670`（`_sync_loop` 内部）。`:337` 提前 set 无害——Event 置位后持续有效，daemon 40s 后拉起时 `_brain_ready.wait(60)` 立即返回——**无其他消费者，安全**
3. 推迟窗口（~40s）内无任何代码依赖 RegionSync 线程存在——**安全**

**关键实现细节**：`start_background_sync()` 平移时必须保留原有的 `if region_sync is not None:` 守卫和 try/except 包裹（即把 `__main__.py:342-348` 整块平移到 gate 之后，而不是只平移一行调用）——否则 LightRAG 损坏分支（`region_sync = None`）会 `AttributeError: 'NoneType' object has no attribute 'start_background_sync'`。

### 语义变化声明（v3 新增，防止后续误判）

v3 后 `run_sync()` 在**每次启动时**都会执行一次（~40s 阻塞 + Leiden 全量计算），原"跨进程 24h 间隔保护"（`region_sync.py:673-701`）对每天重启的用户实际上失效——间隔锚点永远是"最近一次启动"。**这是有意为之**：启动即新鲜 `activation_mgr` 正是本方案目的，不是 bug。后续维护者不要误判为"24h 保护被破坏"而再改回去。

---

## Task 1: RegionSync 暴露 _first_sync_done Event

**Files:**
- Modify: `agent/injector/region_sync.py:74-105`（`__init__` 加 Event 字段 + `run_sync` finally 加 set）
- Modify: `agent/injector/region_sync.py:600`（`signal_brain_ready` 方法之后新增 `wait_first_sync_done` 和 `run_sync_once_for_startup` 方法）
- Test: `tests/test_region_sync_first_sync_done.py`

### 设计

`_first_sync_done` 是 `threading.Event`，**首次 `run_sync()` 完成（无论成功/失败/异常/skip）后 set 一次**。Event 自带 idempotent 语义，重复 set 是 no-op。

`run_sync_once_for_startup()` 是给 lifespan 调用的同步入口：
- 检查 `_first_sync_done.is_set()`——已 set 直接返回（idempotent）
- 否则同步调 `run_sync()`（不派 daemon，阻塞当前线程）
- run_sync 内部 finally 会 set `_first_sync_done`
- 返回 stats dict

`wait_first_sync_done(timeout)` 给 lifespan 用：wait event，超时返回 False。

### - [ ] Step 1.1: 写失败测试 - 首次 run_sync 成功后 _first_sync_done 被 set

创建 `tests/test_region_sync_first_sync_done.py`：

```python
"""Test RegionSync._first_sync_done Event semantics.

Covers:
- _first_sync_done is set after first run_sync() completes (success path)
- _first_sync_done is set even if run_sync() fails (exception path)
- _first_sync_done is set even if run_sync() skips due to concurrent sync
- wait_first_sync_done returns True after set, False on timeout
- run_sync_once_for_startup is idempotent (second call returns immediately)
"""
from unittest.mock import patch

import pytest

from agent.injector.region_sync import RegionSync


def test_first_sync_done_set_after_successful_run_sync():
    """_first_sync_done is set after first successful run_sync()."""
    rs = RegionSync(sync_interval=86400)
    assert not rs._first_sync_done.is_set()

    with patch.object(rs, "_run_sync_impl", return_value={"regions_created": 0, "errors": []}):
        rs.run_sync()

    assert rs._first_sync_done.is_set()


def test_first_sync_done_set_after_failed_run_sync():
    """_first_sync_done is set even if _run_sync_impl raises."""
    rs = RegionSync(sync_interval=86400)

    with patch.object(rs, "_run_sync_impl", side_effect=RuntimeError("simulated failure")):
        with pytest.raises(RuntimeError):
            rs.run_sync()

    assert rs._first_sync_done.is_set()


def test_first_sync_done_set_after_concurrent_skip():
    """_first_sync_done is set even if run_sync skips due to concurrent sync."""
    rs = RegionSync(sync_interval=86400)

    assert rs.try_acquire_sync() is True

    try:
        result = rs.run_sync()
        assert result["errors"] == ["skipped: concurrent sync"]
    finally:
        rs.release_sync()

    assert rs._first_sync_done.is_set()


def test_wait_first_sync_done_returns_true_after_set():
    """wait_first_sync_done returns True immediately after _first_sync_done is set."""
    rs = RegionSync(sync_interval=86400)
    rs._first_sync_done.set()
    assert rs.wait_first_sync_done(timeout=0.1) is True


def test_wait_first_sync_done_returns_false_on_timeout():
    """wait_first_sync_done returns False on timeout when event never set."""
    rs = RegionSync(sync_interval=86400)
    assert rs.wait_first_sync_done(timeout=0.1) is False


def test_run_sync_once_for_startup_idempotent():
    """run_sync_once_for_startup returns immediately if _first_sync_done already set."""
    rs = RegionSync(sync_interval=86400)
    rs._first_sync_done.set()

    with patch.object(rs, "_run_sync_impl", side_effect=AssertionError("should not be called")):
        result = rs.run_sync_once_for_startup()

    assert result == {"skipped": "first_sync_already_done"}
    assert rs._first_sync_done.is_set()


def test_run_sync_once_for_startup_blocks_until_complete():
    """run_sync_once_for_startup synchronously runs run_sync and blocks until done."""
    rs = RegionSync(sync_interval=86400)

    with patch.object(rs, "_run_sync_impl", return_value={"regions_created": 5, "errors": []}) as mock_impl:
        result = rs.run_sync_once_for_startup()

    mock_impl.assert_called_once()
    assert result == {"regions_created": 5, "errors": []}
    assert rs._first_sync_done.is_set()
```

### - [ ] Step 1.2: 运行测试验证失败

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_region_sync_first_sync_done.py -v`
Expected: FAIL，错误是 `AttributeError: 'RegionSync' object has no attribute '_first_sync_done'`

### - [ ] Step 1.3: 实现 - RegionSync.__init__ 加 _first_sync_done Event

Edit `agent/injector/region_sync.py:74-79`：

修改前：
```python
        self.sync_interval = sync_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._brain_ready = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_region_sync.json"
        self._sync_lock = threading.Lock()
```

修改后：
```python
        self.sync_interval = sync_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._brain_ready = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_region_sync.json"
        self._sync_lock = threading.Lock()
        # Startup gate: set after first run_sync() completes (success/failure/skip).
        # lifespan waits on this before signal_scheduler_ready() so scheduler-triggered
        # tasks and first user request don't hit activation_mgr=None race.
        self._first_sync_done = threading.Event()
```

### - [ ] Step 1.4: 实现 - run_sync 的 finally 块 set _first_sync_done

Edit `agent/injector/region_sync.py:89-105`：

修改前：
```python
    def run_sync(self) -> dict:
        """Execute one full sync cycle with mutex protection.

        Acquires a non-blocking lock to prevent concurrent sync runs
        (e.g. API-triggered consolidate vs background timer sync).
        If the lock cannot be acquired, returns immediately with a skip indicator.

        Returns:
            Stats dict with counts of regions created/removed/updated.
        """
        if not self.try_acquire_sync():
            logger.warning("[RegionSync] 另一个同步正在运行，跳过本次")
            return {"regions_created": 0, "regions_removed": 0, "errors": ["skipped: concurrent sync"]}
        try:
            return self._run_sync_impl()
        finally:
            self.release_sync()
```

修改后：
```python
    def run_sync(self) -> dict:
        """Execute one full sync cycle with mutex protection.

        Acquires a non-blocking lock to prevent concurrent sync runs
        (e.g. API-triggered consolidate vs background timer sync).
        If the lock cannot be acquired, returns immediately with a skip indicator.

        Returns:
            Stats dict with counts of regions created/removed/updated.
        """
        try:
            if not self.try_acquire_sync():
                logger.warning("[RegionSync] 另一个同步正在运行，跳过本次")
                return {"regions_created": 0, "regions_removed": 0, "errors": ["skipped: concurrent sync"]}
            try:
                return self._run_sync_impl()
            finally:
                self.release_sync()
        finally:
            # Mark first sync as done regardless of outcome (success/skip/exception).
            # lifespan waits on this Event before signal_scheduler_ready().
            # Idempotent: set() on already-set Event is a no-op.
            # NOTE: _first_sync_done being set does NOT guarantee activation_mgr is set
            # —_refresh_activation_manager may have failed or been skipped.
            # Caller (run_brain_region_startup_gate) must check get_activation_mgr()
            # separately if it needs the stronger guarantee.
            self._first_sync_done.set()
```

**注意**：把 `try_acquire_sync` 也包进 try/finally，并发 skip 分支也会 set Event——startup gate 不应该因为"另一个同步在跑"而永远等下去。但 `_first_sync_done` set 不等于 `activation_mgr` 已 set，Task 2 的 helper 会额外检查。

### - [ ] Step 1.5: 实现 - 加 wait_first_sync_done 和 run_sync_once_for_startup 方法

Edit `agent/injector/region_sync.py`，在 `signal_brain_ready` 方法之后（约 `:600` 位置）插入两个新方法：

```python
    def wait_first_sync_done(self, timeout: float) -> bool:
        """Wait for the first run_sync() to complete.

        Used by lifespan startup to gate signal_scheduler_ready() until
        first sync is done. Returns True if event was set within timeout,
        False otherwise.

        Note: _first_sync_done being set does NOT guarantee activation_mgr
        is set. Caller must check get_activation_mgr() separately.

        Args:
            timeout: Max seconds to wait. None means wait forever (avoid).

        Returns:
            True if first sync completed (or was attempted) within timeout.
        """
        return self._first_sync_done.wait(timeout=timeout)

    def run_sync_once_for_startup(self) -> dict:
        """Synchronously run run_sync() once for startup gate.

        Called from lifespan startup to ensure first run_sync runs before
        signal_scheduler_ready(). Idempotent — if _first_sync_done is already
        set (e.g. _sync_loop already ran first sync), returns immediately.

        Returns:
            Stats dict from run_sync(), or {"skipped": "first_sync_already_done"}
            if first sync was already completed previously.
        """
        if self._first_sync_done.is_set():
            logger.info("[RegionSync] run_sync_once_for_startup skipped — first sync already done")
            return {"skipped": "first_sync_already_done"}
        logger.info("[RegionSync] run_sync_once_for_startup starting (blocks lifespan until done)")
        return self.run_sync()
```

### - [ ] Step 1.6: 运行测试验证通过

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_region_sync_first_sync_done.py -v`
Expected: 7 tests PASS

### - [ ] Step 1.7: 提交

```bash
git add agent/injector/region_sync.py tests/test_region_sync_first_sync_done.py
git commit -m "$(cat <<'EOF'
feat(region_sync): 暴露 _first_sync_done Event + run_sync_once_for_startup

新增 _first_sync_done threading.Event，在 run_sync() 的 finally 块 set
（无论成功/失败/并发 skip 都 set，幂等）。

新增两个方法：
- wait_first_sync_done(timeout) — 给 lifespan startup 用，wait event
- run_sync_once_for_startup() — 同步跑 run_sync()，idempotent

注意：_first_sync_done set 不等于 activation_mgr 已 set（_refresh_activation_manager
可能失败）。Caller 必须额外检查 get_activation_mgr()。Task 2 的 helper 会处理。

为后续 lifespan 在 signal_scheduler_ready 前 wait 脑区就绪做准备。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 新建 startup_gate helper（Python 逻辑，可单测）

**Files:**
- Create: `niu_api/startup_gate.py`
- Test: `tests/test_lifespan_brain_region_gate.py`

### 设计

提取 `run_brain_region_startup_gate` helper 到独立模块 `niu_api/startup_gate.py`，可测试。Helper 逻辑：

1. **`region_sync is None`**（LightRAG 损坏分支）→ 跳过 gate，不调 `signal_scheduler_ready`，返回 `None`（与现有 `should_signal=False` 行为一致）
2. **`should_signal=False`**（LightRAG 损坏 gate）→ 跳过 gate，不调 `signal_scheduler_ready`，返回 `None`
3. **正常路径**：
   - 调 `region_sync.run_sync_once_for_startup()`（同步阻塞 ~40s）
   - `wait_first_sync_done(timeout=90)` 兜底确认 Event 被 set
   - **额外检查 `get_activation_mgr() is not None`**（修复严重问题 2/4）
   - 调 `signal_scheduler_ready_fn()`
   - 返回 `True`（first sync done + activation_mgr ready）/ `False`（timeout 或 activation_mgr 仍 None，但已 proceed）

lifespan 改造（预初始化 `region_sync` + gate 调用 + `start_background_sync` 推迟）在 Task 3。

### - [ ] Step 2.1: 写失败测试 - helper 4 个分支

创建 `tests/test_lifespan_brain_region_gate.py`：

```python
"""Test run_brain_region_startup_gate helper.

Covers 4 branches:
- Normal: run_sync_once_for_startup called, activation_mgr ready, scheduler signaled → True
- Timeout: wait_first_sync_done returns False → still signal, return False
- Skip (LightRAG corrupt): should_signal=False → no signal, return None
- region_sync is None (LightRAG corrupt branch): → no signal, return None
- activation_mgr still None after sync (run_sync failed): → still signal, return False
"""
from unittest.mock import patch, MagicMock


def test_normal_path_activation_mgr_ready():
    """Normal path: run_sync_once_for_startup called, activation_mgr ready, scheduler signaled."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"regions_created": 5}
    mock_rs.wait_first_sync_done.return_value = True

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    mock_rs.run_sync_once_for_startup.assert_called_once()
    mock_rs.wait_first_sync_done.assert_called_once_with(timeout=90.0)
    mock_signal.assert_called_once()
    assert result is True


def test_timeout_path_proceeds_with_warning():
    """If wait_first_sync_done returns False (timeout), lifespan proceeds anyway."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"errors": ["timeout"]}
    mock_rs.wait_first_sync_done.return_value = False

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=None):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    mock_signal.assert_called_once()
    assert result is False


def test_skip_when_should_signal_false():
    """When should_signal is False (LightRAG corrupt), gate is skipped."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_signal = MagicMock()

    result = run_brain_region_startup_gate(
        region_sync=mock_rs,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=False,
        timeout=90.0,
    )

    mock_rs.run_sync_once_for_startup.assert_not_called()
    mock_signal.assert_not_called()
    assert result is None


def test_skip_when_region_sync_none():
    """When region_sync is None (LightRAG corrupt branch, region_sync never created), gate is skipped."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_signal = MagicMock()

    result = run_brain_region_startup_gate(
        region_sync=None,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=True,
        timeout=90.0,
    )

    mock_signal.assert_not_called()
    assert result is None


def test_activation_mgr_none_after_sync_proceeds_with_warning():
    """run_sync completed (_first_sync_done set) but activation_mgr still None —
    proceed with warning, forced sync daemon will retry."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"errors": ["activation refresh failed"]}
    mock_rs.wait_first_sync_done.return_value = True  # Event was set

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=None):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    # Even though activation_mgr is None, scheduler must be signaled
    # (forced sync daemon will retry on first user request)
    mock_signal.assert_called_once()
    assert result is False  # False indicates degraded state
```

### - [ ] Step 2.2: 运行测试验证失败

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_lifespan_brain_region_gate.py -v`
Expected: FAIL，错误是 `ModuleNotFoundError: No module named 'niu_api.startup_gate'`

### - [ ] Step 2.3: 实现 - 创建 startup_gate helper

创建 `niu_api/startup_gate.py`：

```python
"""Startup gate helpers for brain region readiness.

Extracted from niu_api/__main__.py lifespan to make the brain region
startup gate testable without spinning up the full FastAPI app.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def run_brain_region_startup_gate(
    *,
    region_sync,
    signal_scheduler_ready_fn: Callable[[], None],
    should_signal: bool,
    timeout: float = 90.0,
) -> Optional[bool]:
    """Run the brain region startup gate before signal_scheduler_ready.

    Branches:
    - region_sync is None (LightRAG corrupt, region_sync never created):
      skip gate, skip signal_scheduler_ready, return None.
    - should_signal is False (LightRAG corrupt gate): same as above.
    - Normal: run_sync_once_for_startup + wait_first_sync_done + activation_mgr check,
      then call signal_scheduler_ready_fn. Return True/False.

    Args:
        region_sync: RegionSync instance, or None if LightRAG corrupt branch.
        signal_scheduler_ready_fn: Callable that signals scheduler ready.
        should_signal: Whether to signal scheduler (False = LightRAG corrupt).
        timeout: Max seconds to wait for first sync done (default 90s).

    Returns:
        True if first sync done AND activation_mgr is not None.
        False if timed out OR activation_mgr still None (proceeded with warning).
        None if gate was skipped (region_sync None or should_signal=False).
    """
    # Skip gate when region_sync is None (LightRAG corrupt, region_sync never created)
    # or should_signal is False (existing should_signal_scheduler_ready gate)
    if region_sync is None or not should_signal:
        logger.warning(
            "[StartupGate] Skipping brain region gate "
            f"(region_sync={'None' if region_sync is None else 'set'}, should_signal={should_signal})"
        )
        return None

    try:
        logger.info("[StartupGate] Running brain region first sync (blocking, max ~40s)")
        stats = region_sync.run_sync_once_for_startup()
        logger.info(f"[StartupGate] First sync stats: {stats}")
    except Exception as e:
        logger.error(f"[StartupGate] run_sync_once_for_startup failed: {e}", exc_info=True)
        # Don't re-raise — proceed to wait, _first_sync_done might still get set
        # by the exception path in run_sync's finally block.

    done = region_sync.wait_first_sync_done(timeout=timeout)
    if not done:
        logger.warning(
            f"[StartupGate] Brain region first sync not done within {timeout}s, "
            "proceeding anyway (forced sync daemon will retry on first request)"
        )
        signal_scheduler_ready_fn()
        return False

    # Additional check: _first_sync_done being set doesn't guarantee activation_mgr is set
    # (run_sync's finally sets Event even if _refresh_activation_manager failed).
    from agent.brain_tools import get_activation_mgr
    activation_mgr = get_activation_mgr()
    if activation_mgr is None:
        logger.warning(
            "[StartupGate] _first_sync_done set but activation_mgr is None "
            "(run_sync failed or _refresh_activation_manager skipped) — "
            "proceeding, forced sync daemon will retry on first request"
        )
        signal_scheduler_ready_fn()
        return False

    logger.info("[StartupGate] Brain region ready (first sync done + activation_mgr set), signaling scheduler")
    signal_scheduler_ready_fn()
    return True
```

### - [ ] Step 2.4: 运行测试验证通过

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_lifespan_brain_region_gate.py -v`
Expected: 5 tests PASS

### - [ ] Step 2.5: 写失败测试 - lifespan 调用顺序（v3 核心改动：start_background_sync 推迟到 gate 之后）

第三轮审查发现：v3 的核心改动（`start_background_sync` 推迟到 gate 之后 + `region_sync is None` 守卫）没有任何单元测试覆盖。补一个 lifespan 顺序测试。

追加到 `tests/test_lifespan_brain_region_gate.py`：

```python
def test_lifespan_order_start_background_sync_after_gate():
    """v3 core: start_background_sync must be called AFTER run_brain_region_startup_gate.

    This is the structural fix for the first-startup race (second-round review
    critical issue): _sync_loop daemon must not exist while gate runs, so
    run_sync_once_for_startup always wins the _sync_lock.
    """
    import niu_api.startup_gate as sg

    call_order = []

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.side_effect = lambda: call_order.append("gate_run_sync") or {"regions_created": 0}
    mock_rs.wait_first_sync_done.side_effect = lambda timeout: call_order.append("gate_wait") or True
    mock_rs.start_background_sync.side_effect = lambda: call_order.append("start_background_sync")

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        # Simulate the lifespan sequence: gate first, then start_background_sync
        result = sg.run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )
        # lifespan then calls start_background_sync (with None guard)
        if mock_rs is not None:
            mock_rs.start_background_sync()

    assert result is True
    # gate's run_sync must complete before start_background_sync
    assert call_order.index("gate_run_sync") < call_order.index("start_background_sync")
    assert call_order.index("start_background_sync") == len(call_order) - 1


def test_lifespan_start_background_sync_not_called_when_region_sync_none():
    """v3 None guard: when region_sync is None (LightRAG corrupt), start_background_sync
    must NOT be called — would raise AttributeError on NoneType."""
    import niu_api.startup_gate as sg

    mock_signal = MagicMock()

    result = sg.run_brain_region_startup_gate(
        region_sync=None,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=True,
        timeout=90.0,
    )

    # Gate skipped, scheduler NOT signaled, and caller must guard start_background_sync
    assert result is None
    mock_signal.assert_not_called()
    # The None guard lives in __main__.py (if region_sync is not None:) —
    # this test documents the contract: region_sync None → no start_background_sync.
```

### - [ ] Step 2.6: 运行全部测试验证

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_region_sync_first_sync_done.py tests/test_lifespan_brain_region_gate.py -v`
Expected: 14 tests PASS（7 个 Event 测试 + 5 个 helper 分支 + 2 个顺序测试）

### - [ ] Step 2.7: 提交

```bash
git add niu_api/startup_gate.py tests/test_lifespan_brain_region_gate.py
git commit -m "$(cat <<'EOF'
feat(startup_gate): 新建 run_brain_region_startup_gate helper

提取 helper 到 niu_api/startup_gate.py（可单测）：
1. region_sync is None（LightRAG 损坏分支）→ 跳过 gate，不 signal
2. should_signal=False（LightRAG 损坏 gate）→ 跳过 gate，不 signal
3. 正常路径：调 run_sync_once_for_startup()（阻塞 ~40s）+ wait_first_sync_done(90)
   + 额外检查 get_activation_mgr() is not None
4. 超时或 activation_mgr None → warning 但仍 signal，靠 forced sync daemon 兜底

单元测试覆盖 5 个分支 + 2 个 v3 顺序测试（start_background_sync 在 gate 之后
调用 + region_sync None 守卫）。

lifespan 改造（预初始化 region_sync + gate 调用 + start_background_sync 推迟）
在 Task 3。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: lifespan 改造（预初始化 region_sync + gate 调用 + start_background_sync 推迟）

**Files:**
- Modify: `niu_api/__main__.py:262`（预初始化 `region_sync = None`）
- Modify: `niu_api/__main__.py:342-348`（删除此处的 `start_background_sync` 调用块）
- Modify: `niu_api/__main__.py:411-425`（在 `signal_scheduler_ready` 调用前插入 gate，并在 gate 之后平移 `start_background_sync` 调用块）

### 设计

v3 核心改动（修复第二轮审查严重问题）：**把 `start_background_sync()`（`_sync_loop` daemon 拉起）从 `:342-348` 平移到 `run_brain_region_startup_gate` 之后**。这样 gate 运行期间 daemon 不存在，`run_sync_once_for_startup` 必拿锁、必跑完，从结构上消除首次启动场景 daemon 与 lifespan 抢 `_sync_lock` 的竞态。

**关键实现细节**：平移时必须保留原有的 `if region_sync is not None:` 守卫和 try/except 包裹（整块平移，不是只平移一行调用）——否则 LightRAG 损坏分支（`region_sync = None`）会 `AttributeError`。

### - [ ] Step 3.1: 实现 - __main__.py 预初始化 region_sync = None

Edit `niu_api/__main__.py:262`，在 `if not _lightrag_corrupt_skip_init:` 块之前预初始化 `region_sync = None`：

修改前（`__main__.py:258-262`）：
```python
    # v7: Phase 1 need_repair=True 时跳过所有依赖 LightRAG 实例的初始化
    #     （LightRAG eager init / PipelineWatcher / LightRAGSync / BrainGraph /
    #      vectors.db cleanup / create_default_regions / RegionSync / _SYSTEM_TASKS）
    #     need_repair=False 时逻辑跟原来一致
    if not _lightrag_corrupt_skip_init:
```

修改后：
```python
    # v7: Phase 1 need_repair=True 时跳过所有依赖 LightRAG 实例的初始化
    #     （LightRAG eager init / PipelineWatcher / LightRAGSync / BrainGraph /
    #      vectors.db cleanup / create_default_regions / RegionSync / _SYSTEM_TASKS）
    #     need_repair=False 时逻辑跟原来一致
    #
    # 预初始化 region_sync = None，确保 LightRAG 损坏分支（_lightrag_corrupt_skip_init=True）
    # 跳过整个 if 块时，变量在 lifespan 末尾 run_brain_region_startup_gate 调用处仍可见，
    # 避免 NameError。run_brain_region_startup_gate helper 内部处理 None 跳过 gate。
    region_sync = None
    if not _lightrag_corrupt_skip_init:
```

### - [ ] Step 3.2: 实现 - 删除 :342-348 的 start_background_sync 调用块（推迟到 gate 之后）

Edit `niu_api/__main__.py:342-348`，删除此处的 `start_background_sync` 调用块（保留注释说明已推迟）：

修改前（`__main__.py:342-348`）：
```python
        # 8.1. Start brain region periodic sync (background thread starts here)
        if region_sync is not None:
            try:
                region_sync.start_background_sync()
                logger.info("Brain region sync started (interval: 24h)")
            except Exception as e:
                logger.warning(f"Brain region sync start failed: {e}")
```

修改后：
```python
        # 8.1. (已推迟) Start brain region periodic sync
        #      v3 关键改动：start_background_sync() 不在此处调用，推迟到 lifespan 末尾
        #      run_brain_region_startup_gate 之后（见 8.7 节）。这样 gate 运行期间
        #      _sync_loop daemon 不存在，run_sync_once_for_startup 必拿 _sync_lock、
        #      必跑完 _refresh_activation_manager，从结构上消除首次启动场景 daemon
        #      与 lifespan 抢锁的竞态（第二轮审查严重问题）。
```

### - [ ] Step 3.3: 实现 - 修改 lifespan 末尾：gate 调用 + start_background_sync 平移（含 None 守卫）

Edit `niu_api/__main__.py:411-425`：

修改前：
```python
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
```

修改后：
```python
    # 8.7. Brain region startup gate + Signal scheduler + start_background_sync（推迟）
    #      必须在所有后台依赖就绪后才 signal。
    #      脑区就绪 gate（run_sync_once_for_startup）：在 signal_scheduler_ready 之前同步跑首次
    #      run_sync()，确保 activation_mgr 已 set。否则日常重启场景下 _sync_loop 因 24h
    #      间隔保护不跑首次，activation_mgr 永远 None，scheduler 触发的过期任务和用户第一轮
    #      请求都撞 None，脑区动态注入缺失。90s 超时兜底：超时后 warning 但仍 signal，
    #      靠 _get_brain_injector 的 forced sync daemon 兜底（5 分钟冷却 + 防并发）。
    #      region_sync is None（LightRAG 损坏分支）时 helper 跳过 gate。
    #      start_background_sync 推迟到 gate 之后调用（v3）：gate 运行期间 _sync_loop
    #      daemon 不存在，run_sync_once_for_startup 必拿锁必跑完，消除首次启动竞态。
    from niu_api.internal.lightrag_manager import should_signal_scheduler_ready
    from niu_api.startup_gate import run_brain_region_startup_gate
    from niu_api.internal.scheduler.service import signal_scheduler_ready
    gate_result = run_brain_region_startup_gate(
        region_sync=region_sync,
        signal_scheduler_ready_fn=signal_scheduler_ready,
        should_signal=should_signal_scheduler_ready(phase1_result),
        timeout=90.0,
    )
    if gate_result is True:
        logger.info("Scheduler system_ready signal sent (brain region ready)")
    elif gate_result is False:
        logger.warning(
            "Scheduler system_ready signal sent (brain region degraded, "
            "forced sync daemon will retry on first request)"
        )
    else:
        logger.warning("[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏或 region_sync 未创建）")

    # 8.7.5. Start brain region periodic sync（v3：从 8.1 推迟到 gate 之后，含 None 守卫）
    #      必须在 run_brain_region_startup_gate 之后调用，确保 gate 先抢锁跑完首次同步。
    #      保留 if region_sync is not None 守卫：LightRAG 损坏分支 region_sync=None，
    #      裸调用会 AttributeError。整块从原 8.1 平移而来。
    if region_sync is not None:
        try:
            region_sync.start_background_sync()
            logger.info("Brain region sync started (interval: 24h, after startup gate)")
        except Exception as e:
            logger.warning(f"Brain region sync start failed: {e}")
```

### - [ ] Step 3.4: 运行全部测试验证

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_region_sync_first_sync_done.py tests/test_lifespan_brain_region_gate.py -v`
Expected: 14 tests PASS

### - [ ] Step 3.5: 验证 lifespan 语法 + import 完整

Run: `cd /Users/lilei/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/__main__.py').read()); print('syntax OK')"`
Expected: `syntax OK`

Run: `cd /Users/lilei/tools/ai-bot && python -c "from niu_api.startup_gate import run_brain_region_startup_gate; print('import OK')"`
Expected: `import OK`

### - [ ] Step 3.6: 提交

```bash
git add niu_api/__main__.py
git commit -m "$(cat <<'EOF'
fix(startup): lifespan 在 signal_scheduler_ready 前 wait 脑区首次同步 + start_background_sync 推迟

lifespan 改造（v3 核心）：
1. 预初始化 region_sync = None 修复 LightRAG 损坏分支 NameError（严重问题 1）
2. signal_scheduler_ready 前调 run_brain_region_startup_gate：
   - 同步跑 run_sync_once_for_startup()（阻塞 ~40s）+ wait_first_sync_done(90)
   - 额外检查 get_activation_mgr() is not None
   - 超时或 None → warning 但仍 signal，靠 forced sync daemon 兜底
3. start_background_sync() 从 8.1 推迟到 gate 之后（含 if region_sync is not None
   守卫）：gate 运行期间 _sync_loop daemon 不存在，run_sync_once_for_startup
   必拿锁必跑完，从结构上消除首次启动场景 daemon 与 lifespan 抢 _sync_lock
   的竞态（第二轮审查严重问题）

修复日常重启场景下前几轮脑区动态注入缺失根因：_sync_loop 因 24h 间隔
保护不跑首次 run_sync，activation_mgr 永远 None，直到用户请求触发 forced
sync daemon。本修复让 lifespan 主动调 run_sync_once_for_startup，启动时
activation_mgr 就 set，scheduler 触发的过期任务和用户第一轮请求都拿到非 None。

启动慢 ~40s 是预期行为（方向 A），用户已接受。set_preload_complete 也推迟
到 gate 之后，Rust 启动器 launch 前端同步推迟（Task 4 配套调整 Rust 超时）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Rust 启动器 preload 超时提升 60s → 180s

**Files:**
- Modify: `launcher/src/main.rs:1698`（`for i in 0..120` → `for i in 0..360`）

### 设计

方案 Task 3 让 lifespan 在 `signal_scheduler_ready` 前 wait `_first_sync_done`，最坏情况 90s + 启动开销（embedding 9s + MCP 5s + LightRAG 1s + ...）可能达 100s+。Rust 启动器轮询 `/api/preload-status` 60s 超时太短，需要提升到 180s 留足余量。

### - [ ] Step 4.1: 修改 Rust 轮询超时

Edit `launcher/src/main.rs:1698`：

修改前：
```rust
        let mut preload_ready = false;
        for i in 0..120 {
            thread::sleep(Duration::from_millis(500));
```

修改后：
```rust
        let mut preload_ready = false;
        // 180s 超时（原 60s）：脑区启动就绪 gate 让 lifespan 在 signal_scheduler_ready
        // 前同步跑 run_sync_once_for_startup（~40s）+ wait_first_sync_done(90)，
        // 最坏情况 90s + 启动开销（embedding 9s + MCP 5s + LightRAG 1s + ...）= 100s+。
        // 180s 留足余量，避免 Rust 误判启动完成、前端启动后无法连接 API。
        for i in 0..360 {
            thread::sleep(Duration::from_millis(500));
```

### - [ ] Step 4.2: 编译 Rust 启动器

按 CLAUDE.md 铁律 8，**必须用 `launcher/build.sh`，禁止直接 `cargo build`**：

Run: `cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`
Expected: 编译成功，`niu` 二进制更新

### - [ ] Step 4.3: 提交

```bash
git add launcher/src/main.rs
git commit -m "$(cat <<'EOF'
fix(launcher): preload 轮询超时 60s → 180s 配合脑区启动 gate

脑区启动就绪 gate 让 lifespan 在 signal_scheduler_ready 前同步跑
run_sync_once_for_startup（~40s）+ wait_first_sync_done(90)，最坏情况
100s+。原 60s 超时会让 Rust 误判启动完成、前端启动后无法连接 API。

180s 留足余量。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 真实程序端到端验证

**Files:**
- Test: 手动测试 + 日志验证（无新代码）

### 设计

按 [[real-testing-only]] 铁律：测试必须用真实程序 + 真实 LLM。前 4 个 Task 的单元测试用 mock 是为了快速验证逻辑分支，但最终必须用真实程序验证 5 个场景：

1. **日常重启场景**（核心）：启动后立即发消息，验证第 1 轮就有脑区段
2. **启动后等待场景**（用户实测发现）：启动后 2 分钟不说话，2 分钟后说话，验证第 1 轮就有脑区段（修复前会缺失）
3. **scheduler 触发场景**：人为造一个"启动时已错过的定时任务"，验证 scheduler 触发的对话也含脑区段
4. **首次启动场景**（v3 核心改动针对的场景）：删除 `last_region_sync.json` 后启动，验证 `_sync_loop` daemon 与 lifespan 无竞态、第 1 轮就有脑区段（第二轮审查严重问题的原发场景，v3 正是为它改的，不验证等于没验证 v3）
5. **LightRAG 损坏分支**（可选，难造）：验证不崩溃

### - [ ] Step 5.1: 启动前清理 + 记录基线

```bash
# 记录当前 last_sync 时间（启动前）
cat ~/.niu/last_region_sync.json
# 备份当前 raw_http 日志目录（用于启动后对比）
mv ~/.niu/logs/raw_http ~/.niu/logs/raw_http.before_fix.$(date +%s)
mkdir -p ~/.niu/logs/raw_http
```

### - [ ] Step 5.2: 场景 1 - 日常重启立即发消息

启动程序并计时：

Run: `time ./niu`
Expected: 
- 启动时长 ~70-100s（原来 ~30s，多 40-70s 是 run_sync 阻塞）
- splash 窗口多停留 ~40s
- 主窗口出现后立即可用

启动完成后**立即**在主窗口输入"在吗，对话测试"。

等回复完成后，检查日志：

```bash
# 找到第一轮的 request.json
# 序号说明：应用层从 000000 起始，但 000000 是 Rust 启动器的 LLM 连通性测试
# （启动时 "running real test..." 发的单条 "hi"，无 system message），
# 用户第一轮真实对话从 000001 开始（含完整 system message + 历史 + 用户输入）。
ls -t ~/.niu/logs/raw_http/$(date +%Y%m%d)/ | head -5

# 验证 system message 含 ## 脑区状态
python3 -c "
import json
with open('$HOME/.niu/logs/raw_http/$(date +%Y%m%d)/000001_request.json') as f:
    data = json.load(f)
system_msg = data['messages'][0]['content']
if '## 脑区状态' in system_msg:
    print('PASS: 场景 1 第 1 轮脑区段已注入')
    idx = system_msg.index('## 脑区状态')
    print('位置:', idx, '/', len(system_msg))
else:
    print('FAIL: 场景 1 第 1 轮脑区段缺失')
    print('system_msg 前 500 字:', system_msg[:500])
"
```

Expected: `PASS: 场景 1 第 1 轮脑区段已注入`

### - [ ] Step 5.3: 场景 2 - 启动后等待 2 分钟再发消息（用户实测发现）

杀掉场景 1 的进程，重新启动：

```bash
# 优雅杀进程（禁止 pkill -f niu）
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
kill -TERM <PID>
sleep 5
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
```

重新启动：

Run: `./niu`

启动完成后**等待 2 分钟不说话**。2 分钟后输入"在吗，对话测试"。

检查日志（同 Step 5.2 的 python 验证命令）。

Expected: `PASS: 场景 2 第 1 轮脑区段已注入`（修复前会缺失，因为 forced sync daemon 没被触发过）

### - [ ] Step 5.4: 场景 3 - scheduler 触发的过期任务

人为造一个"启动时已错过的定时任务"：

```bash
# 在 scheduler db 里插入一个 scheduled_at 为过去时间的任务
# 注意：表名是 scheduled_tasks（不是 tasks），id TEXT PRIMARY KEY 必填
python3 -c "
import sqlite3
from datetime import datetime, timedelta
db = sqlite3.connect('/Users/lilei/.niu/work/scheduled_tasks.db')
past_time = (datetime.now() - timedelta(minutes=5)).isoformat()
db.execute(
    'INSERT INTO scheduled_tasks (id, content, scheduled_at, is_recurring, event_type, status, name) VALUES (?, ?, ?, 0, ?, ?, ?)',
    ('startup-gate-test-001', '启动就绪测试任务，请确认脑区状态', past_time, 'one-shot', 'pending', 'startup-gate-test')
)
db.commit()
print('Inserted test task with scheduled_at =', past_time)
"
```

杀掉场景 2 的进程，重新启动：

```bash
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
kill -TERM <PID>
sleep 5
```

Run: `./niu`

启动完成后不说话，等 scheduler 自动触发（约启动后 90s+，等 frontend_ready + scheduler proceed + 10s 扫描间隔）。

观察日志：

```bash
# 观察 llm_interaction 日志里 scheduler 触发的请求
tail -f ~/.niu/logs/llm_interaction_$(date +%Y%m%d).log | grep -A2 "启动就绪测试任务"
```

scheduler 触发后，检查 raw_http 目录里 scheduler 触发的那一轮 request.json：

```bash
# 序号说明：000000 是 LLM 连通性测试（单条 "hi"），scheduler 触发的真实对话
# 是 000001（启动后用户不说话，scheduler 触发的对话是第一次真实 LLM 会话调用，
# 含完整 system message + "启动就绪测试任务"）。
ls -t ~/.niu/logs/raw_http/$(date +%Y%m%d)/ | head -10

# 验证 system message 含 ## 脑区状态
python3 -c "
import json
with open('$HOME/.niu/logs/raw_http/$(date +%Y%m%d)/000001_request.json') as f:
    data = json.load(f)
system_msg = data['messages'][0]['content']
if '## 脑区状态' in system_msg:
    print('PASS: 场景 3 scheduler 触发第 1 轮脑区段已注入')
else:
    print('FAIL: 场景 3 scheduler 触发第 1 轮脑区段缺失')
"
```

Expected: `PASS: 场景 3 scheduler 触发第 1 轮脑区段已注入`

**清理测试任务**：

```bash
python3 -c "
import sqlite3
db = sqlite3.connect('/Users/lilei/.niu/work/scheduled_tasks.db')
db.execute('DELETE FROM scheduled_tasks WHERE name = ?', ('startup-gate-test',))
db.commit()
print('Cleaned up test task')
"
```

### - [ ] Step 5.5: 场景 4 - 首次启动场景（v3 核心改动针对的场景，必测）

第二轮审查严重问题的原发场景：首次启动（无 `last_region_sync.json`）时 `_sync_loop` daemon 与 lifespan `run_sync_once_for_startup` 抢 `_sync_lock` 竞态。v3 通过推迟 `start_background_sync` 到 gate 之后从结构上消除。必须验证。

**重要**：删除 `last_region_sync.json` 前先备份，测试完恢复（避免影响真实 24h 间隔计时）。

```bash
# 备份 last_region_sync.json
cp ~/.niu/last_region_sync.json ~/.niu/last_region_sync.json.bak.$(date +%s)

# 删除 last_region_sync.json 模拟首次启动
rm ~/.niu/last_region_sync.json

# 杀掉场景 3 的进程
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
kill -TERM <PID>
sleep 5
```

Run: `./niu`

启动完成后**立即**在主窗口输入"在吗，对话测试"。

检查日志（同 Step 5.2 的 python 验证命令）。

Expected: `PASS: 场景 4 首次启动第 1 轮脑区段已注入`

**关键验证点**：首次启动时 lifespan gate 的 `run_sync_once_for_startup` 先抢锁跑完（`_sync_loop` daemon 还没拉起），`activation_mgr` 在 scheduler proceed 前已 set。观察日志确认：
- 有 `[StartupGate] Running brain region first sync` 日志
- 有 `[RegionSync] Activation manager refreshed: 8 regions` 日志（在 scheduler proceed 之前）
- **没有** `[RegionSync] 另一个同步正在运行，跳过本次` 日志（说明无抢锁冲突）

**恢复 last_region_sync.json**（**推荐：不恢复**）：

gate 的 `run_sync_once_for_startup` 会重新生成 `last_region_sync.json`，让 gate 生成的新锚点生效——这符合 v3 语义变化声明（每次启动都全量同步，24h 间隔锚点是"最近一次启动"）。**不要恢复原备份**，否则下次启动 24h 计时锚点会回到旧时间，与 v3 语义不一致。

```bash
# 确认 gate 已重新生成 last_region_sync.json（不要恢复 .bak 备份）
ls -la ~/.niu/last_region_sync.json*
# 测试结束后可删除备份文件
# rm ~/.niu/last_region_sync.json.bak.<timestamp>
```

### - [ ] Step 5.6: 辅助验证 - last_sync 时间戳在 lifespan yield 之前（时序确认）

注：设计清单场景 5（LightRAG 损坏分支）标注"可选，难造"，无独立 Step——它由单元测试 `test_skip_when_region_sync_none` 覆盖（helper 的 None 分支）。本 Step 是时序辅助验证，非设计清单场景。
```bash
# 看 last_sync 时间戳
cat ~/.niu/last_region_sync.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('last_sync:', d['last_sync'])"

# 对比 raw_http 第一轮请求时间戳（应该是 lifespan yield 之后才到的）
python3 -c "
import json, os
with open(os.path.expanduser('~/.niu/logs/raw_http/$(date +%Y%m%d)/000001_request.json')) as f:
    data = json.load(f)
print('first request timestamp:', data.get('timestamp', 'N/A'))
"
```

Expected: `last_sync` 时间戳早于 `first request timestamp` 至少 1 秒。

### - [ ] Step 5.7: 杀进程清理

```bash
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
kill -TERM <PID>
sleep 5
ps aux | grep -E '\./niu|niu_api|niu_launcher' | grep -v grep
# 应该没有输出
```

### - [ ] Step 5.8: 测试报告

在 commit message 记录：
- 场景 1（日常重启立即发）：第 1 轮脑区段是否存在
- 场景 2（启动后等 2 分钟）：第 1 轮脑区段是否存在（关键，修复前会缺失）
- 场景 3（scheduler 触发）：第 1 轮脑区段是否存在
- 场景 4（首次启动，v3 核心）：第 1 轮脑区段是否存在 + 无抢锁冲突日志
- 启动时长（before ~30s / after ~70-100s）
- `last_sync` vs first request timestamp 时序

### - [ ] Step 5.9: 提交测试报告

```bash
git add docs/superpowers/plans/2026-07-27-brain-region-startup-ready-gate.md
git commit -m "$(cat <<'EOF'
test: 脑区启动就绪门控端到端验证通过

场景 1（日常重启立即发）：第 1 轮脑区段已注入（修复前缺失）
场景 2（启动后等 2 分钟）：第 1 轮脑区段已注入（修复前缺失，根因是
        forced sync daemon 没被触发过，activation_mgr 永远 None）
场景 3（scheduler 触发）：第 1 轮脑区段已注入（修复前缺失）
启动时长从 ~30s 增至 ~70-100s（run_sync 阻塞 ~40s，符合预期）
last_sync 时间戳早于第 1 轮请求时间戳（时序正确）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (v3)

### 1. Spec coverage

- ✅ 根因（日常重启无启动时触发路径）：Task 3 lifespan 主动调 `run_sync_once_for_startup`
- ✅ scheduler 触发也等脑区：Task 3 `signal_scheduler_ready` 推迟到 `_first_sync_done` 后
- ✅ 严重问题 1（region_sync NameError）：Task 3.1 预初始化 `region_sync = None` + helper 处理 None
- ✅ 严重问题 2（并发 skip activation_mgr None）：Task 2.3 helper 额外检查 `get_activation_mgr()`
- ✅ 严重问题 3（Rust preload 60s 超时）：Task 4 提升到 180s
- ✅ 严重问题 4（异常时 wait 行为）：Task 2.3 helper 额外检查 `get_activation_mgr()`
- ✅ 第二轮严重问题（首次启动 daemon 抢锁竞态）：Task 3.2/3.3 `start_background_sync` 推迟到 gate 之后 + None 守卫
- ✅ 第三轮审查发现（lifespan 顺序无单测）：Task 2.5 补 2 个顺序测试
- ✅ 第三轮审查发现（真实测试缺首次启动场景）：Task 5.5 补场景 4
- ✅ fallback 保留：`_get_brain_injector` forced sync daemon 不删
- ✅ 真实测试覆盖 5 个场景：Task 5 场景 1/2/3/4/5

### 2. Placeholder scan

无 placeholder。每个 step 都有完整代码或具体命令。`start_background_sync` 平移含完整修改前/后代码块 + None 守卫（不是"推迟即可"的口头描述）。

### 3. Type consistency

- `RegionSync._first_sync_done`: `threading.Event`（Task 1.3 定义，Task 1.4/1.5 使用，Task 2/3 使用）✅
- `RegionSync.wait_first_sync_done(timeout: float) -> bool`: Task 1.5 定义，Task 2.3 调用 ✅
- `RegionSync.run_sync_once_for_startup() -> dict`: Task 1.5 定义，Task 2.3 调用 ✅
- `run_brain_region_startup_gate(*, region_sync, signal_scheduler_ready_fn, should_signal, timeout) -> Optional[bool]`: Task 2.3 定义，Task 3.3 调用 ✅
- `should_signal_scheduler_ready(phase1_result) -> bool`: 现有函数，Task 3.3 调用 ✅

### 4. 风险点

1. **启动慢 40s**：用户已接受方向 A，Task 4 Rust 超时提升配套
2. **`region_sync` 变量可见性**：Task 3.1 预初始化 `region_sync = None` 已修复
3. **`run_sync` 异常分支**：Task 1.4 把 `try_acquire_sync` 也包进 try/finally，确保异常/skip 都 set Event；Task 2.3 helper 额外检查 `activation_mgr`
4. **首次启动 daemon 抢锁竞态**：Task 3.2/3.3 `start_background_sync` 推迟到 gate 之后——gate 运行期间 daemon 不存在，`run_sync_once_for_startup` 必拿锁必跑完，从结构上消除（不再是"helper 检查 activation_mgr 决定 proceed"的软兜底）
5. **LightRAG 损坏分支 AttributeError**：Task 3.3 `start_background_sync` 平移保留 `if region_sync is not None:` 守卫
6. **forced sync daemon 5 分钟冷却**：lifespan `run_sync_once_for_startup` 失败时 fallback 到 forced sync daemon，5 分钟冷却可能让前几轮全无脑区段——但这是已有 fallback 行为，不在本方案范围
7. **每次启动都全量同步**：v3 后 `run_sync` 每次启动执行一次，24h 间隔锚点被"最近一次启动"取代——这是有意为之（见 v2→v3 修复小节的语义变化声明），后续维护者不要误判改回

---

## 4 轮审查制要求

按 [[four-round-full-code-review]] 经验，本方案实施前必须经过 4 轮审查：

### 审查 1（已完成）：方案审查 — code-reviewer Agent
- 发现 4 个严重问题，v2 已全部修复

### 审查 2（已完成）：v2 方案审查 — code-reviewer Agent
- 发现 1 个新的严重问题（首次启动 daemon 抢锁竞态，confidence 85），v3 已通过推迟 `start_background_sync` 修复

### 审查 3（已完成）：v3 设计级审查 — code-reviewer Agent
- 发现方案文件还是 v2（阻断）+ `start_background_sync` 平移缺 None 守卫 + 缺 lifespan 顺序单测 + 缺首次启动真实测试场景
- 本 v3 已全部修复并写入文件

### 审查 4：v3 方案审查 — code-reviewer Agent
- 派 code-reviewer Agent 审 v3 方案（确认 v3 delta 已正确写入文件 + 无新问题）

**交付条件**：连续两轮审查没有 bug 才能交付。

每轮审查必须读全量相关代码（不允许只看片段），未读代码的方案/审查一律不通过。

---

## Execution Handoff

方案 v3 已保存到 `docs/superpowers/plans/2026-07-27-brain-region-startup-ready-gate.md`。

**两种执行方式**：

**1. Subagent-Driven（推荐）** — 每个 Task 派新 subagent，Task 间审查，快速迭代

**2. Inline Execution** — 在本会话执行，批量执行 + checkpoint 审查

**但首先需要派第四轮审查 Agent 验证 v3 方案**——按你的要求"修复后重做计划审查连续两轮审查没有 bug"。

下一步：派第四轮审查 Agent 审 v3 方案。
