"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import asyncio
import json
import os
import threading
from pathlib import Path

from agent.handler import code_run
from loguru import logger

from niu_api.chat_queue import get_chat_queue

from .scheduler import Scheduler
from .task_store import TaskStore

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
    任务触发回调：通过 ChatQueue 入队，入队成功即完成（fire-and-forget）

    从调度器工作线程调用。ChatQueue 串行处理消息，自动持久化到数据库并 SSE 推送。

    完成语义：**入队成功 = 通知已送达 = 任务完成**，不再等待 Agent 回复。
    此前实现用 enqueue_and_wait（120s 超时）等 Agent 回复，超时判失败后
    10s 重试再入队——长任务（周报类 >120s）每次踩线超时 → 同一条任务入队两次
    → 重复发送（2026-08-10 09:00 weekly-report-reminder 实证：121.8s 生成耗时
    触发超时重试，09:00:00 与 09:02:10 两条周报入队）。
    Agent 处理失败由 ChatQueue 降级回复机制兜底，不需要 scheduler 知道。

    返回：
    - "ok"：入队成功（循环任务 reschedule / 一次性任务 completed）
    - None：入队失败（loop 不可用）——scheduler 走失败链
      （循环任务失败计数器 3 次标 failed；一次性任务标 failed 由 retry_failed_tasks
      5 分钟后重置）

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

    # ===== reminder 分支（fire-and-forget） =====
    prompt = f"[定时任务] {task['content']}"

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("[INTERNAL SCHEDULER] Main event loop not available, cannot trigger task")
        return None

    # 同步非阻塞入队（enqueue_sync 内部经 call_soon_threadsafe 桥接到主 loop）
    # channel 必须显式传 "scheduler"（enqueue_sync 默认 "im"）：ChatQueue worker
    # （chat_queue.py 回复路由 elif 分支）会把 Agent 回复自动 route 回 channel——若为 "im"，
    # 回复会被 push 到 IM（channel/gateway.py 空 channel_id 回退广播），叠加下方手动
    # route_out(prompt) = 同一任务两条 IM 消息。"scheduler" 通道未注册 → 不投递独立消息；
    # chat_queue 对 scheduler 特判"仅终结不投递"：有 IM 继承（_im_channel_id 非空）时
    # send_sync 空 content 终结流式卡片（卡片显示完整回复、streaming_mode 关闭），
    # 无 IM 继承时维持 no-op——回复只走 SSE 前端，与原 enqueue_and_wait(channel="scheduler") 语义一致。
    q = get_chat_queue()
    enqueue_result = q.enqueue_sync(content=prompt, channel="scheduler", source="scheduler", session_id="default")
    if not enqueue_result.queued:
        logger.error(f"[INTERNAL SCHEDULER] Enqueue failed (task_id={task.get('id')})")
        return None

    # 蹦高提醒：入队即触发（不再等 Agent 回复）
    task_content = task.get("content", "⏰")
    alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
    try:
        add_pending_alert(alert_text)
    except Exception as e:
        logger.warning(f"[INTERNAL SCHEDULER] add_pending_alert failed: {e}")

    # IM 通道推送（内容 = 任务内容，不再等 Agent 回复）
    try:
        from niu_api.channel import get_channel_router
        router = get_channel_router()
        if router.has_channel("im"):
            # 优先用 task chat_id，回退到继承的 _im_channel_id（确保 route_out 走 SEND 终结卡片）
            from niu_api.chat import get_or_create_runner
            _runner = get_or_create_runner()
            im_cid = _runner.get_im_channel() if _runner else ""
            push_chat_id = task.get("chat_id") or im_cid
            push_future = asyncio.run_coroutine_threadsafe(
                router.route_out(prompt, "im", push_chat_id),
                loop,
            )
            push_future.result(timeout=30)
    except Exception as e:
        logger.warning(f"[SCHEDULER] IM push failed: {e}")

    return "ok"


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

    # fire-and-forget：入队即完成，不等待 Agent 回复（与 reminder 分支一致，
    # 消除"等待超时 → 重试再入队"导致的重复触发）。
    # channel 必须显式传 "scheduler"（enqueue_sync 默认 "im"）：ChatQueue worker
    # 会把 Agent 回复自动 route 回 channel——若为 "im" 则回复被 push 到 IM，
    # 叠加下方手动 route_out(prompt) = 双 IM 消息。"scheduler" 通道未注册 → no-op。
    q = get_chat_queue()
    enqueue_result = q.enqueue_sync(content=prompt, channel="scheduler", source="scheduler", session_id="default")
    if not enqueue_result.queued:
        logger.error(f"[BG_SCRIPT] Enqueue failed: {task.get('id')}")
        return None

    # 蹦高 + IM 推送（与 reminder 分支对齐；内容 = prompt）
    task_content = task.get("content", "⏰")
    alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
    try:
        add_alert_fn(alert_text)
    except Exception as e:
        logger.warning(f"[BG_SCRIPT] add_pending_alert failed: {e}")

    try:
        from niu_api.channel import get_channel_router
        router = get_channel_router()
        if router.has_channel("im"):
            from niu_api.chat import get_or_create_runner
            _runner = get_or_create_runner()
            im_cid = _runner.get_im_channel() if _runner else ""
            push_chat_id = task.get("chat_id") or im_cid
            push_future = asyncio.run_coroutine_threadsafe(
                router.route_out(prompt, "im", push_chat_id),
                loop,
            )
            push_future.result(timeout=30)
    except Exception as e:
        logger.warning(f"[BG_SCRIPT] IM push failed: {e}")

    # 脚本报错已注入主 Agent（Agent 会看到错误输出并处理）。
    # recurring 报错返回 None：保留 3-strike DLQ（scheduler 失败计数，3 次标
    # status='failed' 终态——task_store.retry_failed_tasks 只重置 one-time，
    # recurring failed 不再重试）。返回 "ok" 会让永久失败脚本每周期无限注入报错。
    # one-time 报错返回 "ok"（scheduler 成功路径自动删除）+ 手动永久删除双保险，
    # 避免 retry_failed_tasks 5 分钟后重置 → 无限循环。
    if is_error and not task.get("is_recurring"):
        logger.warning(f"[BG_SCRIPT] one-time 任务报错，永久删除 {task.get('id')}")
        try:
            get_store().delete_task_permanent(task["id"])
        except Exception as e:
            logger.error(f"[BG_SCRIPT] 删除失败任务出错: {e}")
        return "ok"
    if is_error:
        return None
    return "ok"


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
