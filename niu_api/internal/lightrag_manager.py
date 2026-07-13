"""
LightRAG Instance Manager

Manages the LightRAG instance lifecycle: initialization, configuration,
and access. LightRAG runs in-process, sharing the same Python runtime
as the ai-bot API server.

Architecture:
- LLM calls: direct LiteLLMSession.chat() (→ LiteLLM → user-config.json)
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

STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# ============== LightRAG LLM Function Builder ==============

# Cache a shared LiteLLMSession instance keyed by config tuple.
# Avoids connection init overhead for high-frequency entity extraction calls.
_cached_session: Optional[Any] = None
_cached_config_key: Optional[tuple] = None
_session_lock = threading.Lock()


def _get_litellm_session(config: dict) -> Any:
    """Get or create a cached LiteLLMSession for LightRAG LLM calls.

    Config changes (model/api_base/api_key/api_type/reasoning_effort) trigger session rebuild.
    Thread-safe via double-check locking.
    """
    global _cached_session, _cached_config_key
    from agent.generic.litellm_adapter import LiteLLMSession

    config_key = (config.get("model"), config.get("apibase"), config.get("apikey"), config.get("type"), config.get("reasoning_effort"), config.get("provider"), tuple(sorted(config.get("litellm_kwargs", {}).items())))

    if _cached_session is not None and _cached_config_key == config_key:
        return _cached_session

    with _session_lock:
        if _cached_session is not None and _cached_config_key == config_key:
            return _cached_session

        llm_config = {
            "api_type": config.get("type", "openai"),  # type -> api_type mapping
            "apikey": config["apikey"],
            "apibase": config["apibase"],
            "model": config["model"],
            "reasoning_effort": config.get("reasoning_effort"),
            "provider": config.get("provider", ""),
            "litellm_kwargs": config.get("litellm_kwargs", {}),
        }

        _cached_session = LiteLLMSession(cfg=llm_config)
        _cached_config_key = config_key
        logger.info("Created LiteLLMSession for LightRAG: model=%s, api_type=%s, provider=%s", config.get("model"), config.get("type"), config.get("provider"))
        return _cached_session


def _build_llm_model_func():
    """Build the async LLM function for LightRAG.

    Returns an async function that LightRAG calls for all LLM operations.
    Calls LiteLLMSession.chat() directly via asyncio.to_thread, avoiding
    OpenAI SDK compatibility issues and HTTP proxy overhead.

    Brain region injection is done here (not in proxy layer) for entity
    extraction requests.
    """
    from niu_api.llm_proxy import get_llm_config
    from agent.generic.litellm_adapter import MockResponse
    from niu_api.internal.brain_region_prompt import (
        build_static_brain_region_prompt,
        build_dynamic_brain_region_prompt,
        BRAIN_REGION_MARKER,
    )

    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> str:
        # 1. Pop LightRAG internal params (concurrency control, not for LLM)
        kwargs.pop("hashing_kv", None)
        kwargs.pop("_priority", None)
        kwargs.pop("_timeout", None)
        kwargs.pop("_queue_timeout", None)

        # 2. Brain region injection for entity extraction requests
        if system_prompt and BRAIN_REGION_MARKER in system_prompt:
            if "大脑区域架构" not in system_prompt:  # idempotent guard
                static_part = build_static_brain_region_prompt()
                dynamic_part = build_dynamic_brain_region_prompt()
                system_prompt = system_prompt + f"\n\n{static_part}\n\n{dynamic_part}"

        # 3. Handle keyword_extraction: try response_format, fallback to prompt
        # Models that support json_schema Structured Outputs (e.g. OpenAI) get the
        # reliable response_format path. Models that don't (e.g. ark-code-latest)
        # raise BadRequestError — we catch that, append JSON instructions to prompt,
        # and retry without response_format. LightRAG's json_repair.loads() handles
        # parsing the text-only output.
        response_format = None
        kw_prompt_suffix = ""
        if keyword_extraction:
            from lightrag.types import GPTKeywordExtractionFormat
            schema = GPTKeywordExtractionFormat.model_json_schema()
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "keyword_extraction",
                    "strict": True,
                    "schema": schema,
                },
            }
            kw_prompt_suffix = '\n\nReturn your response as a JSON object with "high_level_keywords" and "low_level_keywords" arrays.'

        # 4. Build messages list
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            for msg in history_messages:
                content = msg.get("content") or ""  # litellm safety: None -> ""
                messages.append({"role": msg.get("role", "user"), "content": content})
        messages.append({"role": "user", "content": prompt})

        # 5. Get LLM config
        config = get_llm_config(use_lightrag_config=True)

        # 6. Handle enable_cot and stream from kwargs
        enable_cot = kwargs.pop("enable_cot", False)
        stream = kwargs.pop("stream", False)

        # 7. Call LiteLLMSession via asyncio.to_thread
        def _consume_generator(gen):
            """Consume a LiteLLMSession.chat() generator, return (chunks, mock_response)."""
            chunks = []
            mock_response = None
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_response = e.value
            return chunks, mock_response

        def sync_call():
            from litellm import BadRequestError
            session = _get_litellm_session(config)

            # Try with response_format first (works for models like OpenAI that support it)
            gen = session.chat(messages=messages, response_format=response_format)
            try:
                chunks, mock_response = _consume_generator(gen)
            except BadRequestError:
                if not keyword_extraction:
                    raise
                # Model doesn't support response_format — retry with prompt-only approach
                logger.info("response_format not supported by model, retrying with prompt-only JSON instruction")
                fallback_messages = list(messages)
                fallback_messages[-1]["content"] = prompt + kw_prompt_suffix
                gen = session.chat(messages=fallback_messages, response_format=None)
                chunks, mock_response = _consume_generator(gen)

            full_content = "".join(chunks)

            # Handle enable_cot (thinking chain)
            if enable_cot and mock_response and mock_response.thinking:
                if full_content:
                    # Content exists — ignore thinking, just return content
                    pass
                else:
                    # No content but thinking exists — wrap in think tags
                    full_content = f"<think>{mock_response.thinking}</think>\n"

            return full_content

        result = await asyncio.to_thread(sync_call)

        # 8. Stream handling
        if stream:
            # Pseudo-streaming: split complete result into chunks as AsyncIterator
            chunk_size = 20
            async def _async_gen():
                for i in range(0, max(len(result), 1), chunk_size):
                    yield result[i:i + chunk_size]
            return _async_gen()

        return result

    return _llm_model_func


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
    connected to the region via "包含" edges.

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

        # Find members via "包含" edges (region -> member)
        # Note: LightRAG stores edge type in 'keywords' field, not 'type'
        # Note: LightRAG graph keys are all lowercase, so compare in lowercase
        region_name_lower = region_name.lower() if isinstance(region_name, str) else region_name
        members = []
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "包含":
                if src == region_name_lower:
                    members.append(tgt)
                elif tgt == region_name_lower:
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
            if edge_type.lower() == "包含":
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


def remove_region_edges(region_name: str, edge_type: str) -> int:
    """Remove edges of a specific type from a brain region node.

    Directly operates on the internal NetworkX graph under write lock.

    Args:
        region_name: Brain region entity name
        edge_type: Edge keywords to match (case-insensitive)

    Returns:
        Number of edges removed.

    Note: Assumes nx.Graph (not MultiGraph). LightRAG uses nx.Graph,
    add_edge is upsert semantics — no parallel edges.
    """
    removed = 0
    try:
        rag = get_lightrag()
        if rag is None:
            return 0
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return 0
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None:
            return 0
        region_key = region_name.lower() if isinstance(region_name, str) else region_name
        with graph_write_lock():
            if region_key not in nx_graph:
                return 0
            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                if kw.lower() == edge_type.lower():
                    nx_graph.remove_edge(region_key, neighbor_id)
                    removed += 1
    except Exception as e:
        logger.debug("remove_region_edges failed for %s: %s", region_name, e)
    return removed


def remove_region_stale_edges(
    region_name: str, edge_type: str, keep_members: set[str]
) -> int:
    """Remove edges of a specific type from a brain region, except those
    connecting to members in keep_members.

    Directly operates on the internal NetworkX graph under write lock.
    Used for atomic drift updates: inject new edges first, then remove
    stale edges — avoiding the zero-member window.

    Args:
        region_name: Brain region entity name
        edge_type: Edge keywords to match (case-insensitive)
        keep_members: Set of member entity names whose edges to preserve.
                      Names are compared case-insensitively.

    Returns:
        Number of edges removed.
    """
    removed = 0
    try:
        rag = get_lightrag()
        if rag is None:
            return 0
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return 0
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None:
            return 0
        region_key = region_name.lower() if isinstance(region_name, str) else region_name
        keep_lower = {m.lower() for m in keep_members}
        with graph_write_lock():
            if region_key not in nx_graph:
                return 0
            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                if kw.lower() == edge_type.lower():
                    if neighbor_id not in keep_lower:
                        nx_graph.remove_edge(region_key, neighbor_id)
                        removed += 1
    except Exception as e:
        logger.debug("remove_region_stale_edges failed for %s: %s", region_name, e)
    return removed


def _ensure_loop() -> asyncio.AbstractEventLoop():
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
_init_error: dict | None = None
_integrity_result: dict | None = None  # Phase 1 一致性检测结果，供 get_lightrag_status 暴露
_INIT_RETRY_SECONDS: float = 60.0

# repair 期间标志：避免 get_lightrag 报 critical 日志，避免 SkillSync 后台轮询误报。
# run_repair_on_user_request 设/清（try/finally 保证异常路径清除）。
# 期间 get_lightrag 静默返回 None，不报 critical 日志。
_repairing: bool = False

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
    except ImportError:
        raise ImportError(
            "LightRAG is not installed. Run: pip install lightrag-hku"
        )

    config = _get_lightrag_config()
    embedding_dim = _get_embedding_dim_for_lightrag()

    # Ensure storage directory exists
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Build LLM function using LiteLLMSession (direct call, no proxy).
    # LightRAG calls llm_model_func(prompt, system_prompt=..., **kwargs).
    # LiteLLMSession is cached and reused across calls.
    llm_model_func = _build_llm_model_func()

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

    # Read tunable params from preferences.json (lightrag section)
    config = _get_lightrag_config()
    chunk_token_size = config.get("chunk_token_size", 1200)
    chunk_overlap_token_size = config.get("chunk_overlap_token_size", 50)
    llm_model_max_async = config.get("llm_model_max_async", 4)
    entity_extract_max_gleaning = config.get("max_gleaning", 1)

    rag_params = dict(
        working_dir=str(STORAGE_DIR),
        llm_model_func=llm_model_func,
        llm_model_name="proxy-model",
        embedding_func=EmbeddingFunc(**embedding_func_config),
        chunk_overlap_token_size=chunk_overlap_token_size,
        chunk_token_size=chunk_token_size,
        llm_model_max_async=llm_model_max_async,
        addon_params={
            "entity_types": CUSTOM_ENTITY_TYPES,
            "language": "Chinese",
            "entity_extract_max_gleaning": entity_extract_max_gleaning,
        },
    )

    logger.info(
        "LightRAG params: chunk_size=%d, chunk_overlap=%d, max_async=%d, max_gleaning=%d",
        chunk_token_size, chunk_overlap_token_size, llm_model_max_async, entity_extract_max_gleaning,
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

    三级启动门控（基于 _integrity_result 的 critical/major/minor 计数）：
    - A 级（critical > 0 或 unrecoverable）：拒绝初始化，返回 None
    - B 级（major > 0）：拒绝初始化，返回 None（需用户修复）
    - C 级（仅 minor > 0）：允许初始化，日志警告降级
    - 无 error：正常初始化

    repair 期间（_repairing=True）静默返回 None，不报 critical 日志，
    避免 SkillSync 后台轮询误报。

    Returns None if LightRAG is not installed, init failed recently
    (_INIT_RETRY_SECONDS cooldown), or repair in progress.
    After init failure, waits _INIT_RETRY_SECONDS before retrying so
    the system does not permanently lock up.
    """
    global _rag_instance, _init_failed_at

    # repair 期间静默返回 None（不报 critical 日志，避免 SkillSync 误报）
    if _repairing:
        return None

    # Fast path: already initialized
    if _rag_instance is not None:
        return _rag_instance

    # 三级门控：基于 _integrity_result 的 severity 计数判定
    if _integrity_result is not None:
        critical = _integrity_result.get("critical_errors", 0)
        major = _integrity_result.get("major_errors", 0)
        minor = _integrity_result.get("minor_errors", 0)

        if critical > 0:
            logger.warning(
                f"[LightRAG] 核心数据损坏（{critical} critical errors），拒绝初始化"
            )
            _init_failed_at = time.monotonic()
            return None
        if major > 0:
            logger.warning(
                f"[LightRAG] 数据不一致（{major} major errors），拒绝初始化。请通过修复功能恢复数据。"
            )
            _init_failed_at = time.monotonic()
            return None
        if minor > 0:
            logger.warning(
                f"[LightRAG] 数据有轻微问题（{minor} minor errors），降级启动"
            )
            # 不返回 None，继续初始化（C 级降级）

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

        # repair 期间再次检查（lock 内 double-check）
        if _repairing:
            return None

        # 三级门控 lock 内再次检查（防并发窗口期）
        if _integrity_result is not None:
            critical = _integrity_result.get("critical_errors", 0)
            major = _integrity_result.get("major_errors", 0)
            if critical > 0 or major > 0:
                if _init_failed_at is None:
                    _init_failed_at = time.monotonic()
                return None

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


def run_resilience_phase1() -> dict:
    """Phase 1（LightRAG eager init 之前）：只做一致性检测。

    v6 修正：不做 cleanup / full_backup（备份是用户自己的事）。
    检测到损坏不自动修复，由 rfd 原生弹窗让用户选'退出'或'尝试修复'。

    Returns:
        {"check_ok": bool, "need_repair": bool, "check_result": dict}
    """
    global _integrity_result
    from niu_api.internal.lightrag_integrity import check_all

    # 只做检测，不动任何文件
    try:
        check_result = check_all()
    except Exception as e:
        logger.warning(f"[LightRAG] 一致性检测失败（不影响启动）: {e}")
        # 异常路径也保留 errors_by_level 结构，避免下游 .get("errors_by_level") 报 KeyError
        check_result = {
            "ok": True,
            "errors_by_level": {"critical": [], "major": [], "minor": []},
            "error": str(e),
        }

    _integrity_result = check_result

    # 从 errors_by_level 汇总各级错误数（Task 5 后 check_all 用 errors_by_level 结构）
    _ebl = check_result.get("errors_by_level", {}) or {}
    _critical = len(_ebl.get("critical", []) or [])
    _major = len(_ebl.get("major", []) or [])
    _minor = len(_ebl.get("minor", []) or [])
    _total = _critical + _major + _minor
    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"critical={_critical}, major={_major}, minor={_minor}, total_errors={_total}"
    )
    return {
        "check_ok": check_result.get("ok", True),
        "need_repair": not check_result.get("ok", True),
        "check_result": check_result,
    }


def should_signal_scheduler_ready(phase1_result: dict) -> bool:
    """Phase 1 后是否通知 scheduler 系统就绪。

    损坏时不通知，让 scheduler 60s 超时强行扫描的漏洞被堵住
    （配合 scheduler.cancel_delayed_start 让超时后 _delayed_start 线程
    直接 return，不强行 start）。

    用户决策退出或修复后，scheduler 跟随程序整体退出，不需要 ready signal。

    Returns:
        True 表示应该调 signal_scheduler_ready()（正常启动）
        False 表示跳过（LightRAG 损坏）
    """
    return not phase1_result.get("need_repair", False)


def should_start_db_monitor(phase1_result: dict) -> bool:
    """Phase 1 后是否启动 db_monitor task。

    损坏时不启动，避免 db_monitor 路由消息到 ChatQueue → runner.chat 报错
    （ChatQueue worker 已 pause，但 db_monitor 入队后只堆积在队列里，
    程序退出时丢失——可接受，损坏期间不应处理 IM 消息）。

    Returns:
        True 表示应该启动 db_monitor（正常启动）
        False 表示跳过（LightRAG 损坏）
    """
    return not phase1_result.get("need_repair", False)


def pause_chatqueue_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时 pause ChatQueue，让 worker 不消费消息。

    用户决策期间 IM/scheduler 入队的消息只堆积在队列里，不触发 runner.chat。
    程序退出时 ChatQueue 跟随整体 shutdown（stop_chat_queue cancel worker task），
    不需要 resume。

    异常处理：pause 失败只 log warning，不抛异常（不阻塞 lifespan 继续走
    Phase 1 后的 gate 流程，最坏情况是 ChatQueue 仍消费，但 scheduler
    被 cancel + db_monitor 未启动 + signal 未发，已堵住 90% 的触发路径）。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.chat_queue import get_chat_queue
            q = get_chat_queue()
            q.pause()
            logger.info("[LightRAG] ChatQueue paused due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to pause ChatQueue: {e}")


def cancel_scheduler_delayed_start_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时取消 scheduler 的 delayed start。

    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106），即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动 + 阻塞 120 秒（_CALLBACK_TIMEOUT）。
    虽然此期间 ChatQueue 被 pause 阻塞不会触发 runner.chat，但 scheduler
    线程跑起来后 60s+120s 才结束，期间用户决策/退出流程会被拖延。

    调 scheduler.cancel_delayed_start() 设 _delayed_start_cancelled=True，
    _delayed_start 线程 60s 超时后检查到 flag 直接 return。

    异常处理：cancel 失败只 log warning，不阻塞 lifespan。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.internal.scheduler.service import get_scheduler
            sched = get_scheduler()
            if sched is not None:
                sched.cancel_delayed_start()
                logger.info("[LightRAG] Scheduler delayed start cancelled due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to cancel scheduler delayed start: {e}")


def run_repair_on_user_request() -> dict:
    """用户在弹窗点'尝试修复'后调用（通过 /api/kg/lightrag/repair 触发）。

    v6: 不自动修复，等用户决策。用户确认后才调 repair_all。
    v8 (redo): _repairing try/finally 保护 + pipeline busy 等待 +
              unrecoverable 判定 + severity 判定 repaired。

    修复流程：
        1. 先读 pipeline busy 等空闲（必须在 _repairing=True 之前，否则
           get_lightrag 返回 None → _read_pipeline_busy 返回 None → busy 检查被绕过）
        2. 设 _repairing=True（try/finally 保护）
        3. 置 _rag_instance = None（避免新 ingest 请求并发写文件竞争）
        4. 调 repair_all
        5. reset_init_state + 重跑 check_all 更新 _integrity_result
        6. 主动调 get_lightrag() 触发重试初始化
        7. 判定 repaired（任一 status=error 或 unrecoverable 或 重检 critical/major>0 都算失败）

    Returns:
        {
            "repaired": bool,
            "check_ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "repair_result": dict,
            "check_result": dict,
        }
    """
    global _integrity_result, _rag_instance, _repairing
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all

    logger.warning("[LightRAG] 用户选择'尝试修复'，启动 repair_all")

    # 1. 先检查 pipeline busy，等空闲再设 _repairing=True
    #    原因：_repairing=True 时 get_lightrag() 返回 None →
    #    _read_pipeline_busy() 调 get_lightrag() 拿到 None → 返回 None →
    #    `not None` = True → 直接 break，pipeline busy 检查被绕过。
    #    所以必须先读 busy，等空闲后再设 _repairing=True。
    from niu_api.kg_api import _read_pipeline_busy

    deadline = time.monotonic() + 300  # 超时 300s
    waited = False
    while time.monotonic() < deadline:
        busy = _read_pipeline_busy()
        if not busy:
            break
        waited = True
        time.sleep(5)
    else:
        return {
            "repaired": False,
            "check_ok": False,
            "message": "pipeline busy 超过 300s，请稍后重试",
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
            "repair_result": {},
            "check_result": _integrity_result,
        }

    if waited:
        logger.info("[LightRAG] pipeline 空闲，开始 repair")

    _repairing = True
    try:
        # 2. repair 期间置 _rag_instance = None（避免新 ingest 请求并发写文件竞争）
        # 注意：_repairing=True 已经让 get_lightrag 静默返回 None，
        #       但已持有的 _rag_instance 仍可能被其他模块通过 call_async 直接调用，
        #       这里显式置 None 强制下一次重新初始化。
        _rag_instance = None

        # 3. 调 repair_all（按依赖链顺序修复所有文件）
        repair_result = repair_all()

        # 4. 检查 unrecoverable（顶层标记或单个 result 字段）
        has_unrecoverable = bool(repair_result.get("_unrecoverable", False)) or any(
            isinstance(v, dict) and v.get("unrecoverable")
            for v in repair_result.values()
            if isinstance(v, dict)
        )

        # 5. reset + 重跑 check_all
        reset_init_state()
        check_result = check_all()
        _integrity_result = check_result

        # 6. 主动调 get_lightrag 触发重试初始化（_repairing 仍 True，
        #    但下面 finally 会清掉；此处先不清，让 get_lightrag 看到 _repairing
        #    返回 None 不报错——但我们要的是触发初始化，所以先临时关掉）
        # 实际上：get_lightrag 看到 _repairing=True 会返回 None，不触发初始化。
        # 这里改为先清 _repairing，让 get_lightrag 走三级门控重新初始化。
        _repairing = False
        try:
            get_lightrag()
        except Exception as e:
            logger.warning(f"[LightRAG] 修复后 get_lightrag 重试失败（不影响返回）: {e}")

        # 6.5 等 SkillSync 首次扫描完成 + 二次 repair
        # 原因：SkillSync 在 LightRAG ready 后会异步跑 scan_and_sync，
        # 其中"ghost skill 清理"会调 adelete_by_entity 删除不在磁盘上的 skill 实体。
        # 但 adelete_by_entity 在我们环境下存在部分失败：
        # GraphML/vdb_entities/vdb_relationships 删除成功，但 entity_chunks/relation_chunks
        # 未持久化（storage_updated flag 在 _persist_graph_updates 并发场景下漏置位）。
        # 这会导致 check_all 报 entity_chunks_dangling / relation_chunks_dangling major 错误。
        #
        # 修复策略：等 SkillSync 首次扫描跑完（仅当 LightRAG 可用时 scan_and_sync 才真跑）→
        # 重检 → 若仍有 major → 再跑一次 repair_all
        # （repair_entity_chunks 从 GraphML 重建，会清掉残留的 entity_chunks 条目）。
        #
        # 超时 120s = LightRAG ready 后 SkillSync 最多 60s 触发下一轮 scan + 容错 60s。
        try:
            from agent.injector.sync import wait_first_scan_complete
            scan_done = wait_first_scan_complete(timeout=120)
            if scan_done:
                logger.info("[LightRAG] SkillSync 首次扫描完成，重检一致性")
            else:
                logger.warning("[LightRAG] SkillSync 首次扫描超时（120s），继续重检")
        except Exception as e:
            logger.warning(f"[LightRAG] 等待 SkillSync 首次扫描失败（继续重检）: {e}")

        # 重检 + 二次 repair（仅当重检发现新 major/critical 时）
        try:
            post_skill_check = check_all()
            post_critical = post_skill_check.get("critical_errors", 0)
            post_major = post_skill_check.get("major_errors", 0)
        except Exception as e:
            logger.warning(f"[LightRAG] SkillSync 后重检失败: {e}")
            post_critical, post_major = 0, 0
            post_skill_check = check_result

        if post_critical > 0 or post_major > 0:
            logger.warning(
                f"[LightRAG] SkillSync 后重检发现新问题（critical={post_critical}, "
                f"major={post_major}），启动二次 repair_all"
            )
            try:
                second_repair = repair_all()
                # 合并二次 repair 结果到 repair_result
                for k, v in second_repair.items():
                    if k.startswith("_"):
                        continue
                    repair_result[f"post_skill_sync_{k}"] = v
            except Exception as e:
                logger.error(f"[LightRAG] 二次 repair_all 失败: {e}")
            # 二次 repair 后重检
            try:
                check_result = check_all()
                _integrity_result = check_result
            except Exception as e:
                logger.warning(f"[LightRAG] 二次 repair 后重检失败: {e}")

        # 7. 判定 repaired
        # - 任一 repair result status=error → False
        # - unrecoverable 标记 → False
        # - 重检 critical > 0 或 major > 0 → False
        critical = check_result.get("critical_errors", 0)
        major = check_result.get("major_errors", 0)
        minor = check_result.get("minor_errors", 0)

        repaired = True
        for vdb_name, vdb_result in repair_result.items():
            if not isinstance(vdb_result, dict):
                continue
            if vdb_result.get("status") == "error":
                repaired = False
                logger.warning(
                    f"[LightRAG] 修复失败项: {vdb_name} - {vdb_result.get('message', '')}"
                )

        if critical > 0 or major > 0 or has_unrecoverable:
            repaired = False
            logger.warning(
                f"[LightRAG] 修复后重检仍有 critical({critical})/major({major})"
                f"/unrecoverable({has_unrecoverable})"
            )

        logger.info(
            f"[LightRAG] 修复完成: repaired={repaired}, "
            f"重检: critical={critical}, major={major}, minor={minor}"
        )

        return {
            "repaired": repaired,
            "check_ok": check_result.get("ok", True),
            "critical_errors": critical,
            "major_errors": major,
            "minor_errors": minor,
            "repair_result": repair_result,
            "check_result": check_result,
        }
    except Exception as e:
        logger.error(f"[LightRAG] 修复失败: {e}")
        return {
            "repaired": False,
            "check_ok": False,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
            "repair_result": {"error": str(e)},
            "check_result": _integrity_result,
        }
    finally:
        _repairing = False


def reset_init_state() -> None:
    """重置初始化失败状态，让下次 get_lightrag 重试。"""
    global _init_failed_at, _init_error
    _init_failed_at = None
    _init_error = None


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

    result = {
        "installed": is_lightrag_available(),
        "initialized": initialized,
        "init_failed": init_failed,
        "init_retry_in_seconds": retry_in,
        "storage_dir": str(STORAGE_DIR),
        "llm_mode": "litellm_direct",
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
        "loop_running": loop_running,
    }
    if _integrity_result:
        # 从 errors_by_level 结构汇总各级错误数（Task 5 后 check_all 用 errors_by_level.critical/major/minor）
        errors_by_level = _integrity_result.get("errors_by_level", {}) or {}
        critical_errors = len(errors_by_level.get("critical", []) or [])
        major_errors = len(errors_by_level.get("major", []) or [])
        minor_errors = len(errors_by_level.get("minor", []) or [])
        total_errors = critical_errors + major_errors + minor_errors
        result["integrity"] = {
            "ok": _integrity_result.get("ok", True),
            "critical_errors": critical_errors,
            "major_errors": major_errors,
            "minor_errors": minor_errors,
            "total_errors": total_errors,
        }
    return result


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
