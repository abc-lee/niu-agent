"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from .scheduler import Scheduler
from .task_store import TaskStore


# ============== 全局状态 ==============

_scheduler: Optional[Scheduler] = None
_store: Optional[TaskStore] = None


# ============== 数据库路径 ==============

def get_db_path() -> str:
    """获取数据库路径"""
    # 优先使用环境变量
    db_path = os.environ.get("SCHEDULER_DB_PATH")
    if db_path and Path(db_path).parent.exists():
        return db_path

    # 从 ~/.niu/memory.json 读取工作目录
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
                workspace = memory.get("workspace", {}).get("path")
                if workspace and Path(workspace).exists():
                    db_path = str(Path(workspace) / "scheduled_tasks.db")
                    # 确保目录存在
                    os.makedirs(Path(db_path).parent, exist_ok=True)
                    return db_path
        except Exception as e:
            logger.warning(f"Failed to read memory.json: {e}")

    # 默认路径
    default_path = str(Path.home() / ".niu" / "scheduled_tasks.db")
    os.makedirs(Path(default_path).parent, exist_ok=True)
    return default_path


# ============== 触发回调 ==============

def trigger_callback(task: dict) -> str:
    """
    任务触发回调：调用主 API Agent 处理任务

    由于 scheduler 和 niu_api 在同一进程，直接调用内部接口。
    """
    from niu_api.alerts import add_pending_alert

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # 构建提示词
    prompt = f"⏰ 定时提醒：该「{task['content']}」了。请根据情况提醒用户或执行相关操作。"

    # 获取主 API URL（虽然在同一进程，但仍可通过 HTTP 调用 /chat/sync）
    main_url = os.environ.get("MAIN_API_URL", "http://127.0.0.1:9876")

    # 检查主 API 可用性（带重试）
    for attempt in range(3):
        try:
            resp = requests.get(f"{main_url}/health", timeout=5)
            if resp.status_code == 200:
                break
        except requests.RequestException as e:
            logger.warning(f"Main API unavailable (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    # 调用 /chat/sync
    try:
        response = requests.post(
            f"{main_url}/chat/sync",
            json={
                "session_id": "default",
                "message": prompt
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            agent_reply = data.get("reply", "")
            logger.info(f"[INTERNAL SCHEDULER] Agent replied: {agent_reply[:100] if agent_reply else '(empty)'}")

            # ✅ 把提醒加入 pending-alerts 队列，触发前端小女孩状态机
            if agent_reply:
                add_pending_alert(agent_reply)
                logger.info(f"[INTERNAL SCHEDULER] Added to pending-alerts queue")

            return agent_reply if agent_reply else f"定时提醒：{task['content']}"
        else:
            logger.error(f"[INTERNAL SCHEDULER] Chat API error: {response.status_code}")
            # 即使API出错，也发送基础提醒
            fallback_msg = f"定时提醒：{task['content']}"
            add_pending_alert(fallback_msg)
            return fallback_msg

    except Exception as e:
        logger.error(f"[INTERNAL SCHEDULER] Failed to call chat API: {e}")
        # 即使异常，也发送基础提醒
        fallback_msg = f"定时提醒：{task['content']}"
        add_pending_alert(fallback_msg)
        return fallback_msg


# ============== 生命周期管理 ==============

def start_scheduler():
    """启动调度器（延迟启动，等待主服务就绪）"""
    global _scheduler, _store

    db_path = get_db_path()
    logger.info(f"[INTERNAL SCHEDULER] Initializing with database: {db_path}")

    _store = TaskStore(db_path)
    _scheduler = Scheduler(db_path, trigger_callback)
    # 延迟启动，等待 FastAPI 完全就绪后再开始检查任务
    _scheduler.start_delayed(delay_seconds=10)

    logger.info("[INTERNAL SCHEDULER] Scheduled to start (delayed 10s)")


def stop_scheduler():
    """停止调度器"""
    global _scheduler

    if _scheduler:
        _scheduler.stop()
        logger.info("[INTERNAL SCHEDULER] Stopped")


def get_store() -> TaskStore:
    """获取 TaskStore 实例"""
    if _store is None:
        raise RuntimeError("Scheduler not initialized")
    return _store


def get_scheduler() -> Scheduler:
    """获取 Scheduler 实例"""
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized")
    return _scheduler
