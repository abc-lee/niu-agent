"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests
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
    任务触发回调：调用主 API Agent 处理任务

    由于 scheduler 和 niu_api 在同一进程，直接调用内部接口。
    所有消息都写入数据库，前端通过轮询检测新消息。
    """
    from niu_api.alerts import add_pending_alert

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # 构建提示词（[定时任务] 前缀标识系统触发，前端据此用灰色样式展示）
    prompt = f"[定时任务] {task['content']}"

    # 获取主 API URL（虽然在同一进程，但仍可通过 HTTP 调用 /chat/sync）
    port = os.environ.get("NIU_API_PORT", "9876")
    main_url = os.environ.get("MAIN_API_URL", f"http://127.0.0.1:{port}")

    # 检查主 API 可用性（带重试）
    api_healthy = False
    for attempt in range(3):
        try:
            resp = requests.get(f"{main_url}/health", timeout=5)
            if resp.status_code == 200:
                api_healthy = True
                break
            logger.warning(f"Main API returned {resp.status_code} (attempt {attempt + 1}/3)")
        except requests.RequestException as e:
            logger.warning(f"Main API unavailable (attempt {attempt + 1}/3): {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)

    if not api_healthy:
        logger.error("[INTERNAL SCHEDULER] All health checks failed, using fallback")
        fallback_msg = f"定时提醒：{task['content']}"
        _persist_fallback_message(prompt, fallback_msg)
        add_pending_alert("⏰")
        return fallback_msg

    # 调用 /chat/sync（已持久化消息到数据库）
    try:
        response = requests.post(
            f"{main_url}/chat/sync",
            json={
                "session_id": "default",
                "message": prompt
            },
            timeout=90
        )

        if response.status_code == 200:
            data = response.json()
            agent_reply = data.get("reply", "")
            logger.info(f"[INTERNAL SCHEDULER] Agent replied: {agent_reply[:100] if agent_reply else '(empty)'}")

            # /chat/sync 已将 user + assistant 消息持久化到数据库
            # 前端轮询会自动检测到新消息并显示

            # 触发小女孩蹦高提醒（仅用于视觉提示，不传递消息内容）
            add_pending_alert("⏰")

            # 飞书通道推送
            try:
                from niu_api.channel import get_channel_router
                channel_router = get_channel_router()
                if channel_router.has_channel("feishu"):
                    feishu_adapter = channel_router.channels["feishu"]
                    if feishu_adapter.user_p2p_chat_id:
                        feishu_adapter.channel.schedule(
                            feishu_adapter.push(feishu_adapter.user_p2p_chat_id, agent_reply)
                        )
            except Exception as e:
                logger.warning(f"[SCHEDULER] Feishu push failed: {e}")

            return agent_reply if agent_reply else f"定时提醒：{task['content']}"
        else:
            logger.error(f"[INTERNAL SCHEDULER] Chat API error: {response.status_code}")
            # API 出错，直接将降级提醒写入消息数据库
            fallback_msg = f"定时提醒：{task['content']}"
            _persist_fallback_message(prompt, fallback_msg)
            add_pending_alert("⏰")
            return fallback_msg

    except Exception as e:
        logger.error(f"[INTERNAL SCHEDULER] Failed to call chat API: {e}")
        # API 异常，直接将降级提醒写入消息数据库
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
        await notify_new_message(user_msg_id, "user", user_content)
        await notify_new_message(msg_id, "assistant", assistant_content)
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
