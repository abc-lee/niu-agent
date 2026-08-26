"""
Scheduler Service Lifecycle Management

管理内部调度器的启动、停止和状态。
"""

import json
import os
import threading
import time
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

    journal_daily 任务：后台线程直执行——向 journal-agent 派发自理工作流任务文本，
    由其经 session-manager get_messages 直读 messages.db 自取增量（journal.md 内
    最近一条「覆盖至: <message_id>」标记即水位线），**严禁经 ChatQueue enqueue**
    （journal 内容写进 messages.db 会反污染上下文窗口）。派发即返回 "ok"。
    """
    from niu_api.alerts import add_pending_alert
    from niu_api.chat import _main_loop
    from niu_api.chat_queue import get_chat_queue

    logger.info(f"[INTERNAL SCHEDULER] Triggering task: {task['content']}")

    # ===== background_script 分支 =====
    if task.get("task_kind") == "background_script":
        return _trigger_background_script(task, _main_loop, add_pending_alert)

    # ===== journal_daily 分支（T7：journal 迁出睡眠管道后的定时直执行）=====
    if task.get("task_kind") == "journal_daily":
        return _trigger_journal_daily(task)

    # ===== reminder 分支（fire-and-forget） =====
    prompt = f"[定时任务] {task['content']}"

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("[INTERNAL SCHEDULER] Main event loop not available, cannot trigger task")
        return None

    # 同步非阻塞入队（enqueue_sync 内部经 call_soon_threadsafe 桥接到主 loop）
    # 定时提醒只写 DB 唤醒主 Agent（enqueue_sync 入队即写入 Message.DB；Chat 前端由 DB 变更
    # SSE 刷新显示）——程序消息本身不推 IM；主 Agent 的话由 chat_queue scheduler 特判经
    # should_push_im 闸门投递 IM。channel 必须显式传 "scheduler"（默认 "im" 会让主 Agent 回复
    # 走 router.push 广播回退 _push_target，非任务会话）。
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

    return "ok"


# ============== journal_daily 定时任务（T7：journal 直执行，严禁经 ChatQueue） ==============

# 串行化同进程内 journal_daily 运行（防上一轮未完、下一轮触发重叠）
_journal_run_lock = threading.Lock()
_journal_thread: threading.Thread | None = None


def read_journal_scheduled_enabled() -> bool:
    """读 context.journalScheduledEnabled（默认 True——定时 journal 默认开启）。

    D13 禁用语义覆盖 journal：关闭后定时任务静默跳过本轮（返回成功语义，
    不进失败链——禁用是配置而非故障）。避让活跃对话由 scheduler backend-busy
    等待 + 运行前复查共同承担。
    """
    try:
        from niu_api.config import CONFIG_PATH

        config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        return bool(config.get("context", {}).get("journalScheduledEnabled", True))
    except Exception as e:
        logger.debug(f"[JOURNAL_DAILY] 配置读取失败，按开启处理: {e}")
        return True


def _trigger_journal_daily(task: dict) -> str | None:
    """journal_daily 触发：派后台线程直执行，**不经 ChatQueue enqueue**。

    journal 内容若经 enqueue 写入 messages.db 会反污染上下文窗口（T7 铁律），
    故本分支与 reminder/background_script 不同：journal-agent 经 session-manager
    get_messages 直读 messages.db，水位线由 journal.md 内「覆盖至:」标记自理
    （日志即水位线），对主 Agent 上下文零注入。

    完成语义：派发即返回 "ok"（recurring 正常 reschedule）。执行失败仅落日志，
    水位线不推进 → 下轮自动重试（journal.md 写入侧内容去重兜底），无需借
    scheduler 失败计数器重试。运行中重复触发由 _run_journal_daily_job 内
    _journal_run_lock 非阻塞去重（跳过本轮，下轮 cron 再来）。

    D13 避让：后台线程先复用 Scheduler 既有 backend-busy 轮询（_is_backend_busy
    + 二次确认防抖，stagger 同款），活跃对话期等待、超时兜底放行。
    """
    global _journal_thread

    if not read_journal_scheduled_enabled():
        logger.info("[JOURNAL_DAILY] context.journalScheduledEnabled=false, skip")
        return "ok"

    _journal_thread = threading.Thread(
        target=_run_journal_daily_job, name="journal-daily", daemon=True
    )
    _journal_thread.start()
    logger.info(f"[JOURNAL_DAILY] dispatched (task_id={task.get('id')})")
    return "ok"


def _wait_backend_idle(sched: "Scheduler | None") -> None:
    """复用 scheduler 既有 backend-busy 等待：忙则轮询，非忙二次确认后放行。

    总超时兜底沿用 stagger_max_wait 先例（一直忙也强制执行，防永久饥饿）。
    """
    if sched is None:
        return
    poll = sched._busy_poll_interval
    confirm = sched._double_confirm_delay
    deadline = time.monotonic() + sched._stagger_max_wait
    while True:
        if not sched._is_backend_busy():
            # 二次确认防抖（与 scheduler stagger 同款）
            time.sleep(confirm)
            if not sched._is_backend_busy():
                return
            logger.debug("[JOURNAL_DAILY] backend became busy during double-confirm, rewaiting")
        if time.monotonic() >= deadline:
            logger.warning(f"[JOURNAL_DAILY] backend busy wait exceeded {sched._stagger_max_wait}s, forcing run")
            return
        time.sleep(poll)


# 夜间整理任务文本：journal-agent 自理工作流（直读 DB、日志即水位线）。
# 起点判定/分页/错误分流/空批/覆盖标记/@end 协议全部内联在任务文本——与
# config/agents/journal-agent.md 提示词双保险；程序侧不再读游标、不再导出
# 文件、不再解析推进。
_JOURNAL_DAILY_TASK = """请执行夜间工作日志整理（走你的整理流程）：

1. 起点判定：读取 journal.md（不限当天），找最近一条带「覆盖至: <message_id>」标记的整理条目作为起点；整个日志找不到标记则按首次整理处理（get_messages 不传 after_id、limit=200 取最新，回复中注明首次整理）。
2. 分页拉取：调 get_messages(session_id="default", after_id=<起点ID>, limit=200)；返回 has_more=true 时以 next_after_id 继续拉取，直到拉完。
3. 错误分流：reason=="invalid_after_id"（起点已删，如 /new 清库）→ 按首次整理兜底；reason=="transient"（瞬时故障）→ 本轮放弃：回复失败原因即可，不写任何条目和标记，@end 结束。
4. 空批：无新消息 → 回复「无新消息可整理」，不写条目不更新标记，@end 结束。
5. 有新消息 → 与当天已有条目内容级去重后写整理条目（日期标题+要点归纳），条目末尾元信息行必须写「覆盖至: <本批最后一条消息的id>」。
6. 完成后回复报告：写了什么、覆盖到几点、共处理多少条，并以 @end 结束。"""


def _run_journal_daily_job() -> None:
    """journal_daily 执行体：避让等待 → 调 journal-agent 自理整理（直读 DB、日志即水位线）。

    journal-agent 经 session-manager get_messages 直读 messages.db，起点由
    journal.md 内「覆盖至:」标记自判；本函数只负责避让、开关复查与派发，
    不读游标、不导出文件、不解析推进。运行中重复触发由 _journal_run_lock
    非阻塞去重：抢不到锁直接跳过本轮（下轮 cron 再来），锁由本函数独占持有/释放。
    """
    if not _journal_run_lock.acquire(blocking=False):
        logger.warning("[JOURNAL_DAILY] previous run still in progress, skip")
        return
    try:
        from agent.subagent import call_subagent_with_auto_answer

        from niu_api.compat import (
            _extract_overflow_info,
            _incomplete_reason,
            _is_subagent_failure,
            _is_subagent_incomplete,
            _is_subagent_overflow,
        )

        # 1. D13 避让：复用既有 backend-busy 等待（scheduler 未启动时跳过避让）
        try:
            sched = get_scheduler()
        except RuntimeError:
            sched = None
        _wait_backend_idle(sched)

        # 2. LLM 调用前复查开关（避让等待期间用户可能改配置——禁用也应终止本轮）
        if not read_journal_scheduled_enabled():
            logger.info("[JOURNAL_DAILY] disabled after idle wait, skip")
            return

        # 3. 取 runner（llm_config 来源）
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[JOURNAL_DAILY] Runner not initialized, skip")
            return

        # 4. 调 journal-agent 自理整理（直执行；结果只写 journal.md 与回复文本）
        result = call_subagent_with_auto_answer(
            "journal-agent",
            _JOURNAL_DAILY_TASK,
            llm_config=runner.llm_config,
            mcp_client=None,
        )
        logger.info(f"[JOURNAL_DAILY] journal-agent result: {result[:200]}")

        # 5. 成功判定：正常返回即成功；failure/incomplete/overflow 仅落日志
        #    （水位线由子 Agent 按标记协议自理，下轮 cron 自然重试）
        if _is_subagent_failure(result):
            logger.warning(f"[JOURNAL_DAILY] failure: {result[:200]}")
        elif _is_subagent_incomplete(result):
            logger.warning(f"[JOURNAL_DAILY] incomplete ({_incomplete_reason(result)})")
        elif _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[JOURNAL_DAILY] overflow: {overflow_info.get('turns_completed', 0)} turns")
    except Exception as e:
        import traceback

        logger.error(f"[JOURNAL_DAILY] run failed: {e}\n{traceback.format_exc()}")
    finally:
        _journal_run_lock.release()


def _trigger_background_script(task: dict, main_loop, add_alert_fn) -> str | None:
    """background_script 触发：跑脚本，有输出才通知主 Agent。

    复用模块级 code_run / get_chat_queue（service.py 顶部已 import）。
    两分支一致：enqueue_sync 写 DB 唤醒主 Agent（程序消息不推 IM），add_pending_alert 蹦高提醒；主 Agent 的话由 chat_queue scheduler 特判投递 IM。
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
    # 程序消息只写 DB 唤醒主 Agent（enqueue_sync 入队即写入 Message.DB；Chat 前端由 DB 变更
    # SSE 刷新显示）——程序消息本身不推 IM；主 Agent 的话由 chat_queue scheduler 特判经
    # should_push_im 闸门投递 IM。channel 必须显式传 "scheduler"（默认 "im" 会让主 Agent 回复
    # 走 router.push 广播回退 _push_target，非任务会话）。
    q = get_chat_queue()
    enqueue_result = q.enqueue_sync(content=prompt, channel="scheduler", source="scheduler", session_id="default")
    if not enqueue_result.queued:
        logger.error(f"[BG_SCRIPT] Enqueue failed: {task.get('id')}")
        return None

    # 蹦高提醒（与 reminder 分支对齐；内容 = task content）
    task_content = task.get("content", "⏰")
    alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
    try:
        add_alert_fn(alert_text)
    except Exception as e:
        logger.warning(f"[BG_SCRIPT] add_pending_alert failed: {e}")

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
