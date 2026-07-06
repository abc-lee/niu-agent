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


# ============== Test 7: _refresh_activation_manager 失败保护 ==============


def test_refresh_activation_manager_does_not_overwrite_on_bulk_read_failure(monkeypatch):
    """get_all_region_members 返回空（读取失败）时，不应覆盖现有 _entity_to_region 映射"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock

    sync = RegionSync(sync_interval=86400)

    # 构造已有激活管理器
    fake_existing_mgr = mock.MagicMock()
    fake_existing_mgr._entity_to_region = {"existing_entity": "existing_region脑区"}
    fake_existing_mgr._member_counts = {"existing_region脑区": 1}

    # 构造 fake region（用 spec 避免 MagicMock name 特殊参数问题）
    fake_region = mock.MagicMock()
    fake_region.name = "智家脑区"
    fake_region.members = []
    fake_region.description = "d1"

    with mock.patch("agent.brain_tools.get_activation_mgr", return_value=fake_existing_mgr), \
         mock.patch("agent.brain_tools.set_activation_mgr") as mock_set, \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGIngester"), \
         mock.patch(
             "niu_api.internal.region_manager.RegionManager.get_all_regions",
             return_value=[fake_region],
         ), \
         mock.patch(
             "niu_api.internal.lightrag_manager.get_all_region_members",
             return_value={},  # 空 dict 模拟读取失败
         ):
        sync._refresh_activation_manager({})

    # 断言：early return，initialize_from_regions 没被调用
    fake_existing_mgr.initialize_from_regions.assert_not_called()
    # set_activation_mgr 也不应被调用（early return 前不设置）
    mock_set.assert_not_called()


def test_refresh_activation_manager_skips_when_coverage_too_low(monkeypatch):
    """get_all_region_members 返回部分脑区（覆盖率 < 50%）时，不覆盖现有映射"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock

    sync = RegionSync(sync_interval=86400)

    fake_existing_mgr = mock.MagicMock()

    # 3 个脑区，但 get_all_region_members 只返回 1 个（覆盖率 33% < 50%）
    fake_regions = []
    for name in ["智家脑区", "工作脑区", "聊天脑区"]:
        r = mock.MagicMock()
        r.name = name
        r.members = []
        r.description = "d"
        fake_regions.append(r)

    with mock.patch("agent.brain_tools.get_activation_mgr", return_value=fake_existing_mgr), \
         mock.patch("agent.brain_tools.set_activation_mgr"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter"), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGIngester"), \
         mock.patch(
             "niu_api.internal.region_manager.RegionManager.get_all_regions",
             return_value=fake_regions,
         ), \
         mock.patch(
             "niu_api.internal.lightrag_manager.get_all_region_members",
             return_value={"智家脑区": ["实体1"]},  # 只返回 1/3 脑区
         ):
        sync._refresh_activation_manager({})

    fake_existing_mgr.initialize_from_regions.assert_not_called()


def test_sync_loop_skips_first_sync_when_recently_synced(tmp_path):
    """距上次同步不足 sync_interval*0.9 时，_sync_loop 跳过首次同步"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock
    from datetime import datetime, timedelta
    import json

    sync = RegionSync(sync_interval=86400)
    sync._status_file = tmp_path / "last_region_sync.json"

    recent_time = (datetime.now() - timedelta(minutes=5)).isoformat()
    sync._status_file.write_text(json.dumps({
        "last_sync": recent_time,
        "stats": {"regions_created": 0},
    }))

    run_sync_called = []
    sync.run_sync = mock.Mock(side_effect=lambda: run_sync_called.append(True))

    # 用真实 threading.Event，通过 set 控制退出
    sync._brain_ready.set()
    sync._stop_event.set()  # 让所有 wait 立即返回 True，循环跑一次就退出

    with mock.patch(
        "agent.injector.region_sync.wait_lightrag_ready", return_value=True
    ):
        sync._sync_loop()

    # 断言：run_sync 没被调用（距上次同步 5 分钟 < 24h*0.9）
    # _stop_event 已 set，所以 _stop_event.wait(wait_seconds) 立即返回 True，
    # 然后 while True 里 _stop_event.wait(sync_interval) 也立即返回 True 退出
    assert len(run_sync_called) == 0, "距上次同步不足 24h，不应触发 run_sync"


def test_sync_loop_handles_future_last_sync(tmp_path):
    """last_sync 是未来时间（系统回拨）时，不卡住等待"""
    from agent.injector.region_sync import RegionSync
    from unittest import mock
    from datetime import datetime, timedelta
    import json

    sync = RegionSync(sync_interval=86400)
    sync._status_file = tmp_path / "last_region_sync.json"

    # last_sync 是 1 天后的未来时间
    future_time = (datetime.now() + timedelta(days=1)).isoformat()
    sync._status_file.write_text(json.dumps({
        "last_sync": future_time,
        "stats": {},
    }))

    run_sync_called = []
    sync.run_sync = mock.Mock(side_effect=lambda: run_sync_called.append(True))

    sync._brain_ready.set()
    sync._stop_event.set()  # 立即退出

    with mock.patch(
        "agent.injector.region_sync.wait_lightrag_ready", return_value=True
    ):
        sync._sync_loop()

    # 断言：run_sync 应该被调用（未来时间应被视为 elapsed=0，立即跑首次同步）
    assert len(run_sync_called) >= 1, "未来时间应被视为 elapsed<=0，立即跑首次同步"


def test_merge_and_dissolve_logs_warning_on_dissolve_exception(monkeypatch):
    """dissolve 异常应被 logger.warning 记录，不是 logger.debug"""
    from agent.injector import region_sync
    from unittest import mock

    # 拦截 loguru logger 的 warning/debug 调用
    warning_calls = []
    debug_calls = []
    monkeypatch.setattr(
        region_sync.logger,
        "warning",
        lambda *args, **kwargs: warning_calls.append(args[0] if args else None),
    )
    monkeypatch.setattr(
        region_sync.logger,
        "debug",
        lambda *args, **kwargs: debug_calls.append(args[0] if args else None),
    )

    sync = region_sync.RegionSync(sync_interval=86400)

    with mock.patch(
        "niu_api.internal.region_manager.RegionManager.dissolve_shrunk_regions",
        side_effect=RuntimeError("test dissolve failure"),
    ), mock.patch(
        "niu_api.internal.lightrag_adapter.LightRAGAdapter"
    ), mock.patch(
        "niu_api.internal.lightrag_adapter.LightRAGIngester"
    ), mock.patch(
        "agent.brain_tools.get_activation_mgr", return_value=None
    ):
        sync._merge_and_dissolve({})

    # 断言：warning 调用里包含 "Dissolve" 或 "dissolve"
    assert any("Dissolve" in str(msg) or "dissolve" in str(msg) for msg in warning_calls), \
        f"dissolve 异常应被 warning 记录，实际 warning 调用: {warning_calls}"
