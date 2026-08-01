"""
Knowledge Graph API endpoints for the graph visualization UI.

Routes delegate to LightRAG adapter for queries (replacing KuzuDB direct calls).

Response format: all graph endpoints return structured {nodes: [...], edges: [...]}
to match the frontend force-graph renderer expectations.
"""

import re
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


def _cleanup_failed_docs(rag) -> dict[str, int]:
    """Remove unrecoverable FAILED entries from doc_status.

    Two categories are deleted:
      1. ``dup-`` entries — duplicate markers that have no content and can
         never succeed on retry.
      2. Empty-content documents — content_length == 0 or no row in
         full_docs, which fail with "Set of Tasks/Futures is empty".

    Other FAILED docs are left intact (they may be real failures worth
    retrying).

    Returns:
        ``{"dup_deleted": N, "empty_deleted": N, "real_failures": N}``
    """
    from lightrag.base import DocStatus

    from niu_api.internal.lightrag_manager import call_async

    counts = {"dup_deleted": 0, "empty_deleted": 0, "real_failures": 0}

    try:
        failed_docs = call_async(
            rag.doc_status.get_docs_by_status(DocStatus.FAILED), timeout=30
        )
    except Exception as e:
        logger.error(f"cleanup_failed_docs: failed to read doc_status: {e}")
        return counts

    if not failed_docs:
        return counts

    dup_ids: list[str] = []
    empty_ids: list[str] = []

    for doc_id, doc in failed_docs.items():
        if doc_id.startswith("dup-"):
            dup_ids.append(doc_id)
            continue

        # Check for empty content
        is_empty = doc.content_length == 0
        if not is_empty:
            try:
                content_data = call_async(rag.full_docs.get_by_id(doc_id), timeout=10)
                if content_data is None:
                    is_empty = True
                elif not content_data.get("content"):
                    is_empty = True
            except Exception as e:
                logger.warning(
                    f"cleanup_failed_docs: could not check full_docs for {doc_id}: {e}"
                )

        if is_empty:
            empty_ids.append(doc_id)
        else:
            counts["real_failures"] += 1

    # Delete dup- entries
    if dup_ids:
        try:
            call_async(rag.doc_status.delete(dup_ids), timeout=30)
            counts["dup_deleted"] = len(dup_ids)
            logger.info(f"cleanup_failed_docs: deleted {len(dup_ids)} dup- entries")
        except Exception as e:
            logger.error(f"cleanup_failed_docs: failed to delete dup- entries: {e}")

    # Delete empty-content entries
    if empty_ids:
        try:
            call_async(rag.doc_status.delete(empty_ids), timeout=30)
            counts["empty_deleted"] = len(empty_ids)
            logger.info(
                f"cleanup_failed_docs: deleted {len(empty_ids)} empty-content entries"
            )
        except Exception as e:
            logger.error(
                f"cleanup_failed_docs: failed to delete empty-content entries: {e}"
            )

    return counts

# LightRAG merges multi-valued node fields (file_path, source_id, description)
# using <SEP> as a separator. For file_path, this produces values like:
#   "e:/photos/2026/05/photo.jpg<SEP>unknown_source<SEP>custom_kg"
# The frontend needs a single clean file path for thumbnail preview and
# file-open actions, so we extract the first value that looks like a
# real local file path (not a placeholder like "unknown_source" or "custom_kg").
_GRAPH_FIELD_SEP = "<SEP>"
_FILE_PATH_PLACEHOLDERS = {"unknown_source", "custom_kg"}


def _clean_file_path(raw: str) -> str:
    """Extract the first real file path from a LightRAG <SEP>-merged file_path field.

    LightRAG's _merge_nodes_then_upsert merges file_path values from multiple
    sources using <SEP>. When a photo entity is created by custom_kg (with the
    real photo path) and later merged by ainsert (with "unknown_source" or
    "custom_kg"), the stored value becomes:
        "e:/photos/photo.jpg<SEP>unknown_source"

    The frontend renderer checks if uri ends with .jpg/.png etc. to render
    a thumbnail. The <SEP>-polluted value fails this check, so we must
    extract just the real path.
    """
    if not raw:
        return ""
    if _GRAPH_FIELD_SEP not in raw:
        return raw
    # Split and return the first part that looks like a real file path
    for part in raw.split(_GRAPH_FIELD_SEP):
        part = part.strip()
        if part and part not in _FILE_PATH_PLACEHOLDERS:
            return part
    # Fallback: all parts are placeholders — no real file path exists
    return ""


_SOURCE_ID_PLACEHOLDERS = {"unknown_source", "custom_kg"}


def _clean_source_id(raw: str) -> str:
    """Extract the first real source_id from a LightRAG <SEP>-merged field.

    Same <SEP> merging pattern as file_path. source_id gets polluted with
    "unknown_source" or "custom_kg" when ainsert merges with custom_kg entities.
    """
    if not raw:
        return ""
    if _GRAPH_FIELD_SEP not in raw:
        return raw
    for part in raw.split(_GRAPH_FIELD_SEP):
        part = part.strip()
        if part and part not in _SOURCE_ID_PLACEHOLDERS:
            return part
    return ""


def _format_description(entity_type: str, description: str) -> str:
    """Format node description for frontend display.

    For brainregion entities, the raw description contains brain_meta_*
    metadata that is meaningless to users. This function extracts and
    formats the human-readable summary.

    For all other entity types, <SEP> separators (from LightRAG multi-source
    merging) are replaced with spaces for clean display.
    """
    if not description:
        return ""
    if entity_type.lower() == "brainregion" and "<SEP>" in description:
        from niu_api.internal.region_manager import _format_summary_for_display, _parse_description
        parsed = _parse_description(description)
        return _format_summary_for_display(parsed)
    # Non-brainregion: clean <SEP> separators for display
    return description.replace("<SEP>", " ")


def _normalize_nodes(nodes: list) -> list:
    """Convert adapter node format {id, name, type, description, file_path, source_id} to frontend-expected format.

    Frontend expects: {id, label, name, nodeType, entityType, description, uri, source}

    nodeType is set to "Entity" for entity nodes (not the specific type like "Person"),
    with the specific type in entityType. Document nodes keep nodeType "Document".
    """
    result = []
    for n in nodes:
        raw_id = n.get("id", "")
        node_type = n.get("type", "other")
        if node_type.lower() == "document":
            node_id = raw_id
            normalized_type = "Document"
        else:
            node_id = raw_id
            normalized_type = "Entity"
        description = _format_description(node_type, n.get("description", ""))
        result.append({
            "id": node_id,
            "label": n.get("name", n.get("id", "")),
            "name": n.get("name", ""),
            "nodeType": normalized_type,
            "entityType": node_type,
            "description": description,
            "uri": _clean_file_path(n.get("file_path", "")),
            "source": _clean_source_id(n.get("source_id", "")),
        })
    return result


def _normalize_edges(edges: list) -> list:
    """Convert adapter edge format to frontend-expected format.

    Frontend expects: {source, target, relation, edgeType, confidence}
    Adapter returns:  {source, target, relation, description, weight}
    """
    result = []
    for e in edges:
        src = e.get("source", e.get("src_id", ""))
        tgt = e.get("target", e.get("tgt_id", ""))
        result.append({
            "source": src,
            "target": tgt,
            "relation": e.get("relation", e.get("keywords", "")),
            "edgeType": e.get("type", "relation"),
            "confidence": e.get("confidence", e.get("weight", 1.0)),
        })
    return result


class ExploreRequest(BaseModel):
    entity_id: str
    depth: int = Field(default=2, ge=1, le=5)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    direction: Literal["both", "outgoing", "incoming"] = "both"


class FindPathRequest(BaseModel):
    from_id: str
    to_id: str
    max_depth: int = Field(default=5, ge=1, le=10)


def _get_graph():
    """Get LightRAG's NetworkX graph (unwrapped from OperableGraph wrapper).

    Returns the raw NetworkX DiGraph, not the LightRAG OperableGraph wrapper.
    This is necessary because callers use NetworkX APIs (nx.shortest_path,
    g.nodes(), g.edges(), g.copy()) which require a NetworkX Graph object.
    """
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return None
    graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
    if graph_obj is None:
        return None
    # Unwrap OperableGraph to get the underlying NetworkX graph
    return graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj


# ============== Singleton Adapter ==============

_adapter = None
_adapter_lock = threading.Lock()


def _get_adapter():
    """Get or create the singleton LightRAGAdapter instance (thread-safe)."""
    global _adapter
    if _adapter is not None:
        return _adapter
    with _adapter_lock:
        if _adapter is not None:
            return _adapter
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        _adapter = LightRAGAdapter()
    return _adapter


def _parse_file_progress(msg: str) -> int:
    """Parse within-file progress from LightRAG latest_message. Returns 0-100.

    LightRAG's pipeline emits messages at different processing stages.
    This function maps those messages to a 0-100 progress scale:

      Extraction (0-70%): chunk-level progress from "Chunk X of Y extracted"
      Merging (70-98%): phase indicators and merge messages
      Completion (100%): "Completed processing file"
    """
    if not msg:
        return 0

    # Completion
    if msg.startswith("Completed processing file"):
        return 100
    if msg.startswith("Completed merging"):
        return 98

    # Persist phase (almost done)
    if "In memory DB persist to disk" in msg:
        return 99

    # Rebuild phase
    if "Rebuilding knowledge from" in msg:
        return 60

    # Phase indicators
    if "Phase 3" in msg:
        return 95
    if "Phase 2" in msg:
        return 85
    if "Phase 1" in msg:
        return 75

    # Relation merge messages (contain ~)
    if "Merged:" in msg and "~" in msg:
        return 82
    if "LLMmrg:" in msg and "~" in msg:
        return 82

    # Entity merge messages (no ~)
    if "LLMmrg:" in msg:
        return 78
    if "Merged:" in msg:
        return 78

    # Merge start (must come before "Merging stage failed" check)
    if msg.startswith("Merging stage failed"):
        return -1
    if msg.startswith("Merging stage"):
        return 70

    # Extraction just started (must come before chunk extraction regex)
    if msg.startswith("Extracting stage"):
        return 5

    # Extraction error
    if msg.startswith("Failed to extract entities"):
        return -1

    # Chunks appended from relation (side-effect during edge merge, after Phase 2)
    if msg.startswith("Chunks appended from relation"):
        return 86

    # Chunk extraction — most granular progress (0-70%)
    m = re.search(r"Chunk (\d+) of (\d+) extracted", msg)
    if m:
        chunk_cur = int(m.group(1))
        chunk_total = int(m.group(2))
        if chunk_total > 0:
            return int(chunk_cur / chunk_total * 70)
        return 35  # fallback

    # Document started
    if msg.startswith("Processing d-id:"):
        return 1
    if "document(s)" in msg and "Processing" in msg:
        return 0

    # Error/cancel messages — don't reset progress, return -1 to signal "keep previous"
    if msg.startswith("Failed to extract document") or msg.startswith("User cancelled"):
        return -1
    if msg.startswith("Error processing"):
        return -1

    # Unknown message during busy state — don't reset to 0
    return -1


# ============== Pipeline Watcher ==============
# Background thread that monitors LightRAG's _shared_dicts pipeline_status
# and pushes SSE events when the pipeline becomes busy or idle.
# This ensures the frontend progress ring appears even when ingestion
# is triggered by MCP tools (lightrag_insert / ainsert) rather than
# the /api/kg/insert HTTP endpoint.

_pipeline_watcher_stop = threading.Event()


def _read_pipeline_busy() -> bool | None:
    """Read the busy flag from LightRAG's shared pipeline_status.

    Returns True if busy, False if idle, None if LightRAG or
    pipeline_status is not available.
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
        if rag is None:
            return None

        from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace

        ps_key = get_final_namespace("pipeline_status", rag.workspace)
        ps = _shared_dicts.get(ps_key)
        if ps is None:
            return None
        return bool(ps.get("busy", False))
    except Exception:
        return None


def _notify_ingest_started() -> None:
    """Push an ingest-started SSE event to all connected clients."""
    from niu_api.chat import _main_loop, _sync_broadcast

    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    event = {"type": "ingest-started"}
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass


def _notify_ingest_completed() -> None:
    """Push an ingest-completed SSE event to all connected clients.

    Also triggers a lightweight refresh of entity-to-region mapping so that
    brain region entity counts and /api/stats reflect the newly ingested data.
    """
    from niu_api.chat import _main_loop, _sync_broadcast

    loop = _main_loop
    if loop is not None and not loop.is_closed():
        event = {"type": "ingest-completed"}
        try:
            loop.call_soon_threadsafe(_sync_broadcast, event)
        except RuntimeError:
            pass

    # Refresh entity mapping in a background thread (non-blocking)
    # This updates _entity_to_region (brain region counts) and
    # _entity_type_counts (/api/stats) without a full run_sync().
    try:
        import threading

        from agent.injector.region_sync import get_region_sync

        sync = get_region_sync()
        if sync is not None:
            t = threading.Thread(
                target=sync.refresh_entity_mapping_only,
                name="entity-mapping-refresh",
                daemon=True,
            )
            t.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("Entity mapping refresh trigger failed: %s", e)


def _pipeline_watcher() -> None:
    """Background thread: monitor pipeline busy transitions and push SSE events.

    Polls _shared_dicts every 1 second. When the pipeline transitions
    from idle to busy, sends ingest-started. When it transitions from
    busy to idle, sends ingest-completed.
    """
    logger.info("[PipelineWatcher] Started")
    prev_busy = False

    while not _pipeline_watcher_stop.is_set():
        try:
            busy = _read_pipeline_busy()

            if busy is not None:
                if busy and not prev_busy:
                    logger.info("[PipelineWatcher] Pipeline became busy -> ingest-started")
                    _notify_ingest_started()
                elif not busy and prev_busy:
                    logger.info("[PipelineWatcher] Pipeline became idle -> ingest-completed")
                    _notify_ingest_completed()
                prev_busy = busy
        except Exception as e:
            logger.warning(f"[PipelineWatcher] Error reading pipeline status: {e}")

        _pipeline_watcher_stop.wait(timeout=1.0)

    logger.info("[PipelineWatcher] Stopped")


def start_pipeline_watcher() -> None:
    """Start the pipeline watcher as a daemon thread (idempotent)."""
    if _pipeline_watcher_stop.is_set():
        _pipeline_watcher_stop.clear()

    t = threading.Thread(target=_pipeline_watcher, name="pipeline-watcher", daemon=True)
    t.start()


def stop_pipeline_watcher() -> None:
    """Signal the pipeline watcher thread to stop."""
    _pipeline_watcher_stop.set()


# ============== Endpoints ==============
# NOTE: All endpoints use `def` (not `async def`) because LightRAGAdapter.query()
# and _shared_dicts reads are blocking.  FastAPI runs regular def endpoints in a
# thread pool, so they won't block the ASGI event loop.


@router.get("/snapshot")
def graph_snapshot(
    limit: int = Query(default=2000, ge=1, le=5000),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Get full graph snapshot for visualization.

    Returns ``{nodes: [...], edges: [...]}`` matching the frontend renderer.
    """
    adapter = _get_adapter()

    result = adapter.get_graph_snapshot(limit=limit)
    if result is None:
        return {"status": "error", "message": "LightRAG not available"}
    # Normalize adapter format to frontend-expected format
    if "nodes" in result:
        result["nodes"] = _normalize_nodes(result["nodes"])
    if "edges" in result:
        result["edges"] = _normalize_edges(result["edges"])
    # Apply min_confidence filter on edges
    if min_confidence > 0 and "edges" in result:
        result["edges"] = [e for e in result["edges"] if e.get("confidence", 1.0) >= min_confidence]
    return result


@router.get("/stats")
def graph_stats():
    """Get knowledge graph statistics."""
    from niu_api.internal.lightrag_manager import get_lightrag_status

    return get_lightrag_status()


@router.get("/pipeline_status")
def pipeline_status():
    """Get LightRAG ingestion pipeline progress.

    Returns busy flag, progress percentage, and the latest pipeline message.
    Frontend polls this endpoint to show a graphical progress indicator.
    """
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return {"busy": False, "progress": 0, "message": "LightRAG not available"}

    try:
        from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace

        ps_key = get_final_namespace("pipeline_status", rag.workspace)
        ps = _shared_dicts.get(ps_key)
        if ps is None:
            return {"busy": False, "progress": 0, "message": "pipeline_status not initialized"}
    except Exception as e:
        return {"busy": False, "progress": 0, "message": f"Error: {e}"}

    busy = bool(ps.get("busy", False))
    cur_batch = int(ps.get("cur_batch", 0))
    batchs = int(ps.get("batchs", 0))
    job_name = str(ps.get("job_name", ""))
    latest_message = str(ps.get("latest_message", ""))

    # Clean up unrecoverable FAILED docs at two moments:
    # 1. When pipeline just started (busy, cur_batch <= 1, batchs > 0) — before processing
    # 2. When pipeline just completed (not busy, completion message still in latest_message)
    # Cleanup is cheap when there are no FAILED docs (early return), so the
    # post-completion trigger only matters until the message is overwritten.
    should_cleanup = (busy and cur_batch <= 1 and batchs > 0)
    if not busy and ("Completed processing" in latest_message
                     or "Enqueued document processing pipeline stopped" in latest_message):
        should_cleanup = True
    if should_cleanup:
        try:
            cleanup_result = _cleanup_failed_docs(rag)
            if cleanup_result["dup_deleted"] > 0 or cleanup_result["empty_deleted"] > 0:
                logger.info(
                    f"pipeline_status cleanup: {cleanup_result['dup_deleted']} dup, "
                    f"{cleanup_result['empty_deleted']} empty, "
                    f"{cleanup_result['real_failures']} real failures remain"
                )
                # Re-read pipeline_status after cleanup (LightRAG may have updated batchs)
                ps = _shared_dicts.get(ps_key, ps)
                cur_batch = int(ps.get("cur_batch", 0))
                batchs = int(ps.get("batchs", 0))
                latest_message = str(ps.get("latest_message", ""))
        except Exception as e:
            logger.warning(f"pipeline_status cleanup failed (non-fatal): {e}")

    # Progress combines document-level base with within-file progress.
    # cur_batch increments when a file STARTS processing, so (cur_batch - 1)
    # gives the count of completed files. _parse_file_progress returns -1
    # for unknown messages — we use a rough estimate based on cur_batch then.
    if not busy:
        # Check if pipeline just completed — briefly show 100% before resetting
        if "Completed processing" in latest_message or "Enqueued document processing pipeline stopped" in latest_message:
            progress = 100
        else:
            progress = 0
    elif batchs > 0:
        doc_base = (cur_batch - 1) / batchs * 100
        file_progress = _parse_file_progress(latest_message)
        if file_progress >= 0:
            progress = doc_base + file_progress / batchs
        else:
            # Unknown message — estimate from doc_base alone (current file ~50% done)
            progress = doc_base + 50 / batchs
        progress = min(int(progress), 100)
    else:
        # Pipeline just started — hasn't counted docs yet
        progress = 1

    return {
        "busy": busy,
        "progress": progress,
        "cur_batch": cur_batch,
        "batchs": batchs,
        "job_name": job_name,
        "message": latest_message,
    }


@router.get("/hubs")
def hub_entities(
    limit: int = Query(default=20, ge=1, le=100),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Find hub entities by connection count.

    Returns ``{nodes: [...], edges: [...]}`` — the top ``limit`` entities
    ranked by edge count, plus edges between them.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            snapshot = g.copy()

        node_degrees = {n: snapshot.degree(n) for n in snapshot.nodes()}
        sorted_nodes = sorted(node_degrees.keys(), key=lambda n: node_degrees[n], reverse=True)
        top = sorted_nodes[:limit]
        top_set = set(top)

        nodes = []
        for node_name in top:
            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
            nodes.append(
                {
                    "id": node_name,
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "other"),
                    "description": _format_description(attrs.get("entity_type", "other"), attrs.get("description", "")),
                    "uri": _clean_file_path(attrs.get("file_path", "")),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        for u, v, data in snapshot.edges(data=True):
            if u in top_set and v in top_set:
                confidence = data.get("weight", 1.0)
                if min_confidence > 0 and confidence < min_confidence:
                    continue
                edges.append(
                    {
                        "source": u,
                        "target": v,
                        "relation": data.get("keywords", ""),
                        "confidence": confidence,
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during hub_entities read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.post("/explore")
def explore_node(request: ExploreRequest):
    """Explore graph from a specific entity.

    Returns ``{nodes: [...], edges: [...]}`` — the entity's neighborhood
    up to ``request.depth`` hops.
    """
    adapter = _get_adapter()

    entity_name = request.entity_id
    result = adapter.explore_node(
        entity_name=entity_name,
        depth=request.depth,
    )
    if result is None:
        return {"status": "error", "message": "LightRAG not available"}
    # Normalize adapter format to frontend-expected format
    if "nodes" in result:
        result["nodes"] = _normalize_nodes(result["nodes"])
    if "edges" in result:
        result["edges"] = _normalize_edges(result["edges"])
    # Apply min_confidence filter on edges
    if request.min_confidence > 0 and "edges" in result:
        result["edges"] = [e for e in result["edges"] if e.get("confidence", 1.0) >= request.min_confidence]
    if "center" in result and isinstance(result["center"], dict):
        c = result["center"]
        center_type = c.get("type", "other")
        center_desc = c.get("description", "")
        center_desc = _format_description(center_type, center_desc)
        result["center"] = {
            "id": c.get("id", ""),
            "label": c.get("name", c.get("id", "")),
            "name": c.get("name", ""),
            "nodeType": "Entity",
            "entityType": center_type,
            "description": center_desc,
            "uri": _clean_file_path(c.get("file_path", "")),
            "source": _clean_source_id(c.get("source_id", "")),
        }
    return result


@router.post("/find-path")
def find_path(request: FindPathRequest):
    """Find shortest path between two entities.

    Returns ``{nodes: [...], edges: [...]}`` along the path, or an empty
    graph if no path exists.
    """
    import networkx as nx

    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    src = request.from_id
    tgt = request.to_id

    try:
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            snapshot = g.copy()

        path_nodes = nx.shortest_path(snapshot, source=src, target=tgt)
    except (nx.NodeNotFound, nx.NetworkXNoPath):
        return {"nodes": [], "edges": []}
    except RuntimeError:
        logger.warning("Graph modified during find_path read")
        return {"nodes": [], "edges": []}

    try:
        nodes = []
        for node_name in path_nodes:
            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
            nodes.append(
                {
                    "id": node_name,
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "other"),
                    "description": _format_description(attrs.get("entity_type", "other"), attrs.get("description", "")),
                    "uri": _clean_file_path(attrs.get("file_path", "")),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        # Only include edges between consecutive path nodes
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            # Check both directions since the graph is directed
            if snapshot.has_edge(u, v):
                data = snapshot.edges[u, v]
                edges.append(
                    {
                        "source": u,
                        "target": v,
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
            elif snapshot.has_edge(v, u):
                data = snapshot.edges[v, u]
                edges.append(
                    {
                        "source": v,
                        "target": u,
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during find_path iteration")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/entities")
def list_entities(
    limit: int = Query(default=100, ge=1, le=500),
    entity_type: str | None = Query(default=None),
):
    """List all entities.

    Returns ``{nodes: [...], edges: [...]}`` — entity nodes (and edges
    between them) up to ``limit``.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            snapshot = g.copy()

        collected = []
        for node_name in snapshot.nodes():
            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
            # Filter by entity_type if requested
            if entity_type and attrs.get("entity_type", "").lower() != entity_type.lower():
                continue
            collected.append((node_name, attrs))
            if len(collected) >= limit:
                break

        node_names = {name for name, _ in collected}
        nodes = []
        for node_name, attrs in collected:
            nodes.append(
                {
                    "id": node_name,
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "other"),
                    "description": _format_description(attrs.get("entity_type", "other"), attrs.get("description", "")),
                    "uri": _clean_file_path(attrs.get("file_path", "")),
                    "source": attrs.get("source_id", ""),
                }
            )

        edges = []
        for u, v, data in snapshot.edges(data=True):
            if u in node_names and v in node_names:
                edges.append(
                    {
                        "source": u,
                        "target": v,
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during list_entities read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/concepts")
def list_concepts(limit: int = Query(default=100, ge=1, le=500)):
    """List all concepts.

    Returns ``{nodes: [...], edges: [...]}`` — concept nodes (entities
    with entityType "concept") and their interconnecting edges.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            snapshot = g.copy()

        collected = []
        for node_name in snapshot.nodes():
            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
            if attrs.get("entity_type", "").lower() == "concept":
                collected.append((node_name, attrs))
            if len(collected) >= limit:
                break

        node_names = {name for name, _ in collected}
        nodes = []
        for node_name, attrs in collected:
            nodes.append(
                {
                    "id": node_name,
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Concept",
                    "entityType": attrs.get("entity_type", "other"),
                    "description": _format_description(attrs.get("entity_type", "other"), attrs.get("description", "")),
                    "uri": _clean_file_path(attrs.get("file_path", "")),
                    "source": _clean_source_id(attrs.get("source_id", "")),
                }
            )

        edges = []
        for u, v, data in snapshot.edges(data=True):
            if u in node_names and v in node_names:
                edges.append(
                    {
                        "source": u,
                        "target": v,
                        "relation": data.get("keywords", ""),
                        "confidence": data.get("weight", 1.0),
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during list_concepts read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/surprising")
def surprising_connections(
    min_shared: int = Query(default=2, ge=1),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    max_entities: int = Query(default=200, ge=1, le=1000),
):
    """Find surprising connections between entities.

    Returns ``{nodes: [...], edges: [...]}`` — entity pairs that share
    at least ``min_shared`` common neighbors.
    """
    g = _get_graph()
    if g is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            snapshot = g.copy()

        # Build adjacency sets only for the requested number of entities (not the full graph)
        candidate_nodes = list(snapshot.nodes())[:max_entities]
        adj: dict[str, set] = {n: set(snapshot.neighbors(n)) for n in candidate_nodes}

        surprising_pairs: list[tuple] = []  # (u, v, shared_count)
        node_set = set()

        for u in candidate_nodes:
            for v in adj[u]:
                if v <= u or v not in adj:
                    continue
                shared = len(adj[u] & adj[v])
                if shared >= min_shared:
                    surprising_pairs.append((u, v, shared))
                    node_set.add(u)
                    node_set.add(v)

        if not node_set:
            return {"nodes": [], "edges": []}

        nodes = []
        for node_name in node_set:
            attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
            nodes.append(
                {
                    "id": node_name,
                    "label": node_name,
                    "name": node_name,
                    "nodeType": "Entity",
                    "entityType": attrs.get("entity_type", "other"),
                    "description": _format_description(attrs.get("entity_type", "other"), attrs.get("description", "")),
                    "uri": _clean_file_path(attrs.get("file_path", "")),
                    "source": attrs.get("source_id", ""),
                }
            )

        # Include edges between the surprising pairs
        edges = []
        pair_set = {(u, v) for u, v, _ in surprising_pairs}
        for u, v, data in snapshot.edges(data=True):
            if (u, v) in pair_set or (v, u) in pair_set:
                confidence = data.get("weight", 1.0)
                if min_confidence > 0 and confidence < min_confidence:
                    continue
                edges.append(
                    {
                        "source": u,
                        "target": v,
                        "relation": data.get("keywords", ""),
                        "confidence": confidence,
                        "edgeType": "RELATED_TO",
                    }
                )
    except RuntimeError:
        logger.warning("Graph modified during surprising_connections read")
        return {"nodes": [], "edges": []}

    return {"nodes": nodes, "edges": edges}


@router.get("/changelog")
def graph_changelog(
    limit: int = Query(default=200, ge=1, le=500),
    since: str | None = Query(default=None),
):
    """Get recent graph changes for incremental frontend updates.

    Returns ``{changes: [...]}`` — a list of entity_created, edge_created,
    entity_deleted, and entity_merged events since the given timestamp.

    The frontend polls this endpoint every 1 second to merge new nodes/edges
    into the force-graph visualization without re-fetching the full snapshot.
    """
    from niu_api.internal.lightrag_manager import get_change_log

    change_log = get_change_log()
    changes = change_log.get_changes(since=since or "", limit=limit)
    return {"changes": changes}


@router.post("/test_ingest")
def test_ingest(dir_path: str = None):
    """Test endpoint: trigger directory ingestion for pipeline status testing."""
    if dir_path is None:
        import tempfile
        dir_path = str(Path(tempfile.gettempdir()) / "niu_test_ingest3")
    import os

    from niu_api.internal.lightrag_manager import call_async, fire_and_forget, get_lightrag

    rag = get_lightrag()
    if rag is None:
        return {"error": "LightRAG not available"}

    if not os.path.isdir(dir_path):
        return {"error": f"Directory not found: {dir_path}"}

    files = sorted(f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f)))
    if not files:
        return {"error": "No files in directory"}

    for fname in files:
        fpath = os.path.join(dir_path, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        call_async(rag.apipeline_enqueue_documents(content, file_paths=fpath), timeout=60)

    fire_and_forget(rag.apipeline_process_enqueue_documents(), context="test-ingest")

    return {"status": "started", "files": len(files), "dir": dir_path}


@router.post("/cleanup_failed_docs")
def cleanup_failed_docs():
    """Manually clean up unrecoverable FAILED documents from doc_status.

    Removes dup- entries (duplicate markers with no content) and
    empty-content documents that fail with "Set of Tasks/Futures is empty".
    Real failures are left intact for potential retry.
    """
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return {"error": "LightRAG not available"}

    result = _cleanup_failed_docs(rag)
    return {"status": "ok", **result}


@router.post("/lightrag/repair")
async def repair_lightrag_storage(target: str = "all") -> dict:
    """修复 LightRAG 存储（用户在 splash 点'尝试修复'触发）。

    实际路径：/api/kg/lightrag/repair（router prefix=/api/kg + 端点 /lightrag/repair）

    v6: 改 async def + asyncio.to_thread，避免 repair_all 跑几千个 entity
        本地 embedding 阻塞 FastAPI event loop 数十秒（期间 splash 轮询
        status 会超时，整个 API 卡死）。
    v5: 调 run_repair_on_user_request（封装 repair_all + reset_init_state + 重跑 check_all）。
    v5 只支持 target=all（用户决策驱动，不分单文件修复）。

    Args:
        target: 只支持 "all"（其他值返回 400）

    Returns:
        {"status": "ok", "result": {"repaired": bool, "check_ok": bool, ...}}
    """
    import asyncio

    from fastapi import HTTPException

    from niu_api.internal.lightrag_manager import run_repair_on_user_request

    if target != "all":
        raise HTTPException(status_code=400, detail=f"v5 只支持 target=all，收到: {target}")

    result = await asyncio.to_thread(run_repair_on_user_request)
    return {"status": "ok", "result": result}


@router.get("/search_entities")
def search_entities(query: str = Query(default=""), top_k: int = Query(default=20, ge=1, le=100)):
    """按关键词语义搜索实体，返回匹配的实体列表（供前端搜索栏使用）。"""
    if not query.strip():
        return {"entities": []}

    try:
        adapter = _get_adapter()
        # Pass query as keywords to skip LLM keyword extraction (5-30s overhead)
        result = adapter.query_data(query=query, mode="local", top_k=top_k, keywords=[query])

        if result is None:
            return {"entities": []}

        # query_data returns {status, data: {entities: [...], relationships: [...], chunks: [...]}}
        data = result.get("data", {})
        if not data:
            data = result
        raw_entities = data.get("entities", [])

        entities = []
        seen = set()
        for ent in raw_entities:
            name = ent.get("entity_name", "")
            if name and name not in seen:
                seen.add(name)
                entities.append({
                    "id": name,
                    "name": name,
                    "entityType": ent.get("entity_type", ""),
                    "description": ((ent.get("description", "") or "").replace("<SEP>", " "))[:120],
                })

        return {"entities": entities[:top_k]}
    except Exception as e:
        logger.error(f"search_entities failed: {e}")
        return {"entities": [], "error": str(e)}
