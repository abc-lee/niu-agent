"""
Niu API Server - Main Entry Point

HTTP API server for Niu Agent using FastAPI + Uvicorn

单进程架构：Embedding 和 Scheduler 作为内部模块运行。
"""

import sys
import os
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

    # 7. Run weekly vector cleanup if needed
    cleanup_status_file = Path.home() / ".niu" / "last_cleanup.txt"
    should_cleanup = False

    if cleanup_status_file.exists():
        try:
            last_cleanup_str = cleanup_status_file.read_text().strip()
            last_cleanup = datetime.fromisoformat(last_cleanup_str)
            if datetime.now() - last_cleanup > timedelta(days=7):
                should_cleanup = True
        except Exception:
            should_cleanup = True
    else:
        should_cleanup = True

    if should_cleanup:
        logger.info("Scheduling weekly vector cleanup in 3 minutes...")

        def delayed_cleanup():
            time.sleep(180)  # 延时 3 分钟
            try:
                from agent.vector_cleanup import get_cleanup_service
                cleanup = get_cleanup_service()
                cleanup.run_full_cleanup()
                cleanup_status_file.write_text(datetime.now().isoformat())
                logger.info("Vector cleanup completed")
            except Exception as e:
                logger.warning(f"Vector cleanup failed: {e}")

        cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
        cleanup_thread.start()

    yield

    # Shutdown
    logger.info("Niu API Server shutting down...")

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
