"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import json
import os
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

from .scheduler import Scheduler
from .task_store import TaskStore


# ============== 全局状态 ==============

_scheduler: Optional[Scheduler] = None
_init_lock = threading.Lock()


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
    任务触发回调：通过 ChatQueue 入队并等待 Agent 回复

    从调度器工作线程调用，通过 run_coroutine_threadsafe 桥接到主事件循环。
    ChatQueue 串行处理消息，自动持久化到数据库并 SSE 推送。
    """
    import asyncio

    from niu_api.alerts import add_pending_alert
    from niu_api.chat import _main_loop
    from niu_api.chat_queue import get_chat_queue

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # 构建提示词（[定时任务] 前缀标识系统触发，前端据此用灰色样式展示）
    prompt = f"[定时任务] {task['content']}"

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("[INTERNAL SCHEDULER] Main event loop not available, using fallback")
        fallback_msg = f"定时提醒：{task['content']}"
        _persist_fallback_message(prompt, fallback_msg)
        add_pending_alert("⏰")
        return fallback_msg

    # 通过 ChatQueue 入队并等待回复
    try:
        q = get_chat_queue()
        future = asyncio.run_coroutine_threadsafe(
            q.enqueue_and_wait(
                content=prompt,
                source="scheduler",
                session_id="default",
            ),
            loop,
        )
        agent_reply = future.result(timeout=300)  # 5 分钟超时

        if agent_reply:
            logger.info(f"[INTERNAL SCHEDULER] Agent replied: {agent_reply[:100]}")
        else:
            logger.warning("[INTERNAL SCHEDULER] Agent returned empty reply")
            agent_reply = f"定时提醒：{task['content']}"

        # 触发小女孩蹦高提醒（仅用于视觉提示，不传递消息内容）
        add_pending_alert("⏰")

        # 飞书通道推送：有推送目标时才推送
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            if router.has_channel("feishu"):
                push_future = asyncio.run_coroutine_threadsafe(
                    router.push(agent_reply, "feishu", ""),
                    loop,
                )
                push_future.result(timeout=30)
        except Exception as e:
            logger.warning(f"[SCHEDULER] Feishu push failed: {e}")

        return agent_reply

    except Exception as e:
        logger.error(f"[INTERNAL SCHEDULER] ChatQueue call failed: {e}")
        fallback_msg = f"定时提醒：{task['content']}"
        _persist_fallback_message(prompt, fallback_msg)
        add_pending_alert("⏰")
        return fallback_msg


def _persist_fallback_message(user_content: str, assistant_content: str):
    """
    降级路径：直接将消息写入数据库（绕过 /chat/sync）

    当 /chat/sync 不可用时，用此方法确保消息仍然入库。
    使用 run_coroutine_threadsafe 在主 event loop 上执行异步 DB 操作，
    避免创建新 event loop 与 aiosqlite 的 singleton 连接冲突。
    """
    import asyncio
    from agent.session import get_message_store
    from niu_api.chat import _main_loop

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.warning("[INTERNAL SCHEDULER] Main event loop not available, cannot persist fallback message")
        return

    async def _do_persist():
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=user_content)
        msg_id = await store.add_message(role="assistant", content=assistant_content)
        # 通知 SSE 推送（已在主 loop 中，可直接用 async 版本）
        from niu_api.chat import notify_new_message
        await notify_new_message(user_msg_id, "user", user_content, source="scheduler")
        await notify_new_message(msg_id, "assistant", assistant_content, source="scheduler")
        logger.info("[INTERNAL SCHEDULER] Fallback message persisted to DB")

    try:
        future = asyncio.run_coroutine_threadsafe(_do_persist(), loop)
        future.result(timeout=10)
    except asyncio.TimeoutError:
        logger.warning("[INTERNAL SCHEDULER] Fallback persistence timed out")
    except Exception as e:
        logger.error(f"[INTERNAL SCHEDULER] Failed to persist fallback message: {e}")


# ============== 生命周期管理 ==============

def start_scheduler():
    """启动调度器（延迟启动，等待主服务就绪）"""
    global _scheduler

    with _init_lock:
        if _scheduler is not None:
            logger.warning("[INTERNAL SCHEDULER] Already started")
            return

        db_path = get_db_path()
        logger.info(f"[INTERNAL SCHEDULER] Initializing with database: {db_path}")

        _scheduler = Scheduler(
            db_path=db_path,
            trigger_callback=trigger_callback,
            store_factory=get_store,  # 传入 factory，让 Scheduler 动态获取 store
        )
        # 延迟启动，等待 FastAPI 完全就绪后再开始检查任务
        _scheduler.start_delayed(delay_seconds=10)

        logger.info("[INTERNAL SCHEDULER] Scheduled to start (delayed 10s)")


def stop_scheduler():
    """停止调度器"""
    global _scheduler

    with _init_lock:
        if _scheduler:
            _scheduler.stop()
            _scheduler = None
            logger.info("[INTERNAL SCHEDULER] Stopped")


def get_store() -> TaskStore:
    """获取 TaskStore 实例（动态计算数据库路径，与 MCP scheduler-server 保持一致）"""
    db_path = get_db_path()
    return TaskStore(db_path)


def get_scheduler() -> Scheduler:
    """获取 Scheduler 实例"""
    with _init_lock:
        if _scheduler is None:
            raise RuntimeError("Scheduler not initialized")
        return _scheduler
