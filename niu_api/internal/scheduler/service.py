"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from loguru import logger

from .scheduler import Scheduler
from .task_store import TaskStore
from agent.handler import code_run
from niu_api.chat_queue import get_chat_queue

# ============== 全局状态 ==============

_scheduler: Scheduler | None = None
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
            with open(memory_path, encoding="utf-8") as f:
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

def trigger_callback(task: dict) -> str | None:
    """
    任务触发回调：通过 ChatQueue 入队并等待 Agent 回复

    从调度器工作线程调用，通过 run_coroutine_threadsafe 桥接到主事件循环。
    ChatQueue 串行处理消息，自动持久化到数据库并 SSE 推送。

    失败重试：ChatQueue.enqueue_and_wait 内置 2 分钟超时（asyncio.wait_for timeout=120），
    返回空字符串或异常时，10s 后重试 1 次。仍失败返回 None，
    scheduler 收到 None 后会 reschedule（recurring）或标 failed（one-time）。
    连续 3 次失败的 task 由 scheduler.py 内的失败计数器标记 status='failed'
    （不引入 DLQ 表，复用现有 status 字段）。

    注意：本函数内部重试 1 次 = 2 次真实 ChatQueue.enqueue_and_wait 尝试。
    scheduler 的失败计数器阈值 3 = trigger_callback 被调 3 次 = 6 次真实尝试。
    循环任务 reschedule 到下次 cron 时间重试，一次性任务失败直接标 failed
    （由 retry_failed_tasks 5 分钟后重置，见 scheduler.py _check_and_trigger_impl）。

    外层 future.result(timeout=300) 兜底 5 分钟总超时，最坏情况（两次尝试都卡满
    5 分钟）总耗时约 10 分钟。实践中内层 120s 超时会先返回空串触发重试。

    IM 推送失败只 log warning，不影响 task 状态（避免重复触发 Agent 生成重复回复）。

    background_script 任务：读 {workspace}/scripts/{script_file} → code_run →
    stdout 空+成功=静默返回 '(silent)'；有 stdout 或 status=error=stdout 注入主 Agent。
    脚本文件不存在=永久删除任务（避免无限重试）。
    """
    from niu_api.alerts import add_pending_alert
    from niu_api.chat import _main_loop
    from niu_api.chat_queue import get_chat_queue

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # ===== background_script 分支 =====
    if task.get("task_kind") == "background_script":
        return _trigger_background_script(task, _main_loop, add_pending_alert)

    # ===== reminder 原逻辑（不动） =====
    from niu_api.chat_queue import get_chat_queue  # reminder 局部 import 保持原样
    prompt = f"[定时任务] {task['content']}"

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("[INTERNAL SCHEDULER] Main event loop not available, cannot trigger task")
        return None

    # 单次尝试函数：通过 ChatQueue 入队并等待回复
    def _try_once() -> str | None:
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
                return agent_reply
            else:
                logger.warning("[INTERNAL SCHEDULER] Agent returned empty reply")
                return None
        except Exception as e:
            logger.error(f"[INTERNAL SCHEDULER] ChatQueue call failed: {e}")
            return None

    # 第一次尝试
    agent_reply = _try_once()

    # 失败重试 1 次（10s 间隔）
    if agent_reply is None:
        logger.warning(f"[INTERNAL SCHEDULER] First attempt failed, retrying in 10s (task_id={task.get('id')})")
        time.sleep(10)
        agent_reply = _try_once()

    if agent_reply is None:
        logger.error(f"[INTERNAL SCHEDULER] Both attempts failed (task_id={task.get('id')})")
        return None

    # 触发小女孩蹦高提醒，传递任务内容摘要让用户知道是什么事
    task_content = task.get("content", "⏰")
    alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
    add_pending_alert(alert_text)

    # IM 通道推送：有推送目标时才推送
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
        logger.warning(f"[SCHEDULER] IM push failed: {e}")

    return agent_reply


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

        # 报错（is_error）走失败路径；有输出非报错走成功路径
        if is_error:
            # one-time 报错：永久删除任务（retry_failed_tasks 会重置 is_recurring=0 的 failed，无限循环）
            # recurring 报错：返回 None 走失败计数器（3次后标 failed，retry 不重置 recurring failed，不循环）
            if not task.get("is_recurring"):
                logger.warning(f"[BG_SCRIPT] one-time 任务报错，永久删除 {task.get('id')}")
                try:
                    get_store().delete_task_permanent(task["id"])
                except Exception as e:
                    logger.error(f"[BG_SCRIPT] 删除失败任务出错: {e}")
            return None
        return agent_reply
    except Exception as e:
        logger.error(f"[BG_SCRIPT] ChatQueue call failed: {e}")
        return None


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
        # 延迟启动，等待系统就绪信号后再开始扫描任务
        _scheduler.start_delayed()

        logger.info("[INTERNAL SCHEDULER] Scheduled to start (waiting for system_ready signal)")


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


def signal_scheduler_ready():
    """通知调度器系统就绪（_main_loop + ChatQueue 已启动）"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.signal_ready()
        logger.info("[INTERNAL SCHEDULER] System ready signal sent")
