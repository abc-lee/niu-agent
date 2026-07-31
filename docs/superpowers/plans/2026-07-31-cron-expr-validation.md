# cron_expr 存储前校验实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在定时任务创建/更新时预校验 cron_expr 合法性，防止非法表达式存入数据库后到触发时才暴露。

**Architecture:** 在 `TaskStore.create_task` 和 `TaskStore.update_task` 加单点校验——复用 `CronParser.__init__` 的完整解析逻辑（5 字段 + `#`/`L`/`LW` 修饰符 + 互斥约束），构造即验证，非法抛 ValueError。同时加 `is_recurring`/`cron_expr` 交叉校验。API 层把 ValueError 单独映射为 HTTP 400（当前统一返回 500）。

**Tech Stack:** Python 3.11+，标准库，pytest。无新依赖。

---

## 背景

当前 `schedule_task`（MCP 工具）和 `POST /scheduler/tasks`（API）都把 `cron_expr` 原样透传给 `TaskStore.create_task` 存入 SQLite，不经过 `CronParser` 解析。非法表达式（如 `8L`、`1#6`、`0 9 15 * 1#2`）能成功存入，直到调度器触发时 `_calc_next_trigger` 调 `CronParser` 才报错——此时任务可能已经无意义地执行了一次。

MCP 工具和 API 路由是两条独立路径，都汇入 `TaskStore.create_task`。在数据层加单点校验能覆盖所有入口。

错误传播路径：MCP 工具的 `except Exception` 会捕获 `ValueError` 返回 `{"status": "error", "message": str(e)}`，Agent 可读取错误信息重试。API 路径单独映射为 HTTP 400。调度器内部所有 `update_task` 调用均不传 `cron_expr`（仅传 `scheduled_at`/`status`），不受新校验影响。上线前已有的非法 `cron_expr` 任务在触发时仍会被 `_calc_next_trigger` 标 `failed`（现有行为不变）。

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `niu_api/internal/scheduler/task_store.py` | 修改 | `create_task`/`update_task` 加 cron_expr 预校验 + is_recurring 交叉校验 |
| `niu_api/internal/scheduler/routes.py` | 修改 | `create_task`/`update_task` 路由把 ValueError 映射为 HTTP 400 |
| `tests/test_cron_validation.py` | 新建 | 校验逻辑单元测试 |

## 校验规则

1. **`cron_expr` 非空时**：`CronParser(cron_expr)` 试构造，非法抛 `ValueError`（复用现有解析器的全部校验：5 字段格式、`#`/`L`/`LW` 修饰符、范围检查、互斥约束）
2. **`is_recurring=True` 且 `cron_expr` 为空**：`raise ValueError("循环任务必须提供 cron_expr")`
3. **`is_recurring=False` 且 `cron_expr` 非空**：`raise ValueError("一次性任务不应提供 cron_expr")`
4. **`update_task` 传了 `cron_expr`**：同样试构造校验（不校验 is_recurring，因为 update 不改 is_recurring 字段）

---

## Task 1: create_task 加 cron_expr 预校验

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py:85-112`（`create_task` 方法）
- Test: `tests/test_cron_validation.py`

- [ ] **Step 1: 写校验失败测试**

创建 `tests/test_cron_validation.py`：

```python
"""Tests for cron_expr validation in TaskStore.create_task/update_task"""
import pytest
from niu_api.internal.scheduler.task_store import TaskStore


class TestCreateTaskCronValidation:
    """create_task 的 cron_expr 校验"""

    def test_invalid_cron_8L_rejected(self, tmp_path):
        """非法 cron_expr（8L）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="Invalid weekday"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 ? * 8L"
            )

    def test_invalid_cron_1_hash_6_rejected(self, tmp_path):
        """非法 cron_expr（1#6）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="Invalid N"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 ? * 1#6"
            )

    def test_invalid_cron_mutex_rejected(self, tmp_path):
        """互斥校验失败（# + 具体 dom）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="day-of-month 必须是"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 15 * 1#2"
            )

    def test_recurring_without_cron_rejected(self, tmp_path):
        """is_recurring=True 但无 cron_expr 被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="循环任务必须提供 cron_expr"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr=None
            )

    def test_onetime_with_cron_rejected(self, tmp_path):
        """is_recurring=False 但传了 cron_expr 被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="一次性任务不应提供 cron_expr"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=False,
                cron_expr="0 9 * * *"
            )

    def test_valid_recurring_accepted(self, tmp_path):
        """合法循环任务正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 9 ? * 1#2"
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 9 ? * 1#2"

    def test_valid_onetime_without_cron_accepted(self, tmp_path):
        """合法一次性任务（无 cron）正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=False,
            cron_expr=None
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] is None

    def test_valid_advanced_modifier_accepted(self, tmp_path):
        """合法高级修饰符（LW）正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 0 LW * *"
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 0 LW * *"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py -v`
Expected: 5 个 FAIL（非法 cron 未被拒、交叉校验未实现），3 个 PASS（合法任务当前能创建）

- [ ] **Step 3: 实现 create_task 校验**

修改 `niu_api/internal/scheduler/task_store.py` 的 `create_task` 方法（当前 L85-112），在 `task_id = str(uuid.uuid4())` 之前插入校验（校验失败不消耗 UUID）：

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
        """创建任务"""
        # --- cron_expr 预校验 ---
        # 归一化：空串/纯空格视为 None，避免脏数据
        if cron_expr is not None:
            cron_expr = cron_expr.strip() or None
        if is_recurring and not cron_expr:
            raise ValueError("循环任务必须提供 cron_expr")
        if not is_recurring and cron_expr:
            raise ValueError("一次性任务不应提供 cron_expr")
        if cron_expr:
            from .cron_parser import CronParser
            CronParser(cron_expr)  # 非法表达式构造时抛 ValueError

        task_id = str(uuid.uuid4())

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO scheduled_tasks
                (id, content, scheduled_at, is_recurring, cron_expr, event_type, status, name, chat_id, task_kind, script_file)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type, name, chat_id, task_kind, script_file))
            conn.commit()
        finally:
            conn.close()

        return task_id
```

注意：`from .cron_parser import CronParser` 放在方法内惰性导入，与 scheduler.py:474 现有模式保持一致。`cron_expr.strip() or None` 归一化使空串/纯空格统一当 None 处理，避免 `is_recurring=False` 时空串被静默存库。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py -v`
Expected: 8 个全 PASS

- [ ] **Step 5: 运行现有测试确认无回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py tests/test_cron_parser.py tests/test_scheduler_service.py tests/test_scheduler_overdue.py tests/test_scheduler_group_push.py tests/test_scheduler_frontend_ready.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_validation.py niu_api/internal/scheduler/task_store.py
git commit -m "feat(scheduler): create_task 加 cron_expr 预校验

- 复用 CronParser 构造校验非法 cron 表达式
- is_recurring/cron_expr 交叉校验
- 防止非法表达式存入数据库到触发时才暴露"
```

---

## Task 2: update_task 加 cron_expr 预校验

**Files:**
- Modify: `niu_api/internal/scheduler/task_store.py:210-285`（`update_task` 方法）
- Test: `tests/test_cron_validation.py`

- [ ] **Step 1: 写 update_task 校验测试**

在 `tests/test_cron_validation.py` 末尾追加：

```python
class TestUpdateTaskCronValidation:
    """update_task 的 cron_expr 校验"""

    def _create_store_with_task(self, tmp_path):
        """辅助：建临时文件库并预置一个合法循环任务"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 9 * * *"
        )
        return store, task_id

    def test_update_to_invalid_cron_rejected(self, tmp_path):
        """更新为非法 cron_expr 被拒"""
        store, task_id = self._create_store_with_task(tmp_path)
        with pytest.raises(ValueError, match="Invalid weekday"):
            store.update_task(
                task_id=task_id,
                cron_expr="0 9 ? * 8L"
            )

    def test_update_to_invalid_mutex_rejected(self, tmp_path):
        """更新为互斥违规被拒"""
        store, task_id = self._create_store_with_task(tmp_path)
        with pytest.raises(ValueError, match="day-of-month 必须是"):
            store.update_task(
                task_id=task_id,
                cron_expr="0 9 15 * 1#2"
            )

    def test_update_to_valid_cron_accepted(self, tmp_path):
        """更新为合法 cron_expr 成功"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            cron_expr="0 9 ? * 1#2"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 9 ? * 1#2"

    def test_update_to_valid_L_accepted(self, tmp_path):
        """更新为合法 L 修饰符成功"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            cron_expr="0 17 ? * 5L"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 17 ? * 5L"

    def test_update_without_cron_not_validated(self, tmp_path):
        """不传 cron_expr 时不触发校验（其他字段更新正常）"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            content="updated content"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["content"] == "updated content"
        # cron_expr 保持不变
        assert task["cron_expr"] == "0 9 * * *"

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py::TestUpdateTaskCronValidation -v`
Expected: 2 个 FAIL（非法 cron 未被拒），3 个 PASS（合法更新当前能成功）

- [ ] **Step 3: 实现 update_task 校验**

修改 `niu_api/internal/scheduler/task_store.py` 的 `update_task` 方法（当前 L210-285）。在 `updates = []` 之前（即方法体开头、docstring 之后）插入校验：

```python
    def update_task(
        self,
        task_id: str,
        content: str | None = None,
        scheduled_at: str | None = None,
        cron_expr: str | None = None,
        status: str | None = None,
        expected_status: str | None = None,
        name: str | None = None,
        triggered_at: str | None = None,
        task_kind: str | None = None,
        script_file: str | None = None
    ) -> bool:
        """更新任务

        Args:
            expected_status: CAS 条件，仅当当前状态匹配时才更新（防止竞态）
        """
        # --- cron_expr 预校验（仅当传入新值时）---
        if cron_expr is not None:
            cron_expr = cron_expr.strip() or None  # 归一化空串
        if cron_expr is not None:
            from .cron_parser import CronParser
            CronParser(cron_expr)  # 非法表达式构造时抛 ValueError

        updates = []
        params = []
        # ...（以下原有逻辑不变）
```

注意：`update_task` 的 `cron_expr=None` 表示"不更新此字段"（现有逻辑 L239: `if cron_expr is not None`），不是"清空"。所以校验只在 `cron_expr is not None` 时触发。不校验 is_recurring 交叉关系，因为 update_task 不改 is_recurring 字段。update_task 无法清空 cron_expr（None=不更新，空串会被归一化为 None 也不触发更新），如需"取消循环"功能需另行设计。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py::TestUpdateTaskCronValidation -v`
Expected: 5 个全 PASS

- [ ] **Step 5: 运行全部校验测试 + 回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py tests/test_cron_parser.py tests/test_scheduler_service.py tests/test_scheduler_overdue.py tests/test_scheduler_group_push.py tests/test_scheduler_frontend_ready.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_validation.py niu_api/internal/scheduler/task_store.py
git commit -m "feat(scheduler): update_task 加 cron_expr 预校验

- 更新 cron_expr 时复用 CronParser 构造校验
- 不传 cron_expr 时不触发校验（保持现有语义）"
```

---

## Task 3: API 路由把 ValueError 映射为 HTTP 400

**Files:**
- Modify: `niu_api/internal/scheduler/routes.py:46-75`（create_task 路由）和 `routes.py:125-142`（update_task 路由）

- [ ] **Step 1: 写 API 层校验测试**

在 `tests/test_cron_validation.py` 末尾追加：

```python
class TestApiValidationErrorMapping:
    """API 路由把 ValueError 映射为 HTTP 400"""

    def test_create_task_invalid_cron_returns_400(self, tmp_path):
        """非法 cron_expr 创建任务返回 400 而非 500"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from niu_api.internal.scheduler.routes import router

        # 用 TestClient 直接测 router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)

        # mock get_store 返回真实 TaskStore（文件库）
        store = TaskStore(str(tmp_path / "test.db"))
        with patch("niu_api.internal.scheduler.routes.get_store", return_value=store):
            client = TestClient(app)
            response = client.post("/scheduler/tasks", json={
                "content": "test",
                "scheduled_at": "2026-08-01T09:00:00",
                "is_recurring": True,
                "cron_expr": "0 9 ? * 8L"
            })
        assert response.status_code == 400
        assert "Invalid weekday" in response.json()["detail"]

    def test_create_task_recurring_without_cron_returns_400(self, tmp_path):
        """循环任务无 cron_expr 返回 400"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from niu_api.internal.scheduler.routes import router

        app = FastAPI()
        app.include_router(router)

        store = TaskStore(str(tmp_path / "test.db"))
        with patch("niu_api.internal.scheduler.routes.get_store", return_value=store):
            client = TestClient(app)
            response = client.post("/scheduler/tasks", json={
                "content": "test",
                "scheduled_at": "2026-08-01T09:00:00",
                "is_recurring": True,
                "cron_expr": None
            })
        assert response.status_code == 400
        assert "循环任务必须提供" in response.json()["detail"]

    def test_create_task_valid_returns_200(self, tmp_path):
        """合法任务返回 200"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from niu_api.internal.scheduler.routes import router

        app = FastAPI()
        app.include_router(router)

        store = TaskStore(str(tmp_path / "test.db"))
        with patch("niu_api.internal.scheduler.routes.get_store", return_value=store):
            client = TestClient(app)
            response = client.post("/scheduler/tasks", json={
                "content": "test",
                "scheduled_at": "2026-08-01T09:00:00",
                "is_recurring": True,
                "cron_expr": "0 9 ? * 1#2"
            })
        assert response.status_code == 200
        assert response.json()["status"] == "success"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py::TestApiValidationErrorMapping -v`
Expected: 前两个 FAIL（返回 500 而非 400），第三个 PASS（合法任务本就 200）

- [ ] **Step 3: 修改 create_task 路由异常处理**

修改 `niu_api/internal/scheduler/routes.py` 的 `create_task` 路由（当前 L46-75）。把 `except Exception` 拆分为 ValueError 优先：

将 L73-75：
```python
    except Exception as e:
        logger.error(f"[SCHEDULER] Create task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

替换为：
```python
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"[SCHEDULER] Create task validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"[SCHEDULER] Create task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

> **顺带修复**：`except HTTPException: raise` 放最前，确保 routes.py L50-53 的 script_file 校验 HTTPException（status_code=400）不再被 `except Exception` 吞为 500。这是现有 bug 的正向修复。

- [ ] **Step 4: 修改 update_task 路由异常处理**

同样修改 `update_task` 路由（当前 L125-142）。将 L140-142：
```python
    except Exception as e:
        logger.error(f"[SCHEDULER] Update task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

替换为：
```python
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"[SCHEDULER] Update task validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"[SCHEDULER] Update task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

- [ ] **Step 5: 运行 API 测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py::TestApiValidationErrorMapping -v`
Expected: 3 个全 PASS

- [ ] **Step 6: 运行全部测试确认无回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_validation.py tests/test_cron_parser.py tests/test_scheduler_service.py tests/test_scheduler_overdue.py tests/test_scheduler_group_push.py tests/test_scheduler_frontend_ready.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_validation.py niu_api/internal/scheduler/routes.py
git commit -m "feat(scheduler): API 路由 ValueError 映射为 HTTP 400

- create_task/update_task 路由把 ValueError 单独处理为 400
- 其他异常仍返回 500
- 改善 HTTP 语义，调用方可区分校验错误和服务器错误"
```

---

## 验收清单

对照需求：

- [ ] 非法 cron_expr（如 `8L`、`1#6`）创建任务时被拒（Task 1）
- [ ] `is_recurring=True` 但无 `cron_expr` 创建时被拒（Task 1）
- [ ] `is_recurring=False` 但传了 `cron_expr` 创建时被拒（Task 1）
- [ ] 合法 cron_expr（含 `#`/`L`/`LW`）创建正常（Task 1）
- [ ] 更新为非法 `cron_expr` 被拒（Task 2）
- [ ] 更新为合法 `cron_expr` 成功（Task 2）
- [ ] API 返回 400 而非 500（Task 3）
- [ ] 现有测试无回归（各 Task 的回归步骤）
- [ ] 无新依赖（仅复用 CronParser）
