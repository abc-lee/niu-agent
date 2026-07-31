# background_script 后台静默定时任务 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `background_script` 任务类型，到点执行 Python 脚本，无输出静默、有输出（含报错）才通知主 Agent（含 IM）。

**Architecture:** 在 `trigger_callback` 内联分支——`background_script` 走"读脚本→code_run→判 stdout→有输出 enqueue / 无输出静默"，`reminder` 走原逻辑。调度器、CAS、失败计数器、cron_parser 全部复用不改。

**Tech Stack:** Python 3.11+ / SQLite / FastAPI / iced（无）/ 现有 code_run（subprocess）

**Spec:** `docs/superpowers/specs/2026-07-31-background-script-design.md`

---

## 文件结构

| 文件 | 责任 | 动作 |
|------|------|------|
| `niu_api/internal/scheduler/task_store.py` | 任务数据模型 + SQLite | 改：加 `task_kind`/`script_file` 列 + 迁移 + create/get/list/update 签名 |
| `niu_api/internal/scheduler/service.py` | 触发回调链路 | 改：`trigger_callback` 加 background_script 分支 |
| `tests/test_scheduler_service.py` | trigger_callback 单元测试 | 改：加 background_script 分支测试 |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | MCP 工具接口 | 改：schedule_task schema + 函数加 task_kind/script_file |
| `config/disk/scheduler-server.yaml` | 虚拟磁盘映射 | 改：schedule_task parameters 加 task_kind/script_file |
| `memory/skills/background-script.md` | 系统级 skill | 新建 |
| `config/agents/niu.md` | 主 Agent 提示词 | 改：定时任务段落加两句 |

**不动的**：`scheduler.py`（调度循环）、`cron_parser.py`（触发器，留给下个工程）、`chat_queue.py`（静默分支不 enqueue 即天然不触发）、`handler.py` 的 code_run（只复用不改）。`routes.py` 需改（Task 3 Step 4，必须，否则 /scheduler API 拒绝 task_kind/script_file 参数）。

---

## Task 1: task_store 加 task_kind/script_file 列与迁移

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py:20-69`（_init_db 迁移块）
- Modify: `niu_api/internal/scheduler/task_store.py:71-96`（create_task）
- Modify: `niu_api/internal/scheduler/task_store.py:98-138`（list_tasks SELECT 与 row_to_dict）
- Modify: `niu_api/internal/scheduler/task_store.py:190-260`（update_task，若涉及字段）
- Modify: `niu_api/internal/scheduler/task_store.py:320-354`（**get_overdue_tasks——调度器喂给 trigger_callback 的唯一查询，必须改，否则 task_kind 永远 None**）
- Test: `tests/test_scheduler_service.py`（先验证迁移，再加业务测试）

- [ ] **Step 1: 加迁移语句（_init_db 内，仿照现有 name/chat_id 迁移模式）**

在 `task_store.py` `_init_db` 的 chat_id 迁移块之后（约 L66 `conn.commit()` 之前）加：

```python
            # 迁移：老数据库可能没有 task_kind 列
            try:
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN task_kind TEXT DEFAULT 'reminder'
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 迁移：老数据库可能没有 script_file 列
            try:
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN script_file TEXT
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
```

- [ ] **Step 2: 改 create_task 签名与 INSERT**

把 `create_task` 签名改为（在 `chat_id` 参数后加两个）：

```python
    def create_task(
        self,
        content: str,
        scheduled_at: str,
        event_type: str = "reminder",
        is_recurring: bool = False,
        cron_expr: str | None = None,
        name: str | None = None,
        chat_id: str | None = None,
        task_kind: str = "reminder",
        script_file: str | None = None
    ) -> str:
```

INSERT 语句改为（加 task_kind, script_file 两列）：

```python
            conn.execute("""
                INSERT INTO scheduled_tasks
                (id, content, scheduled_at, is_recurring, cron_expr, event_type, status, name, chat_id, task_kind, script_file)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type, name, chat_id, task_kind, script_file))
```

- [ ] **Step 3: 改 list_tasks 的 SELECT 加两列**

list_tasks 有两个 SELECT（status 过滤 / 无过滤），都加 `task_kind, script_file`。同时改 row_to_dict（约 L119-138）加这两个字段映射。SELECT 列改为：

```sql
SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id, task_kind, script_file
FROM scheduled_tasks
...
```

row_to_dict 的 keys 列表加 `"task_kind"`, `"script_file"`（注意保持与 SELECT 顺序一致，否则字段错位——核对现有 row_to_dict 的 zip/keys 实现，按其模式追加）。

- [ ] **Step 4: 改 get_task（同样加两列到 SELECT + dict 映射）**

get_task 约 L260-300，同样加 `task_kind, script_file` 到 SELECT 与返回 dict。
- [ ] **Step 4b: 改 get_overdue_tasks 的 SELECT 与 dict 映射（关键！调度器生产路径）**

`get_overdue_tasks`（L320-354）是调度器 `_check_and_trigger_impl` 取待触发任务的唯一方法，它的 SELECT 与返回 dict **必须**加 `task_kind, script_file`，否则 trigger_callback 里 `task.get("task_kind")` 永远 None，background_script 分支永不触发。

SELECT 改为（L330）：
```sql
SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id, task_kind, script_file
FROM scheduled_tasks
WHERE status = 'pending' AND datetime(scheduled_at) <= datetime(?)
ORDER BY scheduled_at
LIMIT 50
```

返回 dict（L339-353）加两个字段（紧跟 chat_id 之后，与 SELECT 列顺序一致）：
```python
        return [
            {
                "id": row[0],
                "content": row[1],
                "scheduled_at": row[2],
                "is_recurring": bool(row[3]),
                "cron_expr": row[4],
                "event_type": row[5],
                "status": row[6],
                "created_at": row[7],
                "last_executed_date": row[8],
                "name": row[9],
                "chat_id": row[10],
                "task_kind": row[11],
                "script_file": row[12]
            }
            for row in rows
        ]
```

**注意**：`find_task_by_name`（task_store.py L157-188，SELECT + 手动 dict 映射 11 列）**必须**同步加 task_kind/script_file 到 SELECT 与 dict（row[11]/row[12]）——它是除 get_overdue_tasks/list_tasks/get_task 外唯一返回完整 task dict 的查询，缺列会导致未来调用方走 trigger_callback 时 task_kind 为 None。`recover_orphaned_tasks`、`reset_stale_in_progress`、`retry_failed_tasks` 等方法若也有 SELECT + dict 映射，同样检查并加列（grep `SELECT id, content` 全文件，所有返回 task dict 的 SELECT 都要加 task_kind, script_file 以保持一致）。

- [ ] **Step 5: 改 update_task（若 update 支持改 task_kind/script_file）**

读 update_task 现有签名（约 L190-260）。若它支持动态字段更新，加 task_kind/script_file 可选参数；若它是固定字段更新，按其模式追加。**不改 update_task 的 CAS 逻辑。**

- [ ] **Step 6: 写迁移测试——验证老库迁移不报错且新列默认值正确**

在 `tests/test_scheduler_service.py` 末尾加：

```python
import os
import tempfile
from niu_api.internal.scheduler.task_store import TaskStore


class TestTaskStoreMigration:
    def test_new_db_has_task_kind_and_script_file_columns(self, tmp_path):
        """新建库自动含 task_kind(script_file 列，task_kind 默认 reminder"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(content="t", scheduled_at="2026-01-01 00:00:00")
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_kind"] == "reminder"
        assert tasks[0]["script_file"] is None

    def test_old_db_migrates_adds_columns(self, tmp_path):
        """模拟老库（无 task_kind/script_file 列）迁移后可正常读写"""
        db_path = str(tmp_path / "old.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        # 建一个不含新列的老表
        conn.execute("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL, is_recurring INTEGER DEFAULT 0,
                cron_expr TEXT, event_type TEXT, status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO scheduled_tasks (id, content, scheduled_at) VALUES ('old1', 'old', '2026-01-01 00:00:00')")
        conn.commit()
        conn.close()
        # 重新初始化触发迁移
        store = TaskStore(db_path)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_kind"] == "reminder"  # 迁移后默认值
        assert tasks[0]["script_file"] is None

    def test_create_background_script_task(self, tmp_path):
        """创建 background_script 任务存入 task_kind/script_file"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="清理临时文件", scheduled_at="2026-01-01 00:00:00",
            task_kind="background_script", script_file="clean_tmp.py",
            is_recurring=True, cron_expr="0 3 * * *",
        )
        tasks = store.list_tasks()
        assert tasks[0]["task_kind"] == "background_script"
        assert tasks[0]["script_file"] == "clean_tmp.py"
```

- [ ] **Step 7: 跑测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_scheduler_service.py::TestTaskStoreMigration -v`
Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
git add niu_api/internal/scheduler/task_store.py tests/test_scheduler_service.py
git commit -m "feat(scheduler): task_store 加 task_kind/script_file 列与迁移"
```

---

## Task 2: trigger_callback 加 background_script 分支

**Files:**
- Modify: `niu_api/internal/scheduler/service.py:57-150`（trigger_callback）
- Test: `tests/test_scheduler_service.py`（加分支测试）

**关键事实（来自 spec 审查核实）：**
- `code_run` 在 `agent/handler.py:307`，签名 `code_run(code, code_type="python", timeout=60, cwd=None) -> dict`
- 返回 dict：成功 `{"status":"success","stdout":str,"exit_code":0}`；失败 `{"status":"error","stdout":str_with_stderr,"exit_code":int}`；进程启动失败 `{"status":"error","msg":str}`（无 stdout 键）
- stderr 合并进 stdout（`stderr=subprocess.STDOUT`），无独立 stderr
- 超时：stdout 追加 `\n[Timeout Error] Process killed after {timeout}s`，status='error'
- stdout 已被 code_run 截断到 10000 字符，定时注入再截断到 2000
- workspace 路径：`get_db_path()` 已在 service.py，`workspace = Path(get_db_path()).parent`，`scripts_dir = workspace / "scripts"`
- `enqueue_and_wait(source="scheduler")`，chat_queue 内部强制 assistant 回复 source 改 'electron' 推 SSE
- 现有 trigger_callback 末尾有 `add_pending_alert` 调用（蹦高）——background_script 静默分支不调，有输出分支走 enqueue 后由现有逻辑调

- [ ] **Step 1: 读 trigger_callback 完整实现（确认 add_pending_alert 在哪）**

Read: `niu_api/internal/scheduler/service.py:120-160`，确认 enqueue 成功后 add_pending_alert 的调用位置与参数（content 用于 alert 摘要）。

- [ ] **Step 2: 写失败测试——静默分支（stdout 空 + 成功）不调 enqueue**

在 `tests/test_scheduler_service.py` 加：

```python
class TestTriggerCallbackBackgroundScript:
    """background_script 分支测试"""

    def _make_bg_task(self, script_file="clean.py"):
        return {
            "id": "bg1", "content": "清理", "task_kind": "background_script",
            "script_file": script_file, "is_recurring": True, "cron_expr": "0 3 * * *",
        }

    def test_silent_success_no_enqueue(self, tmp_path, monkeypatch):
        """脚本 stdout 空 + exit 0 → 静默，不调 enqueue_and_wait"""
        from niu_api.internal.scheduler import service

        # workspace = tmp_path, scripts/clean.py 存在
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("import os\nprint('', end='')\n")

        # get_db_path 返回 tmp_path 下，使 workspace=tmp_path
        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        # code_run 返回静默成功
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "", "exit_code": 0})

        enqueue_called = []
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_and_wait": lambda self, **kw: enqueue_called.append(kw) or "repl"
        }()))

        result = service.trigger_callback(self._make_bg_task())
        assert result is not None  # 静默成功返回 truthy（调度器据此走成功路径，非 None=成功）
        assert result == "(silent)"
        assert enqueue_called == []  # 未通知

    def test_has_output_enqueues(self, tmp_path, monkeypatch):
        """脚本 stdout 非空 → enqueue_and_wait 注入主 Agent"""
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("print('有垃圾')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "有垃圾", "exit_code": 0})

        captured = {}
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_and_wait": lambda self, **kw: captured.update(kw) or "Agent处理"
        }()))

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.internal.scheduler.service.asyncio") as mock_a, \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.channel.get_channel_router") as mock_cr:
            mock_cr.return_value.has_channel.return_value = False  # 无 IM 通道，跳过推送
            mock_a.run_coroutine_threadsafe.return_value = MagicMock(result=lambda: "Agent处理", timeout=300)
            result = service.trigger_callback(self._make_bg_task())

        assert result == "Agent处理"
        assert captured["content"].startswith("[定时任务]")
        assert "有垃圾" in captured["content"]
        assert captured["source"] == "scheduler"

    def test_error_enqueues_with_stderr(self, tmp_path, monkeypatch):
        """脚本异常 → code_run status=error，stdout(含traceback) 注入主 Agent"""
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("raise Exception('boom')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "error", "stdout": "Traceback...boom", "exit_code": 1})

        captured = {}
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_and_wait": lambda self, **kw: captured.update(kw) or "Agent处理"
        }()))
        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.internal.scheduler.service.asyncio") as mock_a, \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.channel.get_channel_router") as mock_cr:
            mock_cr.return_value.has_channel.return_value = False
            mock_a.run_coroutine_threadsafe.return_value = MagicMock(result=lambda: "Agent处理", timeout=300)
            result = service.trigger_callback(self._make_bg_task())

        assert "Traceback" in captured["content"]
        assert result is None  # 报错走失败路径（spec：报错=失败+通知，调度器走失败计数器）

    def test_missing_script_file_returns_none_no_enqueue(self, tmp_path, monkeypatch):
        """脚本文件不存在 → 永久删除任务 + 返回 None，不调 code_run/enqueue"""
        from niu_api.internal.scheduler import service

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        # scripts 目录存在但文件不存在
        (tmp_path / "scripts").mkdir()

        code_run_called = []
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: code_run_called.append(1) or {"status": "success", "stdout": "", "exit_code": 0})

        enqueue_called = []
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_and_wait": lambda self, **kw: enqueue_called.append(kw) or "x"
        }()))

        deleted = []
        monkeypatch.setattr(service, "get_store", lambda: type("S", (), {
            "delete_task_permanent": lambda self, tid: deleted.append(tid)
        }()))

        result = service.trigger_callback(self._make_bg_task(script_file="nonexistent.py"))
        assert result is None
        assert code_run_called == []  # 文件不存在不调 code_run
        assert enqueue_called == []
        assert deleted == ["bg1"]  # 任务被永久删除

    def test_stdout_truncated_to_2000(self, tmp_path, monkeypatch):
        """stdout 超 2000 字符 → 截断"""
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("print('x'*5000)\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "x"*5000, "exit_code": 0})

        captured = {}
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_and_wait": lambda self, **kw: captured.update(kw) or "ok"
        }()))

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.internal.scheduler.service.asyncio") as mock_a, \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.channel.get_channel_router") as mock_cr:
            mock_cr.return_value.has_channel.return_value = False
            mock_a.run_coroutine_threadsafe.return_value = MagicMock(result=lambda: "ok", timeout=300)
            service.trigger_callback(self._make_bg_task())

        # [定时任务] 前缀 + 截断提示 + ≤2000 字符正文
        assert len(captured["content"]) < 2200
        assert "…[截断]" in captured["content"]  # 截断标记必须存在（spec：超出加提示）
```

- [ ] **Step 3: 跑测试验证全部失败（函数未改）**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_scheduler_service.py::TestTriggerCallbackBackgroundScript -v`
Expected: 5 failed（trigger_callback 还没分支，reminder 路径会把 content 当 prompt）

- [ ] **Step 4: 改 trigger_callback——开头加 background_script 分支**

在 `service.py` trigger_callback 函数体开头（`logger.info(...)` 之前或之后，`prompt = ...` 之前）加分支。完整改法：

在文件顶部 import 区（service.py L7-17）加模块级 import（**关键：必须模块级，测试才能 monkeypatch service.code_run 等**）：
```python
from agent.handler import code_run
from niu_api.chat_queue import get_chat_queue
```
（`from pathlib import Path` 已存在于 L12，勿重复加。`add_pending_alert`/`get_channel_router`/`_main_loop` 仍在函数内局部 import，与现有 reminder 分支一致。）

在 trigger_callback 内，把现有的 `prompt = f"[定时任务] {task['content']}"` 这行**之前**插入 background_script 分支。改后 trigger_callback 开头结构：

```python
def trigger_callback(task: dict) -> str | None:
    """...（保留原 docstring，末尾追加 background_script 说明）...
    
    background_script 任务：读 {workspace}/scripts/{script_file} → code_run →
    stdout 空 + 成功 = 静默返回 None；有 stdout 或 status=error = stdout 注入主 Agent。
    脚本文件不存在 = 永久删除任务（recurring 亦然，避免无限重试；用户恢复脚本需重建任务）。
    """
    from niu_api.alerts import add_pending_alert
    from niu_api.chat import _main_loop

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # ===== background_script 分支 =====
    if task.get("task_kind") == "background_script":
        return _trigger_background_script(task, _main_loop, add_pending_alert)

    # ===== reminder 原逻辑（不动） =====
    from niu_api.chat_queue import get_chat_queue  # reminder 局部 import 保持原样
    prompt = f"[定时任务] {task['content']}"
    # ... 原有代码全部保留 ...
```

**注意**：reminder 分支的 `from niu_api.chat_queue import get_chat_queue` 若原本就在函数顶部，保持原位不动；background_script 分支用模块级的 `get_chat_queue`（已 import）。若现有 reminder 也是局部 import，不要挪动它（避免行为变化）。

然后在 service.py 内 trigger_callback 之外新增辅助函数 `_trigger_background_script`：

```python
def _trigger_background_script(task: dict, main_loop, add_alert_fn) -> str | None:
    """background_script 触发：跑脚本，有输出才通知主 Agent。
    
    复用模块级 code_run / get_chat_queue（service.py 顶部已 import）。
    IM 推送与 reminder 分支保持一致（enqueue 后调 add_pending_alert + channel_router.push）。
    """
    script_file = task.get("script_file")
    if not script_file:
        logger.error(f"[BG_SCRIPT] task {task.get('id')} 无 script_file")
        return None

    # workspace = get_db_path 父目录，scripts_dir = workspace/scripts
    db_path = get_db_path()
    scripts_dir = Path(db_path).parent / "scripts"
    script_path = scripts_dir / script_file

    if not script_path.exists():
        # 永久性失败：删除任务（recurring 亦然），避免 retry_failed_tasks 无限重试
        logger.error(f"[BG_SCRIPT] 脚本不存在: {script_path}，永久删除任务 {task.get('id')}")
        try:
            store = get_store()
            store.delete_task_permanent(task["id"])
        except Exception as e:
            logger.error(f"[BG_SCRIPT] 删除任务失败: {e}")
        return None

    code = script_path.read_text(encoding="utf-8")
    logger.info(f"[BG_SCRIPT] 执行 {script_file} (cwd={scripts_dir})")

    result = code_run(code=code, code_type="python", timeout=60, cwd=str(scripts_dir))

    # 取 stdout（进程启动失败时 dict 无 stdout 键）
    if result.get("status") == "error" and "stdout" not in result:
        output = result.get("msg", "进程启动失败")
        is_error = True
    else:
        output = (result.get("stdout") or "").strip()
        is_error = result.get("status") != "success" or result.get("exit_code") != 0

    # 静默：成功 + 无输出 → 返回 truthy（非 None），让调度器走成功路径
    # （调度器用 `result is None` 判失败：None→标failed/retry；非None→one-time硬删除/recurring reschedule）
    # 若返回 None：one-time 静默成功会进 retry_failed_tasks 无限重试、recurring 静默3次后标 failed 卡死
    if not is_error and not output:
        logger.info(f"[BG_SCRIPT] {script_file} 静默完成（无输出）")
        return "(silent)"  # truthy 占位，调度器据此走成功路径

    # 有输出或报错 → 注入主 Agent
    if not output:
        output = "(无 stdout，但执行失败)" if is_error else ""

    # 截断 2000 字符
    if len(output) > 2000:
        output = output[:2000] + "…[截断]"

    prompt = f"[定时任务] {output}"

    loop = main_loop
    if loop is None or loop.is_closed():
        logger.error("[BG_SCRIPT] Main event loop not available")
        return None

    try:
        q = get_chat_queue()
        future = asyncio.run_coroutine_threadsafe(
            q.enqueue_and_wait(content=prompt, source="scheduler", session_id="default"),
            loop,
        )
        agent_reply = future.result(timeout=300)
        if not agent_reply:
            logger.warning("[BG_SCRIPT] Agent returned empty reply")
            return None

        logger.info(f"[BG_SCRIPT] Agent replied: {agent_reply[:100]}")

        # ===== 与 reminder 分支对齐：蹦高 + IM 推送（复制 service.py L131-148 逻辑） =====
        task_content = task.get("content", "⏰")
        alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
        try:
            add_alert_fn(alert_text)
        except Exception as e:
            logger.warning(f"[BG_SCRIPT] add_pending_alert failed: {e}")

        # IM 通道推送
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            if router.has_channel("im"):
                push_chat_id = task.get("chat_id") or ""
                push_future = asyncio.run_coroutine_threadsafe(
                    router.push(agent_reply, "im", push_chat_id),
                    loop,
                )
                push_future.result(timeout=30)
        except Exception as e:
            logger.warning(f"[BG_SCRIPT] IM push failed: {e}")

        # 报错（is_error）走失败路径：返回 None 让调度器走失败计数器/retry（spec：报错=失败+通知）
        # 有输出但非报错（status=success+exit_code 0+stdout 非空）走成功路径：返回 agent_reply
        return None if is_error else agent_reply
    except Exception as e:
        logger.error(f"[BG_SCRIPT] ChatQueue call failed: {e}")
        return None
```

**关键修复说明（对照 Round 1 审查）**：
1. `code_run`/`get_chat_queue` 改为**模块级 import**（service.py 顶部），测试 `monkeypatch.setattr(service, "code_run", ...)` 才能生效（P1#3）
2. `add_alert_fn(alert_text)` **传 content 摘要参数**（P1#4，对齐 service.py L134）
3. **补 IM 推送块**（P2#8，复制 L136-148，含 channel_router.push）
4. `get_store()` **直接调用**（get_store 在 service.py 本模块，L189，无需 import；P0#2）
5. recurring 文件不存在也永久删除（P2#7，设计取舍：脚本丢失=配置错误，永久删除避免无限重试，用户恢复脚本需重建任务——这是 spec 已确认的设计）

**注意**：现有 reminder 分支末尾的 `add_pending_alert` 与 IM 推送调用保持原样不动。background_script 分支自带的蹦高+IM 与 reminder 对齐。


- [ ] **Step 5: 跑测试验证通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_scheduler_service.py::TestTriggerCallbackBackgroundScript -v`
Expected: 5 passed

- [ ] **Step 6: 回归现有 reminder 测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_scheduler_service.py::TestTriggerCallback -v`
Expected: 全部 passed（reminder 行为不变）

- [ ] **Step 7: Commit**

```bash
git add niu_api/internal/scheduler/service.py tests/test_scheduler_service.py
git commit -m "feat(scheduler): trigger_callback 加 background_script 静默分支"
```

---

## Task 3: MCP schedule_task 工具加 task_kind/script_file 参数

**Files:**
- Modify: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py:30-200`（TOOL_SCHEMAS + schedule_task 函数）

- [ ] **Step 1: 读现有 schedule_task 的 TOOL_SCHEMAS 与函数实现**

Read: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py:30-200`，确认 schema 结构（properties/required）与函数签名（它内部调 TaskStore.create_task）。

- [ ] **Step 2: TOOL_SCHEMAS 的 schedule_task 加 task_kind/script_file properties**

在 schedule_task 的 input_schema properties 里加（紧跟 chat_id 之后）：

```python
                "task_kind": {
                    "type": "string",
                    "enum": ["reminder", "background_script"],
                    "default": "reminder",
                    "description": "任务类型：reminder=提醒式（到点通知主 Agent）；background_script=后台静默脚本（有输出才通知）"
                },
                "script_file": {
                    "type": "string",
                    "description": "脚本文件名（仅 background_script 用，如 clean_tmp.py）。脚本须存于 {workspace}/scripts/ 下"
                },
```
**注意（一致性）**：`__init__.py` 的 `run_server()` 内 `@server.list_tools()` 有内联的 schedule_task schema 副本（stdio MCP 模式用），`call_tool` handler 内也有 `store.create_task(...)` 调用。虽然主路径走同进程 ToolRegistry（用 TOOL_SCHEMAS + 顶层函数），但为保持一致，`run_server()` 内的内联 schema 与 create_task 调用也同步加 task_kind/script_file。

- [ ] **Step 3: schedule_task 函数签名加参数并透传 create_task**

函数签名加 `task_kind="reminder"` 和 `script_file=None`（位置在 chat_id 之后），函数体调 `TaskStore.create_task(...)` 时透传 `task_kind=task_kind, script_file=script_file`。

**校验**：若 task_kind=='background_script' 且 script_file 为空，返回错误提示（不写库）：

```python
    if task_kind == "background_script" and not script_file:
        return {"error": "background_script 任务必须提供 script_file"}
```

- [ ] **Step 4: 改 routes.py 请求模型与 POST 端点（必须，否则 /scheduler API 拒绝 task_kind/script_file 参数）**

读 `niu_api/internal/scheduler/routes.py`，CreateTaskRequest（Pydantic model）加两字段：
```python
    task_kind: str = "reminder"
    script_file: str | None = None
```
POST `/scheduler/tasks` 端点的 create_task 调用透传 `task_kind=req.task_kind, script_file=req.script_file`。

- [ ] **Step 5: 手动验证 MCP 工具 + /scheduler API 可调**

启动 niu_api（`python/bin/python -m niu_api`），通过 /scheduler API 创建一个 background_script 任务，确认入库 task_kind/script_file 正确：

```bash
curl -s -X POST http://localhost:9876/scheduler/tasks -H "Content-Type: application/json" -d '{"content":"测试","scheduled_at":"2026-12-31 23:59:00","task_kind":"background_script","script_file":"test.py","is_recurring":true,"cron_expr":"0 3 * * *"}'
```

确认返回 task_id，再 `curl -s http://localhost:9876/scheduler/tasks` 确认 task_kind=background_script。

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py niu_api/internal/scheduler/routes.py
git commit -m "feat(scheduler): MCP schedule_task + /scheduler API 加 task_kind/script_file"
```

---

## Task 4: disk 映射加 task_kind/script_file

**Files:**
- Modify: `config/disk/scheduler-server.yaml`

- [ ] **Step 1: 读现有 schedule_task 参数映射格式**

Read: `config/disk/scheduler-server.yaml`，确认现有参数（如 cron_expr/is_recurring）的映射写法（position/flag/type/enum）。

- [ ] **Step 2: 加 task_kind（enum）与 script_file 参数映射**

现有 schedule_task 的 positional 参数只有 content(pos1)/scheduled_at(pos2)，其余参数（event_type/is_recurring/cron_expr）用 `flag` 不用 `position`（disk_config.py 校验 position 必须从 1 连续无间隔，加 position 3/4 会导致 gap 报错——这些是可选 flag 参数，不应占用 position）。**task_kind/script_file 也用 flag，不用 position**（对齐 event_type 写法）。

在 schedule_task 的 parameters 下、name 之前加（list 风格，与现有参数对齐）：

```yaml
      - name: task_kind
        flag: kind
        type: string
        enum: [reminder, background_script]
        default: reminder
      - name: script_file
        flag: script
        type: string
```

（参考 event_type 的 `flag: type` 写法，flag 值是短名不带 `--`，disk_config 内部处理前缀）

- [ ] **Step 3: 验证 niu_api 启动不报 disk 解析错误**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import yaml; yaml.safe_load(open('config/disk/scheduler-server.yaml'))" && echo OK`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add config/disk/scheduler-server.yaml
git commit -m "feat(scheduler): disk 映射加 task_kind/script_file"
```

---

## Task 5: 编写系统级 skill 文件

**Files:**
- Create: `memory/skills/background-script.md`

**内容**按 spec §3 大纲，frontmatter 对齐现有 skill 格式（参考 `memory/skills/note-management.md`）。

- [ ] **Step 1: 写 skill 文件**

创建 `memory/skills/background-script.md`，完整内容：

```markdown
---
name: background-script
description: Use when user asks to run scheduled background tasks silently, periodic cleanup, periodic checking (email/messages), or any task that should only notify on output. 后台静默定时任务, 脚本定时执行
status: active
created: 2026-07-31
last_tested: 2026-07-31
---

# Background Script（后台静默定时任务）

## Overview

`background_script` 是定时任务的一种类型，与 `reminder`（提醒式）并列。到点时调度器执行一段你预先写好的 Python 脚本：

- **脚本无输出（stdout 空 + 退出码 0）→ 静默**，不打扰任何人
- **脚本有输出（stdout 非空）→ 通知主 Agent**（含前端 SSE + 蹦高提醒 + IM 推送）
- **脚本报错（异常/非零退出/超时）→ 报错文本通知主 Agent** + 失败计数

适用场景：定时清理（无异常就静默）、定期检查邮件/消息（无新内容静默，有才通知处理）、监控类任务。

## When to use

| 场景 | 用哪个 |
|------|--------|
| 到点提醒用户做事 / 需要主 Agent 思考决策 | `reminder` |
| 代码能搞定、不需要 Agent 推理、无事静默有事才报 | `background_script` |
| 定时清理临时文件 | `background_script` |
| 定期检查邮箱新邮件 | `background_script` |
| 每天早上汇报天气 | `reminder`（需要 Agent 组织语言） |

## How to create

1. **写 Python 脚本**，存到工作目录的 `scripts/` 子目录下（即 `{workspace}/scripts/`）。
   - **先 `ls {workspace}/scripts/` 检查已有文件，避免覆盖同名脚本**
   - 目录不存在时自行创建
2. **调 `chat-with-event-manager`** 子 Agent，让它创建任务：
   ```
   schedule_task(
     task_kind='background_script',
     script_file='你的脚本.py',
     content='任务描述（人类可读）',
     cron_expr='0 3 * * *',        # 每天 3 点
     is_recurring=true
   )
   ```

## Script writing rules

- **`print()` 输出 = 通知主 Agent（含 IM）**；不 print 且退出码 0 = 静默。用 `print()` 精确控制是否通知。
- **异常 / 非零退出 / 超时 = 报错通知**：报错文本（含 traceback，stderr 合并进 stdout）会随通知发给主 Agent。recurring 任务连续 3 次失败标 failed；one-time 任务脚本文件丢失等永久性失败直接标 failed 不重试。
- **stdout 注入主 Agent 时截断 2000 字符**，长输出请自行截断或写文件后 print 文件路径。
- **cwd = `{workspace}/scripts/`**，脚本可用相对路径读写同目录文件（如 `open('data.json')`）。但 **不能直接 `import` 同目录其他 .py 文件**——code_run 把代码写到临时文件执行，`sys.path[0]` 是临时目录而非 cwd。多文件脚本需用 `exec(open('helper.py').read())` 或合并成单文件。
- **超时 60 秒**（code_run 默认），超时进程被杀、stdout 追加 `[Timeout Error]` 后作为报错通知。长任务请拆分。
- **运行环境**：项目自带的 Python 解释器与已装依赖（numpy/opencv/requests 等均可直接 import）。

## Examples

### 例1：静默清理临时文件

```python
# {workspace}/scripts/clean_tmp.py
import os, glob, shutil

tmp_dir = os.path.expanduser("~/Downloads/tmp")
removed = 0
for f in glob.glob(os.path.join(tmp_dir, "*")):
    try:
        if os.path.isdir(f):
            shutil.rmtree(f)
        else:
            os.remove(f)
        removed += 1
    except Exception:
        pass  # 单个失败不报错，继续

# 不 print → 静默。除非你想记录清理数量：
# print(f"已清理 {removed} 个临时文件")
```

### 例2：检查邮件（有新邮件才通知）

```python
# {workspace}/scripts/check_mail.py
import imaplib, email

conn = imaplib.IMAP4_SSL("imap.example.com")
conn.login("user", "pass")
conn.select("INBOX")
typ, data = conn.search(None, "UNSEEN")
ids = data[0].split()
conn.logout()

if not ids:
    # 无新邮件，不 print → 静默
    pass
else:
    # 有新邮件，print 摘要 → 通知主 Agent 处理
    print(f"收到 {len(ids)} 封新邮件，请处理")
```

## What happens on trigger

调度器到点触发时：
1. 读取 `{workspace}/scripts/{script_file}`
2. 用 `code_run` 执行（cwd=scripts 目录，超时 60s）
3. 判定输出：
   - **无输出** → 静默，你（主 Agent）无感知
   - **有输出** → 你会收到一条 `[定时任务]` 开头的消息，内容是脚本的 stdout。按内容正常处理即可（如整理邮件、报告异常等）。这条消息同时触发前端提醒与 IM 推送。
```

- [ ] **Step 2: 验证 skill 被 SkillSync 识别（格式正确）**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import frontmatter; print(frontmatter.load(open('memory/skills/background-script.md'))['name'])"` （若 frontmatter 库不可用，手动核对 YAML 缩进）
Expected: `background-script`

- [ ] **Step 3: Commit**

```bash
git add memory/skills/background-script.md
git commit -m "docs: background_script 系统级 skill"
```

---

## Task 6: niu.md 定时任务段落加概览

**Files:**
- Modify: `config/agents/niu.md:183-185`

- [ ] **Step 1: 在定时任务段落末尾加两句**

在 `config/agents/niu.md` 的 `# 定时任务` 段落，现有文字（L185）之后追加一段：

```markdown

除上述提醒式任务外，另支持 `background_script` 后台静默任务：到点执行一段 Python 脚本，无输出则静默、有输出（含报错）才通知你。需要创建此类任务时，阅读 `memory/skills/background-script.md` 了解用法与脚本编写规则。
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: niu.md 定时任务段落加 background_script 概览"
```

---

## Task 7: 运行环境实测（真实数据）

**Files:** 无（验证步骤）

**铁律 5：测试必须用真实数据 + 真实 LLM。** 以下在运行中的 niu_api 上实测。

- [ ] **Step 1: 启动 niu_api（若未运行）**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m niu_api &
```
等待 "Uvicorn running on http://0.0.0.0:9876" 日志。

- [ ] **Step 2: 准备测试脚本——静默清理**

```bash
WORKSPACE=$(python/bin/python -c "import json,pathlib; m=json.load(open(pathlib.Path.home()/'.niu'/'memory.json')); print(m['workspace']['path'])")
mkdir -p "$WORKSPACE/scripts"
cat > "$WORKSPACE/scripts/bg_test_silent.py" <<'EOF'
# 静默：不 print
import os
_ = os.listdir(os.path.expanduser("~"))
EOF
```

- [ ] **Step 3: 创建静默 background_script 任务（cron 每分钟）**

```bash
curl -s -X POST http://localhost:9876/scheduler/tasks -H "Content-Type: application/json" -d "{\"content\":\"静默测试\",\"scheduled_at\":\"2026-01-01 00:00:00\",\"task_kind\":\"background_script\",\"script_file\":\"bg_test_silent.py\",\"is_recurring\":true,\"cron_expr\":\"* * * * *\"}"
```
记下返回的 task_id。

- [ ] **Step 4: 等待 1 分钟触发，确认静默**

等待 ~70 秒。检查：
- 前端无新消息气泡（无 `[定时任务]` 消息）
- 无小女孩蹦高
- 日志 `tail -f logs/api_stderr.log | grep BG_SCRIPT` 应有 "静默完成（无输出）"

```bash
grep "BG_SCRIPT" logs/api_stderr.log | tail -5
```
Expected: 含 "静默完成"

- [ ] **Step 5: 改脚本加输出，确认通知**

```bash
cat > "$WORKSPACE/scripts/bg_test_silent.py" <<'EOF'
print("测试输出：有异常需要处理")
EOF
```
等待 ~70 秒。确认：
- 前端收到 `[定时任务] 测试输出：有异常需要处理` 消息
- 小女孩蹦高
- 主 Agent 回复了（真实 LLM 处理）
- 日志含 "Agent replied"

```bash
grep "BG_SCRIPT" logs/api_stderr.log | tail -5
```

- [ ] **Step 6: 改脚本抛异常，确认报错通知**

```bash
cat > "$WORKSPACE/scripts/bg_test_silent.py" <<'EOF'
raise Exception("故意报错")
EOF
```
等待 ~70 秒。确认主 Agent 收到含 Traceback 的 `[定时任务]` 消息。此任务是 recurring（Step 3 is_recurring:true），报错返回 None 触发失败计数器——**连续触发 3 次**（等约 3 分钟，每次 cron `* * * * *`）后，`curl -s http://localhost:9876/scheduler/tasks` 确认该任务 `status` 变为 `failed`（不再 reschedule），验证失败计数器语义生效。

- [ ] **Step 7: 清理测试任务**

```bash
curl -s -X DELETE http://localhost:9876/scheduler/tasks/<task_id>
rm "$WORKSPACE/scripts/bg_test_silent.py"
```

- [ ] **Step 8: reminder 回归——创建一个 reminder 任务确认行为不变**

```bash
curl -s -X POST http://localhost:9876/scheduler/tasks -H "Content-Type: application/json" -d '{"content":"reminder回归测试","scheduled_at":"2026-01-01 00:00:00","is_recurring":true,"cron_expr":"* * * * *"}'
```
等待触发，确认走原 reminder 链路（`[定时任务] reminder回归测试` + 蹦高 + Agent 回复）。测完删除。

---

## Self-Review（经 4 轮审查修订后最终版）

**Spec 覆盖**：task_kind/script_file 列+迁移+所有 SELECT 方法（含 get_overdue_tasks/find_task_by_name）→ Task 1；trigger_callback background_script 分支（静默返回 "(silent)"/报错返回 None/有输出返回 agent_reply/文件不存在永久删除）→ Task 2；code_run 模块级 import+返回 dict 取值+IM 推送+add_alert 传参 → Task 2；MCP+routes → Task 3；disk flag → Task 4；skill+import 警告 → Task 5；niu.md → Task 6；运行实测含失败终态验证 → Task 7。

**审查修订历程**：
- Round 1（8 条）：get_overdue_tasks 漏列/模块级 import/add_alert 参数/IM 推送/get_store 路径/routes 必须/position flag/recurring 永久删除
- Round 2（1 P0）：静默成功返回 None 被调度器误判失败 → 返回 "(silent)"
- Round 3（1 P1+3 P2）：报错返回 agent_reply 被当成功 → 报错返回 None 走失败路径；find_task_by_name 漏列；import 警告；Task 7 失败终态验证

**Placeholder 扫描**：无 TBD/TODO，代码块完整。

**类型一致性**：task_kind/script_file 字段名跨 Task 一致；_trigger_background_script 返回语义三态明确（"(silent)"=静默成功/agent_reply=有输出成功/None=失败）。
