"""
LightRAG Instance Manager

Manages the LightRAG instance lifecycle: initialization, configuration,
and access. LightRAG runs in-process, sharing the same Python runtime
as the ai-bot API server.

Architecture:
- LLM calls: routed through /llm/v1/ proxy (→ LiteLLM → user-config.json)
- Embedding calls: routed through /llm/v1/ proxy (→ niu_api.internal.embedding)
- Reranker: direct Python callable (→ niu_api.internal.reranker)
- Storage: NanoVectorDB (LightRAG default) in ~/.niu/lightrag_storage/

Usage:
    from niu_api.internal.lightrag_manager import get_lightrag, ensure_lightrag

    # Get instance (lazy-init on first call)
    rag = get_lightrag()

    # Or force initialization
    rag = await ensure_lightrag()
"""

import asyncio
import json
import os
import threading
from functools import partial
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# ============== Config ==============

PROXY_BASE_URL = "http://localhost:9876/llm/v1"
PROXY_API_KEY = "not-needed"  # Proxy reads from user-config.json
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"


def _get_lightrag_config() -> Dict[str, Any]:
    """Read LightRAG config from preferences.json."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            return prefs.get("lightrag", {})
    except Exception as e:
        logger.debug(f"Failed to read lightrag config: {e}")
    return {}


def _get_embedding_dim_for_lightrag() -> int:
    """Get embedding dimension for LightRAG from config."""
    from niu_api.internal.embedding import get_embedding_dim
    return get_embedding_dim()


# ============== Async/Sync Bridge ==============

# LightRAG is async. We run it in a dedicated daemon thread with its own
# event loop, bridging sync callers (handler) to async LightRAG.

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Ensure the daemon event loop is running (thread-safe)."""
    global _loop, _loop_thread

    # Fast path: already running
    if _loop is not None and _loop.is_running():
        return _loop

    with _loop_lock:
        # Double-check after acquiring lock
        if _loop is not None and _loop.is_running():
            return _loop

        _loop_ready.clear()

        def _run_loop():
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop_ready.set()  # Signal that _loop is assigned
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="lightrag-loop")
        _loop_thread.start()

        # Wait for loop to be ready (Event is set after _loop assignment)
        if not _loop_ready.wait(timeout=5.0):
            raise RuntimeError("LightRAG event loop failed to start")

        return _loop


def call_async(coro):
    """Run an async coroutine in the LightRAG event loop (blocking).

    Usage:
        result = call_async(rag.aquery("hello"))
    """
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=120)  # 2 minute timeout for LLM calls
    except Exception:
        future.cancel()
        raise


# ============== LightRAG Instance ==============

_rag_instance = None
_rag_lock = threading.Lock()


def _create_lightrag_instance():
    """Create a LightRAG instance with our proxy and local models.

    This is called lazily on first access. LightRAG must be installed
    (pip install lightrag-hku).
    """
    try:
        from lightrag import LightRAG
        from lightrag.llm import openai_complete_if_cache, openai_embed
    except ImportError:
        raise ImportError(
            "LightRAG is not installed. Run: pip install lightrag-hku"
        )

    config = _get_lightrag_config()
    embedding_dim = _get_embedding_dim_for_lightrag()

    # Ensure storage directory exists
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Build LLM function (routed through our proxy)
    llm_model_func = partial(
        openai_complete_if_cache,
        model="proxy-model",  # Proxy ignores this, uses user-config.json
        base_url=PROXY_BASE_URL,
        api_key=PROXY_API_KEY,
    )

    # Build embedding function (routed through our proxy)
    embedding_func_config = dict(
        embedding_dim=embedding_dim,
        max_token_size=8192,
        func=partial(
            openai_embed,
            model="bge-m3",  # Proxy ignores this, uses preferences.json
            base_url=PROXY_BASE_URL,
            api_key=PROXY_API_KEY,
        ),
    )

    # Build reranker callable (direct, no proxy)
    from niu_api.internal.reranker import make_lightrag_reranker_callable
    reranker_func = make_lightrag_reranker_callable()

    # Create LightRAG instance
    rag_params = dict(
        working_dir=str(STORAGE_DIR),
        llm_model_func=llm_model_func,
        llm_model_name="proxy-model",
        embedding_func=EmbeddingFunc(**embedding_func_config),
        chunk_overlap_token_size=50,
        chunk_token_size=1200,
    )

    # Add reranker if configured
    if reranker_func is not None:
        rag_params["enable_rerank"] = True
        rag_params["rerank_model_func"] = reranker_func
        logger.info("LightRAG reranker enabled")
    else:
        rag_params["enable_rerank"] = False
        logger.info("LightRAG reranker disabled")

    rag = LightRAG(**rag_params)
    return rag


# We need EmbeddingFunc from lightrag for type annotation
# Define a placeholder that gets replaced at runtime
try:
    from lightrag import EmbeddingFunc
except ImportError:
    # LightRAG not installed yet - create a placeholder
    class EmbeddingFunc:
        def __init__(self, **kwargs):
            self.kwargs = kwargs


def get_lightrag():
    """Get the LightRAG instance (lazy-init on first call).

    Returns None if LightRAG is not installed.
    """
    global _rag_instance

    if _rag_instance is not None:
        return _rag_instance

    with _rag_lock:
        if _rag_instance is not None:
            return _rag_instance

        try:
            logger.info("Initializing LightRAG instance...")
            _rag_instance = _create_lightrag_instance()
            logger.info("LightRAG instance ready")
        except ImportError as e:
            logger.warning(f"LightRAG not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            return None

    return _rag_instance


async def ensure_lightrag():
    """Async version of get_lightrag() for use in async contexts."""
    return get_lightrag()


def is_lightrag_available() -> bool:
    """Check if LightRAG is available (installed and initialized)."""
    try:
        import lightrag  # noqa: F401
        return True
    except ImportError:
        return False


def get_lightrag_status() -> Dict[str, Any]:
    """Get LightRAG status info for diagnostics."""
    from niu_api.internal.embedding import get_current_model_info
    from niu_api.internal.reranker import get_current_reranker_info

    return {
        "installed": is_lightrag_available(),
        "initialized": _rag_instance is not None,
        "storage_dir": str(STORAGE_DIR),
        "proxy_base_url": PROXY_BASE_URL,
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
        "loop_running": _loop is not None and _loop.is_running(),
    }
