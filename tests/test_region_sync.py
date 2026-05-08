"""
Tests for agent/injector/region_sync.py — Brain Region Periodic Update Service.

Validates RegionSync:
1. run_sync with LightRAG unavailable returns error stats
2. run_sync with community detection failure returns error stats
3. start/stop background sync thread
4. status file save/load round-trip
5. get_region_sync singleton accessor
6. REGION_CONFIG_DEFAULTS structure
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.injector.region_sync import (
    REGION_CONFIG_DEFAULTS,
    RegionSync,
    get_region_sync,
)


# ============== Test 1: run_sync with LightRAG unavailable ==============


class TestRunSyncLightRAGUnavailable:
    """Verify run_sync handles LightRAG not available gracefully."""

    def test_lightrag_none_returns_error(self) -> None:
        """When get_lightrag() returns None, stats contain error."""
        sync = RegionSync(sync_interval=86400)

        with patch(
            "agent.injector.region_sync.RegionSync._save_status"
        ):
            with patch(
                "agent.injector.region_sync.get_lightrag",
                return_value=None,
            ):
                stats = sync.run_sync()

        assert "lightrag_not_available" in stats["errors"]
        assert stats["regions_created"] == 0
        assert stats["regions_removed"] == 0

    def test_lightrag_import_error_returns_error(self) -> None:
        """When LightRAG import fails, stats contain import error."""
        sync = RegionSync(sync_interval=86400)

        with patch(
            "agent.injector.region_sync.RegionSync._save_status"
        ):
            # Simulate LightRAG call failure
            with patch(
                "agent.injector.region_sync.get_lightrag",
                side_effect=ImportError("no lightrag"),
            ):
                stats = sync.run_sync()

        assert len(stats["errors"]) > 0
        assert "lightrag_check" in stats["errors"][0]


# ============== Test 2: run_sync with detection failure ==============


class TestRunSyncDetectionFailure:
    """Verify run_sync handles community detection failure gracefully."""

    def test_detection_failure_returns_error(self) -> None:
        """When community detection fails, stats contain detection error."""
        sync = RegionSync(sync_interval=86400)

        with patch(
            "agent.injector.region_sync.RegionSync._save_status"
        ):
            with patch(
                "agent.injector.region_sync.get_lightrag",
                return_value=MagicMock(),
            ):
                with patch(
                    "agent.injector.region_sync.RegionSync._run_detection",
                    return_value=None,
                ):
                    stats = sync.run_sync()

        assert stats["regions_created"] == 0
        # Detection returned None, so no regions were processed
        assert stats["total_regions"] == 0

    def test_detection_exception_returns_error(self) -> None:
        """When detection raises unexpectedly, stats contain error."""
        sync = RegionSync(sync_interval=86400)

        with patch(
            "agent.injector.region_sync.RegionSync._save_status"
        ):
            with patch(
                "agent.injector.region_sync.get_lightrag",
                return_value=MagicMock(),
            ):
                with patch(
                    "agent.injector.region_sync.RegionSync._run_detection",
                    side_effect=RuntimeError("detection crashed"),
                ):
                    stats = sync.run_sync()

        assert stats["regions_created"] == 0
        assert any("detection_unexpected" in e for e in stats["errors"])


# ============== Test 3: start/stop background sync ==============


class TestStartStopBackgroundSync:
    """Verify background sync thread lifecycle."""

    def test_start_creates_thread(self) -> None:
        """start_background_sync creates a daemon thread."""
        sync = RegionSync(sync_interval=86400)
        sync.start_background_sync()

        assert sync._thread is not None
        assert sync._thread.is_alive()
        assert sync._thread.daemon is True

        sync.stop_background_sync()

    def test_stop_sets_stop_event(self) -> None:
        """stop_background_sync sets the stop event."""
        sync = RegionSync(sync_interval=86400)

        sync.start_background_sync()
        assert not sync._stop_event.is_set()

        sync.stop_background_sync()
        assert sync._stop_event.is_set()

    def test_start_idempotent(self) -> None:
        """Calling start_background_sync twice does not create a second thread."""
        sync = RegionSync(sync_interval=86400)

        sync.start_background_sync()
        thread1 = sync._thread

        sync.start_background_sync()
        thread2 = sync._thread

        assert thread1 is thread2

        sync.stop_background_sync()


# ============== Test 4: status file save/load ==============


class TestStatusFileIO:
    """Verify status file save and load round-trip."""

    def test_save_and_load_status(self, tmp_path: Path) -> None:
        """Save then load preserves last_sync and stats."""
        sync = RegionSync(sync_interval=86400)
        sync._status_file = tmp_path / "test_region_sync.json"

        stats = {
            "regions_created": 3,
            "regions_removed": 1,
            "regions_updated": 2,
            "total_regions": 5,
            "total_nodes": 100,
            "total_edges": 200,
            "modularity": 0.45,
            "errors": [],
        }
        sync._save_status(stats)

        loaded = sync._load_status()
        assert loaded["stats"]["regions_created"] == 3
        assert loaded["stats"]["total_regions"] == 5
        assert "last_sync" in loaded

    def test_load_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        """Loading from nonexistent file returns empty dict."""
        sync = RegionSync(sync_interval=86400)
        sync._status_file = tmp_path / "nonexistent.json"

        loaded = sync._load_status()
        assert loaded == {}

    def test_load_corrupt_returns_empty(self, tmp_path: Path) -> None:
        """Loading from corrupt file returns empty dict."""
        sync = RegionSync(sync_interval=86400)
        sync._status_file = tmp_path / "corrupt.json"
        sync._status_file.write_text("not valid json{{{")

        loaded = sync._load_status()
        assert loaded == {}


# ============== Test 5: get_region_sync singleton ==============


class TestGetRegionSync:
    """Verify get_region_sync singleton accessor."""

    def test_returns_region_sync_instance(self) -> None:
        """get_region_sync returns a RegionSync instance."""
        # Reset global singleton
        import agent.injector.region_sync as mod
        mod._region_sync = None

        sync = get_region_sync()
        assert isinstance(sync, RegionSync)

    def test_returns_same_instance(self) -> None:
        """Multiple calls return the same instance."""
        import agent.injector.region_sync as mod
        mod._region_sync = None

        sync1 = get_region_sync()
        sync2 = get_region_sync()
        assert sync1 is sync2

    def test_custom_interval(self) -> None:
        """Custom interval is passed to the instance."""
        import agent.injector.region_sync as mod
        mod._region_sync = None

        sync = get_region_sync(sync_interval=3600)
        assert sync.sync_interval == 3600


# ============== Test 6: REGION_CONFIG_DEFAULTS structure ==============


class TestRegionConfigDefaults:
    """Verify REGION_CONFIG_DEFAULTS has expected keys and types."""

    def test_has_all_required_keys(self) -> None:
        """All expected configuration keys are present."""
        required_keys = [
            "enabled",
            "algorithm",
            "resolution",
            "min_graph_size",
            "incremental_update",
            "neighbor_unfreeze_depth",
            "decay_factor",
            "activation_boost",
            "activation_threshold",
            "tool_reinforce_value",
            "spillover_factor",
            "context_budget_tokens",
            "high_activation_budget",
            "mid_activation_budget",
            "skills_budget",
            "query_boost_factor",
            "update_threshold_pct",
        ]
        for key in required_keys:
            assert key in REGION_CONFIG_DEFAULTS, f"Missing key: {key}"

    def test_values_are_valid(self) -> None:
        """Default values are within expected ranges."""
        assert REGION_CONFIG_DEFAULTS["enabled"] is True
        assert REGION_CONFIG_DEFAULTS["algorithm"] == "leiden"
        assert 0.0 < REGION_CONFIG_DEFAULTS["resolution"] <= 2.0
        assert 0.0 < REGION_CONFIG_DEFAULTS["decay_factor"] < 1.0
        assert 0.0 < REGION_CONFIG_DEFAULTS["activation_threshold"] < 1.0
        assert 0.0 < REGION_CONFIG_DEFAULTS["tool_reinforce_value"] <= 1.0
        assert 0.0 < REGION_CONFIG_DEFAULTS["spillover_factor"] <= 1.0