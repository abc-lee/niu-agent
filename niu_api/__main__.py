"""
Niu API Server - Main Entry Point

HTTP API server for Niu Agent using FastAPI + Uvicorn

单进程架构：Embedding 和 Scheduler 作为内部模块运行。
"""

import sys
import os
import asyncio
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from niu_api.config import get_config
from niu_api.session import router as session_router
from niu_api.chat import router as chat_router
from niu_api.compat import router as compat_router
from niu_api.injector import router as injector_router
from niu_api.alerts_api import router as alerts_router
from niu_api.kg_api import router as kg_router
from niu_api.brain_api import router as brain_router
from niu_api.brain_region_api import router as brain_region_router
from niu_api.notes_api import router as notes_router
from niu_api.llm_proxy import router as llm_proxy_router


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

    # 6.5. Save main event loop for SSE sync notifications
    from niu_api.chat import set_main_event_loop
    set_main_event_loop(asyncio.get_running_loop())
    logger.info("SSE event loop captured")

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

    # 8. Start LightRAG background sync (periodic photo/document backfill)
    try:
        from agent.injector.lightrag_sync import get_lightrag_sync
        lightrag_sync = get_lightrag_sync(auto_start=True)
        logger.info("LightRAG background sync started (interval: 6h)")
    except Exception as e:
        logger.warning(f"LightRAG background sync start failed: {e}")

    # 8.05. Start brain region periodic sync (community detection + region node refresh)
    try:
        from agent.injector.region_sync import get_region_sync
        region_sync = get_region_sync(auto_start=True)
        logger.info("Brain region sync started (interval: 24h)")
    except Exception as e:
        logger.warning(f"Brain region sync start failed: {e}")

    # 8.1. Initialize Niu self entity
    try:
        from niu_api.internal.brain_graph import get_brain_graph
        brain = get_brain_graph()
        brain.ensure_niu_entity()
        logger.info("Brain graph initialized (Niu entity ensured)")
    except Exception as e:
        logger.warning(f"Brain graph initialization failed: {e}")

    # 8.2. Create default brain regions (聊天历史, 文档库, 知识体系)
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

    # 8.6. Ensure entity-extractor daily task exists (replaces deleted kg-enricher)
    _ENTITY_EXTRACTOR_TASK_CONTENT = (
        "调用 chat-with-entity-extractor 子 Agent，task 参数为："
        "\"提炼有价值内容：扫描近期对话，筛选偏好/技能/经验，形成精炼文档通过 lightrag_insert 增量注入 LightRAG。\" "
        "不要从对话历史中提取内容，只执行此 task。"
    )
    try:
        from niu_api.internal.scheduler import get_store

        ts = get_store()
        existing_tasks = ts.list_tasks()

        # Cancel any stale kg-enricher tasks (cancel_task only transitions pending→cancelled)
        for task in existing_tasks:
            if (
                task.get("event_type") == "recurring"
                and "chat-with-kg-enricher" in task.get("content", "")
            ):
                try:
                    ts.cancel_task(task["id"])
                    logger.info(f"Cancelled stale kg-enricher task: {task['id']} (status={task.get('status')})")
                except Exception as cancel_err:
                    logger.warning(f"Could not cancel kg-enricher task {task['id']}: {cancel_err}")

        # Find existing entity-extractor task
        extractor_task = next(
            (
                task for task in existing_tasks
                if task.get("event_type") == "recurring"
                and task.get("cron_expr") == "0 8 * * *"
                and "chat-with-entity-extractor" in task.get("content", "")
                and task.get("status") != "cancelled"
            ),
            None,
        )

        if extractor_task is None:
            # Create new task
            now = datetime.now()
            next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if next_8am <= now:
                next_8am += timedelta(days=1)

            ts.create_task(
                content=_ENTITY_EXTRACTOR_TASK_CONTENT,
                scheduled_at=next_8am.isoformat(),
                is_recurring=True,
                cron_expr="0 8 * * *",
                event_type="recurring",
            )
            logger.info(f"Created entity-extractor daily task (next run: {next_8am})")
        elif extractor_task.get("content") != _ENTITY_EXTRACTOR_TASK_CONTENT:
            # Update existing task with new content
            ts.update_task(extractor_task["id"], content=_ENTITY_EXTRACTOR_TASK_CONTENT)
            logger.info(f"Updated entity-extractor task content (id={extractor_task['id']})")
    except Exception as e:
        logger.warning(f"Failed to ensure entity-extractor task: {e}")

    yield

    # Shutdown
    logger.info("Niu API Server shutting down...")

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

    from niu_api.internal.scheduler import stop_scheduler
    stop_scheduler()
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
app.include_router(brain_router)  # Brain Graph API
app.include_router(brain_region_router)  # Brain Region API
app.include_router(notes_router)  # Notes API
app.include_router(llm_proxy_router)  # LLM Proxy API (/llm/v1/*)


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
