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
import time
from collections import deque
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# ============== Config ==============

PROXY_BASE_URL = "http://localhost:9876/llm/v1"
PROXY_API_KEY = "not-needed"  # Placeholder — proxy reads real key from user-config.json
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# ============== Shared OpenAI Client ==============
# Reuse a single AsyncOpenAI client across all LightRAG LLM calls.
# This avoids the overhead of creating a new client for each call,
# which can add 10-15 seconds of latency due to connection pool initialization.
_shared_openai_client: Optional[Any] = None
_client_lock = threading.Lock()


def _get_shared_openai_client():
    """Get or create a shared AsyncOpenAI client for LightRAG LLM calls."""
    global _shared_openai_client

    if _shared_openai_client is not None:
        return _shared_openai_client

    with _client_lock:
        if _shared_openai_client is not None:
            return _shared_openai_client

        from openai import AsyncOpenAI

        _shared_openai_client = AsyncOpenAI(
            base_url=PROXY_BASE_URL,
            api_key=PROXY_API_KEY,
            timeout=180.0,  # 3 minutes timeout
        )
        logger.info("Created shared AsyncOpenAI client for LightRAG")
        return _shared_openai_client


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

# Read-write lock for the NetworkX knowledge graph.
#
# IMPORTANT: call_async operations (ainsert_custom_kg, adelete_by_entity,
# amerge_entities, ainsert) must NOT be called inside this lock.
# call_async submits to LightRAG's asyncio loop and blocks for up to 600s;
# holding the lock during that time would freeze all reads.
#
# Lock usage:
# - Write lock: only for direct NetworkX mutations (e.g. _decay_structural_edges
#   which calls kg.remove_edge / edge_data["weight"] directly).
#   call_async-based writes do NOT need this lock — they run serialized in the
#   asyncio loop and update the NetworkX graph internally.
# - Read lock: acquired by get_graph_snapshot, list_entities (entity_type path),
#   and any other direct NetworkX graph traversal. Readers should copy() the
#   graph under the lock, then iterate the snapshot lock-free.
#
# CAVEAT: graph_read_lock only synchronizes with direct NetworkX mutations
# (graph_write_lock holders like _decay_structural_edges). It does NOT
# synchronize with call_async-based writes, which run in the asyncio loop
# without acquiring this lock. This means snapshot = g.copy() under
# graph_read_lock may still encounter concurrent modification from call_async.
# This is a deliberate trade-off: holding the lock during call_async would
# freeze reads for up to 600s. In practice, call_async writes are serialized
# in the asyncio loop and brief; the risk of partial snapshot is low but not
# zero. If a RuntimeError occurs, the endpoint returns an empty result and
# the frontend retries on the next poll cycle.
#
_graph_rwlock = threading.RLock()


def graph_read_lock():
    """Context manager for read access to the NetworkX graph.

    Usage:
        with graph_read_lock():
            snapshot = nx_graph.copy()
    """
    return _graph_rwlock


def graph_write_lock():
    """Context manager for direct NetworkX graph mutations (NOT call_async).

    Only use for operations that directly modify the NetworkX graph object
    (e.g. kg.remove_edge, edge_data["weight"] = ...).
    Do NOT wrap call_async() calls — they block too long and freeze reads.

    Usage:
        with graph_write_lock():
            kg.remove_edge(src, tgt)
    """
    return _graph_rwlock


def get_brain_regions() -> list[str]:
    """Get list of brain region names from the knowledge graph.

    Directly reads from the NetworkX in-memory graph without calling
    LightRAG API, avoiding potential event loop deadlocks.

    This is a pure synchronous read — safe to call from anywhere,
    including LLM proxy callbacks.

    Returns:
        List of brain region names (e.g., ["聊天历史脑区", "文档库脑区"]),
        or empty list if LightRAG is unavailable or graph is empty.
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return []

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return []

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return []

        # Take a snapshot under read lock to prevent RuntimeError from
        # concurrent graph modification by background sync threads.
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Filter nodes whose entity_type is BrainRegion
        brain_regions = [
            name for name, data in snapshot.nodes(data=True)
            if data.get("entity_type", "").lower() == "brainregion"
        ]

        return brain_regions

    except Exception as e:
        logger.debug("get_brain_regions failed: %s", e)
        return []


def get_region_members(region_name: str) -> list[str]:
    """Get member entity names for a specific brain region.

    Directly reads from the NetworkX in-memory graph, finding entities
    connected to the region via "_region:contains" edges.

    This is a pure synchronous read — safe to call from anywhere,
    including LLM proxy callbacks.

    Args:
        region_name: Brain region entity name (e.g., "文档库脑区")

    Returns:
        List of member entity names, or empty list if region not found.
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return []

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return []

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return []

        # Take a snapshot under read lock
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Find members via "_region:contains" edges (region -> member)
        # Note: LightRAG stores edge type in 'keywords' field, not 'type'
        members = []
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "_region:contains":
                if src == region_name:
                    members.append(tgt)
                elif tgt == region_name:
                    members.append(src)

        return members

    except Exception as e:
        logger.debug("get_region_members failed: %s", e)
        return []


def get_all_region_members() -> dict[str, list[str]]:
    """Get all brain regions and their member entity names.

    Directly reads from the NetworkX in-memory graph without calling
    LightRAG API, avoiding potential event loop deadlocks.

    Returns:
        Dict mapping region name to list of member entity names,
        e.g., {"文档库脑区": ["Python", "NumPy"], "聊天历史脑区": ["用户"]}
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return {}

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return {}

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return {}

        # Take a snapshot under read lock
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Build mapping: region -> members
        # Note: LightRAG stores edge type in 'keywords' field, not 'type'
        region_members: dict[str, list[str]] = {}
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "_region:contains":
                # 无向图中 src/tgt 顺序不确定，需判断哪端是脑区
                if src.endswith("脑区"):
                    region, member = src, tgt
                elif tgt.endswith("脑区"):
                    region, member = tgt, src
                else:
                    continue
                if region not in region_members:
                    region_members[region] = []
                region_members[region].append(member)

        return region_members

    except Exception as e:
        logger.debug("get_all_region_members failed: %s", e)
        return {}


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


def call_async(coro, timeout: int = 120):
    """Run an async coroutine in the LightRAG event loop (blocking).

    Usage:
        result = call_async(rag.aquery("hello"))
        result = call_async(rag.ainsert(content), timeout=600)  # 10 min for large docs
    """
    import concurrent.futures as _cf

    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except _cf.TimeoutError:
        future.cancel()
        raise
    except asyncio.CancelledError:
        future.cancel()
        raise
    except Exception:
        future.cancel()
        raise


# Track pending fire-and-forget futures for graceful shutdown.
_pending_futures: list = []
_pending_lock = threading.Lock()


def fire_and_forget(coro, context: str = ""):
    """Submit an async coroutine to the LightRAG event loop without waiting.

    The coroutine runs in the background and any exception is logged.
    Use for long-running operations (e.g. entity extraction pipeline)
    where the caller should not block.

    Args:
        coro: The async coroutine to submit.
        context: Optional context string for error logging (e.g. track_id, file name).

    Usage:
        fire_and_forget(rag.apipeline_process_enqueue_documents(), context="track-123")
    """
    loop = _ensure_loop()

    # Capture future ref so _wrapped can remove only its own entry.
    future_ref: list = [None]

    async def _wrapped():
        try:
            await coro
        except asyncio.CancelledError:
            ctx = f" context={context}" if context else ""
            logger.debug(f"[fire_and_forget] coroutine cancelled:{ctx}")
        except Exception as e:
            ctx = f" context={context}" if context else ""
            logger.error(f"[fire_and_forget] coroutine failed:{ctx} error={e}")
        finally:
            with _pending_lock:
                f = future_ref[0]
                if f is not None and f in _pending_futures:
                    _pending_futures.remove(f)

    future = asyncio.run_coroutine_threadsafe(_wrapped(), loop)
    future_ref[0] = future
    with _pending_lock:
        _pending_futures.append(future)


def shutdown_pending_futures(timeout: float = 10.0):
    """Wait for pending fire-and-forget futures to complete, then cancel remaining.

    Called during application shutdown to prevent documents stuck in PENDING state.
    Uses a total deadline across all futures, not per-future timeout.
    """
    import concurrent.futures
    import time

    with _pending_lock:
        futures = list(_pending_futures)

    if not futures:
        return

    logger.info(f"[fire_and_forget] shutdown: waiting for {len(futures)} pending futures")

    deadline = time.monotonic() + timeout
    for future in futures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            future.cancel()
            continue
        try:
            future.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            logger.info("[fire_and_forget] shutdown: future timed out, cancelling")
            future.cancel()
        except Exception:
            future.cancel()
        except BaseException:  # KeyboardInterrupt, SystemExit — re-raise
            future.cancel()
            raise

    # Remove only the futures we managed (waited/cancelled), not any
    # that were added to _pending_futures after our snapshot was taken.
    with _pending_lock:
        for f in futures:
            if f in _pending_futures:
                _pending_futures.remove(f)

    logger.info("[fire_and_forget] shutdown: all futures resolved")


def shutdown_lightrag_loop(timeout: float = 10.0):
    """Stop the LightRAG event loop gracefully.

    First cancels all pending fire-and-forget futures, then stops the loop.
    Called during application shutdown.
    """
    global _loop, _loop_thread

    # Step 1: Cancel pending fire-and-forget futures
    shutdown_pending_futures(timeout=timeout)

    # Step 2: Stop the event loop
    with _loop_lock:
        loop = _loop
        thread = _loop_thread

    if loop is None or not loop.is_running():
        return

    logger.info("[lightrag-loop] Stopping event loop...")

    # Submit loop.stop() from a different thread
    loop.call_soon_threadsafe(loop.stop)

    # Wait for the daemon thread to finish
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
        if thread.is_alive():
            logger.warning("[lightrag-loop] Loop thread did not stop within timeout")
        else:
            logger.info("[lightrag-loop] Loop thread stopped")

    with _loop_lock:
        _loop = None
        _loop_thread = None


# ============== LightRAG Instance ==============

_rag_instance = None
_rag_lock = threading.Lock()

# Init failure tracking: timestamp-based retry gate instead of permanent sentinel.
# After init fails, _init_failed_at records the time. get_lightrag() will
# return None until _INIT_RETRY_SECONDS have elapsed, then retry.
_init_failed_at: Optional[float] = None
_INIT_RETRY_SECONDS: float = 60.0

# Signaling event: set when LightRAG initializes successfully.
# Other threads call wait_lightrag_ready() instead of polling get_lightrag().
_lightrag_ready = threading.Event()


def _clear_sync_state_if_storage_empty(storage_dir: Path) -> None:
    """Clear sync state caches when lightrag_storage is freshly created/empty.

    When users delete lightrag_storage and restart, the graph starts empty.
    But skill_sync_state.json and last_region_sync.json may still exist,
    causing SkillSync/RegionSync to think everything is already synced
    and skip re-injection. This function detects the empty-graph condition
    and deletes those stale cache files, then notifies the sync services
    to reload their in-memory state.
    """
    entities_file = storage_dir / "kv_store_full_entities.json"
    if not entities_file.exists():
        # Fresh storage — no entities yet, clear all sync state caches
        state_files = [
            Path.home() / ".niu" / "skill_sync_state.json",
            Path.home() / ".niu" / "last_region_sync.json",
        ]
        cleared = False
        for state_file in state_files:
            if state_file.exists():
                try:
                    state_file.unlink()
                    logger.info(f"Cleared stale sync state: {state_file}")
                    cleared = True
                except OSError as e:
                    logger.warning(f"Failed to clear sync state {state_file}: {e}")

        # Notify SkillSync to reload state from disk (now empty)
        if cleared:
            try:
                from agent.injector.sync import get_skill_sync
                skill_sync = get_skill_sync(auto_start=False)
                skill_sync._last_scan = skill_sync._load_state()
                skill_sync._last_notes_scan = skill_sync._load_notes_state()
                logger.info("[LightRAG] SkillSync state reloaded after clearing stale cache")
            except Exception as e:
                logger.warning(f"[LightRAG] Failed to notify SkillSync: {e}")


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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: model.encode(texts, convert_to_numpy=True, show_progress_bar=False))

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

    # Build LLM function with shared OpenAI client.
    # LightRAG calls llm_model_func(prompt, system_prompt=..., **kwargs).
    # Using a shared client avoids the 10-15s overhead of creating a new
    # AsyncOpenAI client for each call (connection pool initialization).
    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> str:
        return await openai_complete_if_cache(
            "proxy-model", prompt,
            system_prompt=system_prompt, history_messages=history_messages,
            base_url=PROXY_BASE_URL, api_key=PROXY_API_KEY,
            keyword_extraction=keyword_extraction, **kwargs,
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
        "person", "organization", "technology", "concept",
        "location", "event", "document", "photo", "video",
        "note", "chat", "skill", "tool", "knowledge",
        "interactionhabit", "episodicevent", "brainregion", "other",
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
    call_async(rag.initialize_storages(), timeout=300)
    # If lightrag_storage is freshly created (empty graph), clear sync state caches
    # so that SkillSync/LightRAGSync/RegionSync will re-inject everything
    _clear_sync_state_if_storage_empty(STORAGE_DIR)
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


def get_lightrag():
    """Get the LightRAG instance (lazy-init on first call).

    Returns None if LightRAG is not installed or init failed recently.
    After init failure, waits _INIT_RETRY_SECONDS before retrying so
    the system does not permanently lock up.
    """
    global _rag_instance, _init_failed_at

    # Fast path: already initialized
    if _rag_instance is not None:
        return _rag_instance

    # Retry gate: if init failed recently, return None until cooldown expires
    if _init_failed_at is not None:
        elapsed = time.monotonic() - _init_failed_at
        if elapsed < _INIT_RETRY_SECONDS:
            return None
        # Cooldown expired — clear the flag and retry below
        logger.info(
            f"LightRAG init retry cooldown expired ({elapsed:.0f}s), retrying..."
        )
        _init_failed_at = None

    with _rag_lock:
        # Double-check after acquiring lock
        if _rag_instance is not None:
            return _rag_instance

        if _init_failed_at is not None:
            elapsed = time.monotonic() - _init_failed_at
            if elapsed < _INIT_RETRY_SECONDS:
                return None
            _init_failed_at = None

        try:
            logger.info("Initializing LightRAG instance...")
            _rag_instance = _create_lightrag_instance()
            logger.info("LightRAG instance ready")
            # Signal readiness to other threads
            _lightrag_ready.set()
        except ImportError as e:
            logger.warning(f"LightRAG not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            _init_failed_at = time.monotonic()
            return None

    return _rag_instance


def wait_lightrag_ready(timeout: float) -> bool:
    """Block until LightRAG is initialized, or until timeout expires.

    Uses threading.Event.wait() internally — no polling, no deadlock.
    Other threads (SkillSync, LightRAGSync, RegionSync) should call
    this instead of polling get_lightrag() in a loop.

    Args:
        timeout: Max seconds to wait. If 0, returns immediately.

    Returns:
        True if LightRAG is ready, False if timeout expired.
    """
    return _lightrag_ready.wait(timeout=timeout)


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
        initialized = _rag_instance is not None
        init_failed = _init_failed_at is not None
        if init_failed:
            retry_in = max(0, round(_INIT_RETRY_SECONDS - (time.monotonic() - _init_failed_at), 1))
        else:
            retry_in = None
    with _loop_lock:
        loop_running = _loop is not None and _loop.is_running()

    return {
        "installed": is_lightrag_available(),
        "initialized": initialized,
        "init_failed": init_failed,
        "init_retry_in_seconds": retry_in,
        "storage_dir": str(STORAGE_DIR),
        "proxy_base_url": PROXY_BASE_URL,
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
        "loop_running": loop_running,
    }


# ============== Graph Change Log ==============

class GraphChangeLog:
    """In-memory change buffer for graph write operations.

    Records entity_created, edge_created, entity_deleted, entity_merged
    events so the frontend can poll /api/kg/changelog for incremental
    updates instead of re-fetching the full snapshot.

    Uses deque with maxlen to bound memory; old entries auto-evict.
    Thread-safe via internal lock.
    """

    def __init__(self, max_size: int = 2000) -> None:
        self._changes: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def record_change(self, change_type: str, data: dict) -> None:
        with self._lock:
            self._changes.append({
                "type": change_type,
                "timestamp": datetime.now().isoformat(),
                "data": data,
            })

    def get_changes(self, since: str = "", limit: int = 200) -> list[dict]:
        """Return changes after *since* timestamp (ISO 8601).

        Does NOT drain — the buffer is preserved so late-arriving polls
        can still catch up.  Old entries are auto-evicted by deque maxlen.

        If *since* is older than the earliest entry in the deque, some
        changes have been evicted and the incremental result is incomplete.
        In that case, a snapshot_refresh event is appended so the frontend
        re-fetches the full snapshot instead of relying on partial data.
        """
        with self._lock:
            if not since:
                return list(self._changes)[-limit:]
            result = [c for c in self._changes if c["timestamp"] > since]
            # Detect overflow: if since is strictly older than the earliest
            # deque entry, some changes were evicted between the frontend's
            # last poll and now, and the incremental result is incomplete.
            # Use strict < (not <=): when since equals the earliest entry's
            # timestamp, the frontend has already processed that entry
            # (syncSince was set to that timestamp), so > since correctly
            # excludes it. Only < means entries were lost before since.
            if self._changes and since < self._changes[0]["timestamp"]:
                result.append({
                    "type": "snapshot_refresh",
                    "timestamp": datetime.now().isoformat(),
                    "data": {"reason": "changelog_overflow"},
                })
            return result[-limit:]


_change_log = GraphChangeLog()


def get_change_log() -> GraphChangeLog:
    return _change_log
