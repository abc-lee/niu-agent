"""
Niu API Server - Main Entry Point

HTTP API server for Niu Agent using FastAPI + Uvicorn

单进程架构：Embedding 和 Scheduler 作为内部模块运行。
"""

import sys
import os
import asyncio
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from niu_api.session import router as session_router
from niu_api.chat import router as chat_router
from niu_api.compat import router as compat_router
from niu_api.injector import router as injector_router
from niu_api.alerts_api import router as alerts_router
from niu_api.kg_api import router as kg_router
from niu_api.brain_region_api import router as brain_region_router
from niu_api.notes_api import router as notes_router
from niu_api.llm_proxy import router as llm_proxy_router
from niu_api.http_log_api import router as http_log_router


# Configure logging
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # Startup
    logger.info("Niu API Server starting...")
    logger.info(f"[PROCESS-START] PID={os.getpid()} PPID={os.getppid()} started")

    # 1. Initialize session store
    from agent.session import get_session_store
    store = await get_session_store()
    logger.info("Session store initialized")

    # 1.5. Notes use JSON storage (no DB init needed)
    from niu_api.notes import init_db as notes_init_db
    await notes_init_db()  # Creates notes directory if missing

    # 2. Preload embedding model
    from niu_api.internal.embedding import preload as preload_embedding
    logger.info("Preloading embedding model...")
    preload_embedding()
    logger.info("Embedding model ready")

    # 3. Start internal scheduler
    from niu_api.internal.scheduler import start_scheduler
    start_scheduler()
    logger.info("Internal scheduler started")

    # 3.1. Start HAWatcher (if HA configured)
    try:
        from niu_api.internal.ha_watcher import check_and_start
        check_and_start()
        logger.info("HAWatcher check done")
    except Exception as e:
        logger.debug(f"HAWatcher not started: {e}")

    # 3.5. Start page-agent-mcp (Node.js browser automation)
    # NOTE: page-agent-mcp should run as a standalone process, NOT started here.
    # The hub-bridge.js creates an HTTP server on port 38401, and if started via
    # subprocess, each MCP call would spawn a new process that can't bind the port.
    # Instead, page-agent-mcp should be started separately or integrated differently.
    # Skipping auto-start to avoid EADDRINUSE errors.

    # 4. Load MCP tools using ToolRegistry
    logger.info("Loading MCP tools...")
    from agent.mcp_loader import load_mcp_tools

    try:
        tool_registry = load_mcp_tools()
        logger.info(f"MCP tools loaded: {len(tool_registry.get_schemas())} tools")
    except Exception as e:
        logger.error(f"Failed to load MCP tools: {e}")
        raise

    # 5. Initialize runner with ToolRegistry
    logger.info("Initializing NiuRunner...")
    from niu_api.chat import init_runner
    init_runner(tool_registry)

    # 6. Mark preload as complete
    from niu_api.compat import set_preload_complete
    set_preload_complete()
    logger.info("Preload complete, ready to show window")

    # 6.0. Enable SQLite WAL mode for messages.db
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
    from niu_api.chat_queue import start_chat_queue
    await start_chat_queue()
    logger.info("ChatQueue started")

    # 6.7. Signal scheduler that system is ready (ChatQueue operational)
    from niu_api.internal.scheduler.service import signal_scheduler_ready
    signal_scheduler_ready()
    logger.info("Scheduler system_ready signal sent")

    # 7. (Removed) Weekly vector cleanup — vectors.db is deprecated,
    #    LightRAG manages its own storage. Cleanup is no longer needed.

    # 7.5. Eagerly initialize LightRAG (triggers lazy init before background threads start)
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
    try:
        from agent.injector.lightrag_sync import get_lightrag_sync
        lightrag_sync = get_lightrag_sync(auto_start=True)
        logger.info("LightRAG background sync started (interval: 6h)")
    except Exception as e:
        logger.warning(f"LightRAG background sync start failed: {e}")

    # 8.01. Initialize Niu self entity (must be before RegionSync so brain regions exist)
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
        from niu_api.internal.region_manager import create_default_regions
        from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
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

    # 8.1. Start brain region periodic sync (background thread starts here)
    if region_sync is not None:
        try:
            region_sync.start_background_sync()
            logger.info("Brain region sync started (interval: 24h)")
        except Exception as e:
            logger.warning(f"Brain region sync start failed: {e}")

    # 8.6. Ensure system recurring tasks exist (by name, not cron_expr)
    _SYSTEM_TASKS = [
        {
            "name": "daily-journal-check",
            "content": "请调用 journal-agent 记录今天的日志，整理后展示给用户确认是否需要修改",
            "cron_expr": "0 18 * * *",
            "hour": 18,
        },
        {
            "name": "weekly-report-reminder",
            "content": "提醒用户本周工作已汇总，询问是否需要生成周报。如需生成，请调用 journal-agent",
            "cron_expr": "0 9 * * 1",
            "hour": 9,
            "dow": 1,
        },
    ]

    try:
        from niu_api.internal.scheduler import get_store

        ts = get_store()

        # Ensure each system task exists (by name, not cron_expr)
        for task_def in _SYSTEM_TASKS:
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
                )
                logger.info(f"Created system task '{task_def['name']}' (next run: {next_time})")
            elif existing.get("content") != task_def["content"]:
                # Update content only (keep user's cron_expr changes)
                ts.update_task(existing["id"], content=task_def["content"])
                logger.info(f"Updated system task '{task_def['name']}' content (id={existing['id']})")
            else:
                logger.debug(f"System task '{task_def['name']}' already exists and up-to-date")

    except Exception as e:
        logger.warning(f"Failed to ensure system tasks: {e}")

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

    # 停止 ChatQueue
    try:
        from niu_api.chat_queue import stop_chat_queue
        await asyncio.wait_for(stop_chat_queue(), timeout=10.0)
        logger.info("ChatQueue stopped")
    except asyncio.TimeoutError:
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to specific origins
    allow_credentials=True,
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
app.include_router(http_log_router)  # LLM Proxy API (/llm/v1/*)


# Mount scheduler router
from niu_api.internal.scheduler import scheduler_router
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
    except:
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


def main():
    """Main entry point - run with: python -m niu_api"""
    import uvicorn
    import atexit

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

    # Get port from environment or default
    port = int(os.environ.get("NIU_API_PORT", "9876"))

    logger.info(f"Starting Niu API Server on port {port}")

    uvicorn.run(
        "niu_api.__main__:app",
        host="127.0.0.1",
        port=port,
        reload=False,  # Disable reload for production
        log_level="warning",  # Reduce noise from pending-alerts polling
    )


if __name__ == "__main__":
    main()
