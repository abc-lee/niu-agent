# 启动阻塞 + repair 结果展示 + 截断修复 Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 LightRAG 检测到损坏后启动流程没被阻塞的问题，让用户弹窗决策期间所有依赖 LightRAG 的后台任务都不消费消息；修复 `run_repair_on_user_request` 假成功 + 前端不显示修复结果；新增 vdb 截断修复逻辑（断在 base64 vector 字段中间也能恢复断点前所有完整 entity）；提供安全可恢复的文件损坏现场让用户端到端测试。

**Architecture:**
- **P1 启动阻塞**：在 `niu_api/__main__.py` lifespan 加一个全局 `_lightrag_blocked` flag（基于 `wait_lightrag_ready` Event 的反向），Phase 1 检测到 `need_repair=True` 时不调 `signal_scheduler_ready()` + pause ChatQueue + 不启动 db_monitor 任务（db_monitor 推迟到 Phase 1 之后启动，启动时再 gate）；用户决策期间 scheduler/ChatQueue/db_monitor 三个任务都不消费消息。修复完或退出后程序完全退出，不恢复后台任务（用户重启时新进程跑全套启动流程）。
- **P2 repair 结果展示**：`run_repair_on_user_request` 的 `repaired` 改为遍历 results dict，任一 vdb `status==error` 则 `repaired=False`；前端 main.rs `RepairResult` 成功分支也弹结果对话框，解析 JSON 把每个 vdb 的 status/message 列成清单，用户点确定 → ExitApp。失败分支保持现有"修复失败"对话框 + ExitApp。
- **P3 repair 截断修复**：新增 `_try_truncate_repair(vdb_filename)` 在 `_read_data_from_vdb` 失败后调用，用括号配平计数法（遇 `{` depth+1，遇 `}` depth-1，depth 从 1 回到 0 即一个完整对象结束）找最后一个完整对象的 `}` 边界，截掉半截，补 `]}` 闭合，拿到断点前所有完整 entity 走 `repair_vdb` 后续流程。括号配平能避免 content 字段里 `}`（代码片段/JSON 示例）干扰边界识别。
- **P4 文件损坏现场**：提供 shell 脚本，先备份用户真实 vdb 到 `.corrupt.bak.test`，再制造截断现场（`head -c NNN`），测完恢复。

**Tech Stack:** Python 3.11+，pytest，Rust + Iced + rfd，shell

---

## Context

### 当前 bug（排查结论已确认的事实）

**问题一：启动流程不阻塞（核心）**

`niu_api/__main__.py` lifespan 顺序：
- L67 `start_scheduler()` — 启动 scheduler 实例，调 `start_delayed()` 立即起一个后台线程等 ready signal（60s 超时强行跑）
- L180 `await start_chat_queue()` — ChatQueue worker 立即开始消费消息
- L185 `db_monitor_task = asyncio.create_task(run_db_monitor())` — db_monitor 开始轮询
- L189 `signal_scheduler_ready()` — 通知 scheduler 系统就绪，scheduler 收到信号后 sleep 2s 开始扫描
- L199-205 Phase 1 检测 `phase1_result = run_resilience_phase1()` → 这时 scheduler/ChatQueue/db_monitor 都已启动

`_lightrag_ready` Event（`lightrag_manager.py` L736）只在 LightRAG init 成功时 set（L940）。损坏时永不 set，但 scheduler/ChatQueue/db_monitor 都不查这个 Event。

后果：
- scheduler 60s 超时强行扫描 → journal-agent 任务触发 → 走 ChatQueue → `runner.chat` → LightRAG 检索报错
- ChatQueue worker 不等 LightRAG ready，立即消费，前端任何消息都会触发 `runner.chat` 报错
- db_monitor 轮询 messages.db 路由消息最终也经 ChatQueue → `runner.chat`

**问题二：repair 假成功**

`run_repair_on_user_request`（`lightrag_manager.py` L1014-1053）：
```python
try:
    repair_result = repair_all()
    ...
    return {
        "repaired": True,           # ← 硬编码
        ...
    }
except Exception as e:
    return {
        "repaired": False,          # ← 只在 repair_all 抛异常时
        ...
    }
```

`repair_all`（`lightrag_repair.py` L564-573）永不抛异常——它收集每个 vdb 的 status 到 results dict，单文件失败也返回 `{"status": "error", "message": ...}`，不抛。所以 `run_repair_on_user_request` 永远走 try 分支返回 `repaired=True`。

前端 `main.rs` `RepairResult` 成功分支只 `resp.text()` 不解析 JSON：
```rust
SplashMessage::RepairResult(result) => {
    if let Err(e) = result {
        // 失败分支：弹"修复失败"对话框 + ExitApp
    }
    // 成功分支：直接 ExitApp，不弹结果对话框 ← 这里
    Task::done(SplashMessage::ExitApp)
}
```

用户点"修复"后看到 splash 消失、程序退出，不知道修复了什么、是否成功。

**问题三：repair 截断修复缺失**

`vdb_entities.json` JSON 截断（断在 vector 字段中间的 base64）时：
- `_read_data_from_vdb`（`lightrag_repair.py` L92-112）`json.load` 抛 `JSONDecodeError` → 返回 None
- fallback kv_store，但 entities 无 fallback（`_VDB_FALLBACK_KV` L40-43 只有 chunks）
- `repair_vdb` 返回 `{"status": "error", "message": "无可用数据源重建"}`

截断修复逻辑不存在——应该用括号配平计数法（遇 `{` depth+1，遇 `}` depth-1，depth 从 1 回到 0 即一个完整对象结束）找最后一个完整对象的 `}` 边界，截掉半截，补 `]}` 闭合，拿到断点前所有完整 entity，重新 embedding + 重建 matrix。括号配平能避免 content 字段里 `}`（代码片段/JSON 示例）在 base64 vector 之前命中导致截断位置落在对象内部。

### 用户要求

1. **全面检查启动流程**——不只是 scheduler/ChatQueue/db_monitor，要系统排查所有"应在 LightRAG 就绪后跑"的任务
2. **修复进程启动后不管成功与否都显示修复结果弹窗**——列出每个 vdb 的 status/message，确定后程序完全退出，需用户重启
3. **提供文件损坏现场**让用户亲自测试

### 全面排查清单（P1 必须系统覆盖）

启动时所有可能触发 `runner.chat` 或 LightRAG 检索的任务，按风险分级：

| 任务 | 启动位置 | 是否触发 runner.chat | 是否查 `_lightrag_ready` | 是否需阻塞 |
|------|---------|---------------------|--------------------------|------------|
| **scheduler** | L67 start_scheduler + L189 signal_scheduler_ready | 是（trigger_callback 走 ChatQueue → runner.chat） | 否（只查自身 `_ready_event`） | **必须阻塞** |
| **ChatQueue worker** | L180 start_chat_queue | 是（直接调 runner.chat） | 否 | **必须阻塞** |
| **db_monitor** | L185 create_task(run_db_monitor) | 是（路由消息走 supplement queue → runner.chat） | 否 | **必须阻塞** |
| _daily_tmp_cleanup | L176 _cleanup_task | 否（只调 `cleanup_old_tmp` 清临时文件） | 否 | 否（不依赖 LightRAG，可保留） |
| HAWatcher | L72 check_and_start | 否（仅轮询 HomeAssistant，不直接调 runner.chat） | 否 | 否（不依赖 LightRAG） |
| IMGateway | L137 gateway.start() | 是（收到 IM 消息入队 ChatQueue → runner.chat） | 否 | **必须阻塞（同 ChatQueue）** — 但 IMGateway 是 TCP Server，启动后只 accept 不主动触发，用户在 LightRAG 损坏时不会主动发 IM 消息。仍建议 gate（pause ChatQueue 即可阻止 IM 消息触发 runner.chat） |
| page-agent-mcp | 跳过（独立进程，不在 lifespan 启） | 否 | 否 | 否 |
| MCP tools loaded | L90 load_mcp_tools | 否（只加载工具 schema） | 否 | 否 |
| NiuRunner init | L99 init_runner | 否（只创建实例） | 否 | 否 |
| SQLite WAL | L108-113 | 否 | 否 | 否 |
| PipelineWatcher | L230 start_pipeline_watcher | 否（只推 SSE 事件，不调 runner.chat） | 否 | 否 |
| LightRAGSync | L239 get_lightrag_sync(auto_start=True) | 是（`_sync_loop` 调 LightRAG 接口做 photo/document backfill） | 是（用 `wait_lightrag_ready`） | 已 gate（等待 LightRAG ready） |
| BrainGraph | L248 brain.ensure_niu_entity() | 是（调 LightRAG chunk + insert） | 否（直接调 rag 实例） | **必须阻塞** — 但此处在 Phase 1 之后执行（L244），且 LightRAG eager init 已在 L210 跑过；如损坏 eager init 失败，`get_lightrag()` 返回 None，`ensure_niu_entity` 应有 None 检查（需验证） |
| create_default_regions | L266-269 | 是（用 LightRAGAdapter/Ingester） | 否 | 同上（依赖 LightRAG 实例） |
| RegionSync | L295 region_sync.start_background_sync() | 是（`_sync_loop` 调 LightRAG） | 是（`wait_brain_ready` + 内部 wait_lightrag_ready） | 已 gate |
| _SYSTEM_TASKS | L317-359 | 否（只 create/update task 记录，不立即触发） | 否 | 否（注册任务，不执行任务） |

**关键发现**：
- **scheduler / ChatQueue / db_monitor 是核心阻塞目标**——这三者都不查 `_lightrag_ready`，损坏时仍跑
- **LightRAGSync / RegionSync 已 gate**（用 `wait_lightrag_ready`），损坏时它们会等，60s 超时后 graceful 失败，不会强行触发 runner.chat
- **BrainGraph / create_default_regions 在 Phase 1 之后**（L244-269），依赖 LightRAG 实例；如 Phase 1 检测到损坏，应在 Phase 1 之后就退出/阻塞，不应执行到 L244
- **_daily_tmp_cleanup / HAWatcher / IMGateway / PipelineWatcher** 不直接依赖 LightRAG，不需要额外阻塞（IMGateway 收到消息会入队 ChatQueue，但 pause ChatQueue 即可阻断）

**P1 改造重点**：
1. Phase 1 检测 `need_repair=True` 时，**不调 `signal_scheduler_ready()`**（scheduler 60s 超时后强行跑的漏洞）
2. Phase 1 检测 `need_repair=True` 时，**pause ChatQueue**（ChatQueue worker 已启动，但 pause 后不消费消息；IM/scheduler/db_monitor 入队也只堆积在队列里）
3. **db_monitor 推迟启动**——当前 L185 启动 db_monitor，应在 Phase 1 检测后启动（如 need_repair=True 则不启动，等用户决策；如 need_repair=False 则正常启动）
4. Phase 1 need_repair=True 时，**L244 之后的代码不执行**（BrainGraph / create_default_regions / RegionSync / _SYSTEM_TASKS）——直接进入"等待用户决策"状态

### 关键代码位置（HEAD = 27b287f4）

**后端**：
- `niu_api/__main__.py:43-360` — lifespan 启动时序
  - L67 `start_scheduler()`
  - L180 `await start_chat_queue()`
  - L185 `db_monitor_task = asyncio.create_task(run_db_monitor())`
  - L189 `signal_scheduler_ready()`
  - L199-205 Phase 1 检测
  - L223-224 need_repair 时不自动修复，等用户决策
  - L244-359 Phase 1 之后的初始化（BrainGraph / RegionSync / _SYSTEM_TASKS）
- `niu_api/chat_queue.py:63-69` ChatQueue.start（worker 立即消费，无 LightRAG gate）
- `niu_api/chat_queue.py:179-185` ChatQueue.pause / resume（已有 pause 机制，可复用）
- `niu_api/internal/scheduler/scheduler.py:88-121` signal_ready + start_delayed（60s 超时强行跑的漏洞）
- `niu_api/internal/scheduler/service.py:125-178` start_scheduler + signal_scheduler_ready
- `niu_api/db_monitor.py:42-59` `_init_routed_baseline` + `run_db_monitor`
- `niu_api/internal/lightrag_manager.py:935-965` `_lightrag_ready` Event + `wait_lightrag_ready`
- `niu_api/internal/lightrag_manager.py:982-1053` `run_resilience_phase1` + `run_repair_on_user_request`（L1041 `repaired=True` 硬编码）
- `niu_api/internal/lightrag_repair.py:92-112` `_read_data_from_vdb`（截断时 JSONDecodeError → 返回 None）
- `niu_api/internal/lightrag_repair.py:145-228` `repair_vdb`（无截断修复逻辑）
- `niu_api/internal/lightrag_repair.py:564-573` `repair_all`（永不抛异常）
- `niu_api/kg_api.py:1084-1103` `/lightrag/repair` 端点

**前端**：
- `launcher/src/main.rs:321-446` — 弹窗 + repair 流程
  - L322-365 `StatusCheckResult`：检测到损坏 → 弹 rfd 对话框
  - L378-401 `UserDialogChoice`：用户选"修复"→ POST /api/kg/lightrag/repair
  - L403-430 `RepairResult`：**成功分支直接 ExitApp，不弹结果对话框** ← 这里要改
  - L431-446 `ExitApp`

---

## File Structure

```
ai-bot/                                # 项目根
├── niu_api/
│   ├── __main__.py                    # 改 lifespan：Phase 1 gate + db_monitor 推迟启动
│   ├── chat_queue.py                  # 改 start_chat_queue：加 _lightrag_blocked gate（pause 机制已有，复用）
│   ├── internal/
│   │   ├── lightrag_manager.py        # 改 run_repair_on_user_request：repaired 遍历 results
│   │   ├── lightrag_repair.py         # 新增 _try_truncate_repair，改 repair_vdb 调用顺序
│   │   └── scheduler/
│   │       ├── scheduler.py          # 新增 cancel_delayed_start 方法（轻量取消 delayed start，不 shutdown 整体）
│   │       └── service.py            # signal_scheduler_ready 加幂等检查（避免重复 set）
│   └── kg_api.py                      # 改 /lightrag/repair 端点为 async def + asyncio.to_thread（避免阻塞 event loop）
├── launcher/src/main.rs               # 改 RepairResult 成功分支：解析 JSON + 弹结果对话框
├── tests/
│   ├── test_lightrag_startup_block.py          # 新增，TDD 失败测试
│   ├── test_lightrag_repair_result_display.py  # 新增，TDD 失败测试
│   ├── test_lightrag_truncate_repair.py        # 新增，TDD 失败测试
│   └── test_lightrag_repair.py                 # 已有，回归验证
├── scripts/
│   └── make_vdb_corrupt_test_env.sh           # 新增，P4 损坏现场脚本
└── docs/superpowers/plans/
    └── 2026-07-09-startup-block-and-repair.md  # 本计划
```

---

## Tasks

### Task 0: 修改前临时备份提交

- [ ] **Step 0.1**：检查工作区干净（除本次新计划文件外）
```bash
cd <repo_root>
git status
```

- [ ] **Step 0.2**：临时备份提交（标注问题名+节点类型+基线 hash）
```bash
cd <repo_root>
git add -A
git commit -m "backup: 启动阻塞+repair结果展示+截断修复 改造前临时备份 (baseline 27b287f4)

问题：
1. LightRAG 损坏时 scheduler/ChatQueue/db_monitor 不阻塞，60s 超时强行跑触发 runner.chat 报错
2. run_repair_on_user_request repaired=True 硬编码，repair_all 永不抛异常所以永远成功
3. 前端 RepairResult 成功分支不弹结果对话框，用户不知道修复了什么
4. vdb 截断时 _read_data_from_vdb 直接返回 None，无截断修复逻辑

准备改六个点：
1. P1 lifespan Phase 1 gate + db_monitor 推迟启动 + ChatQueue pause + scheduler.cancel_delayed_start
2. P2 run_repair_on_user_request 遍历 results 判 repaired + 前端弹结果对话框
3. P2 kg_api.py /lightrag/repair 改 async def + asyncio.to_thread（避免阻塞 event loop）
4. P3 _try_truncate_repair 括号配平计数法找完整对象边界 + 补 ]} 闭合
5. P3 vdb_relationships 截断修复数据丢失风险在 P2 弹窗里提示用户（完整 GraphML 反向补齐留作后续 issue）
6. P4 scripts/make_vdb_corrupt_test_env.sh 安全可恢复的损坏现场

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1: TDD — P1 启动阻塞失败测试

**目标**：用 pytest 写测试覆盖 (1) Phase 1 检测到 need_repair 时不调 signal_scheduler_ready (2) ChatQueue 在 LightRAG 损坏时被 pause (3) db_monitor 推迟启动 (4) scheduler 的 delayed start 被取消。

- [ ] **Step 1.1**：创建测试文件 `tests/test_lightrag_startup_block.py`
```python
"""LightRAG 损坏时启动流程阻塞的单元测试。

背景：scheduler/ChatQueue/db_monitor 在 Phase 1 检测到损坏后仍跑，
60s 超时强行扫描触发 journal-agent → ChatQueue → runner.chat 报错。
本测试验证：
1. Phase 1 need_repair=True 时不调 signal_scheduler_ready
2. Phase 1 need_repair=True 时 ChatQueue 被 pause
3. Phase 1 need_repair=True 时不启动 db_monitor
4. Phase 1 need_repair=True 时 scheduler.cancel_delayed_start 被调用
"""
import asyncio
from unittest import mock


def test_phase1_need_repair_does_not_signal_scheduler_ready():
    """Phase 1 检测到 need_repair=True 时，不调 signal_scheduler_ready"""
    from niu_api.internal.scheduler import service as scheduler_service

    # 模拟 signal_scheduler_ready 已被调用过的标记（用于断言"未再调用"）
    call_count = {"count": 0}
    original_signal = scheduler_service.signal_scheduler_ready

    def mock_signal():
        call_count["count"] += 1

    # monkeypatch signal_scheduler_ready
    with mock.patch.object(scheduler_service, "signal_scheduler_ready", mock_signal):
        # 模拟 lifespan 在 Phase 1 后的判断逻辑（这是 P1 要实现的逻辑）
        # 当前代码无条件调 signal_scheduler_ready，P1 改造后应加 need_repair 检查
        from niu_api.internal.lightrag_manager import run_resilience_phase1

        # mock Phase 1 返回 need_repair=True
        with mock.patch.object(
            scheduler_service,
            # 我们需要模拟 lifespan 内的判断，所以这里直接测一个辅助函数
        ):
            pass

        # 由于 lifespan 是 async context manager，难以直接测；
        # 改为测一个新引入的辅助函数 should_signal_scheduler_ready(phase1_result)
        from niu_api.internal.lightrag_manager import should_signal_scheduler_ready

        # need_repair=True 时返回 False（不 signal）
        assert should_signal_scheduler_ready({"need_repair": True}) is False
        # need_repair=False 时返回 True（signal）
        assert should_signal_scheduler_ready({"need_repair": False}) is True


def test_chatqueue_paused_when_lightrag_corrupt():
    """Phase 1 need_repair=True 时，ChatQueue 被 pause（worker 不消费消息）"""
    # 这个测试验证 lifespan 在 Phase 1 后调用 ChatQueue.pause()
    # 当前代码没有这个调用，所以测试会失败
    from niu_api.chat_queue import ChatQueue

    # 模拟一个 ChatQueue 实例
    with mock.patch("niu_api.chat_queue.get_chat_queue") as mock_get:
        q = mock.MagicMock(spec=ChatQueue)
        q._paused = False
        mock_get.return_value = q

        # 模拟 lifespan 在 Phase 1 后的 pause 调用（P1 要实现的逻辑）
        from niu_api.internal.lightrag_manager import pause_chatqueue_if_corrupt

        pause_chatqueue_if_corrupt({"need_repair": True})
        # 验证 q.pause() 被调用
        q.pause.assert_called_once()

        # need_repair=False 时不 pause
        q.pause.reset_mock()
        pause_chatqueue_if_corrupt({"need_repair": False})
        q.pause.assert_not_called()


def test_db_monitor_not_started_when_lightrag_corrupt():
    """Phase 1 need_repair=True 时，不启动 db_monitor task"""
    # 这个测试验证 lifespan 在 Phase 1 后跳过 db_monitor 启动
    # 当前代码无条件 create_task，P1 改造后应加 need_repair 检查
    from niu_api.internal.lightrag_manager import should_start_db_monitor

    assert should_start_db_monitor({"need_repair": True}) is False
    assert should_start_db_monitor({"need_repair": False}) is True


def test_scheduler_delayed_start_cancelled_when_lightrag_corrupt():
    """Phase 1 need_repair=True 时，scheduler.cancel_delayed_start 被调用

    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106）。即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动。调 cancel_delayed_start 设
    _delayed_start_cancelled=True，_delayed_start 线程超时后检查 flag 直接 return。
    """
    from niu_api.internal.lightrag_manager import cancel_scheduler_delayed_start_if_corrupt

    # mock scheduler service.get_scheduler + Scheduler.cancel_delayed_start
    with mock.patch("niu_api.internal.scheduler.service.get_scheduler") as mock_get:
        sched = mock.MagicMock()
        mock_get.return_value = sched

        # need_repair=True 时调 cancel_delayed_start
        cancel_scheduler_delayed_start_if_corrupt({"need_repair": True})
        sched.cancel_delayed_start.assert_called_once()

        # need_repair=False 时不调
        sched.cancel_delayed_start.reset_mock()
        cancel_scheduler_delayed_start_if_corrupt({"need_repair": False})
        sched.cancel_delayed_start.assert_not_called()
```

- [ ] **Step 1.2**：跑测试确认失败（当前代码没有 should_signal_scheduler_ready / pause_chatqueue_if_corrupt / should_start_db_monitor / cancel_scheduler_delayed_start_if_corrupt，且 Scheduler 无 cancel_delayed_start 方法）
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_startup_block.py -v 2>&1 | tail -40
```
**预期失败**：ImportError（找不到 `should_signal_scheduler_ready` / `pause_chatqueue_if_corrupt` / `should_start_db_monitor` / `cancel_scheduler_delayed_start_if_corrupt`）+ AttributeError（`Scheduler` 无 `cancel_delayed_start`）。

---

### Task 2: P1 实现 — lifespan Phase 1 gate

**目标**：让 Phase 1 检测到 `need_repair=True` 时，scheduler 不收到 ready signal、ChatQueue 被 pause、db_monitor 不启动，且 Phase 1 之后的初始化（BrainGraph / RegionSync / _SYSTEM_TASKS）也不执行。

- [ ] **Step 2.1**：在 `niu_api/internal/scheduler/scheduler.py` 新增 `cancel_delayed_start` 方法

当前 `Scheduler` 类（scheduler.py L24-351）有 `_delayed_start_cancelled` 属性（L59）和 `stop()` 方法（L123-133，会设 `_delayed_start_cancelled=True`），但 `stop()` 会 join 线程 + shutdown executor，副作用太大（启动期损坏时只想让 delayed start 不再 start，不能让 scheduler 整体 shutdown，因为 lifespan 后续 shutdown 流程还会调 scheduler.stop）。

在 `stop()` 方法之前（L123 之前）新增一个轻量级方法：
```python
    def cancel_delayed_start(self):
        """取消 delayed start（不 shutdown 整体 scheduler）。

        场景：启动期检测到 LightRAG 损坏（need_repair=True），
        lifespan 不调 signal_scheduler_ready，但 scheduler.start_delayed
        里的 _ready_event.wait(60) 60s 超时后会强行 start（L103-106）。
        此方法设 _delayed_start_cancelled=True，让 _delayed_start 线程
        在 60s 超时后检查到这个 flag 直接 return，不强行 start。

        与 stop() 的区别：
        - stop() 会 join 线程 + shutdown executor（重操作，整体关闭）
        - cancel_delayed_start 只设 flag，不 join 不 shutdown（轻量）
        """
        with self._lock:
            self._delayed_start_cancelled = True
        logger.info("[SCHEDULER] Delayed start cancelled (start_delayed will no-op on timeout)")
```

**注意**：`_delayed_start_cancelled` 在 `start_delayed()` 开头被重置为 False（L99），所以 cancel_delayed_start 必须在 start_delayed 之后调用。lifespan 顺序是 L67 `start_scheduler()`（内部调 start_delayed）→ Phase 1 检测 → 调 cancel_delayed_start，时序正确。

- [ ] **Step 2.2**：在 `niu_api/internal/lightrag_manager.py` 新增三个辅助函数

在 `run_resilience_phase1` 函数之后（L1011 之后）新增：
```python
def should_signal_scheduler_ready(phase1_result: dict) -> bool:
    """Phase 1 后是否通知 scheduler 系统就绪。

    损坏时不通知，让 scheduler 60s 超时强行扫描的漏洞被堵住。
    用户决策退出或修复后，scheduler 跟随程序整体退出，不需要 ready signal。
    """
    return not phase1_result.get("need_repair", False)


def should_start_db_monitor(phase1_result: dict) -> bool:
    """Phase 1 后是否启动 db_monitor task。

    损坏时不启动，避免 db_monitor 路由消息到 ChatQueue → runner.chat 报错。
    """
    return not phase1_result.get("need_repair", False)


def pause_chatqueue_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时 pause ChatQueue，让 worker 不消费消息。

    用户决策期间 IM/scheduler 入队的消息只堆积在队列里，不触发 runner.chat。
    程序退出时 ChatQueue 跟随整体 shutdown，不需要 resume。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.chat_queue import get_chat_queue
            q = get_chat_queue()
            q.pause()
            logger.info("[LightRAG] ChatQueue paused due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to pause ChatQueue: {e}")


def cancel_scheduler_delayed_start_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时取消 scheduler 的 delayed start。

    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106），即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动 + 阻塞 120 秒（_CALLBACK_TIMEOUT）。
    虽然此期间 ChatQueue 被 pause 阻塞不会触发 runner.chat，但 scheduler
    线程跑起来后 60s+120s 才结束，期间用户决策/退出流程会被拖延。

    调 scheduler.cancel_delayed_start() 设 _delayed_start_cancelled=True，
    _delayed_start 线程 60s 超时后检查到 flag 直接 return。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.internal.scheduler.service import get_scheduler
            sched = get_scheduler()
            if sched is not None:
                sched.cancel_delayed_start()
                logger.info("[LightRAG] Scheduler delayed start cancelled due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to cancel scheduler delayed start: {e}")
```

- [ ] **Step 2.3**：Python 语法检查
```bash
cd <repo_root>
python -c "from niu_api.internal.scheduler.scheduler import Scheduler; assert hasattr(Scheduler, 'cancel_delayed_start'); from niu_api.internal.lightrag_manager import should_signal_scheduler_ready, should_start_db_monitor, pause_chatqueue_if_corrupt, cancel_scheduler_delayed_start_if_corrupt; print('OK')"
```

- [ ] **Step 2.4**：跑 Task 1 的测试，验证四个测试通过
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_startup_block.py -v
```
**预期**：4 个测试全部通过。

- [ ] **Step 2.5**：改 `niu_api/__main__.py` lifespan 加 gate

当前代码 L188-205：
```python
    # 6.7. Signal scheduler that system is ready (ChatQueue operational)
    from niu_api.internal.scheduler.service import signal_scheduler_ready
    signal_scheduler_ready()
    logger.info("Scheduler system_ready signal sent")

    # Phase 1: LightRAG eager init 之前——只做一致性检测（check_all）
    # ...
    try:
        from niu_api.internal.lightrag_manager import run_resilience_phase1
        phase1_result = run_resilience_phase1()
        logger.info(f"LightRAG Phase 1 检测: {phase1_result}")
    except Exception as e:
        logger.warning(f"LightRAG Phase 1 检测失败（不影响启动）: {e}")
        phase1_result = {"need_repair": False, "check_ok": True}
```

改为（调整顺序：Phase 1 先跑，再根据 need_repair 决定是否 signal + 是否启动 db_monitor + 是否 pause ChatQueue）：
```python
    # 6.7. Phase 1 先跑一致性检测，再根据结果决定是否 signal scheduler / 启动 db_monitor
    # v7: 修复 LightRAG 损坏时 scheduler/ChatQueue/db_monitor 不阻塞的 bug
    #     原顺序：L67 start_scheduler → L180 ChatQueue → L185 db_monitor → L189 signal_ready → L199 Phase 1
    #     修复后：Phase 1 先跑，need_repair=True 时不 signal + pause ChatQueue + 不启动 db_monitor
    try:
        from niu_api.internal.lightrag_manager import run_resilience_phase1
        phase1_result = run_resilience_phase1()
        logger.info(f"LightRAG Phase 1 检测: {phase1_result}")
    except Exception as e:
        logger.warning(f"LightRAG Phase 1 检测失败（不影响启动）: {e}")
        phase1_result = {"need_repair": False, "check_ok": True}

    # 6.7.1. Phase 1 检测到损坏时 pause ChatQueue（worker 已启动，pause 后不消费）
    from niu_api.internal.lightrag_manager import pause_chatqueue_if_corrupt
    pause_chatqueue_if_corrupt(phase1_result)

    # 6.7.1.1 Phase 1 检测到损坏时取消 scheduler delayed start
    #        补 P1 漏洞：scheduler 60s 超时强行 start 的漏洞（_ready_event.wait(60)）
    #        即使不调 signal_scheduler_ready，scheduler 线程 60s 后也会强行 start
    from niu_api.internal.lightrag_manager import cancel_scheduler_delayed_start_if_corrupt
    cancel_scheduler_delayed_start_if_corrupt(phase1_result)

    # 6.7.2. db_monitor 推迟到 Phase 1 之后启动（need_repair=True 时跳过）
    from niu_api.internal.lightrag_manager import should_start_db_monitor
    if should_start_db_monitor(phase1_result):
        from niu_api.db_monitor import run_db_monitor
        db_monitor_task = asyncio.create_task(run_db_monitor())
        logger.info("db_monitor task 已启动")
    else:
        # 占位变量，shutdown 时引用不报 NameError
        db_monitor_task = None
        logger.warning("[LightRAG] db_monitor 跳过启动（LightRAG 损坏，等用户决策）")

    # 6.7.3. Signal scheduler that system is ready（need_repair=True 时不 signal）
    from niu_api.internal.lightrag_manager import should_signal_scheduler_ready
    if should_signal_scheduler_ready(phase1_result):
        from niu_api.internal.scheduler.service import signal_scheduler_ready
        signal_scheduler_ready()
        logger.info("Scheduler system_ready signal sent")
    else:
        logger.warning("[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏）")
```

- [ ] **Step 2.6**：调整 Phase 1 之后的初始化加 gate

当前代码 L220-359（Phase 1 之后到 yield 之前）有 LightRAG eager init / LightRAGSync / BrainGraph / create_default_regions / RegionSync / _SYSTEM_TASKS。need_repair=True 时这些都不应执行。

在 L223-224（`if phase1_result.get("need_repair"):`）之前加：
```python
    # v7: Phase 1 need_repair=True 时，跳过所有依赖 LightRAG 的初始化
    #     等 用户在 rfd 弹窗决策退出或修复后程序整体退出
    # !!!铁律：_lightrag_corrupt_skip_init 必须是 lifespan 函数内的局部变量，
    #         不得提升为模块级全局变量。理由：
    #         1. 模块级全局在 exit 路径上不会被清除，下次正常启动（reload/module cache）
    #            仍读到 True，会错误跳过所有初始化，导致 LightRAG 完全不可用
    #         2. lifespan 每次启动重新计算 phase1_result，局部变量自然随函数结束失效
    #         3. 子 Agent 实施时禁止把它改成 `global _lightrag_corrupt_skip_init` 声明
    _lightrag_corrupt_skip_init = phase1_result.get("need_repair", False)
    if _lightrag_corrupt_skip_init:
        logger.warning("[LightRAG] 检测到损坏，跳过 Phase 1 之后的初始化（LightRAGSync/BrainGraph/RegionSync/_SYSTEM_TASKS）")
```

然后把 L207-359 之间的所有"依赖 LightRAG 实例"的初始化用 `if not _lightrag_corrupt_skip_init:` 包裹：
```python
    # 7.5. Eagerly initialize LightRAG
    if not _lightrag_corrupt_skip_init:
        try:
            from niu_api.internal.lightrag_manager import get_lightrag
            rag = get_lightrag()
            if rag is not None:
                logger.info("LightRAG instance initialized (eager)")
            else:
                logger.warning("LightRAG instance not available (init failed or not installed)")
        except Exception as e:
            logger.warning(f"LightRAG eager init failed: {e}")

    # ... 后续 L226-359 全部包到 if not _lightrag_corrupt_skip_init: 块内
```

具体包裹范围：
- L207-218 LightRAG eager init
- L226-234 PipelineWatcher
- L236-242 LightRAGSync
- L244-251 BrainGraph
- L253-260 vectors.db cleanup（这个可以不 gate，它只删文件不依赖 LightRAG）
- L262-272 create_default_regions
- L274-298 RegionSync
- L300-359 _SYSTEM_TASKS（只创建 task 记录，不立即触发，理论上可以不 gate，但为了清晰仍 gate）

**注意**：L223-224 的 `if phase1_result.get("need_repair"):` 这一行保留（只是 log），改造后整体逻辑是 need_repair=True 时所有后续初始化都跳过。

- [ ] **Step 2.7**：调整 shutdown 的 db_monitor 取消逻辑（避免 NameError）

当前 L399-409 shutdown 取消 db_monitor_task。改造后 db_monitor_task 可能是 None（need_repair=True 时未启动）。改：
```python
    # 停止 ChatQueue
    try:
        # 先取消 db_monitor task（避免停止后还在写入）
        if db_monitor_task is not None:
            db_monitor_task.cancel()
            try:
                await db_monitor_task
            except asyncio.CancelledError:
                pass
            logger.info("db_monitor task 已取消")
    except Exception as e:
        logger.warning(f"Failed to cancel db_monitor task: {e}")
```

- [ ] **Step 2.8**：Python 语法检查
```bash
cd <repo_root>
python -c "from niu_api.__main__ import lifespan; print('OK')"
```

- [ ] **Step 2.9**：跑 Task 1 全部测试
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_startup_block.py -v
```
**预期**：4 个测试全部通过。

---

### Task 3: TDD — P2 repair 结果展示失败测试

**目标**：用 pytest 写测试覆盖 (1) `run_repair_on_user_request` 遍历 results 判 repaired (2) 任一 vdb status=error 则 repaired=False。

- [ ] **Step 3.1**：创建测试文件 `tests/test_lightrag_repair_result_display.py`
```python
"""run_repair_on_user_request repaired 判定逻辑的单元测试。

背景：原代码 repaired=True 硬编码，repair_all 永不抛异常所以永远成功。
本测试验证：
1. 所有 vdb status=ok 时 repaired=True
2. 任一 vdb status=error 时 repaired=False
3. entity_sync/relationship_sync status=error 时 repaired=False
"""
from unittest import mock


def test_run_repair_all_ok_returns_repaired_true():
    """所有 vdb 和 sync 都是 ok 时，repaired=True"""
    from niu_api.internal import lightrag_manager

    # mock repair_all 返回全 ok
    all_ok_result = {
        "vdb_entities.json": {"status": "ok", "rebuilt_count": 5, "source": "vdb_data_field"},
        "vdb_relationships.json": {"status": "ok", "rebuilt_count": 3},
        "vdb_chunks.json": {"status": "ok", "rebuilt_count": 10},
        "entity_sync": {"status": "ok", "renamed": 0, "removed": 0, "rebuilt": 0},
        "relationship_sync": {"status": "ok", "renamed": 0, "removed": 0},
    }
    with mock.patch.object(lightrag_manager, "repair_all", return_value=all_ok_result):
        with mock.patch.object(lightrag_manager, "check_all", return_value={"ok": True}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is True
    assert result["check_ok"] is True


def test_run_repair_one_vdb_error_returns_repaired_false():
    """任一 vdb status=error 时，repaired=False"""
    from niu_api.internal import lightrag_manager

    one_error_result = {
        "vdb_entities.json": {"status": "error", "message": "无可用数据源重建"},
        "vdb_relationships.json": {"status": "ok", "rebuilt_count": 3},
        "vdb_chunks.json": {"status": "ok", "rebuilt_count": 10},
        "entity_sync": {"status": "ok"},
        "relationship_sync": {"status": "ok"},
    }
    with mock.patch.object(lightrag_manager, "repair_all", return_value=one_error_result):
        with mock.patch.object(lightrag_manager, "check_all", return_value={"ok": False, "total_errors": 1}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is False
    assert "vdb_entities.json" in result["repair_result"]
    assert result["repair_result"]["vdb_entities.json"]["status"] == "error"


def test_run_repair_sync_error_returns_repaired_false():
    """entity_sync 或 relationship_sync status=error 时，repaired=False"""
    from niu_api.internal import lightrag_manager

    sync_error_result = {
        "vdb_entities.json": {"status": "ok"},
        "vdb_relationships.json": {"status": "ok"},
        "vdb_chunks.json": {"status": "ok"},
        "entity_sync": {"status": "error", "message": "GraphML 读取失败"},
        "relationship_sync": {"status": "ok"},
    }
    with mock.patch.object(lightrag_manager, "repair_all", return_value=sync_error_result):
        with mock.patch.object(lightrag_manager, "check_all", return_value={"ok": False}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is False
```

- [ ] **Step 3.2**：跑测试确认失败（当前 `repaired=True` 硬编码，第一个测试通过，后两个失败）
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_repair_result_display.py -v 2>&1 | tail -40
```
**预期失败**：
- `test_run_repair_one_vdb_error_returns_repaired_false` 失败（repaired 是 True）
- `test_run_repair_sync_error_returns_repaired_false` 失败（repaired 是 True）

---

### Task 4: P2 实现 — run_repair_on_user_request 遍历 results

**目标**：把 `repaired=True` 硬编码改为遍历 results dict，任一 vdb `status==error` 则 `repaired=False`。

- [ ] **Step 4.1**：编辑 `niu_api/internal/lightrag_manager.py` `run_repair_on_user_request`（L1014-1053）

当前代码 L1040-1045：
```python
        logger.info(f"[LightRAG] 修复完成: {repair_result}, 重检: ok={check_result.get('ok')}")
        return {
            "repaired": True,
            "check_ok": check_result.get("ok", True),
            "repair_result": repair_result,
            "check_result": check_result,
        }
```

改为：
```python
        # v7: 遍历 repair_result 判定 repaired，任一 status=error 则 False
        # repair_all 永不抛异常（收集每个 vdb 的 status 到 dict），
        # 所以不能只看是否抛异常，要看 results dict 里每个条目的 status
        repaired = True
        for vdb_name, vdb_result in repair_result.items():
            if not isinstance(vdb_result, dict):
                continue
            if vdb_result.get("status") == "error":
                repaired = False
                logger.warning(f"[LightRAG] 修复失败项: {vdb_name} - {vdb_result.get('message', '')}")

        logger.info(f"[LightRAG] 修复完成: repaired={repaired}, 重检: ok={check_result.get('ok')}")
        return {
            "repaired": repaired,
            "check_ok": check_result.get("ok", True),
            "repair_result": repair_result,
            "check_result": check_result,
        }
```

- [ ] **Step 4.2**：编辑 `niu_api/kg_api.py` `/lightrag/repair` 端点改 async + asyncio.to_thread

当前代码 kg_api.py L1084-1103：
```python
@router.post("/lightrag/repair")
def repair_lightrag_storage(target: str = "all") -> dict:
    """修复 LightRAG 存储（用户在 splash 点'尝试修复'触发）。
    ...
    """
    from fastapi import HTTPException
    from niu_api.internal.lightrag_manager import run_repair_on_user_request

    if target != "all":
        raise HTTPException(status_code=400, detail=f"v5 只支持 target=all，收到: {target}")

    result = run_repair_on_user_request()
    return {"status": "ok", "result": result}
```

**问题**：`def repair_lightrag_storage` 是同步函数，run_repair_on_user_request 跑几千个 entity 本地 embedding 阻塞 FastAPI event loop 数十秒。期间 splash 轮询 status 会超时，整个 API 卡死。

改为（async def + asyncio.to_thread 把同步阻塞调用挪到线程池）：
```python
@router.post("/lightrag/repair")
async def repair_lightrag_storage(target: str = "all") -> dict:
    """修复 LightRAG 存储（用户在 splash 点'尝试修复'触发）。

    实际路径：/api/kg/lightrag/repair（router prefix=/api/kg + 端点 /lightrag/repair）

    v6: 改 async def + asyncio.to_thread，避免 repair_all 跑几千个 entity
        本地 embedding 阻塞 FastAPI event loop 数十秒（期间 splash 轮询
        status 会超时）。
    v5: 调 run_repair_on_user_request（封装 repair_all + reset_init_state + 重跑 check_all）。
    v5 只支持 target=all（用户决策驱动，不分单文件修复）。

    Args:
        target: 只支持 "all"（其他值返回 400）
    """
    import asyncio
    from fastapi import HTTPException
    from niu_api.internal.lightrag_manager import run_repair_on_user_request

    if target != "all":
        raise HTTPException(status_code=400, detail=f"v5 只支持 target=all，收到: {target}")

    result = await asyncio.to_thread(run_repair_on_user_request)
    return {"status": "ok", "result": result}
```

**注意**：
- `async def` 让 FastAPI 在 await 期间释放 event loop，其他请求（splash 轮询 status）可正常处理
- `asyncio.to_thread` 把同步阻塞的 `run_repair_on_user_request` 挪到线程池执行
- 函数签名变化（`def` → `async def`），但 HTTP 调用方（main.rs）只看返回的 JSON，不感知同步/异步

- [ ] **Step 4.3**：Python 语法检查
```bash
cd <repo_root>
python -c "from niu_api.kg_api import repair_lightrag_storage; import inspect; assert inspect.iscoroutinefunction(repair_lightrag_storage); from niu_api.internal.lightrag_manager import run_repair_on_user_request; print('OK')"
```

- [ ] **Step 4.4**：跑 Task 3 全部测试
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_repair_result_display.py -v
```
**预期**：3 个测试全部通过。

---

### Task 5: P2 实现 — 前端 RepairResult 成功分支弹结果对话框

**目标**：让前端 `RepairResult` 成功分支也弹结果对话框，列出每个 vdb 的 status/message，用户点确定 → ExitApp。

- [ ] **Step 5.1**：编辑 `launcher/src/main.rs` `RepairResult` 分支（L403-430）

当前代码：
```rust
            SplashMessage::RepairResult(result) => {
                // 无论修复成功或失败都退出（用户重启做下一轮检测）
                if let Err(e) = result {
                    // 修复失败：弹一个简短的提示对话框...
                    let (tx, rx) =
                        iced::futures::channel::oneshot::channel::<()>();
                    std::thread::spawn(move || {
                        rfd::MessageDialog::new()
                            .set_title("修复失败")
                            .set_description(&format!(
                                "修复失败：{}\n\n请从备份恢复后重启程序。",
                                e
                            ))
                            .set_buttons(rfd::MessageButtons::Ok)
                            .set_level(rfd::MessageLevel::Error)
                            .show();
                        let _ = tx.send(());
                    });
                    return Task::perform(
                        async move { rx.await.unwrap_or(()) },
                        |_| SplashMessage::ExitApp,
                    );
                }
                // 修复成功 → 退出
                Task::done(SplashMessage::ExitApp)
            }
```

改为（成功分支也弹对话框，解析 JSON 列清单）：
```rust
            SplashMessage::RepairResult(result) => {
                // 无论修复成功或失败都退出（用户重启做下一轮检测）
                match result {
                    Err(e) => {
                        // 修复失败：弹"修复失败"对话框 + ExitApp
                        let (tx, rx) =
                            iced::futures::channel::oneshot::channel::<()>();
                        std::thread::spawn(move || {
                            rfd::MessageDialog::new()
                                .set_title("修复失败")
                                .set_description(&format!(
                                    "修复失败：{}\n\n请从备份恢复后重启程序。",
                                    e
                                ))
                                .set_buttons(rfd::MessageButtons::Ok)
                                .set_level(rfd::MessageLevel::Error)
                                .show();
                            let _ = tx.send(());
                        });
                        return Task::perform(
                            async move { rx.await.unwrap_or(()) },
                            |_| SplashMessage::ExitApp,
                        );
                    }
                    Ok(resp_text) => {
                        // 修复成功：解析 JSON 列清单，弹"修复结果"对话框 + ExitApp
                        // API 返回格式：{"status":"ok","result":{"repaired":bool,"check_ok":bool,
                        //   "repair_result":{"vdb_entities.json":{"status":"ok","rebuilt_count":5,...},...}}}
                        let summary = format_repair_summary(&resp_text);
                        let (tx, rx) =
                            iced::futures::channel::oneshot::channel::<()>();
                        std::thread::spawn(move || {
                            rfd::MessageDialog::new()
                                .set_title("修复结果")
                                .set_description(&summary)
                                .set_buttons(rfd::MessageButtons::Ok)
                                .set_level(rfd::MessageLevel::Info)
                                .show();
                            let _ = tx.send(());
                        });
                        return Task::perform(
                            async move { rx.await.unwrap_or(()) },
                            |_| SplashMessage::ExitApp,
                        );
                    }
                }
            }
```

- [ ] **Step 5.2**：在 `launcher/src/main.rs` 新增 `format_repair_summary` 函数

在 `impl Splash` 之前（约 L130 附近，`enum SplashMessage` 之后）新增：
```rust
/// 把 /api/kg/lightrag/repair 的响应文本格式化成弹窗摘要。
///
/// 响应格式：{"status":"ok","result":{"repaired":bool,"check_ok":bool,
///   "repair_result":{"vdb_entities.json":{"status":"ok","rebuilt_count":5,...},...}}}
/// 解析失败时退回原始文本（截断到 500 字符避免超长）。
fn format_repair_summary(resp_text: &str) -> String {
    match serde_json::from_str::<serde_json::Value>(resp_text) {
        Ok(v) => {
            let result = v.get("result");
            let repaired = result
                .and_then(|r| r.get("repaired"))
                .and_then(|b| b.as_bool())
                .unwrap_or(false);
            let check_ok = result
                .and_then(|r| r.get("check_ok"))
                .and_then(|b| b.as_bool())
                .unwrap_or(false);

            let overall = if repaired && check_ok {
                "修复成功，所有数据一致性检查通过。"
            } else if repaired {
                "修复完成，但仍有数据一致性警告（详见下方清单）。"
            } else {
                "修复未全部成功，部分项目失败（详见下方清单）。"
            };

            let mut lines = vec![overall.to_string(), String::new()];

            if let Some(repair_result) = result.and_then(|r| r.get("repair_result")).and_then(|r| r.as_object()) {
                lines.push("修复清单：".to_string());
                for (name, detail) in repair_result {
                    let status = detail
                        .get("status")
                        .and_then(|s| s.as_str())
                        .unwrap_or("unknown");
                    let message = detail
                        .get("message")
                        .and_then(|m| m.as_str())
                        .unwrap_or("");
                    let rebuilt_count = detail
                        .get("rebuilt_count")
                        .and_then(|c| c.as_u64());
                    let source = detail
                        .get("source")
                        .and_then(|s| s.as_str())
                        .unwrap_or("");
                    let status_marker = if status == "ok" { "成功" } else { "失败" };
                    let mut line = format!("  {} [{}]: {}", name, status_marker, if message.is_empty() { status } else { message });
                    if let Some(cnt) = rebuilt_count {
                        line.push_str(&format!("（重建 {} 条）", cnt));
                    }
                    lines.push(line);

                    // vdb_relationships.json 走截断修复时，追加数据丢失风险提示
                    // （断点后的 relationship 永久丢失，GraphML 有但 vdb 无，
                    //  当前无 check_relationship_sync 检测、无 GraphML 反向补齐）
                    if name == "vdb_relationships.json"
                        && status == "ok"
                        && source == "vdb_truncate_repair"
                    {
                        lines.push(
                            "    注意：截断修复可能丢失部分关系数据（GraphML 有但 vdb 重建后缺失），详情见日志".to_string()
                        );
                    }
                }
            }

            lines.push(String::new());
            lines.push("确定后程序将完全退出，请重新启动程序。".to_string());

            lines.join("\n")
        }
        Err(_) => {
            // JSON 解析失败：退回原始文本（截断）
            let truncated = if resp_text.len() > 500 {
                format!("{}...(已截断)", &resp_text[..500])
            } else {
                resp_text.to_string()
            };
            format!(
                "修复已完成（无法解析详细结果）。\n\n原始响应：\n{}\n\n确定后程序将完全退出，请重新启动程序。",
                truncated
            )
        }
    }
}
```

- [ ] **Step 5.3**：用 launcher/build.sh 编译（铁律 #8）
```bash
cd <repo_root>
./launcher/build.sh 2>&1 | tail -20
```
**预期**：编译成功，`niu` 二进制更新到项目根目录。

- [ ] **Step 5.4**：如果编译失败，检查 serde_json 依赖
```bash
cd <repo_root>
grep "serde_json" launcher/Cargo.toml
```
**预期**：`serde_json` 已在依赖里（main.rs 顶部 `use serde::Deserialize` 表明 serde 已引入，serde_json 通常一起引入）。如未引入，加 `serde_json = "1"` 到 `[dependencies]`。

---

### Task 6: TDD — P3 repair 截断修复失败测试

**目标**：用 pytest 写测试覆盖 (1) vdb_entities.json 截断（断在 vector 字段中间）能恢复断点前所有完整 entity (2) data 数组为空（首个对象就截断）返回 error (3) vdb_relationships.json 同样适用。

- [ ] **Step 6.1**：创建测试文件 `tests/test_lightrag_truncate_repair.py`
```python
"""vdb JSON 截断修复的单元测试。

背景：vdb_entities.json 截断在 vector 字段中间的 base64 时，
_read_data_from_vdb json.load 抛 JSONDecodeError 直接返回 None，
无截断修复逻辑。本测试验证：
1. 截断在 data 数组中间时，_try_truncate_repair 能恢复断点前所有完整 entity
2. 截断在首个对象就截断时（data 数组恢复后为空）返回 error
3. vdb_relationships.json 同结构同样适用
"""
import json
import shutil
from pathlib import Path
from unittest import mock


def _make_valid_vdb(entity_count: int = 5) -> dict:
    """生成一个完整的 vdb_entities.json 结构（含 vector 字段）"""
    import base64
    import zlib
    import numpy as np

    data = []
    vectors = []
    for i in range(entity_count):
        vec = np.array([float(i)] * 8, dtype=np.float16)
        data.append({
            "__id__": f"ent-{i:04x}",
            "entity_name": f"entity_{i}",
            "content": f"这是实体 {i} 的描述",
            "vector": base64.b64encode(zlib.compress(vec.tobytes())).decode(),
        })
        vectors.append(vec)
    matrix_f32 = np.array(vectors, dtype=np.float32)
    return {
        "embedding_dim": 8,
        "data": data,
        "matrix": base64.b64encode(matrix_f32.tobytes()).decode(),
    }


def test_try_truncate_repair_recovers_complete_entities(tmp_path):
    """截断在 data 数组中间时，能恢复断点前所有完整 entity"""
    from niu_api.internal import lightrag_repair

    # 1. 生成完整 vdb
    full_vdb = _make_valid_vdb(entity_count=5)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 2. 截断：在第三个 entity 的 vector 字段中间截断
    full_text = vdb_path.read_text(encoding="utf-8")
    # 找到第三个 entity 的 vector 字段位置
    marker = '"entity_2"'  # 第三个 entity 的 entity_name
    marker_pos = full_text.find(marker)
    # 在 marker 之后 100 字符处截断（落在 vector base64 中间）
    truncate_pos = marker_pos + 100
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    # 3. monkeypatch _STORAGE_DIR 到 tmp_path
    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        # 4. _read_data_from_vdb 应该失败（JSON 截断）
        data = lightrag_repair._read_data_from_vdb("vdb_entities.json")
        assert data is None, "截断的 vdb 应该 json.load 失败"

        # 5. _try_truncate_repair 应该恢复前两个 entity（断点前完整的）
        truncated_data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert truncated_data is not None, "截断修复应能恢复部分数据"
        # 第三个 entity 被截断，应只恢复前两个
        assert len(truncated_data) == 2
        assert truncated_data[0]["entity_name"] == "entity_0"
        assert truncated_data[1]["entity_name"] == "entity_1"


def test_try_truncate_repair_first_entity_truncated_returns_none(tmp_path):
    """首个对象就截断时（data 数组恢复后为空）返回 None"""
    from niu_api.internal import lightrag_repair

    full_vdb = _make_valid_vdb(entity_count=3)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第一个 entity 的 vector 字段中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"entity_0"'
    marker_pos = full_text.find(marker)
    truncate_pos = marker_pos + 50  # 在第一个 entity 的 vector 中间
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert data is None, "首个对象截断应返回 None（data 数组恢复后为空）"


def test_try_truncate_repair_relationships_same_logic(tmp_path):
    """vdb_relationships.json 同结构同样适用"""
    from niu_api.internal import lightrag_repair

    import base64
    import zlib
    import numpy as np

    # 生成 3 个关系的 vdb
    data = []
    vectors = []
    for i in range(3):
        vec = np.array([float(i)] * 8, dtype=np.float16)
        data.append({
            "__id__": f"rel-{i:04x}",
            "src_id": f"src_{i}",
            "tgt_id": f"tgt_{i}",
            "content": f"关系 {i} 的描述",
            "vector": base64.b64encode(zlib.compress(vec.tobytes())).decode(),
        })
        vectors.append(vec)
    matrix_f32 = np.array(vectors, dtype=np.float32)
    full_vdb = {
        "embedding_dim": 8,
        "data": data,
        "matrix": base64.b64encode(matrix_f32.tobytes()).decode(),
    }
    vdb_path = tmp_path / "vdb_relationships.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第二个关系 vector 中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"src_1"'
    marker_pos = full_text.find(marker)
    truncate_pos = marker_pos + 80
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        data = lightrag_repair._try_truncate_repair("vdb_relationships.json")
        assert data is not None
        assert len(data) == 1  # 只恢复第一个关系
        assert data[0]["src_id"] == "src_0"


def test_repair_vdb_uses_truncate_repair_when_json_load_fails(tmp_path):
    """repair_vdb 在 _read_data_from_vdb 失败时，应尝试 _try_truncate_repair"""
    from niu_api.internal import lightrag_repair

    full_vdb = _make_valid_vdb(entity_count=4)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第二个 entity vector 中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"entity_1"'
    marker_pos = full_text.find(marker)
    truncated_text = full_text[:marker_pos + 100]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        # mock _embed_text 返回固定向量（避免依赖真实 embedding 模型）
        def mock_embed(text):
            return [0.1] * 8
        with mock.patch.object(lightrag_repair, "_embed_text", mock_embed):
            result = lightrag_repair.repair_vdb("vdb_entities.json")
            assert result["status"] == "ok"
            assert result["rebuilt_count"] == 1  # 只恢复第一个 entity
            assert result["source"] == "vdb_truncate_repair"
```

- [ ] **Step 6.2**：跑测试确认失败（当前没有 `_try_truncate_repair` 函数，repair_vdb 不调用截断修复）
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_truncate_repair.py -v 2>&1 | tail -40
```
**预期失败**：ImportError（找不到 `_try_truncate_repair`）。

---

### Task 7: P3 实现 — _try_truncate_repair + repair_vdb 集成

**目标**：实现括号配平计数法的截断修复，让 repair_vdb 在 _read_data_from_vdb 失败时调用 _try_truncate_repair 恢复断点前所有完整 entity。

- [ ] **Step 7.1**：在 `niu_api/internal/lightrag_repair.py` 新增 `_try_truncate_repair` 函数

在 `_read_data_from_kv_store` 之后（L143 之后），`repair_vdb` 之前新增：
```python
def _try_truncate_repair(vdb_filename: str) -> list[dict] | None:
    """vdb JSON 截断修复：括号配平找最后完整对象边界，补 ]} 闭合。

    场景：vdb_entities.json 写入过程中崩溃，文件断在 data 数组某个对象的
    vector 字段中间的 base64 字符串里，json.load 抛 JSONDecodeError。

    策略（括号配平计数法，避免 content 字段里 `}` 干扰）：
    1. 读原始字节
    2. 找 "data":[ 的位置，从这之后开始扫描
    3. 从 data_start 起逐字符扫描，维护 depth 计数器：
       - 遇 `{` depth+1，遇 `}` depth-1
       - depth 从 1 回到 0 时，记录一个"完整对象结束位置"
         （此 `}` 的下一位即对象边界，后续应是 `,` 或 `]`）
    4. 取最后一个完整对象结束位置，截断到该 `}` 之后，补 `]}` 闭合
    5. json.loads 验证，提取 data 列表
    6. data 数组为空（首个对象就截断，没有任何完整对象）→ 返回 None

    为什么不用"找任意 }"：
      content 字段（用户文档原文/实体描述）经常含 `}`（代码片段、JSON 示例、
      模板字符串）。简单找 `}` 会在 base64 vector 之前命中 content 里的 `}`，
      导致截断位置落在对象内部，补 `]}` 后 json.loads 抛错，截断修复静默失败。
      括号配平能区分"对象闭合的 }"和"字符串里的 }"，因为字符串里的 } 不会
      让 depth 回到 0。

    字符串处理简化：本扫描器不处理转义字符串内的 `{`/`}`（如 JSON 字符串
    里嵌套 JSON 字面量）。但这种场景下：配平法会把字符串内的 `{` 也计入 depth，
    导致最后一个完整对象结束位置偏后。最坏情况是截断后 json.loads 失败，
    返回 None，repair_vdb 继续 fallback kv_store，不引入新 bug。
    比简单找 `}` 可靠性显著提升。

    Returns:
        data 列表（含 __id__ + content），如果恢复后 data 为空或无法解析返回 None。
    """
    vdb_path = _storage_dir() / vdb_filename
    if not vdb_path.exists():
        return None

    try:
        raw_bytes = vdb_path.read_bytes()
    except Exception:
        return None

    raw_text = raw_bytes.decode("utf-8", errors="replace")

    # 1. 找 "data" 字段起始位置
    # JSON 格式：{"embedding_dim":N,"data":[{...},{...},...,"matrix":"...（截断）
    # 找 "data":[ 的位置（容忍空格）
    import re
    data_match = re.search(r'"data"\s*:\s*\[', raw_text)
    if not data_match:
        return None  # 没有 data 字段，无法修复

    data_start = data_match.end()  # data 数组开始位置（[ 之后）

    # 2. 括号配平计数：扫描所有完整对象的结束位置
    # depth 计数：遇 { +1，遇 } -1。depth 从 1 回到 0 时，此 } 是一个完整对象的结束
    complete_positions = []  # 每个完整对象结束 } 的位置（指向 } 字符）
    depth = 0
    i = data_start
    n = len(raw_text)
    while i < n:
        ch = raw_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # 此 } 是 data 数组里一个完整对象的结束
                complete_positions.append(i)
        i += 1

    if not complete_positions:
        return None  # 没有任何完整对象（首个对象就截断在 { 之后某处）

    # 3. 截到最后一个完整对象的 }（含），补 ]} 闭合
    last_obj_end = complete_positions[-1]  # 最后一个完整对象结束 } 的位置
    truncated_text = raw_text[:last_obj_end + 1] + "]}"  # 闭合 data 数组和外层 dict

    # 4. 解析截断后的 JSON
    try:
        parsed = json.loads(truncated_text)
    except (json.JSONDecodeError, Exception):
        return None

    data_list = parsed.get("data")
    if not isinstance(data_list, list) or not data_list:
        return None

    text_field = _VDB_TEXT_FIELD.get(vdb_filename, "content")
    valid = [item for item in data_list if isinstance(item, dict) and item.get(text_field)]
    return valid if valid else None
```

- [ ] **Step 7.2**：在 `repair_vdb` 中调用 `_try_truncate_repair`

当前 `repair_vdb` L145-170：
```python
    # 1. 优先从 vdb data 字段读
    data_list = _read_data_from_vdb(vdb_filename)
    source = "vdb_data_field" if data_list else None

    # 2. fallback 到 kv_store
    if not data_list:
        data_list, fallback_source = _read_data_from_kv_store(vdb_filename)
        if data_list:
            source = fallback_source

    if not data_list:
        return {
            "status": "error",
            "message": f"无可用数据源重建 {vdb_filename}（vdb data 和 fallback kv_store 都损坏）",
        }
```

改为（在 _read_data_from_vdb 失败后，先尝试截断修复，再 fallback kv_store）：
```python
    # 1. 优先从 vdb data 字段读
    data_list = _read_data_from_vdb(vdb_filename)
    source = "vdb_data_field" if data_list else None

    # 1.5. vdb JSON 截断时，尝试字节级括号配平截断修复
    #      （断在 vector base64 中间，json.load 失败，但断点前的完整 entity 可恢复）
    if not data_list:
        data_list = _try_truncate_repair(vdb_filename)
        if data_list:
            source = "vdb_truncate_repair"
            logger.info(f"[LightRAGRepair] 截断修复恢复 {vdb_filename}: {len(data_list)} 条")

    # 2. fallback 到 kv_store
    if not data_list:
        data_list, fallback_source = _read_data_from_kv_store(vdb_filename)
        if data_list:
            source = fallback_source

    if not data_list:
        return {
            "status": "error",
            "message": f"无可用数据源重建 {vdb_filename}（vdb data、截断修复、fallback kv_store 都失败）",
        }
```

- [ ] **Step 7.3**：Python 语法检查
```bash
cd <repo_root>
python -c "from niu_api.internal.lightrag_repair import _try_truncate_repair, repair_vdb; print('OK')"
```

- [ ] **Step 7.4**：跑 Task 6 全部测试
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_truncate_repair.py -v
```
**预期**：4 个测试全部通过。

- [ ] **Step 7.5**：跑现有 lightrag_repair 测试做回归
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_repair.py -v 2>&1 | tail -40
```
**预期**：全部通过（截断修复是新增逻辑，不改原有 repair_vdb 行为，只在新 fallback 路径调用）。

---

### Task 8: 回归测试 — 现有测试不破坏

**目标**：跑现有相关测试，确认改动不破坏其他模块。

- [ ] **Step 8.1**：跑 lightrag 相关全套测试
```bash
cd <repo_root>
python -m pytest tests/test_lightrag_repair.py tests/test_lightrag_integrity.py tests/test_lightrag_truncate_repair.py tests/test_lightrag_repair_result_display.py tests/test_lightrag_startup_block.py -v 2>&1 | tail -60
```
**预期**：全部通过。

- [ ] **Step 8.2**：跑 chat_queue 和 scheduler 回归测试
```bash
cd <repo_root>
python -m pytest tests/test_chat_queue.py tests/test_scheduler*.py -v 2>&1 | tail -40
```
**预期**：全部通过（pause 机制已有，不动；scheduler 不动）。

- [ ] **Step 8.3**：如果任何测试失败，立即撤销改动恢复原状（铁律 #5）
```bash
cd <repo_root>
# 不用 git checkout（铁律 #8），用 Edit 工具精确回退改动点
# 如有需要，从 Task 0 的备份 commit 恢复
git log --oneline -5  # 找到 Task 0 的 backup commit hash
```

---

### Task 9: P4 实现 — 文件损坏现场脚本

**目标**：提供 shell 脚本，先备份用户真实 vdb 到 `.corrupt.bak.test`，再制造截断现场，测完恢复。

- [ ] **Step 9.1**：创建脚本 `scripts/make_vdb_corrupt_test_env.sh`
```bash
#!/usr/bin/env bash
# 制造 vdb_entities.json 截断损坏现场，用于端到端测试启动阻塞 + repair 结果弹窗 + 截断修复。
#
# 安全策略：
# 1. 先备份用户真实 vdb_entities.json 到 vdb_entities.json.pre-corrupt-test.bak
# 2. 制造截断现场（head -c NNN 截断在 vector base64 中间）
# 3. 启动 ./niu，用户看到弹窗 → 点修复 → 看修复结果 → 程序退出
# 4. 测完恢复：mv vdb_entities.json.pre-corrupt-test.bak vdb_entities.json
#
# 用法：
#   ./scripts/make_vdb_corrupt_test_env.sh create   # 制造损坏现场
#   ./scripts/make_vdb_corrupt_test_env.sh restore  # 恢复真实 vdb
#   ./scripts/make_vdb_corrupt_test_env.sh status   # 查看当前状态

set -euo pipefail

STORAGE_DIR="${HOME}/.niu/lightrag_storage"
VDB_FILE="${STORAGE_DIR}/vdb_entities.json"
BACKUP_FILE="${STORAGE_DIR}/vdb_entities.json.pre-corrupt-test.bak"

if [[ ! -d "${STORAGE_DIR}" ]]; then
    echo "ERROR: lightrag_storage 目录不存在: ${STORAGE_DIR}"
    echo "       请先正常运行过程序一次，让 LightRAG 创建 storage 目录"
    exit 1
fi

if [[ ! -f "${VDB_FILE}" ]]; then
    echo "ERROR: vdb_entities.json 不存在: ${VDB_FILE}"
    echo "       请先正常运行过程序一次，让 LightRAG 写入 vdb"
    exit 1
fi

cmd="${1:-status}"
case "${cmd}" in
    create)
        # 1. 备份真实 vdb（如果备份已存在，不覆盖——避免覆盖前一次测试的备份）
        if [[ -f "${BACKUP_FILE}" ]]; then
            echo "WARN: 备份文件已存在: ${BACKUP_FILE}"
            echo "      可能上次测试未恢复。请先运行: $0 restore"
            read -p "      是否覆盖备份继续？(y/N) " confirm
            if [[ "${confirm:-N}" != "y" ]]; then
                echo "ABORTED"
                exit 1
            fi
        fi
        cp "${VDB_FILE}" "${BACKUP_FILE}"
        echo "BACKED UP: ${VDB_FILE} -> ${BACKUP_FILE}"

        # 2. 制造截断现场
        #    vdb_entities.json 格式：{"embedding_dim":N,"data":[{...},{...},...,"matrix":"base64..."}
        #    截断在 data 数组第二个 entity 的 vector 字段中间（base64 字符中间）
        #    注意：nano_vectordb 用 json.dump(storage, f, ensure_ascii=False) 保存，
        #    默认 separators 是 (', ', ': ')，所以输出是 "vector": "..."（冒号后有空格）。
        #    不能用 '"vector":"' 这个 marker（无空格），text.find 会返回 -1，导致
        #    TRUNCATE_POS=0 脚本报错退出。用正则 r'"vector"\s*:\s*"' 兼容有无空格。
        ORIG_SIZE=$(wc -c < "${VDB_FILE}")
        # 找第二个 entity 的 vector 字段位置，截断在它之后 50 字符
        # 用 python 正则匹配（shell 不好处理 JSON）
        TRUNCATE_POS=$(python3 -c "
import re
import sys
with open('${VDB_FILE}', 'rb') as f:
    raw = f.read()
text = raw.decode('utf-8', errors='replace')
# 用正则匹配 '\"vector\":\\s*\"'，兼容冒号后有无空格
# nano_vectordb save() 用默认 separators 输出 '\"vector\": \"'，但兼容无空格更健壮
marker_re = re.compile(r'\"vector\"\s*:\s*\"')
matches = list(marker_re.finditer(text))
if not matches:
    print(0)
    sys.exit(0)
if len(matches) == 1:
    # 只有一个 entity，截断在第一个 vector 中间
    truncate_at = matches[0].end() + 50
else:
    # 截断在第二个 vector 字段中间
    truncate_at = matches[1].end() + 50
print(truncate_at)
")
        if [[ "${TRUNCATE_POS}" == "0" ]]; then
            echo "ERROR: 无法找到 vector 字段，vdb 格式异常"
            mv "${BACKUP_FILE}" "${VDB_FILE}"
            exit 1
        fi

        # 用 head -c 截断
        head -c "${TRUNCATE_POS}" "${BACKUP_FILE}" > "${VDB_FILE}"
        NEW_SIZE=$(wc -c < "${VDB_FILE}")
        echo "TRUNCATED: ${VDB_FILE} (${ORIG_SIZE} bytes -> ${NEW_SIZE} bytes, cut at ${TRUNCATE_POS})"
        echo ""
        echo "现在可以启动程序测试："
        echo "  cd <repo_root> && ./niu"
        echo ""
        echo "预期："
        echo "  1. splash 启动 → 检测到 vdb 损坏 → 弹'LightRAG 数据异常'对话框"
        echo "  2. 点'是-尝试修复'"
        echo "  3. splash 显示'正在修复'"
        echo "  4. 修复完成后弹'修复结果'对话框，列出每个 vdb 的 status"
        echo "  5. 点'确定' → 程序退出"
        echo ""
        echo "测完恢复："
        echo "  $0 restore"
        ;;

    restore)
        if [[ ! -f "${BACKUP_FILE}" ]]; then
            echo "ERROR: 备份文件不存在: ${BACKUP_FILE}"
            echo "       无需恢复（可能从未执行 create）"
            exit 1
        fi
        mv "${BACKUP_FILE}" "${VDB_FILE}"
        echo "RESTORED: ${BACKUP_FILE} -> ${VDB_FILE}"
        echo "现在可以正常启动程序"
        ;;

    status)
        if [[ -f "${BACKUP_FILE}" ]]; then
            echo "STATUS: 测试模式（备份存在）"
            echo "  原始备份: ${BACKUP_FILE} ($(wc -c < "${BACKUP_FILE}") bytes)"
            echo "  当前 vdb: ${VDB_FILE} ($(wc -c < "${VDB_FILE}") bytes)"
            echo "  恢复命令: $0 restore"
        else
            echo "STATUS: 正常模式（无备份）"
            echo "  当前 vdb: ${VDB_FILE} ($(wc -c < "${VDB_FILE}") bytes)"
            echo "  制造损坏: $0 create"
        fi
        ;;

    *)
        echo "Usage: $0 {create|restore|status}"
        exit 1
        ;;
esac
```

- [ ] **Step 9.2**：给脚本可执行权限
```bash
cd <repo_root>
chmod +x scripts/make_vdb_corrupt_test_env.sh
```

---

### Task 10: 真实端到端验证（真实损坏现场 + 真实 LLM）

**目标**：用 P4 的损坏现场脚本，端到端验证 P1 启动阻塞 + P2 repair 结果弹窗 + P3 截断修复 + 程序退出。

**铁律 #5 要求**：测试必须用真实数据 + 真实 LLM，不 mock。

- [ ] **Step 10.1**：清理测试环境（杀掉所有 niu 进程）
```bash
cd <repo_root>
ps aux | grep -E "niu|launcher" | grep -v grep
# 用 kill -TERM <pid> 逐个优雅退出（铁律 #7 不能 pkill -f niu）
```

- [ ] **Step 10.2**：检查 lightrag_storage 状态
```bash
ls -la ~/.niu/lightrag_storage/vdb_entities.json
# 检查当前状态（应该是正常模式）
./scripts/make_vdb_corrupt_test_env.sh status
```

- [ ] **Step 10.3**：制造损坏现场
```bash
cd <repo_root>
./scripts/make_vdb_corrupt_test_env.sh create
```
**预期输出**：BACKED UP + TRUNCATED + 测试说明。

- [ ] **Step 10.4**：启动程序
```bash
cd <repo_root>
./niu &
# 等待 splash 显示
```

- [ ] **Step 10.5**：验证 P1 启动阻塞
```bash
# 监控日志，确认 scheduler 没有 signal ready + ChatQueue 被 pause + db_monitor 没启动 + delayed start 被 cancel
tail -f logs/api_stderr.log | grep -E "Scheduler system_ready|ChatQueue paused|db_monitor|Phase 1|need_repair|delayed start|Delayed start"
```
**预期日志序列**：
1. `[LightRAG] Phase 1 完成: check_ok=False, total_errors=N` — 检测到损坏
2. `[LightRAG] ChatQueue paused due to LightRAG corruption` — P1 阻塞
3. `[SCHEDULER] Delayed start cancelled (start_delayed will no-op on timeout)` — P1 阻塞（cancel_delayed_start）
4. `[LightRAG] Scheduler delayed start cancelled due to LightRAG corruption` — P1 阻塞
5. `[LightRAG] db_monitor 跳过启动（LightRAG 损坏，等用户决策）` — P1 阻塞
6. `[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏）` — P1 阻塞
7. `[LightRAG] 检测到损坏，等待用户在 rfd 弹窗选择'退出'或'尝试修复'`
8. **不应出现** `Scheduler system_ready signal sent`
9. **不应出现** `db_monitor task 已启动`
10. **不应出现** `LightRAG instance initialized (eager)`
11. **不应出现** `Brain graph initialized`
12. **不应出现** `[INTERNAL SCHEDULER] Triggering task`（scheduler delayed start 被 cancel，60s 超时后 _delayed_start 线程检查 flag 直接 return）
13. **不应出现** `[SCHEDULER] Ready signal not received within 60s, forcing start`（cancel_delayed_start 让超时后不再强行 start）
14. **不应出现** `LightRAG background sync started`（Phase 1 check_ok=False 时不应启动后台同步，ChatQueue 已 paused）
15. **不应出现** `Pipeline watcher started`（LightRAG 损坏时不应启动 pipeline watcher）
16. **不应出现** `create_default regions`（Phase 1 检测到损坏后不应继续走 create_default 流程，避免在损坏现场上叠加默认数据）

- [ ] **Step 10.6**：验证 P2 修复结果弹窗

在 splash 弹出的 rfd 对话框点"是-尝试修复"。
**预期**：
1. splash 显示"正在修复"
2. 修复完成后弹"修复结果"对话框
3. 对话框内容：
   - 标题"修复结果"
   - 总体状态（修复成功/警告/失败）
   - 修复清单（每个 vdb 一行，含 status/message/rebuilt_count）
   - 末尾"确定后程序将完全退出，请重新启动程序"
4. 点确定 → 程序退出

- [ ] **Step 10.7**：验证 P3 截断修复

修复完成后检查日志：
```bash
grep -E "截断修复|vdb_truncate_repair|repair_vdb" logs/api_stderr.log | tail -20
```
**预期**：
- `[LightRAGRepair] 截断修复恢复 vdb_entities.json: N 条` — P3 截断修复生效
- 修复结果对话框里 vdb_entities.json 行显示 `成功` + `（重建 N 条）`

**额外验证——vdb_entities.json 完整性（json.load 可解析）**：
```bash
python -c "import json; d=json.load(open('~/.niu/lightrag/vdb_entities.json')); print('OK, entries=', len(d))"
```
**预期**：
- 退出码 0，输出 `OK, entries= N`（N>0）
- 不应抛 `json.JSONDecodeError`（确认截断修复后文件是合法 JSON，不是半截写坏的文件）
- 若有多个 vdb（vdb_entities.json / vdb_relationships.json / vdb_chunks.json），每个都跑一次 json.load 验证

- [ ] **Step 10.8**：恢复真实 vdb
```bash
cd <repo_root>
./scripts/make_vdb_corrupt_test_env.sh restore
```
**预期输出**：RESTORED。

- [ ] **Step 10.9**：测试完彻底杀进程（铁律 #7）
```bash
ps aux | grep -E "niu|launcher" | grep -v grep | awk '{print $2}' | xargs -I {} kill -TERM {}
sleep 5
ps aux | grep -E "niu|launcher" | grep -v grep  # 应为空
```

- [ ] **Step 10.10**：可选——再做一次正常启动验证（确认恢复后程序能正常启动）
```bash
cd <repo_root>
./niu &
# 等待启动完成，看到 "LightRAG instance initialized (eager)" 和 API ready
# 看到 splash 消失，主窗口打开
# 然后 kill -TERM 退出
```

---

### Task 11: 提交修复

- [ ] **Step 11.1**：检查改动范围
```bash
cd <repo_root>
git status
git diff --stat
```
**预期**：改动文件包括
- `niu_api/__main__.py`
- `niu_api/internal/lightrag_manager.py`
- `niu_api/internal/lightrag_repair.py`
- `niu_api/internal/scheduler/scheduler.py`（新增 cancel_delayed_start 方法）
- `niu_api/kg_api.py`（/lightrag/repair 改 async def + asyncio.to_thread）
- `launcher/src/main.rs`
- 新增 `tests/test_lightrag_startup_block.py`
- 新增 `tests/test_lightrag_repair_result_display.py`
- 新增 `tests/test_lightrag_truncate_repair.py`
- 新增 `scripts/make_vdb_corrupt_test_env.sh`
- 新增 `docs/superpowers/plans/2026-07-09-startup-block-and-repair.md`

- [ ] **Step 11.2**：提交修复
```bash
cd <repo_root>
git add niu_api/__main__.py niu_api/internal/lightrag_manager.py niu_api/internal/lightrag_repair.py niu_api/internal/scheduler/scheduler.py niu_api/kg_api.py launcher/src/main.rs tests/test_lightrag_startup_block.py tests/test_lightrag_repair_result_display.py tests/test_lightrag_truncate_repair.py scripts/make_vdb_corrupt_test_env.sh docs/superpowers/plans/2026-07-09-startup-block-and-repair.md
git commit -m "$(cat <<'EOF'
fix(lightrag): 启动阻塞+repair结果展示+截断修复

问题一：LightRAG 损坏时 scheduler/ChatQueue/db_monitor 不阻塞，
60s 超时强行扫描触发 journal-agent → ChatQueue → runner.chat 报错。
修复：Phase 1 检测到 need_repair=True 时不调 signal_scheduler_ready
+ pause ChatQueue + 不启动 db_monitor + 调 scheduler.cancel_delayed_start
（补 P1 漏洞：scheduler 60s 超时强行 start）+ 跳过 Phase 1 之后所有依赖
LightRAG 的初始化（LightRAGSync/BrainGraph/RegionSync/_SYSTEM_TASKS）。

问题二：run_repair_on_user_request repaired=True 硬编码，repair_all
永不抛异常所以永远成功。前端 RepairResult 成功分支不弹结果对话框。
修复：repaired 改为遍历 results dict，任一 vdb status=error 则 False。
前端 RepairResult 成功分支也弹结果对话框，列出每个 vdb 的 status/
message，用户点确定 → ExitApp。/lightrag/repair 端点改 async def +
asyncio.to_thread（避免 repair_all 阻塞 event loop，期间 splash 轮询
status 不超时）。

问题三：vdb_entities.json 截断（断在 vector 字段中间的 base64）时，
_read_data_from_vdb json.load 失败直接返回 None，无截断修复逻辑。
修复：新增 _try_truncate_repair 用括号配平计数法（遇 { depth+1，
遇 } depth-1，depth 从 1 回到 0 即一个完整对象结束）找最后一个
完整对象的 } 边界，截掉半截补 ]} 闭合，拿到断点前所有完整 entity
走 repair_vdb 后续流程。括号配平能避免 content 字段里 } 干扰
边界识别。vdb_relationships.json 同结构同样适用。

已知局限：vdb_relationships.json 截断修复会永久丢失断点后的
relationship（repair_relationship_sync 只删不补，check_all 无
check_relationship_sync）。本次通过 P2 弹窗提示让用户感知风险，
完整修复（GraphML 反向补齐 + check_relationship_sync）留作后续 issue。

P4: scripts/make_vdb_corrupt_test_env.sh 提供安全可恢复的损坏现场
（先备份真实 vdb 到 .pre-corrupt-test.bak，制造截断，测完恢复）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 11.3**：git 操作后修复文件权限（铁律 #7）
```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null
```

- [ ] **Step 11.4**：验证提交成功
```bash
cd <repo_root>
git log --oneline -3
git status
```

---

## Self-Review

### P1 启动阻塞是否系统全面（不只三个任务，所有该阻塞的都覆盖）

- [x] **scheduler**：不调 signal_scheduler_ready + 调 cancel_scheduler_delayed_start_if_corrupt（scheduler.py L103-106 的 60s 超时强行 start 漏洞被真正堵住——service.py L173-178 的 signal_scheduler_ready 在 lifespan L189 调用，P1 加 need_repair gate；scheduler.py 新增 cancel_delayed_start 方法，Phase 1 need_repair=True 时设 _delayed_start_cancelled=True，_delayed_start 线程 60s 超时后检查到 flag 直接 return，不再强行 start。原 Self-Review 说法"scheduler 60s 超时强行跑的漏洞被堵住"在 v1 计划里不准确（v1 只不调 signal_scheduler_ready，没设 _delayed_start_cancelled），v1.1 修订补 cancel_delayed_start 后才真正堵住）
- [x] **ChatQueue**：pause ChatQueue worker（chat_queue.py L179-185 已有 pause 机制，复用）
- [x] **db_monitor**：推迟到 Phase 1 之后启动，need_repair=True 时跳过（lifespan L185 改为条件启动）
- [x] **LightRAGSync**：已 gate（用 wait_lightrag_ready），且 P1 加 `_lightrag_corrupt_skip_init` 跳过 L236-242 的 start
- [x] **BrainGraph**：P1 加 `_lightrag_corrupt_skip_init` 跳过 L244-251 的 ensure_niu_entity
- [x] **create_default_regions**：P1 加 `_lightrag_corrupt_skip_init` 跳过 L262-272
- [x] **RegionSync**：已 gate（用 wait_brain_ready + wait_lightrag_ready），且 P1 加 `_lightrag_corrupt_skip_init` 跳过 L274-298 的 start
- [x] **_SYSTEM_TASKS**：P1 加 `_lightrag_corrupt_skip_init` 跳过 L300-359 的 task create/update（虽然不立即触发，但为清晰仍 gate）
- [x] **_daily_tmp_cleanup**：不依赖 LightRAG，保留（清临时文件无害）
- [x] **HAWatcher**：不依赖 LightRAG（仅轮询 HomeAssistant），保留
- [x] **IMGateway**：TCP Server 启动后只 accept，pause ChatQueue 即可阻断 IM 消息触发 runner.chat，保留启动
- [x] **PipelineWatcher**：只推 SSE 事件，不调 runner.chat，保留

**全面性结论**：所有触发 runner.chat 或调 LightRAG 实例的任务都覆盖。LightRAGSync/RegionSync 原本就 gate（用 wait_lightrag_ready），P1 只补 scheduler/ChatQueue/db_monitor 三个漏洞 + 跳过 Phase 1 之后的初始化。

### P2 repair 结果弹窗是否覆盖成功 + 失败两种情况

- [x] **成功分支**：解析 JSON `{"status":"ok","result":{...}}`，列出每个 vdb 的 status/message/rebuilt_count，弹"修复结果"对话框 + ExitApp
- [x] **失败分支（HTTP 错误）**：保持现有"修复失败"对话框 + ExitApp
- [x] **JSON 解析失败**：退回原始文本（截断到 500 字符），仍弹对话框 + ExitApp
- [x] **repaired=False 但 check_ok=True**：摘要显示"修复完成，但仍有数据一致性警告"
- [x] **repaired=True 但 check_ok=False**：摘要显示"修复完成，但仍有数据一致性警告"
- [x] **repaired=True 且 check_ok=True**：摘要显示"修复成功，所有数据一致性检查通过"

### P3 截断修复的边界情况是否处理

- [x] **data 数组中间截断**（断在第二个 entity 的 vector 中间）：恢复断点前所有完整 entity（第一个 entity）
- [x] **首个对象就截断**（data 数组恢复后为空）：返回 None，repair_vdb 走 fallback kv_store 或返回 error
- [x] **vdb_relationships.json 同结构**：同样的括号配平截断修复逻辑
- [x] **vdb_chunks.json 有 fallback**：截断修复在 fallback 之前调用，截断修复失败再 fallback kv_store
- [x] **JSON 边界识别**：用括号配平计数法（遇 `{` depth+1，遇 `}` depth-1，depth 从 1 回到 0 即一个完整对象结束），避免 content 字段里 `}`（代码片段/JSON 示例/模板字符串）在 base64 vector 之前命中导致截断位置落在对象内部、补 `]}` 后 json.loads 抛错、截断修复静默失败。简单"找任意 }"不可靠，已废弃。
- [x] **matrix 也截断**：matrix 字段在 data 之后，data 截断时 matrix 必然也截断或丢失，但 repair_vdb 重新 embedding 后会重建 matrix，所以不需要单独处理 matrix 截断

### P3 已知局限：vdb_relationships.json 截断修复会永久丢失断点 relationship

- [x] **风险已标注**：`repair_relationship_sync`（lightrag_repair.py L486-521）只删除孤儿关系 + 改大小写，**不补齐缺失关系**（不读 GraphML edges 反向补回 vdb 缺失的 relationship）。`check_all`（lightrag_integrity.py L379-414）只有 `check_entity_sync`，**无 `check_relationship_sync`**。后果：vdb_relationships.json 截断修复丢失断点 relationship 后，repair_relationship_sync 不报错，check_all 不报错，重启不弹窗，但关系数据已永久丢失（GraphML 有 edge 但 vdb 没有对应 relationship 记录，检索时少这条关系）。
- [x] **本次不实现 relationship 从 GraphML edge 重建**：改动大（需要新增 `repair_relationship_sync` 的补齐逻辑 + `check_relationship_sync` 检测函数），本次先标注风险，后续单独 issue 处理。
- [x] **P2 修复结果弹窗加提示**：在 Task 5 Step 5.2 的 `format_repair_summary` 函数里，如果 `repair_result` 包含 `vdb_relationships.json` 且其 `source` 或 `status` 表明走了截断修复，弹窗末尾追加提示"注意：relationship 截断修复可能丢失部分关系数据（GraphML 有但 vdb 重建后缺失），详情见日志"。
- [x] **日志记录**：repair_relationship_sync 在截断修复路径执行后，应 log warning 告知"vdb_relationships 截断修复后未做 GraphML 反向补齐，断点后的 relationship 可能丢失"（这个 log 在 repair_vdb 走 vdb_truncate_repair source 时输出，不需要改 repair_relationship_sync 本身）。

**数据丢失风险结论**：vdb_relationships.json 截断修复后，断点之后的 relationship 会永久丢失。当前无检测（check_all 无 check_relationship_sync）、无补齐（repair_relationship_sync 不补齐）、无告警（重启不弹窗）。本次通过 P2 弹窗提示让用户感知风险，完整修复（GraphML 反向补齐 + check_relationship_sync）留作后续 issue。

### P4 损坏现场是否安全（不动用户真实数据，可恢复）

- [x] **先备份**：`cp vdb_entities.json vdb_entities.json.pre-corrupt-test.bak`（备份存在时不覆盖，提示先 restore）
- [x] **制造截断**：`head -c NNN` 从备份截断到当前 vdb（不动备份）
- [x] **测完恢复**：`mv vdb_entities.json.pre-corrupt-test.bak vdb_entities.json`（恢复真实 vdb）
- [x] **状态查询**：`status` 命令显示当前模式（测试/正常）
- [x] **不覆盖已有备份**：create 时如备份已存在，提示先 restore，用户确认才覆盖
- [x] **marker 正则匹配**：v1.1 修订把 marker 从 `'"vector":"'`（无空格）改为正则 `r'"vector"\s*:\s*"'`，兼容 nano_vectordb `save()` 用 `json.dump(storage, f, ensure_ascii=False)` 默认 separators 输出的 `"vector": "..."`（冒号后有空格）。原 marker `text.find` 返回 -1 → TRUNCATE_POS=0 → 脚本报错退出，正则匹配健壮性更好（兼容有无空格）

### 有没有引入新 bug 的风险

- [x] **P1 不破坏正常启动**：need_repair=False 时所有逻辑跟原来一致（should_signal_scheduler_ready 返回 True，pause_chatqueue_if_corrupt 不调 pause，should_start_db_monitor 返回 True，cancel_scheduler_delayed_start_if_corrupt 不调 cancel_delayed_start）
- [x] **P1 cancel_delayed_start 不影响 scheduler 正常启动**：need_repair=False 时不调 cancel_delayed_start，scheduler.start_delayed 正常走（_ready_event.set 后 sleep 2s 开始扫描）；need_repair=True 时调 cancel_delayed_start 设 _delayed_start_cancelled=True，_delayed_start 线程 60s 超时后检查 flag 直接 return，不强行 start。注意 cancel_delayed_start 不能在 start_delayed 之前调（_delayed_start_cancelled 在 start_delayed 开头被重置为 False），lifespan 顺序是 L67 start_scheduler → Phase 1 → cancel，时序正确
- [x] **P2 不破坏 repair 成功路径**：所有 vdb status=ok 时 repaired=True（测试覆盖）
- [x] **P2 kg_api async 不破坏调用方**：/lightrag/repair 改 async def + asyncio.to_thread 后，HTTP 响应格式不变（仍是 `{"status":"ok","result":{...}}`），前端 main.rs 只看 JSON 不感知同步/异步。asyncio.to_thread 把同步阻塞调用挪到线程池，FastAPI event loop 在 await 期间可处理其他请求（splash 轮询 status 不超时）
- [x] **P3 不破坏原有 repair_vdb**：截断修复只在 _read_data_from_vdb 失败后调用，成功路径不走截断修复（测试覆盖）
- [x] **P3 截断修复失败不影响 fallback**：_try_truncate_repair 返回 None 时，repair_vdb 继续走 _read_data_from_kv_store fallback
- [x] **前端 serde_json 依赖**：main.rs 顶部已 `use serde::Deserialize`，serde_json 通常一起引入；如未引入，Step 5.4 检查 Cargo.toml
- [x] **db_monitor_task 可能为 None**：shutdown 时加 `if db_monitor_task is not None` 检查（Step 2.7）
- [x] **ChatQueue pause 后不 resume**：程序退出时 ChatQueue 跟随整体 shutdown，不需要 resume（stop_chat_queue 会 cancel worker task）

### 潜在风险点（需注意）

- **IMGateway 在 LightRAG 损坏时仍启动**：如果用户在损坏期间收到 IM 消息，消息会入队 ChatQueue 但被 pause 阻塞，不触发 runner.chat。但 IM 消息会堆积在队列里，程序退出时丢失。这是可接受的——损坏期间用户不应处理 IM 消息，退出后重启 IM 消息需要用户重新发送。
- **_daily_tmp_cleanup 在 LightRAG 损坏时仍跑**：它只清临时文件，不依赖 LightRAG，无害。但凌晨 4 点才触发，启动期间不会跑。
- **HAWatcher 在 LightRAG 损坏时仍跑**：它轮询 HomeAssistant，不调 runner.chat。如果 HA 设备状态变化触发 alert，alert 会入队 ChatQueue 但被 pause 阻塞。同 IM，可接受。
- **截断修复的边界识别（已用括号配平法，风险显著降低）**：括号配平能区分"对象闭合的 }"和"字符串里的 }"（字符串内的 } 不会让 depth 回到 0）。但本扫描器不处理转义字符串内的 `{`/`}`（如 JSON 字符串里嵌套 JSON 字面量），这种场景下配平会把字符串内的 `{` 也计入 depth，导致最后一个完整对象结束位置偏后。最坏情况是截断后 json.loads 失败返回 None，repair_vdb 继续 fallback kv_store，不引入新 bug。相比之前"找任意 }"方案（content 字段含 `}` 时静默失败），可靠性显著提升。
- **_lightrag_corrupt_skip_init 变量作用域（已加固）**：计划明确要求此变量必须是 lifespan 函数内的局部变量，不得提升为模块级全局。理由：模块级全局在 exit 路径上不会被清除，下次正常启动（模块缓存/reload）仍读到 True，会错误跳过所有初始化，导致 LightRAG 完全不可用。子 Agent 实施时禁止用 `global _lightrag_corrupt_skip_init` 声明。Task 2.5 已在代码块注释中固化此约束。

---

## Execution Handoff

实施顺序：
1. **Task 0**：临时备份提交（铁律 #3）
2. **Task 1-2**：P1 启动阻塞（TDD + 实现）
3. **Task 3-4**：P2 后端 repaired 判定（TDD + 实现）
4. **Task 5**：P2 前端 RepairResult 弹窗（直接改 + 编译）
5. **Task 6-7**：P3 截断修复（TDD + 实现）
6. **Task 8**：回归测试
7. **Task 9**：P4 损坏现场脚本
8. **Task 10**：端到端验证（真实损坏现场 + 真实 LLM）
9. **Task 11**：提交修复 + 修权限

**关键铁律传达给子 Agent**：
- 修改前必须先做临时提交备份（Task 0 已做，子 Agent 改前可以再做一次子备份）
- 禁止 `git reset --hard` / force push
- 测试必须用真实数据 + 真实 LLM（Task 10 用 P4 脚本制造真实损坏现场）
- git 操作后必须修权限（Task 11.3）
- Rust 改完必须用 `./launcher/build.sh`，禁止直接 `cargo build`（Task 5.3）
- 派出去的子 Agent 必须遵守所有铁律

**需要用户决策的设计选择**：
- 无。所有设计选择在排查结论里已确定，计划按用户要求实施。
