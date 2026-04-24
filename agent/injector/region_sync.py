"""
Brain Region Periodic Update Service

Runs Leiden community detection on the LightRAG knowledge graph,
creates/updates brain:region:* master nodes, and refreshes
the activation manager with new region data.

Follows the same pattern as agent/injector/lightrag_sync.py:
- Background daemon thread with configurable interval
- Status file tracking in ~/.niu/
- start_background_sync() / stop_background_sync() methods
- Initial 5-minute delay before first run
- Global singleton with thread-safe lock

M7 module: Scheduled task + integration tests + config defaults.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# ============== Configuration Defaults ==============

REGION_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "algorithm": "leiden",
    "resolution": 1.0,
    "min_graph_size": 50,
    "incremental_update": True,
    "neighbor_unfreeze_depth": 2,
    "decay_factor": 0.92,
    "activation_boost": 1.0,
    "activation_threshold": 0.3,
    "tool_reinforce_value": 0.85,
    "spillover_factor": 0.3,
    "context_budget_tokens": 4000,
    "high_activation_budget": 2000,
    "mid_activation_budget": 1200,
    "skills_budget": 400,
    "query_boost_factor": 0.3,
    "update_threshold_pct": 5,
}


class RegionSync:
    """Brain region periodic update service.

    Runs Leiden community detection on the LightRAG knowledge graph,
    creates/updates brain:region:* master nodes, and refreshes
    the activation manager with new region data.

    Runs in a background daemon thread with configurable interval.
    """

    def __init__(self, sync_interval: int = 86400) -> None:
        """Initialize RegionSync.

        Args:
            sync_interval: Seconds between sync runs (default 24 hours).
        """
        self.sync_interval = sync_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_region_sync.json"

    def run_sync(self) -> dict:
        """Execute one full sync cycle.

        Steps:
        1. Check if LightRAG is available
        2. Run CommunityDetector.detect_communities()
        3. Call RegionManager.create_region_nodes()
        4. Call RegionManager.cleanup_stale_regions()
        5. Call RegionManager.update_region_summaries() (if available)
        6. Initialize activation manager with new regions
        7. Save status to file

        Returns:
            Stats dict with counts of regions created/removed/updated.
        """
        stats: dict[str, Any] = {
            "regions_created": 0,
            "regions_removed": 0,
            "regions_updated": 0,
            "total_regions": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "modularity": 0.0,
            "errors": [],
        }

        # Step 1: Check LightRAG availability
        try:
            from niu_api.internal.lightrag_manager import get_lightrag

            rag = get_lightrag()
            if rag is None:
                logger.warning("[RegionSync] LightRAG not available, skipping sync")
                stats["errors"].append("lightrag_not_available")
                self._save_status(stats)
                return stats
        except Exception as e:
            logger.warning(f"[RegionSync] LightRAG import failed: {e}")
            stats["errors"].append(f"import: {e}")
            self._save_status(stats)
            return stats

        # Step 2: Run community detection
        detection_result = None
        try:
            detection_result = self._run_detection(stats)
        except Exception as e:
            logger.warning(f"[RegionSync] Detection unexpected error: {e}")
            stats["errors"].append(f"detection_unexpected: {e}")

        if detection_result is None:
            self._save_status(stats)
            return stats

        stats["total_regions"] = detection_result.total_regions
        stats["total_nodes"] = detection_result.total_nodes
        stats["total_edges"] = detection_result.total_edges
        stats["modularity"] = detection_result.modularity

        # Steps 3-5: Region node management
        self._manage_region_nodes(detection_result, stats)

        # Step 6: Initialize activation manager with new regions
        self._refresh_activation_manager(stats)

        # Step 7: Save status
        self._save_status(stats)

        logger.info(
            f"[RegionSync] Sync complete: "
            f"{stats['regions_created']} created, "
            f"{stats['regions_removed']} removed, "
            f"{stats['regions_updated']} updated, "
            f"{stats['total_regions']} total regions"
        )
        return stats

    def _run_detection(self, stats: dict) -> Any:
        """Run Leiden community detection.

        Args:
            stats: Stats dict to update on error.

        Returns:
            CommunityDetectionResult or None on failure.
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            from niu_api.internal.lightrag_manager import call_async
            from niu_api.internal.region_detector import CommunityDetector

            adapter = LightRAGAdapter()
            detector = CommunityDetector(adapter)
            resolution = REGION_CONFIG_DEFAULTS["resolution"]
            detection_result = call_async(detector.detect_communities(resolution=resolution))
            return detection_result
        except Exception as e:
            logger.warning(f"[RegionSync] Community detection failed: {e}")
            stats["errors"].append(f"detection: {e}")
            return None

    def _manage_region_nodes(
        self, detection_result: Any, stats: dict
    ) -> None:
        """Create, cleanup, and update region master nodes.

        Args:
            detection_result: CommunityDetectionResult from detection.
            stats: Stats dict to update.
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.lightrag_manager import call_async
            from niu_api.internal.region_manager import RegionManager

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()
            manager = RegionManager(adapter, ingester)

            # Step 3: Create region nodes
            try:
                created = call_async(manager.create_region_nodes(detection_result))
                stats["regions_created"] = len(created)
            except Exception as e:
                logger.warning(f"[RegionSync] create_region_nodes failed: {e}")
                stats["errors"].append(f"create: {e}")

            # Step 4: Cleanup stale regions
            try:
                removed = call_async(manager.cleanup_stale_regions(detection_result))
                stats["regions_removed"] = len(removed)
            except Exception as e:
                logger.warning(f"[RegionSync] cleanup_stale_regions failed: {e}")
                stats["errors"].append(f"cleanup: {e}")

            # Step 5: Update region summaries (if method exists)
            try:
                if hasattr(manager, "update_region_summaries"):
                    all_regions = call_async(manager.get_all_regions())
                    region_names = [r.name for r in all_regions]
                    call_async(manager.update_region_summaries(region_names))
                    stats["regions_updated"] = len(region_names)
            except Exception as e:
                logger.debug(f"[RegionSync] update_region_summaries skipped: {e}")

        except Exception as e:
            logger.warning(f"[RegionSync] Region management failed: {e}")
            stats["errors"].append(f"management: {e}")

    def _refresh_activation_manager(self, stats: dict) -> None:
        """Initialize activation manager with current region data.

        Args:
            stats: Stats dict (updated in place with activation stats).
        """
        try:
            from agent.brain_tools import set_activation_mgr
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.lightrag_manager import call_async
            from niu_api.internal.region_activation import RegionActivationManager
            from niu_api.internal.region_manager import RegionManager

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()
            manager = RegionManager(adapter, ingester)

            all_regions = call_async(manager.get_all_regions())

            activation_mgr = RegionActivationManager(
                decay_factor=REGION_CONFIG_DEFAULTS["decay_factor"],
                activation_threshold=REGION_CONFIG_DEFAULTS["activation_threshold"],
                spillover_factor=REGION_CONFIG_DEFAULTS["spillover_factor"],
                tool_reinforce_value=REGION_CONFIG_DEFAULTS["tool_reinforce_value"],
            )
            activation_mgr.initialize_from_regions(all_regions)
            set_activation_mgr(activation_mgr)

            logger.info(
                f"[RegionSync] Activation manager refreshed: "
                f"{len(all_regions)} regions"
            )
        except Exception as e:
            logger.warning(f"[RegionSync] Activation manager refresh failed: {e}")
            stats["errors"].append(f"activation: {e}")

    # ------------------------------------------------------------------
    # Status file I/O
    # ------------------------------------------------------------------

    def _load_status(self) -> dict:
        """Load previous sync status from file.

        Returns:
            Dict with keys: last_sync, stats, etc.
            Returns empty dict if the file does not exist or cannot be parsed.
        """
        try:
            if self._status_file.exists():
                data = json.loads(self._status_file.read_text(encoding="utf-8"))
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[RegionSync] Failed to load status: {e}")
        return {}

    def _save_status(self, stats: dict) -> None:
        """Save sync status to file.

        Args:
            stats: Sync statistics for this run.
        """
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            status = {
                "last_sync": datetime.now().isoformat(),
                "stats": stats,
            }
            self._status_file.write_text(
                json.dumps(status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[RegionSync] Failed to save status: {e}")

    # ------------------------------------------------------------------
    # Background thread management
    # ------------------------------------------------------------------

    def start_background_sync(self) -> None:
        """Start the background sync thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"[RegionSync] Started background sync "
            f"(interval: {self.sync_interval}s)"
        )

    def stop_background_sync(self) -> None:
        """Stop the background sync thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _sync_loop(self) -> None:
        """Background sync loop.

        Runs first sync after 5-minute initial delay (to let other services
        start), then repeats every sync_interval seconds.
        """
        # Initial delay: 5 minutes (wait for other services)
        self._stop_event.wait(300)
        while True:
            try:
                self.run_sync()
            except Exception as e:
                logger.error(f"[RegionSync] Sync loop error: {e}")
            if self._stop_event.wait(self.sync_interval):
                break


# Global instance + thread-safe lock
_region_sync: Optional[RegionSync] = None
_region_sync_lock = threading.Lock()


def get_region_sync(
    sync_interval: int = 86400,
    auto_start: bool = False,
) -> RegionSync:
    """Get the global RegionSync instance.

    Args:
        sync_interval: Seconds between sync runs (default 24 hours).
        auto_start: Whether to start the background thread on first call.

    Returns:
        The global RegionSync singleton.
    """
    global _region_sync
    with _region_sync_lock:
        if _region_sync is None:
            _region_sync = RegionSync(sync_interval)
            should_start = auto_start
        else:
            should_start = False
    if should_start:
        _region_sync.start_background_sync()
    return _region_sync