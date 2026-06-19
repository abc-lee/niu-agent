"""
Brain Region API endpoints — Region management and consolidation.

Provides REST API for querying brain region states, triggering
community detection, and inspecting region membership.

Routes:
    GET  /api/brain/regions             — list all regions with activation states
    POST /api/brain/regions/consolidate — trigger community detection
    GET  /api/brain/regions/{name}/members — get region members
    GET  /api/brain/status              — check brain graph status

Integration: Mount this router in niu_api/__main__.py:
    from niu_api.brain_region_api import router as brain_region_router
    app.include_router(brain_region_router)
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from niu_api.internal.region_activation import (
    RegionActivationManager,
    STATUS_OFF,
    BrainRegionState,
)
from niu_api.internal.brain_graph import get_brain_graph
from agent.injector.region_sync import REGION_CONFIG_DEFAULTS

router = APIRouter(prefix="/api/brain", tags=["brain-regions"])


# ============== Request Models ==============


class ConsolidateRequest(BaseModel):
    """Request body for community detection consolidation."""
    resolution: float = 1.0


# ============== Singleton Accessors ==============

_region_mgr = None
_region_mgr_lock = threading.Lock()


def _get_region_mgr():
    """Get or create the singleton RegionManager instance (thread-safe)."""
    global _region_mgr
    if _region_mgr is not None:
        return _region_mgr
    with _region_mgr_lock:
        if _region_mgr is not None:
            return _region_mgr
        from niu_api.internal.region_manager import RegionManager
        from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

        adapter = LightRAGAdapter()
        ingester = LightRAGIngester()
        _region_mgr = RegionManager(adapter, ingester)
    return _region_mgr


def _get_activation_mgr() -> RegionActivationManager | None:
    """Get the activation manager from brain_tools singleton."""
    from agent.brain_tools import get_activation_mgr

    return get_activation_mgr()


# ============== Endpoints ==============


@router.get("/regions")
def get_brain_regions(
    include_dark: bool = Query(default=False, description="Include regions below activation threshold"),
) -> dict[str, Any]:
    """Return all brain regions with activation states.

    Combines RegionManager.get_all_regions() for region metadata
    and RegionActivationManager.get_region_map() for activation scores.
    """
    try:
        region_mgr = _get_region_mgr()
        regions = region_mgr.get_all_regions()

        # Get activation states from ActivationManager
        activation_mgr = _get_activation_mgr()
        activation_map: dict[str, BrainRegionState] = {}
        if activation_mgr is not None:
            for state in activation_mgr.get_region_map():
                activation_map[state.region_id] = state

        # Combine region info with activation states
        result_regions: list[dict[str, Any]] = []
        for region in regions:
            act_state = activation_map.get(region.name)
            if act_state is not None and activation_mgr is not None:
                activation = act_state.activation
                status_light = activation_mgr.get_status_light(activation)
                manually_dimmed = act_state.manually_dimmed
            else:
                activation = 0.0
                status_light = STATUS_OFF
                manually_dimmed = False

            # Skip dark regions unless include_dark
            if not include_dark and activation <= 0.1:
                continue

            result_regions.append({
                "name": region.name,
                "label": region.label,
                "community_id": region.community_id,
                "description": region.description,
                "size": region.size,
                "representative": region.representative,
                "activation": round(activation, 4),
                "status_light": status_light,
                "manually_dimmed": manually_dimmed,
                "updated_at": region.updated_at,
            })

        # Sort by activation descending
        result_regions.sort(key=lambda r: r["activation"], reverse=True)

        return {
            "status": "ok",
            "regions": result_regions,
            "total": len(result_regions),
        }

    except Exception as e:
        logger.error(f"[Brain Region API] get_brain_regions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/regions/consolidate")
def consolidate_brain_regions(
    req: ConsolidateRequest = ConsolidateRequest(),
) -> dict[str, Any]:
    """Trigger community detection and create/update region nodes.

    Uses dry_run two-phase pattern: detect → create → execute cleanup.
    This ensures old regions are only deleted after new ones exist (D-13 fix).
    """
    try:
        # Acquire mutex lock to prevent concurrent sync
        from agent.injector.region_sync import get_region_sync
        sync = get_region_sync(auto_start=False)
        if not sync.try_acquire_sync():
            raise HTTPException(
                status_code=409,
                detail="Another brain region sync is in progress. Please try again later.",
            )
        try:
            from niu_api.internal.region_detector import CommunityDetector
            from niu_api.internal.lightrag_adapter import LightRAGAdapter

            adapter = LightRAGAdapter()
            detector = CommunityDetector(adapter)

            # Step 1: Detect communities
            detection_result = detector.detect_communities(
                resolution=req.resolution,
                min_graph_size=REGION_CONFIG_DEFAULTS.get("min_graph_size", 50),
                min_community_size=REGION_CONFIG_DEFAULTS.get("min_community_size", 100),
            )

            if not detection_result.partitions:
                return {
                    "status": "ok",
                    "message": "No communities detected (graph too small or empty)",
                    "regions_created": 0,
                }

            region_mgr = _get_region_mgr()

            # Step 2: dry_run detect (Phase 1)
            cleanup_ok = True
            try:
                removed, drifted, drifted_cids = region_mgr.cleanup_stale_regions(detection_result, dry_run=True)
            except Exception as e:
                logger.error("[Consolidate] cleanup detection failed: %s", e)
                removed, drifted, drifted_cids = [], [], set()
                cleanup_ok = False

            # Step 3: Create region nodes (Phase 2)
            created: list[str] = []
            create_ok = True
            try:
                created = region_mgr.create_region_nodes(detection_result, skip_community_ids=drifted_cids)
            except Exception as e:
                logger.error("[Consolidate] create_region_nodes failed: %s", e)
                create_ok = False

            # Step 4: Execute cleanup only if create didn't throw and dry_run succeeded (Phase 3)
            # create_ok=True but created=[] is normal (all regions exist), still run cleanup
            if (create_ok or not detection_result.partitions) and cleanup_ok:
                try:
                    actual_removed, actual_drifted, _ = region_mgr.cleanup_stale_regions(detection_result, dry_run=False)
                    removed = actual_removed
                    drifted = actual_drifted
                except Exception as e:
                    logger.error("[Consolidate] cleanup execution failed: %s", e)
            elif not cleanup_ok:
                logger.warning("[Consolidate] dry_run failed, skipping cleanup execution")
            else:
                logger.warning("[Consolidate] create_region_nodes exception, preserving stale regions")

            # Step 4.5: Assign existing entities to default brain regions
            try:
                from niu_api.internal.region_manager import assign_entities_to_default_regions
                result = assign_entities_to_default_regions(adapter)
                assigned = result.get("assigned", 0)
                if assigned > 0:
                    logger.info("[Consolidate] Assigned %d entities to default regions", assigned)
            except Exception as e:
                logger.debug("[Consolidate] assign_entities_to_default_regions skipped: %s", e)

            # Step 4.6: Update region summaries (exclude created and drifted)
            try:
                all_regions = region_mgr.get_all_regions()
                created_set = set(created)
                drifted_set = set(drifted) if cleanup_ok else set()
                region_names = [r.name for r in all_regions
                                if r.name not in created_set and r.name not in drifted_set]
                region_mgr.update_region_summaries(region_names)
            except Exception as e:
                logger.debug("[Consolidate] update_region_summaries skipped: %s", e)

            # Step 5: Initialize activation manager (with D-12 empty list protection)
            activation_mgr = _get_activation_mgr()
            if activation_mgr is not None:
                regions = region_mgr.get_all_regions()
                if not regions:
                    logger.warning("[Consolidate] get_all_regions returned empty, skipping activation init")
                else:
                    from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
                    for region in regions:
                        try:
                            region.members = lightrag_get_region_members(region.name)
                        except Exception as exc:
                            logger.warning("Failed to fetch members for region %s: %s", region.name, exc)
                    activation_mgr.initialize_from_regions(regions)
                    from niu_api.internal.region_neighbors import build_neighbor_map
                    neighbor_map = build_neighbor_map([
                        {"community_id": r.community_id or r.name, "members": r.members}
                        for r in regions
                    ])
                    activation_mgr.set_region_neighbors(neighbor_map)
                    logger.info("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))

            # Step 6: Merge co-activated regions
            regions_merged = 0
            try:
                if activation_mgr is not None:
                    from niu_api.internal.region_manager import is_default_region
                    candidates = activation_mgr.get_merge_candidates(
                        co_activation_threshold=REGION_CONFIG_DEFAULTS.get("co_activation_threshold", 0.9),
                    )
                    if candidates:
                        for source_id, target_id in candidates:
                            source_state = activation_mgr.get_region_state(source_id)
                            target_state = activation_mgr.get_region_state(target_id)
                            if source_state is None or target_state is None:
                                continue
                            if is_default_region(source_state.region_id):
                                continue
                            if is_default_region(target_state.region_id):
                                continue
                            try:
                                source_name = f"{source_state.label}脑区"
                                target_name = f"{target_state.label}脑区"
                                result = adapter.merge_entities(
                                    source_entities=[source_name],
                                    target_entity=target_name,
                                )
                                if isinstance(result, dict) and result.get("status") == "ok":
                                    regions_merged += 1
                                    activation_mgr.merge_region_into(source_id, target_id)
                                    logger.info(
                                        "[Consolidate] 合并脑区: %s -> %s",
                                        source_state.label, target_state.label,
                                    )
                            except Exception as e:
                                logger.debug("[Consolidate] merge_entities failed: %s", e)
            except Exception as e:
                logger.debug("[Consolidate] Merge check skipped: %s", e)

            # Step 7: Dissolve shrunk regions
            regions_dissolved = 0
            try:
                dissolved = region_mgr.dissolve_shrunk_regions(
                    shrink_threshold=REGION_CONFIG_DEFAULTS.get("shrink_threshold", 100),
                    shrink_rounds=REGION_CONFIG_DEFAULTS.get("shrink_rounds", 3),
                )
                regions_dissolved = len(dissolved)
                if dissolved and activation_mgr is not None:
                    for region_name in dissolved:
                        label = region_name.removesuffix("脑区")
                        try:
                            state = activation_mgr.find_region_by_label(label)
                            if state is not None:
                                activation_mgr.remove_region(state.region_id)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("[Consolidate] Dissolve check skipped: %s", e)

            # Step 8: Decay structural edges
            edges_disconnected = 0
            try:
                decay_result = region_mgr.decay_structural_edges()
                edges_disconnected = decay_result.get("deleted", 0)
            except Exception as e:
                logger.debug("[Consolidate] Edge decay skipped: %s", e)

            # Step 9: Invalidate cached tool-to-region mapping
            try:
                from agent.brain_tools import invalidate_tool_to_region
                invalidate_tool_to_region()
            except Exception:
                pass

            return {
                "status": "ok",
                "regions_created": len(created),
                "regions_removed": len(removed),
                "regions_drifted": len(drifted),
                "regions_merged": regions_merged,
                "regions_dissolved": regions_dissolved,
                "edges_disconnected": edges_disconnected,
                "total_regions": detection_result.total_regions,
                "modularity": round(detection_result.modularity, 4),
            }
        finally:
            sync.release_sync()

    except Exception as e:
        logger.error(f"[Brain Region API] consolidate failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/regions/{name}/members")
def get_region_members(name: str) -> dict[str, Any]:
    """Get members of a specific brain region.

    Args:
        name: Region entity name (e.g. "Python脑区")
              or label (e.g. "Python"). The "脑区" suffix is
              added automatically if missing.
    """
    try:
        # Normalize name: add suffix if missing
        if not name.endswith("脑区"):
            region_name = f"{name}脑区"
        else:
            region_name = name

        # Get members via lightrag_manager (reads 包含 edges from graph)
        from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
        members = lightrag_get_region_members(region_name)

        return {
            "status": "ok",
            "region": region_name,
            "members": members,
            "total": len(members),
        }

    except Exception as e:
        logger.error(f"[Brain Region API] get_region_members failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def brain_status():
    """Check brain graph status and ensure Niu entity exists."""
    try:
        bg = get_brain_graph()
        bg.ensure_niu_entity()
        return {"status": "ok", "message": "Brain graph is active. Niu entity ensured."}
    except Exception as e:
        return {"status": "error", "message": str(e)}