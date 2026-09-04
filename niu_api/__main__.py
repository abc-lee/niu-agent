"""
Niu API Server - Main Entry Point

HTTP API server for Niu Agent using FastAPI + Uvicorn

单进程架构：Embedding 和 Scheduler 作为内部模块运行。
"""

import asyncio
import logging as _stdlib_logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from niu_api.alerts_api import router as alerts_router
from niu_api.brain_region_api import router as brain_region_router
from niu_api.chat import router as chat_router
from niu_api.compat import router as compat_router
from niu_api.compat import set_preload_stage
from niu_api.config import get_logging_config
from niu_api.http_log_api import router as http_log_router
from niu_api.injector import router as injector_router
from niu_api.kg_api import router as kg_router
from niu_api.llm_proxy import router as llm_proxy_router
from niu_api.notes_api import router as notes_router
from niu_api.session import router as session_router

# Configure logging — gated by config/logging.enabled (缺省 False)
_logging_cfg = get_logging_config()
logger.remove()
if _logging_cfg.enabled:
    logger.enable("")  # 恢复 loguru 全局启用
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=_logging_cfg.level,
    )
    _stdlib_logging.disable(_stdlib_logging.NOTSET)  # 恢复 stdlib logging
    logger.info(f"Logging enabled at level {_logging_cfg.level}")
else:
    logger.disable("")  # loguru 官方推荐全局禁用方式（不 add dev/null sink）
    _stdlib_logging.disable(_stdlib_logging.CRITICAL)  # 禁用 10+ 处散落的 stdlib logger


def check_critical_versions() -> list[str]:
    """检查强制依赖的版本号，返回不匹配的警告列表。"""
    warnings = []

    # lightrag-hku 必须是 1.4.19+（含 distance 字段返回）
    try:
        import lightrag
        from packaging.version import Version
        version = Version(getattr(lightrag, "__version__", "0"))
        if version < Version("1.4.19"):
            warnings.append(
                f"lightrag-hku 版本过低 ({version})，需要 1.4.19+。"
                f"动态知识注入功能可能降级。"
            )
    except ImportError:
        warnings.append("lightrag-hku 未安装")
    except Exception:
        pass  # packaging 不可用时跳过版本检查（不阻止启动）

    return warnings


def _migrate_legacy_journal_daily(ts) -> None:
    """一次性迁移：旧 journal_daily 硬编码直执行任务 → subagent 通用任务类型。

    已有 journal-daily 任务 task_kind='journal_daily' → UPDATE 为
    task_kind='subagent' + agent_name='journal-daily-agent'。
    只改 kind/agent_name，**保用户 cron_expr 不动**（用户可能改过执行时间）。
    """
    existing = ts.find_task_by_name("journal-daily")
    if existing is not None and existing.get("task_kind") == "journal_daily":
        ts.update_task(existing["id"], task_kind="subagent", agent_name="journal-daily-agent")
        logger.info(
            "Migrated legacy journal-daily task to task_kind='subagent' "
            "(agent_name='journal-daily-agent', cron preserved)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # Startup
    logger.info("Niu API Server starting...")
    logger.info(f"[PROCESS-START] PID={os.getpid()} PPID={os.getppid()} started")
    # 契约 A 清理②：预加载段开始前清陈旧 .startup_error（防上次崩溃残留误判 Fatal；
    # ①在 main() 首行，②在 lifespan 开头——双清理覆盖 uvicorn 直启/重启路径）
    _clear_startup_error_file()
    # 0. 版本检查（不阻止启动，仅警告）
    version_warnings = check_critical_versions()
    for w in version_warnings:
        logger.warning(f"[Version Check] {w}")

    # 1. Initialize session store
    set_preload_stage("正在初始化会话")
    from agent.session import get_session_store
    await get_session_store()
    logger.info("Session store initialized")

    # 1.5. Notes use JSON storage (no DB init needed)
    from niu_api.notes import init_db as notes_init_db
    await notes_init_db()  # Creates notes directory if missing

    # 2. Preload embedding model（致命级：失败 → 契约 A .startup_error 文件 + 进程退出，
    #      Rust 启动器以「进程早退 + 文件存在且非空」判定 Fatal 红字展示）
    set_preload_stage("正在加载向量模型")
    from niu_api.internal.embedding import preload as preload_embedding
    logger.info("Preloading embedding model...")
    try:
        preload_embedding()
    except Exception as e:
        _handle_embedding_preload_failure(e)
    logger.info("Embedding model ready")

    # 2.5. LLM 配置门控：检测失败时不启动依赖 LLM 的后台组件
    #      （scheduler/IM/HAWatcher/db_monitor/脑区 gate/LightRAG 背景同步/
    #      response_format 后台探测），仅保留 API 供配置页使用；
    #      启动器弹配置页 → 配置成功 → 退出重启。
    #      与 LightRAG v7 阻塞模式同型：前置条件不可用 → 不启动依赖它的后台工作。
    #      预算 120s/150s（与 test-llm 端点经 resolve_probe_budget 一致，
    #      与启动器 230s 对齐）——慢首响推理模型不会被误判降级（v2.2/v2.4/v2.5）。
    set_preload_stage("正在检测大模型配置")
    from niu_api.internal.lightrag_manager import set_llm_gate_ready
    # 2.6. response_format 后台探测门控（v2.6 时序收口）：先置 False——慢探测
    #      （150-210s）窗口内任何 daemon（如 SkillSync 30s 等待后 get_lightrag()）
    #      触发的 probe 都被跳过（就绪性未确认，跳过正确）；探测结束后再置 llm_ready
    set_llm_gate_ready(False)
    from niu_api.llm_ready import check_llm_ready
    llm_ready, llm_ready_reason = await check_llm_ready()
    logger.info(f"LLM config gate: ready={llm_ready} ({llm_ready_reason})")
    set_llm_gate_ready(llm_ready)

    # 3. Start internal scheduler（仅 LLM 可用时；get_scheduler() 无 lazy 启动，
    #    跳过 start_scheduler 即无定时任务扫描）
    if llm_ready:
        set_preload_stage("正在启动调度器")
        from niu_api.internal.scheduler import start_scheduler
        start_scheduler()
        logger.info("Internal scheduler started")
    else:
        logger.warning("[LLMGate] scheduler 跳过启动（LLM 不可用，定时任务不会触发）")

    # 3.1. Start HAWatcher (if HA configured)
    if llm_ready:
        try:
            from niu_api.internal.ha_watcher import check_and_start
            check_and_start()
            logger.info("HAWatcher check done")
        except Exception as e:
            logger.debug(f"HAWatcher not started: {e}")
    else:
        logger.warning("[LLMGate] HAWatcher 跳过启动（LLM 不可用）")

    # 3.5. Start page-agent-mcp (Node.js browser automation)
    # NOTE: page-agent-mcp should run as a standalone process, NOT started here.
    # The hub-bridge.js creates an HTTP server on port 38401, and if started via
    # subprocess, each MCP call would spawn a new process that can't bind the port.
    # Instead, page-agent-mcp should be started separately or integrated differently.
    # Skipping auto-start to avoid EADDRINUSE errors.

    # 4. Load MCP tools using ToolRegistry
    set_preload_stage("正在加载工具")
    logger.info("Loading MCP tools...")
    from agent.mcp_loader import load_mcp_tools

    try:
        tool_registry = load_mcp_tools()
        logger.info(f"MCP tools loaded: {len(tool_registry.get_schemas())} tools")
    except Exception as e:
        logger.error(f"Failed to load MCP tools: {e}")
        raise

    # 5. Initialize runner with ToolRegistry
    set_preload_stage("正在初始化 Agent")
    logger.info("Initializing NiuRunner...")
    from niu_api.chat import init_runner
    init_runner(tool_registry)

    # 6. preload_complete 标志挪到 lifespan 末尾（见 signal_scheduler_ready 之后），
    #    让"preload 完成"真实反映后端完全就绪（ChatQueue/LightRAG/BrainGraph/signal 全跑完）。
    #    虽然 FastAPI lifespan 语义保证启动器永远在 yield 后看到 ready=true，
    #    但放在所有依赖就绪后更符合语义清晰性原则。

    # 6.0. Enable SQLite WAL mode for messages.db
    set_preload_stage("正在初始化通信通道")
    import sqlite3
    db_path = Path.home() / ".niu" / "messages.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        logger.info("messages.db WAL mode enabled")

    # 6.1. Initialize channel router
    from niu_api.channel import get_channel_router
    from niu_api.channel.electron_channel import ElectronChannelAdapter

    channel_router = get_channel_router()
    channel_router.register("electron", ElectronChannelAdapter())
    logger.info("Channel router initialized (electron channel registered)")

    # 6.2. Start IM Gateway (if configured)
    if llm_ready:
        try:
            import json as _json
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if prefs_path.exists():
                _prefs = _json.loads(prefs_path.read_text(encoding="utf-8"))
            else:
                _prefs = {}
            im_config = _prefs.get("im", {})
            if im_config.get("enabled"):
                from niu_api.channel.gateway import IMGateway, set_im_gateway
                gateway = IMGateway(channel_router=channel_router, port=im_config.get("gateway_port", 19877))
                channel_router.register("im", gateway)
                set_im_gateway(gateway)
                gateway_task = asyncio.create_task(gateway.start())

                def _on_gateway_done(t: asyncio.Task):
                    if not t.cancelled():
                        exc = t.exception()
                        if exc:
                            logger.error(f"IM Gateway startup failed: {exc}")

                gateway_task.add_done_callback(_on_gateway_done)
                logger.info("IM Gateway starting (TCP Server)")
            else:
                logger.info("IM Gateway disabled")
        except Exception as e:
            logger.warning(f"IM Gateway setup failed: {e}")
    else:
        logger.warning("[LLMGate] IM Gateway 跳过启动（LLM 不可用）")

    # 6.5. Save main event loop for SSE sync notifications
    from niu_api.chat import set_main_event_loop
    set_main_event_loop(asyncio.get_running_loop())
    logger.info("SSE event loop captured")

    # 6.5.1. Start daily tmp cleanup background task
    async def _daily_tmp_cleanup():
        """每天凌晨4点清理非当天的临时文件"""
        while True:
            now = datetime.now()
            # 计算到下一个凌晨4点的时间
            next_run = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            try:
                from agent.tmp_dir import cleanup_old_tmp
                cleaned = cleanup_old_tmp()
                if cleaned > 0:
                    logger.info(f"[TmpCleanup] Cleaned {cleaned} old temp files")
            except Exception as e:
                logger.error(f"[TmpCleanup] Error: {e}")

    _cleanup_task = asyncio.create_task(_daily_tmp_cleanup())

    # 6.6. Start ChatQueue (serial message processing)
    set_preload_stage("正在启动消息队列")
    from niu_api.chat_queue import start_chat_queue
    await start_chat_queue()
    logger.info("ChatQueue started")

    # 6.6.1. Start global tidy pipeline queue (single worker serial execution)
    #        §3.0：与 ChatQueue 同生命周期——所有 9 个入口均要求 ChatQueue 已启动（≥6.6）
    set_preload_stage("正在启动整理队列")
    from niu_api.compat import start_pipeline_queue
    start_pipeline_queue()
    logger.info("Pipeline queue started")

    set_preload_stage("正在检查知识图谱")
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

    # 6.7.0. 指针块一致性校验（spec §3.5 / Task 8）——确证不一致自动整库重建；
    #        失败自愈 + 仅日志、不阻塞启动（检测失败≠损坏，check_blocks_integrity 语义）
    try:
        from agent.context_assembler.integrity import check_blocks_integrity
        blocks_integrity = check_blocks_integrity()
        if blocks_integrity.get("repaired"):
            logger.warning(f"[Blocks] 检测到不一致，已整库重建: {blocks_integrity.get('issues')}")
        elif blocks_integrity.get("check_failed"):
            logger.warning(f"[Blocks] 一致性检测失败（不影响启动）: {blocks_integrity.get('error')}")
        elif not blocks_integrity.get("ok"):
            logger.error(f"[Blocks] 一致性问题且重建失败: {blocks_integrity.get('issues')}")
        else:
            logger.info("[Blocks] 一致性校验通过")
    except Exception as e:
        logger.warning(f"[Blocks] 一致性校验异常（不影响启动）: {e}")

    # 6.7.1. Phase 1 检测到损坏时 pause ChatQueue（worker 已启动，pause 后不消费）
    #        IM/scheduler 入队的消息只堆积在队列里，不触发 runner.chat
    from niu_api.internal.lightrag_manager import pause_chatqueue_if_corrupt
    pause_chatqueue_if_corrupt(phase1_result)

    # 6.7.1.1 Phase 1 检测到损坏时取消 scheduler delayed start
    #        补 P1 漏洞：scheduler 180s 超时强行 start 的漏洞（_ready_event.wait(180)）
    #        即使不调 signal_scheduler_ready，scheduler 线程 180s 后也会强行 start
    from niu_api.internal.lightrag_manager import cancel_scheduler_delayed_start_if_corrupt
    cancel_scheduler_delayed_start_if_corrupt(phase1_result)

    # 6.7.2. db_monitor 推迟到 Phase 1 之后启动（need_repair=True 或 LLM 不可用时跳过）
    db_monitor_task = None  # 占位变量，shutdown 时引用不报 NameError
    from niu_api.internal.lightrag_manager import should_start_db_monitor
    if llm_ready and should_start_db_monitor(phase1_result):
        from niu_api.db_monitor import run_db_monitor
        db_monitor_task = asyncio.create_task(run_db_monitor())
        logger.info("db_monitor task 已启动")
    elif not llm_ready:
        logger.warning("[LLMGate] db_monitor 跳过启动（LLM 不可用）")
    else:
        logger.warning("[LightRAG] db_monitor 跳过启动（LightRAG 损坏，等用户决策）")

    # 6.7.3. Signal scheduler 挪到 lifespan 末尾（见 L8.7），保证所有后台依赖就绪后才 signal。
    #        原位置 L218 在 Phase 1 gate 之后但 L255 之后（LightRAG eager init /
    #        BrainGraph / _system_tasks 等）之前，scheduler sleep 2s 后扫描过期任务
    #        会撞未就绪 runner，导致 user 消息已写 DB 但 runner.chat() 抛异常、任务被标 failed。
    #        挪到末尾后 need_repair=True 分支仍由 should_signal_scheduler_ready gate 控制
    #        （cancel_scheduler_delayed_start_if_corrupt 在 L204 已调，flag 持久，行为一致）。

    # 7. (Removed) Weekly vector cleanup — vectors.db is deprecated,
    #    LightRAG manages its own storage. Cleanup is no longer needed.

    # v7: Phase 1 need_repair=True 时，跳过所有依赖 LightRAG 的初始化
    #     等用户在 rfd 弹窗决策退出或修复后程序整体退出
    # !!!铁律：_lightrag_corrupt_skip_init 必须是 lifespan 函数内的局部变量，
    #         不得提升为模块级全局变量。理由：
    #         1. 模块级全局在 exit 路径上不会被清除，下次正常启动（reload/module cache）
    #            仍读到 True，会错误跳过所有初始化，导致 LightRAG 完全不可用
    #         2. lifespan 每次启动重新计算 phase1_result，局部变量自然随函数结束失效
    #         3. 子 Agent 实施时禁止把它改成 `global _lightrag_corrupt_skip_init` 声明
    _lightrag_corrupt_skip_init = phase1_result.get("need_repair", False)
    if _lightrag_corrupt_skip_init:
        logger.warning(
            "[LightRAG] 检测到损坏，跳过 Phase 1 之后的初始化"
            "（LightRAG eager init / PipelineWatcher / LightRAGSync / "
            "BrainGraph / create_default_regions / RegionSync / _system_tasks）"
        )

    # v6: Phase 2 不自动修复，等用户在 rfd 弹窗点'尝试修复'
    # phase1_result["need_repair"] 状态通过 get_lightrag_status() 的 integrity 字段暴露给 splash
    # 用户点'尝试修复'后，splash 调 /api/kg/lightrag/repair 触发 run_repair_on_user_request
    if phase1_result.get("need_repair"):
        logger.warning("[LightRAG] 检测到损坏，等待用户在 rfd 弹窗选择'退出'或'尝试修复'")

    # v7: Phase 1 need_repair=True 时跳过所有依赖 LightRAG 实例的初始化
    #     （LightRAG eager init / PipelineWatcher / LightRAGSync / BrainGraph /
    #      vectors.db cleanup / create_default_regions / RegionSync / _system_tasks）
    #     need_repair=False 时逻辑跟原来一致
    #
    # 预初始化 region_sync = None，确保 LightRAG 损坏分支（_lightrag_corrupt_skip_init=True）
    # 跳过整个 if 块时，变量在 lifespan 末尾 run_brain_region_startup_gate 调用处仍可见，
    # 避免 NameError。run_brain_region_startup_gate helper 内部处理 None 跳过 gate。
    region_sync = None
    if not _lightrag_corrupt_skip_init:
        # 7.5. Eagerly initialize LightRAG (triggers lazy init before background threads start)
        set_preload_stage("正在初始化知识图谱")
        # This ensures _lightrag_ready Event is set quickly, so SkillSync/LightRAGSync/RegionSync
        # don't have to wait for their timeout. If init fails, threads will handle it gracefully.
        try:
            from niu_api.internal.lightrag_manager import get_lightrag
            rag = get_lightrag()
            if rag is not None:
                logger.info("LightRAG instance initialized (eager)")
            else:
                logger.warning("LightRAG instance not available (init failed or not installed)")
        except Exception as e:
            logger.warning(f"LightRAG eager init failed: {e}")

        # 7.6. Start pipeline watcher (pushes ingest-started/completed SSE events
        #      when LightRAG pipeline becomes busy/idle, so frontend progress ring
        #      appears even for MCP-tool-triggered ingestion)
        try:
            from niu_api.kg_api import start_pipeline_watcher
            start_pipeline_watcher()
            logger.info("Pipeline watcher started")
        except Exception as e:
            logger.warning(f"Pipeline watcher start failed: {e}")

        # 8. Start LightRAG background sync (periodic photo/document backfill)
        #     叠加 LLM 门控（v2.6）：LLM 不可用时 auto_start=False——保留完整
        #     try/except 骨架，勿裸调用；shutdown 的 get_lightrag_sync()/
        #     stop_background_sync() 对未启动实例有 `if self._thread:` 守卫，安全。
        try:
            from agent.injector.lightrag_sync import get_lightrag_sync
            lightrag_sync = get_lightrag_sync(auto_start=llm_ready)
            if llm_ready:
                logger.info("LightRAG background sync started (interval: 6h)")
            else:
                logger.warning("[LLMGate] LightRAG 背景同步跳过启动（LLM 不可用）")
        except Exception as e:
            logger.warning(f"LightRAG background sync start failed: {e}")

        # 8.01. Initialize Niu self entity (must be before RegionSync so brain regions exist)
        set_preload_stage("正在初始化脑区")
        try:
            from niu_api.internal.brain_graph import get_brain_graph
            brain = get_brain_graph()
            brain.ensure_niu_entity()
            logger.info("Brain graph initialized (Niu entity ensured)")
        except Exception as e:
            logger.warning(f"Brain graph initialization failed: {e}")

        # Clean up deprecated vectors.db
        try:
            for _vdb_path in [Path.home() / ".niu" / "work" / "vectors.db", Path.home() / ".niu" / "vectors.db"]:
                if _vdb_path.exists():
                    _vdb_path.unlink()
                    logger.info("Removed deprecated vectors.db: %s", _vdb_path)
        except Exception as e:
            logger.debug(f"vectors.db cleanup failed (non-blocking): {e}")

        # 8.02. Create default brain regions (must be before RegionSync so activation_mgr finds them)
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_manager import create_default_regions
            region_result = create_default_regions(
                adapter=LightRAGAdapter(),
                ingester=LightRAGIngester(),
            )
            logger.info(f"Default brain regions: created={region_result['created']}, existing={region_result['existing']}")
        except Exception as e:
            logger.warning(f"Default brain region creation failed: {e}")

        # 8.025. Get RegionSync singleton (without starting background thread yet)
        #       This must happen after brain regions are created so the singleton exists
        #       for us to signal readiness before the thread starts.
        try:
            from agent.injector.region_sync import get_region_sync
            region_sync = get_region_sync(auto_start=False)
        except Exception as e:
            logger.warning(f"RegionSync singleton creation failed: {e}")
            region_sync = None

        # 8.026. Signal brain regions ready so _sync_loop won't block on first sync
        if region_sync is not None:
            try:
                region_sync.signal_brain_ready()
                logger.info("Brain regions ready signal sent to RegionSync")
            except Exception as e:
                logger.warning(f"Failed to signal brain regions ready: {e}")

        # 8.1. (已推迟) Start brain region periodic sync
        #      v3 关键改动：start_background_sync() 不在此处调用，推迟到 lifespan 末尾
        #      run_brain_region_startup_gate 之后（见 8.7 节）。这样 gate 运行期间
        #      _sync_loop daemon 不存在，run_sync_once_for_startup 必拿 _sync_lock、
        #      必跑完 _refresh_activation_manager，从结构上消除首次启动场景 daemon
        #      与 lifespan 抢锁的竞态（第二轮审查严重问题）。

        # 8.6. Ensure system recurring tasks exist (by name, not cron_expr)
        set_preload_stage("正在加载系统任务")
        _system_tasks = [
            # journal 迁出睡眠管道 → scheduler 内置定时任务（subagent 静默直调
            # journal-daily-agent，不经主 Agent/ChatQueue——journal 内容写进
            # messages.db 会反污染上下文窗口；整理协议见 config/agents/journal-daily-agent.md）
            {
                "name": "journal-daily",
                "content": "每日日志整理：程序直读 DB 增量提取写入 journal.md",
                "cron_expr": "0 18 * * *",
                "hour": 18,
                "task_kind": "subagent",
                "agent_name": "journal-daily-agent",
            },
            {
                "name": "weekly-report-reminder",
                "content": "请调用 journal-agent 生成本周周报，整理后展示给用户确认是否需要修改",
                "cron_expr": "0 9 * * 1",
                "hour": 9,
                "dow": 1,
            },
        ]

        try:
            from niu_api.internal.scheduler import get_store

            ts = get_store()

            # Ensure each system task exists (by name, not cron_expr)
            for task_def in _system_tasks:
                existing = ts.find_task_by_name(task_def["name"])

                if existing is None:
                    # Create new task
                    now = datetime.now()
                    hour = task_def.get("hour", 8)
                    next_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                    if task_def.get("dow") is not None:
                        # Calculate next target weekday
                        days_ahead = task_def["dow"] - now.isoweekday()
                        if days_ahead < 0:
                            days_ahead += 7
                        next_time = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
                        if next_time <= now:
                            next_time += timedelta(days=7)
                    elif next_time <= now:
                        next_time += timedelta(days=1)

                    ts.create_task(
                        content=task_def["content"],
                        scheduled_at=next_time.isoformat(),
                        is_recurring=True,
                        cron_expr=task_def["cron_expr"],
                        event_type="recurring",
                        name=task_def["name"],
                        task_kind=task_def.get("task_kind", "reminder"),
                        agent_name=task_def.get("agent_name"),
                    )
                    logger.info(f"Created system task '{task_def['name']}' (next run: {next_time})")
                elif existing.get("content") != task_def["content"]:
                    # Update content only (keep user's cron_expr changes)
                    ts.update_task(existing["id"], content=task_def["content"])
                    logger.info(f"Updated system task '{task_def['name']}' content (id={existing['id']})")
                else:
                    logger.debug(f"System task '{task_def['name']}' already exists and up-to-date")
            # 旧 journal_daily 硬编码任务一次性迁移为 subagent 通用类型（保用户 cron）
            _migrate_legacy_journal_daily(ts)
            # T7 一次性迁移：旧 daily-journal-check 提醒经主 Agent 调 journal-agent（ChatQueue
            # 注入 messages.db 反污染上下文），已被内置 journal-daily 直执行任务取代
            legacy_journal = ts.find_task_by_name("daily-journal-check")
            if legacy_journal is not None:
                ts.delete_task_permanent(legacy_journal["id"])
                logger.info("Removed legacy system task 'daily-journal-check' (superseded by journal-daily)")

        except Exception as e:
            logger.warning(f"Failed to ensure system tasks: {e}")

    # 8.7. Brain region startup gate + Signal scheduler + start_background_sync（推迟）
    #      llm_ready=False 时整段跳过：scheduler 未启动（signal 无意义）、
    #      脑区 gate 会条件性调 LLM 生成标签（LLM 不可用时应跳过）、
    #      region 背景同步同样依赖 LLM 标签生成。
    if llm_ready:
        set_preload_stage("正在同步脑区状态")
        #      必须在所有后台依赖就绪后才 signal。
        #      脑区就绪 gate（run_sync_once_for_startup）：在 signal_scheduler_ready 之前同步跑首次
        #      run_sync()，确保 activation_mgr 已 set。否则日常重启场景下 _sync_loop 因 24h
        #      间隔保护不跑首次，activation_mgr 永远 None，scheduler 触发的过期任务和用户第一轮
        #      请求都撞 None，脑区动态注入缺失。90s 超时兜底：超时后 warning 但仍 signal，
        #      靠 _get_brain_injector 的 forced sync daemon 兜底（5 分钟冷却 + 防并发）。
        #      region_sync is None（LightRAG 损坏分支）时 helper 跳过 gate。
        #      start_background_sync 推迟到 gate 之后调用（v3）：gate 运行期间 _sync_loop
        #      daemon 不存在，run_sync_once_for_startup 必拿锁必跑完，消除首次启动竞态。
        from niu_api.internal.lightrag_manager import should_signal_scheduler_ready
        from niu_api.internal.scheduler.service import signal_scheduler_ready
        from niu_api.startup_gate import run_brain_region_startup_gate
        gate_result = run_brain_region_startup_gate(
            region_sync=region_sync,
            signal_scheduler_ready_fn=signal_scheduler_ready,
            should_signal=should_signal_scheduler_ready(phase1_result),
            timeout=90.0,
        )
        if gate_result is True:
            logger.info("Scheduler system_ready signal sent (brain region ready)")
        elif gate_result is False:
            logger.warning(
                "Scheduler system_ready signal sent (brain region degraded, "
                "forced sync daemon will retry on first request)"
            )
        else:
            logger.warning("[LightRAG] Scheduler system_ready signal 跳过（LightRAG 损坏或 region_sync 未创建）")

        # 8.7.5. Start brain region periodic sync（v3：从 8.1 推迟到 gate 之后，含 None 守卫）
        #      必须在 run_brain_region_startup_gate 之后调用，确保 gate 先抢锁跑完首次同步。
        #      保留 if region_sync is not None 守卫：LightRAG 损坏分支 region_sync=None，
        #      裸调用会 AttributeError。整块从原 8.1 平移而来。
        if region_sync is not None:
            try:
                region_sync.start_background_sync()
                logger.info("Brain region sync started (interval: 24h, after startup gate)")
            except Exception as e:
                logger.warning(f"Brain region sync start failed: {e}")
    else:
        logger.warning("[LLMGate] 脑区 gate / scheduler signal / 背景同步跳过（LLM 不可用）")

    # 8.8. Mark preload as complete — 所有后端依赖就绪后才标记
    #      启动器轮询 /api/preload-status 看到这个标志后才 launch 前端
    from niu_api.compat import set_preload_complete
    set_preload_complete()
    logger.info("Preload complete (after all backend dependencies ready)")

    yield

    # Shutdown
    logger.info("Niu API Server shutting down...")

    # Cancel daily tmp cleanup task
    try:
        _cleanup_task.cancel()
        logger.info("Daily tmp cleanup task cancelled")
    except Exception:
        pass

    # 停止 brain region 后台同步 (must stop before LightRAG — depends on it)
    try:
        from agent.injector.region_sync import get_region_sync
        region_sync = get_region_sync()
        region_sync.stop_background_sync()
        logger.info("Brain region sync stopped")
    except Exception as e:
        logger.warning(f"Failed to stop region sync: {e}")

    # 停止 LightRAG 后台同步
    try:
        from agent.injector.lightrag_sync import get_lightrag_sync
        lightrag_sync = get_lightrag_sync()
        lightrag_sync.stop_background_sync()
        logger.info("LightRAG background sync stopped")
    except Exception as e:
        logger.warning(f"Failed to stop LightRAG sync: {e}")

    # 停止 LightRAG 事件循环（取消 fire_and_forget 后台任务 + 停止循环）
    try:
        from niu_api.internal.lightrag_manager import shutdown_lightrag_loop
        shutdown_lightrag_loop(timeout=10.0)
        logger.info("LightRAG event loop stopped")
    except Exception as e:
        logger.warning(f"Failed to stop LightRAG event loop: {e}")

    # 停止全局整理队列（先于 ChatQueue——worker 依赖 chat 机制）：排出剩余项 + 取消 worker
    try:
        from niu_api.compat import stop_pipeline_queue
        await asyncio.wait_for(stop_pipeline_queue(), timeout=10.0)
        logger.info("Pipeline queue stopped")
    except TimeoutError:
        logger.warning("Pipeline queue stop timed out after 10s")
    except Exception as e:
        logger.warning(f"Failed to stop pipeline queue: {e}")

    # 停止 ChatQueue
    try:
        # 先取消 db_monitor task（避免停止后还在写入）
        # v7: db_monitor_task 可能是 None（Phase 1 need_repair=True 时未启动）
        if db_monitor_task is not None:
            db_monitor_task.cancel()
            try:
                await db_monitor_task
            except asyncio.CancelledError:
                pass
            logger.info("db_monitor task 已取消")
    except Exception as e:
        logger.warning(f"Failed to cancel db_monitor task: {e}")

    try:
        from niu_api.chat_queue import stop_chat_queue
        await asyncio.wait_for(stop_chat_queue(), timeout=10.0)
        logger.info("ChatQueue stopped")
    except TimeoutError:
        logger.warning("ChatQueue stop timed out after 10s")
    except Exception as e:
        logger.warning(f"Failed to stop ChatQueue: {e}")

    # 停止 IM Gateway
    try:
        from niu_api.channel.gateway import get_im_gateway
        gateway = get_im_gateway()
        if gateway:
            await gateway.stop()
            logger.info("IM Gateway stopped")
    except Exception as e:
        logger.warning(f"Failed to stop IM Gateway: {e}")

    from niu_api.internal.scheduler import stop_scheduler
    stop_scheduler()

    try:
        from niu_api.internal.ha_watcher import stop_watcher
        stop_watcher()
    except Exception as e:
        logger.debug(f"HAWatcher stop skipped: {e}")

    logger.info("Niu API Server shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Niu API",
    description="HTTP API for Niu Agent - Python-based personal knowledge assistant",
    version="0.2.0",
    lifespan=lifespan,
)

# Add CORS middleware
# Electron 前端通过 loadFile 加载本地 HTML，origin 可能为 file:// 或 null；
# 开发调试时可能从 localhost 访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["file://", "null", "http://localhost:9876", "http://127.0.0.1:9876"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(compat_router)  # Compatibility API (must be first to match /api/ paths)
app.include_router(session_router)
app.include_router(chat_router)
app.include_router(injector_router)  # Injector API
app.include_router(alerts_router)  # Alerts API
app.include_router(kg_router)  # Knowledge Graph API
app.include_router(brain_region_router)  # Brain Region API
app.include_router(notes_router)  # Notes API
app.include_router(llm_proxy_router)
if get_logging_config().enabled:
    app.include_router(http_log_router)  # HTTP log viewer (/http-log/*)
    logger.info("HTTP log viewer service enabled at /http-log/")
else:
    logger.info("HTTP log viewer service disabled (logging.enabled=false)")


# Mount scheduler router
from niu_api.internal.scheduler import scheduler_router  # noqa: E402

app.include_router(scheduler_router)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    from niu_api.internal.embedding import is_ready as embedding_ready
    from niu_api.internal.scheduler import get_scheduler

    scheduler_running = False
    try:
        get_scheduler()
        scheduler_running = True
    except Exception:
        pass

    return {
        "status": "ok",
        "service": "niu-api",
        "embedding_ready": embedding_ready(),
        "scheduler_running": scheduler_running,
    }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Niu API",
        "version": "0.2.0",
        "architecture": "single-process",
        "endpoints": {
            "chat": "/chat (POST) - Main chat endpoint",
            "session": "/session/* - Session management",
            "scheduler": "/scheduler/* - Scheduled tasks",
            "health": "/health - Health check",
        },
    }


def _check_single_instance(port: int) -> bool:
    """启动前检查端口是否已被其他 niu_api 占用——占用则返回 False（调用方退出）。

    防双实例并发写 vdb（LightRAG 单进程前提）。launcher kill_stale 是主防线，
    本检查是最后防线（绕过 launcher 手动双起 / kill_stale 漏网）。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR=1：允许 TIME_WAIT 重绑防假阳性；LISTEN 端口双 bind 与
        # REUSEADDR 取值无关（无 SO_REUSEPORT）——检测有效性不受影响（R2 P1-1）
        if sys.platform != "win32":  # Windows setsockopt 语义不同——直接 bind 已满足（R2 P2-2）
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True  # 能 bind = 无实例占用
    except OSError:
        return False  # 端口被占 = 已有实例
    finally:
        s.close()


def _clear_startup_error_file() -> None:
    """清理陈旧 .startup_error（跨域契约 A，embedding 致命错误恢复——自 reranker 工程保留）。

    判定 = 进程早退 + 文件存在且非空 → Rust 启动器显示 Fatal；启动成功时文件必须不存在。
    main() 首行与 lifespan 开头双清理（防上次崩溃残留误判）。
    """
    try:
        (Path.home() / ".niu" / ".startup_error").unlink(missing_ok=True)
    except OSError:
        pass


def _handle_embedding_preload_failure(err: Exception) -> None:
    """Embedding 预加载失败 = 致命错误（跨域契约 A，纯文件语义）。

    写 ~/.niu/.startup_error（人读文案）后退出进程。退出码任意——uvicorn 把
    lifespan 内 sys.exit/raise 吞成退出码 0，不依赖非零码；Rust 侧以
    「进程早退 + 文件存在且非空」判定 Fatal。
    """
    message = f"向量模型（embedding）加载失败：{err}"
    try:
        niu_dir = Path.home() / ".niu"
        niu_dir.mkdir(parents=True, exist_ok=True)
        (niu_dir / ".startup_error").write_text(message, encoding="utf-8")
    except OSError as e:
        logger.error(f"写入 .startup_error 失败：{e}")
    logger.error(f"Embedding 预加载失败，中断启动：{err}")
    sys.exit(1)


def main():
    """Main entry point - run with: python -m niu_api"""
    # 契约 A 清理①：单实例端口自检之前清陈旧 .startup_error——该自检 sys.exit
    # 早于 uvicorn.run，早退分支不得看到上次崩溃残留的文件（误判 Fatal）
    _clear_startup_error_file()
    # 单实例自检（防线二，最后防线）：launcher kill_stale 是主防线，本检查兜底
    # 绕过 launcher 手动双起 / kill_stale 漏网的场景——防双实例并发写 vdb。
    # 端口读取上移到 main() 开头（main() 内仅保留这一处读取，保证 T2.7 env 接线断言唯一语义）
    port = int(os.environ.get("NIU_API_PORT", "9876"))
    if not _check_single_instance(port):
        logger.error(f"检测到已有 niu_api 实例占用端口 {port}，退出（防双实例并发写 vdb）")
        sys.exit(1)

    import atexit

    import uvicorn

    logger.info(f"[PROCESS-START-MAIN] PID={os.getpid()} PPID={os.getppid()} entered main()")

    def _cleanup_multiprocessing():
        """Clean up multiprocessing resources on exit."""
        try:
            from multiprocessing import resource_tracker
            resource_tracker._resource_tracker._stop()
        except Exception:
            pass

    atexit.register(_cleanup_multiprocessing)

    def _log_process_exit():
        """记录进程退出，用于诊断僵尸进程问题"""
        logger.info(f"[PROCESS-EXIT] PID={os.getpid()} exiting normally")

    atexit.register(_log_process_exit)

    logger.info(f"Starting Niu API Server on port {port}")

    uvicorn.run(
        "niu_api.__main__:app",
        host="127.0.0.1",
        port=port,
        reload=False,  # Disable reload for production
        log_level="warning" if get_logging_config().enabled else "critical",
        access_log=get_logging_config().enabled,
    )


if __name__ == "__main__":
    main()
