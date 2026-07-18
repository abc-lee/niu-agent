"""
Brain Region Periodic Update Service

Runs Leiden community detection on the LightRAG knowledge graph,
creates/updates region master nodes (natural language names like "聊天历史脑区"), and refreshes
the activation manager with new region data.

Follows the same pattern as agent/injector/lightrag_sync.py:
- Background daemon thread with configurable interval
- Status file tracking in ~/.niu/
- start_background_sync() / stop_background_sync() methods
- Polling readiness check before first run (5s intervals, 180s max)
- Global singleton with thread-safe lock

M7 module: Scheduled task + integration tests + config defaults.
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from niu_api.internal.lightrag_manager import get_lightrag, wait_lightrag_ready

# ============== Configuration Defaults ==============

REGION_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "algorithm": "leiden",
    "resolution": 1.0,
    "min_graph_size": 50,
    "min_community_size": 100,
    "incremental_update": True,
    "co_activation_threshold": 0.9,
    "shrink_threshold": 10,  # 成员数 < 10 才判萎缩（原 100 误判正常小脑区）
    "shrink_rounds": 3,
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
    creates/updates region master nodes (natural language names like "聊天历史脑区"), and refreshes
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
        self._brain_ready = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_region_sync.json"
        self._sync_lock = threading.Lock()

    def try_acquire_sync(self) -> bool:
        """Try to acquire the sync lock (non-blocking). Prevents concurrent sync."""
        return self._sync_lock.acquire(blocking=False)

    def release_sync(self) -> None:
        """Release the sync lock."""
        self._sync_lock.release()

    def run_sync(self) -> dict:
        """Execute one full sync cycle with mutex protection.

        Acquires a non-blocking lock to prevent concurrent sync runs
        (e.g. API-triggered consolidate vs background timer sync).
        If the lock cannot be acquired, returns immediately with a skip indicator.

        Returns:
            Stats dict with counts of regions created/removed/updated.
        """
        if not self.try_acquire_sync():
            logger.warning("[RegionSync] 另一个同步正在运行，跳过本次")
            return {"regions_created": 0, "regions_removed": 0, "errors": ["skipped: concurrent sync"]}
        try:
            return self._run_sync_impl()
        finally:
            self.release_sync()

    def _run_sync_impl(self) -> dict:
        """Actual sync logic — original run_sync body.

        Steps:
        1. Check if LightRAG is available
        2. Run CommunityDetector.detect_communities()
        3. Call RegionManager.create_region_nodes()
        4. Call RegionManager.cleanup_stale_regions()
        5. Call RegionManager.update_region_summaries() (if available)
        6. Initialize activation manager with new regions
        7. Save status to file
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
            rag = get_lightrag()
            if rag is None:
                logger.warning("[RegionSync] LightRAG not available, skipping sync")
                stats["errors"].append("lightrag_not_available")
                self._save_status(stats)
                return stats
        except Exception as e:
            logger.warning(f"[RegionSync] LightRAG availability check failed: {e}")
            stats["errors"].append(f"lightrag_check: {e}")
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
            # Still refresh activation manager even when detection is skipped
            # (default brain regions exist independently of community detection)
            self._refresh_activation_manager(stats)
            self._save_status(stats)
            return stats

        # BUG 5 fix: Check stop event between steps
        if self._stop_event.is_set():
            self._save_status(stats)
            return stats

        stats["total_regions"] = detection_result.total_regions
        stats["total_nodes"] = detection_result.total_nodes
        stats["total_edges"] = detection_result.total_edges
        stats["modularity"] = detection_result.modularity

        # Steps 3-5: Region node management
        self._manage_region_nodes(detection_result, stats)

        # BUG 5 fix: Check stop event between steps
        if self._stop_event.is_set():
            self._save_status(stats)
            return stats

        # Step 6: Initialize activation manager with new regions
        self._refresh_activation_manager(stats)

        # Step 7: Merge co-activated regions + dissolve shrunk regions
        self._merge_and_dissolve(stats)

        # Step 8: Save status
        self._save_status(stats)

        # Invalidate cached tool-to-region mapping so it will be lazily rebuilt
        # with current region structure (regions may have been merged/dissolved)
        try:
            from agent.brain_tools import invalidate_tool_to_region
            invalidate_tool_to_region()
        except Exception:
            pass

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
            from niu_api.internal.region_detector import CommunityDetector

            adapter = LightRAGAdapter()
            detector = CommunityDetector(adapter)
            resolution = REGION_CONFIG_DEFAULTS["resolution"]
            min_graph_size = REGION_CONFIG_DEFAULTS.get("min_graph_size", 50)
            min_community_size = REGION_CONFIG_DEFAULTS.get("min_community_size", 100)
            # detect_communities is sync — no call_async needed
            detection_result = detector.detect_communities(
                resolution=resolution,
                min_graph_size=min_graph_size,
                min_community_size=min_community_size,
            )
            return detection_result
        except Exception as e:
            logger.warning(f"[RegionSync] Community detection failed: {e}")
            stats["errors"].append(f"detection: {e}")
            return None

    def _manage_region_nodes(
        self, detection_result: Any, stats: dict
    ) -> None:
        """Create, cleanup, and update region master nodes.

        Uses dry_run two-phase pattern to solve D-13 non-atomicity:
        1. cleanup_stale_regions(dry_run=True) — detect only
        2. create_region_nodes — create new regions
        3. cleanup_stale_regions(dry_run=False) — execute cleanup
        This ensures old regions are only deleted after new ones exist.

        Args:
            detection_result: CommunityDetectionResult from detection.
            stats: Stats dict to update.
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_manager import RegionManager

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()
            manager = RegionManager(adapter, ingester)

            # Step 3a: Detect stale and drifted regions (no execution)
            cleanup_ok = True
            try:
                removed, drifted, drifted_cids = manager.cleanup_stale_regions(
                    detection_result, dry_run=True,
                )
            except Exception as e:
                logger.warning(f"[RegionSync] cleanup detection failed: {e}")
                removed, drifted, drifted_cids = [], [], set()
                cleanup_ok = False

            # Step 4: Create region nodes (skip drifted community partitions)
            created: list[str] = []
            create_ok = True
            try:
                created = manager.create_region_nodes(detection_result, skip_community_ids=drifted_cids)
                stats["regions_created"] = len(created)
            except Exception as e:
                logger.warning(f"[RegionSync] create_region_nodes failed: {e}")
                stats["errors"].append(f"create: {e}")
                create_ok = False

            # Step 4.5: Assign existing entities to default brain regions
            try:
                from niu_api.internal.region_manager import assign_entities_to_default_regions
                result = assign_entities_to_default_regions(adapter)
                assigned = result.get("assigned", 0)
                if assigned > 0:
                    logger.info(f"[RegionSync] Assigned {assigned} entities to default regions")
                    stats["entities_assigned"] = assigned
            except Exception as e:
                logger.debug(f"[RegionSync] assign_entities_to_default_regions skipped: {e}")

            # Step 3b: Execute cleanup only if create didn't throw and dry_run succeeded
            # create_ok=True but created=[] is normal (all regions exist), still run cleanup
            # create_ok=False means create threw exception, preserve old regions
            actual_removed: list[str] = []
            actual_drifted: list[str] = []
            if (create_ok or not detection_result.partitions) and cleanup_ok:
                try:
                    actual_removed, actual_drifted, _ = manager.cleanup_stale_regions(
                        detection_result, dry_run=False,
                    )
                    stats["regions_removed"] = len(actual_removed)
                except Exception as e:
                    logger.warning(f"[RegionSync] cleanup execution failed: {e}")
                    stats["errors"].append(f"cleanup: {e}")
            elif not cleanup_ok:
                logger.warning("[RegionSync] dry_run 失败，跳过 cleanup 执行避免重复创建")
            else:
                logger.warning("[RegionSync] create_region_nodes 异常，保留旧脑区避免数据丢失")

            # Step 5: Update region summaries (exclude created and drifted)
            # created regions have accurate summaries, drifted regions updated by _update_drifted_regions
            try:
                if hasattr(manager, "update_region_summaries"):
                    all_regions = manager.get_all_regions()
                    created_set = set(created)
                    drifted_set = set(actual_drifted) if cleanup_ok else set()
                    region_names = [r.name for r in all_regions
                                    if r.name not in created_set and r.name not in drifted_set]
                    manager.update_region_summaries(region_names)
                    stats["regions_updated"] = len(region_names)
            except Exception as e:
                logger.debug(f"[RegionSync] update_region_summaries skipped: {e}")

            # Step 6: Decay structural edges
            try:
                disconnected = manager.decay_structural_edges()
                if disconnected.get("deleted", 0) > 0 or disconnected.get("decayed", 0) > 0:
                    stats["edges_disconnected"] = disconnected.get("deleted", 0)
                    logger.info(f"[RegionSync] 衰减结果: {disconnected}")
            except Exception as e:
                logger.debug(f"[RegionSync] Edge decay skipped: {e}")

        except Exception as e:
            logger.warning(f"[RegionSync] Region management failed: {e}")
            stats["errors"].append(f"management: {e}")

    def _refresh_activation_manager(self, stats: dict) -> None:
        """Initialize activation manager with current region data.

        After getting all regions, fetch members for each region so that
        _entity_to_region is properly populated (BUG 2 fix). Also set
        the neighbor map for spillover activation (BUG 3 fix).

        Args:
            stats: Stats dict (updated in place with activation stats).
        """
        try:
            from agent.brain_tools import get_activation_mgr, set_activation_mgr
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_activation import RegionActivationManager
            from niu_api.internal.region_manager import RegionManager

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()
            manager = RegionManager(adapter, ingester)

            all_regions = manager.get_all_regions()

            # D-12 fix: Empty list means read failure or graph unavailable.
            # Don't overwrite existing activation state with empty list.
            if not all_regions:
                logger.warning("[RegionSync] get_all_regions 返回空，跳过激活管理器刷新")
                return

            # 批量读取所有脑区的成员（一次性调 get_all_region_members）
            # 避免循环逐个调用时单 region 异常污染整个 _entity_to_region
            from niu_api.internal.lightrag_manager import get_all_region_members as lightrag_get_all_region_members
            try:
                region_members_map = lightrag_get_all_region_members()
            except Exception as e:
                logger.warning(
                    "[RegionSync] get_all_region_members 批量读取异常，跳过激活管理器刷新: %s",
                    e,
                )
                stats["errors"].append(f"get_all_region_members: {e}")
                return

            # 批量读取返回空 = 图未就绪或读取失败，不覆盖现有映射
            if not region_members_map:
                logger.warning(
                    "[RegionSync] get_all_region_members 返回空（图未就绪或读取失败），跳过激活管理器刷新"
                )
                return

            # 把成员填充到 region 对象上（缺失的 region 保持空 list）
            for region in all_regions:
                region.members = region_members_map.get(region.name, [])

            # Reuse existing activation manager to preserve co-activation state
            # (creating a new one each cycle would discard _co_activation_counts)
            existing_mgr = get_activation_mgr()
            if existing_mgr is not None:
                activation_mgr = existing_mgr
            else:
                activation_mgr = RegionActivationManager(
                    decay_factor=REGION_CONFIG_DEFAULTS["decay_factor"],
                    activation_threshold=REGION_CONFIG_DEFAULTS["activation_threshold"],
                    spillover_factor=REGION_CONFIG_DEFAULTS["spillover_factor"],
                    tool_reinforce_value=REGION_CONFIG_DEFAULTS["tool_reinforce_value"],
                )
            activation_mgr.initialize_from_regions(all_regions)

            # BUG 3 fix: Build neighbor map for spillover based on shared members
            from niu_api.internal.region_neighbors import build_neighbor_map
            neighbor_map = build_neighbor_map([
                {"community_id": r.community_id or r.name, "members": r.members}
                for r in all_regions
            ])
            activation_mgr.set_region_neighbors(neighbor_map)
            logger.debug(
                f"[RegionSync] Neighbor map set: {len(neighbor_map)} regions have neighbors"
            )

            set_activation_mgr(activation_mgr)

            logger.info(
                f"[RegionSync] Activation manager refreshed: "
                f"{len(all_regions)} regions"
            )
        except Exception as e:
            logger.error(
                "[RegionSync] Activation manager refresh failed: %s\n%s",
                e, traceback.format_exc(),
            )
            stats["errors"].append(f"activation: {e}")

    def refresh_entity_mapping_only(self) -> None:
        """Lightweight refresh: only update entity-to-region mapping and type counts.

        Does NOT run community detection, create/remove regions, or merge/dissolve.
        Much cheaper than run_sync() — intended for calling after ingest completes.

        Safe to call from any thread (uses RLock internally).
        """
        try:
            from agent.brain_tools import get_activation_mgr

            activation_mgr = get_activation_mgr()
            if activation_mgr is not None:
                activation_mgr.refresh_entity_mapping()
                logger.info("[RegionSync] Entity mapping refreshed (lightweight)")
            else:
                logger.debug("[RegionSync] No activation manager, skipping entity mapping refresh")
        except Exception as e:
            logger.warning(f"[RegionSync] Entity mapping refresh failed: {e}")

    # ------------------------------------------------------------------
    # Merge + dissolve
    # ------------------------------------------------------------------

    def _merge_and_dissolve(self, stats: dict) -> None:
        """Check for merge candidates (co-activation) and dissolve shrunk regions.

        Args:
            stats: Stats dict to update.
        """
        # Step 7a: Merge co-activated regions
        try:
            from agent.brain_tools import get_activation_mgr
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            from niu_api.internal.region_manager import is_default_region

            activation_mgr = get_activation_mgr()
            if activation_mgr is not None:
                candidates = activation_mgr.get_merge_candidates(
                    co_activation_threshold=REGION_CONFIG_DEFAULTS.get("co_activation_threshold", 0.9),
                )
                if candidates:
                    adapter = LightRAGAdapter()
                    merged_count = 0
                    for source_id, target_id in candidates:
                        # Find region names from activation manager
                        source_state = activation_mgr.get_region_state(source_id)
                        target_state = activation_mgr.get_region_state(target_id)
                        if source_state is None or target_state is None:
                            continue

                        # Protect default brain regions (defined in preferences.json)
                        if is_default_region(source_state.region_id):
                            logger.debug(f"[RegionSync] 跳过预置脑区合并: {source_state.label}")
                            continue
                        if is_default_region(target_state.region_id):
                            logger.debug(f"[RegionSync] 跳过预置脑区作为合并目标: {target_state.label}")
                            continue

                        # Merge KG nodes via adapter — use full region names, not labels
                        try:
                            source_name = f"{source_state.label}脑区"
                            target_name = f"{target_state.label}脑区"
                            result = adapter.merge_entities(
                                source_entities=[source_name],
                                target_entity=target_name,
                            )
                            if isinstance(result, dict) and result.get("status") == "ok":
                                merged_count += 1
                                # Transfer source members to target, then remove source
                                activation_mgr.merge_region_into(source_id, target_id)
                                logger.info(
                                    "[RegionSync] 合并脑区: %s -> %s",
                                    source_state.label, target_state.label,
                                )
                        except Exception as e:
                            logger.debug(f"[RegionSync] merge_entities failed: {e}")

                    stats["regions_merged"] = merged_count
        except Exception as e:
            logger.warning(f"[RegionSync] Merge check failed: {e}")

        # Step 7b: Dissolve shrunk regions
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_manager import RegionManager

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()
            manager = RegionManager(adapter, ingester)

            dissolved = manager.dissolve_shrunk_regions(
                shrink_threshold=REGION_CONFIG_DEFAULTS.get("shrink_threshold", 100),
                shrink_rounds=REGION_CONFIG_DEFAULTS.get("shrink_rounds", 3),
            )
            stats["regions_dissolved"] = len(dissolved)

            # Remove dissolved regions from activation manager
            if dissolved:
                try:
                    from agent.brain_tools import get_activation_mgr
                    activation_mgr = get_activation_mgr()
                    if activation_mgr is not None:
                        for region_name in dissolved:
                            # Derive region_id from region_name ({label}脑区)
                            label = region_name.removesuffix("脑区")
                            region_id = self._label_to_region_id(activation_mgr, label)
                            if region_id:
                                activation_mgr.remove_region(region_id)
                except Exception as e:
                    logger.debug(f"[RegionSync] Activation cleanup after dissolve: {e}")
        except Exception as e:
            logger.warning(f"[RegionSync] Dissolve check failed: {e}")

    @staticmethod
    def _label_to_region_id(activation_mgr: Any, label: str) -> str | None:
        """Look up region_id from label via activation manager's label index."""
        try:
            state = activation_mgr.find_region_by_label(label)
            if state is not None:
                return state.region_id
        except Exception:
            pass
        return None

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

    def signal_brain_ready(self) -> None:
        """Signal that brain region initialization is complete.

        Called from the main thread after create_default_regions() finishes,
        so that _sync_loop knows the brain region nodes exist before running
        its first sync (which calls _refresh_activation_manager).
        """
        self._brain_ready.set()
        logger.info("[RegionSync] Brain regions ready signal received")

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

    def stop_background_sync_blocking(self, timeout: float = 60.0) -> None:
        """v9：硬停止 RegionSync 守护线程，等待 join 完成。

        跟 stop_background_sync 的区别：
        - stop_background_sync：join(timeout=5)，超时静默返回，线程可能仍在跑
          （in-flight sync 继续写 GraphML）
        - stop_background_sync_blocking：join(timeout=60)，超时抛 RuntimeError

        用途：repair_all 启动前调用，确保 RegionSync 线程完全退出（覆盖 30+ 秒 sync 场景），
              避免线程在 repair 期间写真相源（违反铁律 2，见
              lightrag-graphml-written-by-regionsync.md）。

        Args:
            timeout: join 超时秒数（默认 60，覆盖单次 sync 30+ 秒场景）

        Raises:
            RuntimeError: join 超时后线程仍存活
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError(
                    f"[RegionSync] stop_background_sync_blocking 超时 {timeout}s 线程仍存活，"
                    f"repair 中止避免 GraphML 被写（铁律 2）"
                )

    def _sync_loop(self) -> None:
        """Background sync loop.

        Waits for LightRAG readiness (up to 30s), then waits for brain region
        initialization signal (up to 60s), then runs first sync, then repeats
        every sync_interval seconds.
        """
        # Wait for LightRAG readiness signal instead of fixed delay.
        # If LightRAG init succeeds quickly, we start immediately;
        # if it fails or takes longer, we wait up to 30s then proceed.
        if not wait_lightrag_ready(timeout=30):
            # Timeout — try to trigger init ourselves
            from niu_api.internal.lightrag_manager import get_lightrag
            rag = get_lightrag()
            if rag is None:
                logger.warning("[RegionSync] LightRAG not available after 30s, attempting first sync anyway")
            else:
                logger.info("[RegionSync] LightRAG initialized on retry")

        # Wait for brain region initialization to complete before first sync.
        # This prevents the race where _refresh_activation_manager() calls
        # manager.get_all_regions() before create_default_regions() has finished.
        if not self._brain_ready.wait(timeout=60):
            logger.warning("[RegionSync] Brain region init not signaled after 60s, proceeding anyway")

        # 跨进程 24h 间隔持久化：读 status file，若距上次同步不足 sync_interval*0.9 则等待
        # 避免"每次重启都触发首次同步"——24h 间隔不仅在进程内生效，跨重启也生效
        try:
            status = self._load_status()
            last_sync_str = status.get("last_sync") if status else None
            if last_sync_str:
                try:
                    last_sync = datetime.fromisoformat(last_sync_str)
                    elapsed = (datetime.now() - last_sync).total_seconds()
                    # 系统时间回拨保护：elapsed < 0 视为 0，立即跑首次同步
                    if elapsed < 0:
                        logger.warning(
                            "[RegionSync] last_sync 是未来时间（系统时间回拨？），立即首次同步"
                        )
                        elapsed = 0
                    min_interval = self.sync_interval * 0.9  # 10% 容差
                    # elapsed=0 时不进等待分支（立即跑首次同步）
                    # 仅当 0 < elapsed < min_interval 时才等待剩余时间
                    if 0 < elapsed < min_interval:
                        wait_seconds = min_interval - elapsed
                        logger.info(
                            f"[RegionSync] 距上次同步 {elapsed:.0f} 秒，不足 {min_interval:.0f} 秒，等待 {wait_seconds:.0f} 秒后再首次同步"
                        )
                        if self._stop_event.wait(timeout=wait_seconds):
                            return  # 收到 stop 信号，退出
                except (ValueError, TypeError) as e:
                    logger.warning(f"[RegionSync] 解析 last_sync 失败，立即首次同步: {e}")
        except Exception as e:
            logger.warning(f"[RegionSync] 读 status file 失败，立即首次同步: {e}")

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