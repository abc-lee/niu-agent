"""
Brain Region API endpoints — Region management and consolidation.

Provides REST API for querying brain region states, triggering
community detection, and inspecting region membership.

Routes:
    GET  /api/brain/regions             — list all regions with activation states
    POST /api/brain/regions/consolidate — trigger community detection
    GET  /api/brain/regions/{name}/members — get region members

NOTE: The existing brain_api.py provides /api/brain/remember, /api/brain/recall,
and /api/brain/status for memory operations. This router adds region-specific
endpoints under the same /api/brain prefix.

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

    Steps:
    1. Run Leiden community detection on the LightRAG graph
    2. Create/update xxx脑区 master nodes via RegionManager
    3. Clean up stale regions
    4. Initialize activation manager with new regions
    """
    try:
        from niu_api.internal.region_detector import CommunityDetector
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        detector = CommunityDetector(adapter)

        # Step 1: Detect communities (sync method, no await needed)
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

        # Step 2: Create region master nodes (sync — no call_async)
        region_mgr = _get_region_mgr()
        created = region_mgr.create_region_nodes(detection_result)

        # Step 3: Clean up stale regions (sync — no call_async)
        removed = region_mgr.cleanup_stale_regions(detection_result)

        # Step 4: Initialize activation manager with new regions
        activation_mgr = _get_activation_mgr()
        if activation_mgr is not None:
            regions = region_mgr.get_all_regions()
            # BUG 2 fix: Fetch members for each region
            for region in regions:
                try:
                    region.members = region_mgr.get_region_members(region.name)
                except Exception as exc:
                    logger.warning("Failed to fetch members for region %s: %s", region.name, exc)
            activation_mgr.initialize_from_regions(regions)
            # BUG 3 fix: Build neighbor map for spillover based on shared members
            from niu_api.internal.region_neighbors import build_neighbor_map
            neighbor_map = build_neighbor_map([
                {"community_id": r.community_id, "members": r.members}
                for r in regions
            ])
            activation_mgr.set_region_neighbors(neighbor_map)
            logger.info("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))

        return {
            "status": "ok",
            "regions_created": len(created),
            "regions_removed": len(removed),
            "total_regions": detection_result.total_regions,
            "modularity": round(detection_result.modularity, 4),
        }

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
        region_mgr = _get_region_mgr()

        # Normalize name: add suffix if missing (support both new and old format)
        if name.startswith("brain:region:"):
            # Backward compat: read old-format names as-is
            region_name = name
        elif not name.endswith("脑区"):
            region_name = f"{name}脑区"
        else:
            region_name = name

        # Get members via RegionManager (sync — no call_async)
        members = region_mgr.get_region_members(region_name)

        return {
            "status": "ok",
            "region": region_name,
            "members": members,
            "total": len(members),
        }

    except Exception as e:
        logger.error(f"[Brain Region API] get_region_members failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))