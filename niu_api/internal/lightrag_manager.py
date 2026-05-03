"""
LightRAG Instance Manager

Manages the LightRAG instance lifecycle: initialization, configuration,
and access. LightRAG runs in-process, sharing the same Python runtime
as the ai-bot API server.

Architecture:
- LLM calls: routed through /llm/v1/ proxy (→ LiteLLM → user-config.json)
- Embedding calls: direct Python callable (→ niu_api.internal.embedding)
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
PROXY_API_KEY = "not-needed"  # Placeholder — proxy reads real key from user-config.json
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


def _make_local_embedding_func():
    """Create a direct local embedding callable for LightRAG.

    Bypasses the HTTP proxy entirely — calls model.encode() directly.
    Returns numpy ndarray (not list) because LightRAG's EmbeddingFunc
    validates result.size which requires a numpy array.
    Same pattern as the reranker (direct Python callable, zero overhead).
    """
    from niu_api.internal.embedding import get_model

    async def _embed(texts: list[str]):
        model = get_model()
        return model.encode(texts, convert_to_numpy=True)

    return _embed


def _create_lightrag_instance():
    """Create a LightRAG instance with our proxy and local models.

    This is called lazily on first access. LightRAG must be installed
    (pip install lightrag-hku).
    """
    try:
        from lightrag.lightrag import LightRAG
        from lightrag.llm.openai import openai_complete_if_cache
    except ImportError:
        raise ImportError(
            "LightRAG is not installed. Run: pip install lightrag-hku"
        )

    config = _get_lightrag_config()
    embedding_dim = _get_embedding_dim_for_lightrag()

    # Ensure storage directory exists
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Build LLM function (routed through our proxy)
    # LightRAG calls llm_model_func(prompt, system_prompt=..., **kwargs).
    # openai_complete_if_cache(model, prompt, ...) expects model as the first
    # positional arg. Using partial(model=...) binds model as a keyword arg,
    # which conflicts when LightRAG passes prompt as a positional arg (Python
    # maps it to the first unbound param = model, then finds model= already
    # set by partial → "got multiple values for argument 'model'").
    # Fix: wrapper function whose first param is prompt (matching LightRAG's
    # convention), passing model as a positional arg to openai_complete_if_cache.
    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> str:
        return await openai_complete_if_cache(
            "proxy-model",  # model as positional arg (proxy ignores, uses user-config.json)
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            base_url=PROXY_BASE_URL,
            api_key=PROXY_API_KEY,
            keyword_extraction=keyword_extraction,
            **kwargs,
        )

    llm_model_func = _llm_model_func

    # Build embedding function (direct local call, no proxy)
    from niu_api.internal.embedding import get_embedding_max_seq_length
    max_seq_len = get_embedding_max_seq_length()

    embedding_func_config = dict(
        embedding_dim=embedding_dim,
        max_token_size=max_seq_len,
        func=_make_local_embedding_func(),
    )

    # Build reranker callable (direct, no proxy)
    from niu_api.internal.reranker import make_lightrag_reranker_callable
    reranker_func = make_lightrag_reranker_callable()

    # Create LightRAG instance
    # Custom entity_types: constrain LLM extraction to these categories.
    # If none match, LLM classifies as "Other" (LightRAG prompt convention).
    # This ensures frontend category buttons match actual graph data.
    CUSTOM_ENTITY_TYPES = [
        "Person", "Organization", "Technology", "Concept",
        "Location", "Event", "Document", "Photo", "Video",
        "Note", "Chat", "Skill", "Tool", "Knowledge",
        "InteractionHabit", "EpisodicEvent", "BrainRegion", "Other",
    ]

    rag_params = dict(
        working_dir=str(STORAGE_DIR),
        llm_model_func=llm_model_func,
        llm_model_name="proxy-model",
        embedding_func=EmbeddingFunc(**embedding_func_config),
        chunk_overlap_token_size=50,
        chunk_token_size=1200,
        addon_params={
            "entity_types": CUSTOM_ENTITY_TYPES,
            "language": "Chinese",
        },
    )

    # Add reranker if configured (lightrag-hku 1.4.15 uses rerank_model_func,
    # not enable_rerank — reranking is implicitly enabled when func is provided)
    if reranker_func is not None:
        rag_params["rerank_model_func"] = reranker_func
        logger.info("LightRAG reranker enabled")
    else:
        logger.info("LightRAG reranker disabled")

    rag = LightRAG(**rag_params)
    # lightrag-hku 1.4.15 requires explicit storage initialization
    call_async(rag.initialize_storages())
    return rag


# We need EmbeddingFunc from lightrag for type annotation
# Define a placeholder that gets replaced at runtime
try:
    from lightrag.lightrag import EmbeddingFunc
except ImportError:
    # LightRAG not installed yet - create a placeholder
    class EmbeddingFunc:
        def __init__(self, **kwargs):
            self.kwargs = kwargs


_INIT_FAILED = object()  # Sentinel: init failed, don't retry


def get_lightrag():
    """Get the LightRAG instance (lazy-init on first call).

    Returns None if LightRAG is not installed.
    Caches init failures to avoid retry storms.
    """
    global _rag_instance

    if _rag_instance is not None and _rag_instance is not _INIT_FAILED:
        return _rag_instance

    if _rag_instance is _INIT_FAILED:
        return None

    with _rag_lock:
        if _rag_instance is not None and _rag_instance is not _INIT_FAILED:
            return _rag_instance

        if _rag_instance is _INIT_FAILED:
            return None

        try:
            logger.info("Initializing LightRAG instance...")
            _rag_instance = _create_lightrag_instance()
            logger.info("LightRAG instance ready")
        except ImportError as e:
            logger.warning(f"LightRAG not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            _rag_instance = _INIT_FAILED
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

    with _rag_lock:
        initialized = _rag_instance is not None and _rag_instance is not _INIT_FAILED
    with _loop_lock:
        loop_running = _loop is not None and _loop.is_running()

    return {
        "installed": is_lightrag_available(),
        "initialized": initialized,
        "storage_dir": str(STORAGE_DIR),
        "proxy_base_url": PROXY_BASE_URL,
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
        "loop_running": loop_running,
    }
