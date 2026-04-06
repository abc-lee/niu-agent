# 定时任务系统设计文档 v2

> 版本: 2.0
> 日期: 2026-04-06
> 状态: 设计阶段

## 1. 概述

实现一个定时任务系统，让 Agent 能够：
1. **单次定时任务**：指定时间提醒用户（如"明天下午3点开会"）
2. **循环定时任务**：定期提醒用户（如"每天早上8点提醒我吃药"、"每周一上午10点开会"）
3. **智能通知**：根据聊天窗口焦点状态决定通知方式

## 2. 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                        定时任务系统 v2                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  用户: "每天早上8点提醒我吃药"                                    │
│      ↓                                                          │
│  主 Agent 调用 schedule_task 工具                                │
│      ↓                                                          │
│  ┌─────────────────────────────────────┐                       │
│  │ scheduled_tasks 表 (SQLite)         │                       │
│  │ id, content, scheduled_at,          │                       │
│  │ is_recurring, cron_expr, status     │                       │
│  └─────────────────────────────────────┘                       │
│      ↓                                                          │
│  Scheduler (Python 后台线程)                                    │
│      ↓ 到时间                                                   │
│  调用主Agent处理任务                                             │
│      ↓                                                          │
│  主Agent执行操作或通知用户                                       │
│  （可能调用工具：收邮件、发信息等）                                │
│      ↓                                                          │
│  存储消息到数据库 + 添加到Alert Queue                            │
│      ↓                                                          │
│  前端轮询 /api/pending-alerts (每10秒)                          │
│      ↓                                                          │
│  ┌─────────────────────────────────────────┐                   │
│  │ 聊天窗口是否焦点?                         │                   │
│  │ ├─ 是 → 直接显示消息                      │                   │
│  │ └─ 否 → 小女孩 ALERT 状态，点击后显示     │                   │
│  └─────────────────────────────────────────┘                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 关键流程说明

#### 任务触发流程

1. **Scheduler 检测到到期任务** → 调用 `trigger_callback`

2. **trigger_callback 处理**：
   ```python
   # 构建提示词
   prompt = "⏰ 定时提醒：该「吃药」了。请根据情况提醒用户或执行相关操作。"

   # 调用主Agent
   agent_reply = runner.chat(session_id, prompt)

   # 消息已自动存储到数据库

   # 添加到待推送队列
   add_pending_alert(agent_reply)
   ```

3. **主Agent 处理**：
   - 简单提醒：直接回复"该吃药了"
   - 复杂操作：先收邮件/发信息，再回复结果

4. **前端通知**：
   - 轮询 `/api/pending-alerts` 获取提醒
   - 根据焦点状态决定显示方式

## 3. 数据库设计

### 3.1 scheduled_tasks 表

在工作目录下创建 `scheduled_tasks.db`：

```sql
CREATE TABLE scheduled_tasks (
    id TEXT PRIMARY KEY,              -- UUID
    content TEXT NOT NULL,            -- 任务内容 "吃药"
    scheduled_at DATETIME NOT NULL,   -- 下次触发时间 "2026-04-06T08:00:00"
    is_recurring INTEGER DEFAULT 0,   -- 是否循环任务 (0=否, 1=是)
    cron_expr TEXT,                   -- cron 表达式 "0 8 * * *" (每天8点)
    event_type TEXT,                  -- meeting | task | reminder | recurring
    status TEXT DEFAULT 'pending',    -- pending | triggered | cancelled
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    triggered_at DATETIME,            -- 最后触发时间
    last_triggered_at DATETIME        -- 上次触发时间（用于循环任务）
);

-- 索引：快速查询即将触发的任务
CREATE INDEX idx_scheduled_tasks_pending
ON scheduled_tasks(scheduled_at)
WHERE status = 'pending';
```

### 3.2 Cron 表达式说明

采用标准 5 字段 cron 表达式：

```
┌───────────── 分钟 (0 - 59)
│ ┌───────────── 小时 (0 - 23)
│ │ ┌───────────── 日期 (1 - 31)
│ │ │ ┌───────────── 月份 (1 - 12)
│ │ │ │ ┌───────────── 星期几 (0 - 6, 0=周日)
│ │ │ │ │
* * * * *
```

**示例**：
- `0 8 * * *` — 每天早上 8:00
- `0 9 * * 1` — 每周一上午 9:00
- `30 12 * * 1-5` — 周一到周五中午 12:30
- `0 0 1 * *` — 每月 1 号 0:00

## 4. 工具设计

### 4.1 工具列表

| 工具名 | 功能 | 参数 |
|--------|------|------|
| `schedule_task` | 创建定时任务 | content, scheduled_at, event_type, is_recurring, cron_expr |
| `list_scheduled_tasks` | 查询任务列表 | status |
| `cancel_task` | 取消任务 | task_id |
| `update_task` | 更新任务 | task_id, content, scheduled_at, cron_expr |

### 4.2 参数说明

**schedule_task 参数**：

```python
{
    "content": "吃药",              # 必填：任务内容
    "scheduled_at": "2026-04-06T08:00:00",  # 必填：首次触发时间（ISO格式）
    "event_type": "reminder",       # 可选：事件类型 (meeting/task/reminder/recurring)
    "is_recurring": true,           # 可选：是否循环任务（默认 false）
    "cron_expr": "0 8 * * *"        # 可选：cron 表达式（循环任务必填）
}
```

### 4.3 使用示例

**单次提醒**：
```python
# 用户说："明天下午3点开会"
schedule_task(
    content="开会",
    scheduled_at="2026-04-07T15:00:00",
    event_type="meeting"
)
```

**每天提醒**：
```python
# 用户说："每天早上8点提醒我吃药"
schedule_task(
    content="吃药",
    scheduled_at="2026-04-06T08:00:00",  # 首次触发时间
    is_recurring=True,
    cron_expr="0 8 * * *",
    event_type="recurring"
)
```

**每周提醒**：
```python
# 用户说："每周一上午10点开会"
schedule_task(
    content="周会",
    scheduled_at="2026-04-06T10:00:00",
    is_recurring=True,
    cron_expr="0 10 * * 1",
    event_type="meeting"
)
```

**工作日提醒**：
```python
# 用户说："工作日上午9点提醒我打卡"
schedule_task(
    content="打卡",
    scheduled_at="2026-04-06T09:00:00",
    is_recurring=True,
    cron_expr="0 9 * * 1-5",
    event_type="recurring"
)
```

## 5. 实现方案

### 5.1 新建 MCP 服务器：scheduler-server

**目录结构**：

```
mcp-servers/scheduler-server/
├── src/
│   └── niu_scheduler_server/
│       ├── __init__.py      # MCP 工具定义
│       ├── __main__.py      # 入口点
│       ├── scheduler.py     # 调度器核心
│       ├── store.py         # 数据库操作
│       └── cron_parser.py   # Cron 表达式解析
└── pyproject.toml
```

### 5.2 核心模块

#### 5.2.1 scheduler.py（调度器）

```python
import threading
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Callable
import logging

logger = logging.getLogger(__name__)


class Scheduler:
    """定时任务调度器"""

    def __init__(self, db_path: str, trigger_callback: Callable[[dict], str]):
        """
        Args:
            db_path: 数据库路径
            trigger_callback: 触发回调函数，接收 task 字典，返回 Agent 回复
        """
        self.db_path = db_path
        self.trigger_callback = trigger_callback
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
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
                last_triggered_at DATETIME
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_pending
            ON scheduled_tasks(scheduled_at)
            WHERE status = 'pending'
        """)
        conn.commit()
        conn.close()

    def start(self):
        """启动调度器"""
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Scheduler started")

    def stop(self):
        """停止调度器"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def _run_loop(self):
        """主循环：每分钟检查一次"""
        while self.running:
            try:
                self._check_and_trigger()
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)

            time.sleep(60)  # 每分钟检查一次

    def _check_and_trigger(self):
        """检查并触发到期任务"""
        now = datetime.now()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 查询到期任务
        cursor.execute("""
            SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type
            FROM scheduled_tasks
            WHERE status = 'pending' AND scheduled_at <= ?
        """, (now.isoformat(),))

        tasks = cursor.fetchall()

        for task in tasks:
            task_id, content, scheduled_at, is_recurring, cron_expr, event_type = task

            try:
                # 调用主Agent处理任务
                agent_reply = self.trigger_callback({
                    "id": task_id,
                    "content": content,
                    "event_type": event_type,
                    "scheduled_at": scheduled_at
                })

                logger.info(f"Task triggered: {task_id} - {content}, Agent replied: {agent_reply[:100]}")

            except Exception as e:
                logger.error(f"Failed to trigger task {task_id}: {e}", exc_info=True)
                agent_reply = f"定时提醒：{content}"

            # 更新任务状态
            if is_recurring:
                # 循环任务：计算下次触发时间
                next_time = self._calc_next_trigger(scheduled_at, cron_expr)
                if next_time:
                    cursor.execute("""
                        UPDATE scheduled_tasks
                        SET scheduled_at = ?, last_triggered_at = ?, triggered_at = ?
                        WHERE id = ?
                    """, (next_time.isoformat(), now.isoformat(), now.isoformat(), task_id))
                else:
                    # 无法计算下次时间，标记为已完成
                    cursor.execute("""
                        UPDATE scheduled_tasks
                        SET status = 'triggered', triggered_at = ?
                        WHERE id = ?
                    """, (now.isoformat(), task_id))
            else:
                # 单次任务：标记为已触发
                cursor.execute("""
                    UPDATE scheduled_tasks
                    SET status = 'triggered', triggered_at = ?
                    WHERE id = ?
                """, (now.isoformat(), task_id))

        conn.commit()
        conn.close()

    def _calc_next_trigger(self, scheduled_at: str, cron_expr: str) -> Optional[datetime]:
        """
        计算下次触发时间

        Args:
            scheduled_at: 当前触发时间
            cron_expr: cron 表达式

        Returns:
            下次触发时间，如果无法计算则返回 None
        """
        from .cron_parser import CronParser

        try:
            parser = CronParser(cron_expr)
            current = datetime.fromisoformat(scheduled_at)
            next_time = parser.get_next(current)
            return next_time
        except Exception as e:
            logger.error(f"Failed to calculate next trigger: {e}")
            return None
```

#### 5.2.2 cron_parser.py（Cron 表达式解析）

```python
from datetime import datetime, timedelta
from typing import List, Optional
import re


class CronParser:
    """简单的 Cron 表达式解析器"""

    def __init__(self, cron_expr: str):
        """
        Args:
            cron_expr: cron 表达式，如 "0 8 * * *"
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        self.minute = self._parse_field(parts[0], 0, 59)
        self.hour = self._parse_field(parts[1], 0, 23)
        self.day_of_month = self._parse_field(parts[2], 1, 31)
        self.month = self._parse_field(parts[3], 1, 12)
        self.day_of_week = self._parse_field(parts[4], 0, 6)

    def _parse_field(self, field: str, min_val: int, max_val: int) -> List[int]:
        """解析单个字段"""
        if field == '*':
            return list(range(min_val, max_val + 1))

        # 处理范围，如 "1-5"
        if '-' in field:
            start, end = field.split('-')
            return list(range(int(start), int(end) + 1))

        # 处理列表，如 "1,3,5"
        if ',' in field:
            return [int(x) for x in field.split(',')]

        # 处理单个值
        return [int(field)]

    def get_next(self, current: datetime) -> Optional[datetime]:
        """
        获取下次触发时间

        Args:
            current: 当前时间

        Returns:
            下次触发时间（在当前时间之后）
        """
        # 从当前时间的下一分钟开始检查
        next_time = current.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # 最多检查 366 天
        for _ in range(366 * 24 * 60):
            if self._matches(next_time):
                return next_time
            next_time += timedelta(minutes=1)

        return None

    def _matches(self, dt: datetime) -> bool:
        """检查时间是否匹配 cron 表达式"""
        return (
            dt.minute in self.minute and
            dt.hour in self.hour and
            dt.day in self.day_of_month and
            dt.month in self.month and
            dt.weekday() in self.day_of_week
        )
```

#### 5.2.3 store.py（数据库操作）

```python
import sqlite3
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional


class TaskStore:
    """任务存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_task(
        self,
        content: str,
        scheduled_at: str,
        event_type: str = "reminder",
        is_recurring: bool = False,
        cron_expr: Optional[str] = None
    ) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO scheduled_tasks
            (id, content, scheduled_at, is_recurring, cron_expr, event_type, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type))
        conn.commit()
        conn.close()

        return task_id

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询任务列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if status:
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at
                FROM scheduled_tasks
                WHERE status = ?
                ORDER BY scheduled_at
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at
                FROM scheduled_tasks
                ORDER BY scheduled_at
            """)

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row[0],
                "content": row[1],
                "scheduled_at": row[2],
                "is_recurring": bool(row[3]),
                "cron_expr": row[4],
                "event_type": row[5],
                "status": row[6],
                "created_at": row[7]
            }
            for row in rows
        ]

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE scheduled_tasks
            SET status = 'cancelled'
            WHERE id = ? AND status = 'pending'
        """, (task_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def update_task(
        self,
        task_id: str,
        content: Optional[str] = None,
        scheduled_at: Optional[str] = None,
        cron_expr: Optional[str] = None
    ) -> bool:
        """更新任务"""
        updates = []
        params = []

        if content:
            updates.append("content = ?")
            params.append(content)

        if scheduled_at:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)

        if cron_expr is not None:
            updates.append("cron_expr = ?")
            params.append(cron_expr)

        if not updates:
            return False

        params.append(task_id)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE scheduled_tasks
            SET {', '.join(updates)}
            WHERE id = ? AND status = 'pending'
        """, params)
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
```

#### 5.2.4 __init__.py（MCP 工具定义）

```python
from mcp.server import Server
from mcp.types import Tool, TextContent
import json
import logging
from pathlib import Path
from typing import Optional

from .store import TaskStore
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

# 全局调度器实例
_scheduler: Optional[Scheduler] = None
_store: Optional[TaskStore] = None


def get_db_path() -> str:
    """获取数据库路径"""
    import os

    # 从 ~/.niu/memory.json 读取工作目录
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
                workspace = memory.get("workspace", {}).get("path")
                if workspace and Path(workspace).exists():
                    return str(Path(workspace) / "scheduled_tasks.db")
        except Exception:
            pass

    # 默认路径
    return str(Path.home() / ".niu" / "scheduled_tasks.db")


def trigger_callback(task: dict) -> str:
    """
    任务触发回调：调用主Agent处理任务

    Args:
        task: 任务信息 {id, content, event_type, scheduled_at}

    Returns:
        Agent 回复内容
    """
    from agent.runner import get_runner
    from agent.session import get_session_manager
    from niu_api.alerts import add_pending_alert
    import uuid

    logger.info(f"Triggering task: {task['content']}")

    # 1. 构建提示词
    prompt = f"⏰ 定时提醒：该「{task['content']}」了。请根据情况提醒用户或执行相关操作。"

    # 2. 获取当前 sessionID（从 window-config.json）
    session_id = read_current_session_id()

    # 3. 调用主Agent
    runner = get_runner()
    if runner is None:
        logger.error("Runner not initialized")
        return f"定时提醒：{task['content']}"

    try:
        # 执行对话（生成器，收集所有输出）
        reply_chunks = []
        for chunk in runner.chat(session_id, prompt):
            reply_chunks.append(chunk)

        agent_reply = "".join(reply_chunks).strip()

        # 4. 消息已由 runner.chat 自动存储到数据库

        # 5. 添加到待推送提醒队列
        add_pending_alert(agent_reply)

        logger.info(f"Agent replied: {agent_reply[:100]}")
        return agent_reply

    except Exception as e:
        logger.error(f"Agent call failed: {e}", exc_info=True)
        error_reply = f"定时提醒：{task['content']}"
        add_pending_alert(error_reply)
        return error_reply


def read_current_session_id() -> str:
    """读取当前 sessionID"""
    try:
        config_path = Path(__file__).parent.parent.parent / "ui" / "assistant" / "window-config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                return config.get("sessionId", "default")
    except Exception:
        pass

    return "default"


def run_server():
    """运行 MCP 服务器"""
    global _scheduler, _store

    db_path = get_db_path()
    _store = TaskStore(db_path)
    _scheduler = Scheduler(db_path, trigger_callback)
    _scheduler.start()

    server = Server("scheduler-server")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="schedule_task",
                description="""创建定时任务，支持单次和循环任务。

参数：
- content (必填): 任务内容，如 "开会"、"吃药"
- scheduled_at (必填): 首次触发时间，ISO格式，如 "2026-04-06T08:00:00"
- event_type (可选): 事件类型，meeting/task/reminder/recurring，默认 reminder
- is_recurring (可选): 是否循环任务，默认 false
- cron_expr (可选): cron 表达式（循环任务必填），如 "0 8 * * *"（每天8点）

Cron 表达式格式：
┌───────────── 分钟 (0-59)
│ ┌───────────── 小时 (0-23)
│ │ ┌───────────── 日期 (1-31)
│ │ │ ┌───────────── 月份 (1-12)
│ │ │ │ ┌───────────── 星期几 (0-6, 0=周日)
│ │ │ │ │
* * * * *

示例：
- "0 8 * * *" — 每天早上 8:00
- "0 9 * * 1" — 每周一上午 9:00
- "30 12 * * 1-5" — 周一到周五中午 12:30

使用示例：
1. 单次提醒：
   schedule_task(content="开会", scheduled_at="2026-04-07T15:00:00", event_type="meeting")

2. 每天提醒：
   schedule_task(content="吃药", scheduled_at="2026-04-06T08:00:00", is_recurring=True, cron_expr="0 8 * * *")

重要：相对时间（明天、下周）必须由 Agent 转换为具体的日期时间。""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "任务内容"},
                        "scheduled_at": {"type": "string", "description": "首次触发时间（ISO格式）"},
                        "event_type": {"type": "string", "enum": ["meeting", "task", "reminder", "recurring"]},
                        "is_recurring": {"type": "boolean", "description": "是否循环任务"},
                        "cron_expr": {"type": "string", "description": "cron 表达式"}
                    },
                    "required": ["content", "scheduled_at"]
                }
            ),
            Tool(
                name="list_scheduled_tasks",
                description="""查询定时任务列表。

参数：
- status (可选): 筛选状态，pending/triggered/cancelled

返回：任务列表，包含 id、content、scheduled_at、is_recurring、cron_expr、status""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["pending", "triggered", "cancelled"]}
                    }
                }
            ),
            Tool(
                name="cancel_task",
                description="""取消定时任务。

参数：
- task_id: 任务ID

返回：取消结果""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务ID"}
                    },
                    "required": ["task_id"]
                }
            ),
            Tool(
                name="update_task",
                description="""更新定时任务。

参数：
- task_id: 任务ID
- content: 新的任务内容（可选）
- scheduled_at: 新的触发时间（可选）
- cron_expr: 新的 cron 表达式（可选）

返回：更新结果""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务ID"},
                        "content": {"type": "string", "description": "新的任务内容"},
                        "scheduled_at": {"type": "string", "description": "新的触发时间"},
                        "cron_expr": {"type": "string", "description": "新的 cron 表达式"}
                    },
                    "required": ["task_id"]
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name == "schedule_task":
                task_id = _store.create_task(
                    content=arguments["content"],
                    scheduled_at=arguments["scheduled_at"],
                    event_type=arguments.get("event_type", "reminder"),
                    is_recurring=arguments.get("is_recurring", False),
                    cron_expr=arguments.get("cron_expr")
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success",
                        "task_id": task_id,
                        "message": f"✅ 已创建定时任务：{arguments['content']}"
                    }, ensure_ascii=False)
                )]

            elif name == "list_scheduled_tasks":
                tasks = _store.list_tasks(arguments.get("status"))
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success",
                        "tasks": tasks,
                        "count": len(tasks)
                    }, ensure_ascii=False, indent=2)
                )]

            elif name == "cancel_task":
                success = _store.cancel_task(arguments["task_id"])
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success" if success else "error",
                        "message": "✅ 任务已取消" if success else "❌ 任务不存在或已完成"
                    }, ensure_ascii=False)
                )]

            elif name == "update_task":
                success = _store.update_task(
                    task_id=arguments["task_id"],
                    content=arguments.get("content"),
                    scheduled_at=arguments.get("scheduled_at"),
                    cron_expr=arguments.get("cron_expr")
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success" if success else "error",
                        "message": "✅ 任务已更新" if success else "❌ 任务不存在或已完成"
                    }, ensure_ascii=False)
                )]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except Exception as e:
            logger.error(f"Tool error: {e}", exc_info=True)
            return [TextContent(
                type="text",
                text=json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
            )]

    server.run()


if __name__ == "__main__":
    run_server()
```

### 5.3 Alert Queue（提醒队列）

在 `niu_api/alerts.py` 中实现：

```python
"""提醒队列"""
import threading
from typing import List, Dict
from datetime import datetime

_pending_alerts: List[Dict] = []
_alerts_lock = threading.Lock()


def add_pending_alert(content: str):
    """添加待推送提醒"""
    with _alerts_lock:
        _pending_alerts.append({
            "content": content,
            "timestamp": datetime.now().isoformat()
        })


def get_and_clear_pending_alerts() -> List[Dict]:
    """获取并清空待推送提醒"""
    with _alerts_lock:
        alerts = _pending_alerts.copy()
        _pending_alerts.clear()
        return alerts
```

在 `niu_api/__main__.py` 中添加 API 端点：

```python
from fastapi import APIRouter
from .alerts import get_and_clear_pending_alerts

router = APIRouter()

@router.get("/api/pending-alerts")
async def pending_alerts():
    """获取待推送提醒"""
    alerts = get_and_clear_pending_alerts()
    return alerts
```

## 6. 配置更新

### 6.1 MCP 服务器配置

在 `config/mcp-servers.yaml` 中添加：

```yaml
scheduler-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_scheduler_server"
  workdir: ../mcp-servers/scheduler-server/src
  preload: true
```

### 6.2 event-manager 配置更新

在 `config/agents/event-manager.md` 中：

1. 添加 `scheduler-server` 到 `mcpServers`
2. 更新定时任务说明（参考工具描述）

```yaml
---
name: event-manager
description: 事件管理器 - 负责管理用户的重要事件、待办事项和日程
mode: subagent
temperature: 0.2
mcpServers:
  - vector-store
  - scheduler-server  # 新增
---
```

## 7. 主 Agent 提示词设计

### 7.1 定时任务触发提示

当定时任务触发时，主Agent会收到如下提示：

```
⏰ 定时提醒：该「{任务内容}」了。请根据情况提醒用户或执行相关操作。
```

### 7.2 Agent 处理逻辑

主Agent收到提示后，可以：

1. **简单提醒**：
   ```
   用户收到："该吃药了！"
   ```

2. **复杂操作**：
   ```
   # Agent 内部流程
   - 检查邮件 → 分析内容 → 生成摘要 → 回复用户
   - 查询天气 → 分析是否需要带伞 → 提醒用户
   - 检查日程 → 发送会议提醒到其他平台
   ```

3. **智能决策**：
   ```
   # Agent 根据上下文决定
   - 如果用户在忙 → 简短提醒
   - 如果任务重要 → 详细说明 + 相关建议
   - 如果需要操作 → 先执行工具调用 → 再回复结果
   ```

### 7.3 主 Agent 配置更新

在 `config/agents/niu.md` 中添加：

```markdown
# 定时任务触发

当收到 "⏰ 定时提醒：该「...」了" 格式的提示时，说明是定时任务触发。

处理策略：
1. 简单提醒任务：直接用简洁语言提醒用户
2. 需要执行操作的任务（event_type="task"）：
   - 先调用相关工具执行操作（如检查邮件、发送消息）
   - 再向用户汇报结果
3. 重要任务：提供额外建议或相关信息

注意：
- 保持回复简洁
- 如果执行了工具调用，告诉用户做了什么
- 根据任务类型（meeting/task/reminder）调整语气
```

保持原有设计（轮询 `/api/pending-alerts`），无需修改。

## 8. 前端通知设计

保持原有设计（轮询 `/api/pending-alerts`），无需修改。

## 9. 实现步骤

### 阶段一：核心功能

1. ✅ 创建 `mcp-servers/scheduler-server/` 目录结构
2. ✅ 实现 `cron_parser.py` — Cron 表达式解析
3. ✅ 实现 `store.py` — 数据库操作
4. ✅ 实现 `scheduler.py` — 调度器核心
5. ✅ 实现 `__init__.py` — MCP 工具定义
6. ⏳ 在 `niu_api/alerts.py` 中实现 Alert Queue
7. ⏳ 在 `niu_api/__main__.py` 中添加 `/api/pending-alerts` 端点

### 阶段二：配置集成

8. ⏳ 更新 `config/mcp-servers.yaml` — 添加 scheduler-server
9. ⏳ 更新 `config/agents/event-manager.md` — 添加工具说明和 mcpServers
10. ⏳ 修复子Agent时间注入问题 — 在 `agent/subagent.py` 中注入 `Today:`

### 阶段三：测试验证

11. ⏳ 测试单次定时任务
12. ⏳ 测试循环定时任务（每天、每周、工作日）
13. ⏳ 测试 Agent 触发流程
14. ⏳ 测试前端通知机制

## 10. 使用场景

### 10.1 简单提醒

用户："每天早上8点提醒我吃药"

```python
# Agent 调用工具
schedule_task(
    content="吃药",
    scheduled_at="2026-04-06T08:00:00",
    is_recurring=True,
    cron_expr="0 8 * * *"
)

# 到时间后触发
# 主Agent收到提示："⏰ 定时提醒：该「吃药」了。请根据情况提醒用户或执行相关操作。"
# Agent 回复："该吃药了！记得吃完饭半小时后再吃。"
```

### 10.2 复杂操作

用户："每周一上午9点帮我检查邮件并总结"

```python
# Agent 调用工具
schedule_task(
    content="检查邮件并总结",
    scheduled_at="2026-04-06T09:00:00",
    is_recurring=True,
    cron_expr="0 9 * * 1",
    event_type="task"
)

# 到时间后触发
# 主Agent收到提示："⏰ 定时提醒：该「检查邮件并总结」了。请根据情况提醒用户或执行相关操作。"
# Agent 操作流程：
# 1. 调用邮件工具检查新邮件
# 2. 分析邮件内容
# 3. 生成摘要
# Agent 回复："本周一邮件摘要：收到3封重要邮件，包括客户需求确认、项目进度更新、会议邀请..."
```

### 10.3 发送消息

用户："每周五下午5点提醒我写周报"

```python
# Agent 调用工具
schedule_task(
    content="写周报",
    scheduled_at="2026-04-04T17:00:00",
    is_recurring=True,
    cron_expr="0 17 * * 5",
    event_type="task"
)

# 到时间后触发
# Agent 可以：
# - 简单提醒："该写周报了！"
# - 发送消息到其他平台（如果配置了消息发送工具）
# - 提供周报模板或建议
```

## 11. 注意事项

1. **时区处理**：使用本地时区
2. **任务持久化**：存储在 SQLite 数据库，重启后自动恢复
3. **循环任务边界**：Cron 表达式解析器最多检查 366 天
4. **错误处理**：调度器异常时记录日志，不阻塞主循环
5. **性能**：每分钟检查一次，使用索引优化查询
6. **Agent调用**：
   - 主Agent收到提示后可以自由决策
   - 简单提醒：直接回复用户
   - 复杂操作：先调用工具（收邮件、发信息等），再回复结果
   - Agent调用失败时降级为简单文本提醒
7. **SessionID管理**：定时任务触发时使用当前活跃session，保证消息连续性

## 12. 后续优化

1. **任务模板**：提供常用 cron 表达式模板（每天、每周、工作日等）
2. **任务标签**：支持给任务打标签，方便分类管理
3. **任务历史**：记录触发历史，支持统计分析
4. **智能提醒**：根据用户习惯优化提醒时间
5. **任务依赖**：支持任务间依赖关系（如任务A完成后才执行任务B）
6. **Agent能力扩展**：定时任务可以触发更复杂的Agent操作（数据分析、文件处理等）
