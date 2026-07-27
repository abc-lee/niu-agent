"""Test RegionSync._first_sync_done Event semantics.

Covers:
- _first_sync_done is set after first run_sync() completes (success path)
- _first_sync_done is set even if run_sync() fails (exception path)
- _first_sync_done is set even if run_sync() skips due to concurrent sync
- wait_first_sync_done returns True after set, False on timeout
- run_sync_once_for_startup is idempotent (second call returns immediately)
"""
from unittest.mock import patch

import pytest

from agent.injector.region_sync import RegionSync


def test_first_sync_done_set_after_successful_run_sync():
    """_first_sync_done is set after first successful run_sync()."""
    rs = RegionSync(sync_interval=86400)
    assert not rs._first_sync_done.is_set()

    with patch.object(rs, "_run_sync_impl", return_value={"regions_created": 0, "errors": []}):
        rs.run_sync()

    assert rs._first_sync_done.is_set()


def test_first_sync_done_set_after_failed_run_sync():
    """_first_sync_done is set even if _run_sync_impl raises."""
    rs = RegionSync(sync_interval=86400)

    with patch.object(rs, "_run_sync_impl", side_effect=RuntimeError("simulated failure")):
        with pytest.raises(RuntimeError):
            rs.run_sync()

    assert rs._first_sync_done.is_set()


def test_first_sync_done_set_after_concurrent_skip():
    """_first_sync_done is set even if run_sync skips due to concurrent sync."""
    rs = RegionSync(sync_interval=86400)

    assert rs.try_acquire_sync() is True

    try:
        result = rs.run_sync()
        assert result["errors"] == ["skipped: concurrent sync"]
    finally:
        rs.release_sync()

    assert rs._first_sync_done.is_set()


def test_wait_first_sync_done_returns_true_after_set():
    """wait_first_sync_done returns True immediately after _first_sync_done is set."""
    rs = RegionSync(sync_interval=86400)
    rs._first_sync_done.set()
    assert rs.wait_first_sync_done(timeout=0.1) is True


def test_wait_first_sync_done_returns_false_on_timeout():
    """wait_first_sync_done returns False on timeout when event never set."""
    rs = RegionSync(sync_interval=86400)
    assert rs.wait_first_sync_done(timeout=0.1) is False


def test_run_sync_once_for_startup_idempotent():
    """run_sync_once_for_startup returns immediately if _first_sync_done already set."""
    rs = RegionSync(sync_interval=86400)
    rs._first_sync_done.set()

    with patch.object(rs, "_run_sync_impl", side_effect=AssertionError("should not be called")):
        result = rs.run_sync_once_for_startup()

    assert result == {"skipped": "first_sync_already_done"}
    assert rs._first_sync_done.is_set()


def test_run_sync_once_for_startup_blocks_until_complete():
    """run_sync_once_for_startup synchronously runs run_sync and blocks until done."""
    rs = RegionSync(sync_interval=86400)

    with patch.object(rs, "_run_sync_impl", return_value={"regions_created": 5, "errors": []}) as mock_impl:
        result = rs.run_sync_once_for_startup()

    mock_impl.assert_called_once()
    assert result == {"regions_created": 5, "errors": []}
    assert rs._first_sync_done.is_set()
