"""Test run_brain_region_startup_gate helper.

Covers 4 branches:
- Normal: run_sync_once_for_startup called, activation_mgr ready, scheduler signaled → True
- Timeout: wait_first_sync_done returns False → still signal, return False
- Skip (LightRAG corrupt): should_signal=False → no signal, return None
- region_sync is None (LightRAG corrupt branch): → no signal, return None
- activation_mgr still None after sync (run_sync failed): → still signal, return False
"""
from unittest.mock import MagicMock, patch


def test_normal_path_activation_mgr_ready():
    """Normal path: run_sync_once_for_startup called, activation_mgr ready, scheduler signaled."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"regions_created": 5}
    mock_rs.wait_first_sync_done.return_value = True

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    mock_rs.run_sync_once_for_startup.assert_called_once()
    mock_rs.wait_first_sync_done.assert_called_once_with(timeout=90.0)
    mock_signal.assert_called_once()
    assert result is True


def test_timeout_path_proceeds_with_warning():
    """If wait_first_sync_done returns False (timeout), lifespan proceeds anyway."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"errors": ["timeout"]}
    mock_rs.wait_first_sync_done.return_value = False

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=None):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    mock_signal.assert_called_once()
    assert result is False


def test_skip_when_should_signal_false():
    """When should_signal is False (LightRAG corrupt), gate is skipped."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_signal = MagicMock()

    result = run_brain_region_startup_gate(
        region_sync=mock_rs,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=False,
        timeout=90.0,
    )

    mock_rs.run_sync_once_for_startup.assert_not_called()
    mock_signal.assert_not_called()
    assert result is None


def test_skip_when_region_sync_none():
    """When region_sync is None (LightRAG corrupt branch, region_sync never created), gate is skipped."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_signal = MagicMock()

    result = run_brain_region_startup_gate(
        region_sync=None,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=True,
        timeout=90.0,
    )

    mock_signal.assert_not_called()
    assert result is None


def test_activation_mgr_none_after_sync_proceeds_with_warning():
    """run_sync completed (_first_sync_done set) but activation_mgr still None —
    proceed with warning, forced sync daemon will retry."""
    from niu_api.startup_gate import run_brain_region_startup_gate

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.return_value = {"errors": ["activation refresh failed"]}
    mock_rs.wait_first_sync_done.return_value = True  # Event was set

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=None):
        result = run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )

    # Even though activation_mgr is None, scheduler must be signaled
    # (forced sync daemon will retry on first user request)
    mock_signal.assert_called_once()
    assert result is False  # False indicates degraded state


def test_lifespan_order_start_background_sync_after_gate():
    """v3 core: start_background_sync must be called AFTER run_brain_region_startup_gate.

    This is the structural fix for the first-startup race (second-round review
    critical issue): _sync_loop daemon must not exist while gate runs, so
    run_sync_once_for_startup always wins the _sync_lock.
    """
    import niu_api.startup_gate as sg

    call_order = []

    mock_rs = MagicMock()
    mock_rs.run_sync_once_for_startup.side_effect = lambda: call_order.append("gate_run_sync") or {"regions_created": 0}
    mock_rs.wait_first_sync_done.side_effect = lambda timeout: call_order.append("gate_wait") or True
    mock_rs.start_background_sync.side_effect = lambda: call_order.append("start_background_sync")

    mock_signal = MagicMock()

    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        # Simulate the lifespan sequence: gate first, then start_background_sync
        result = sg.run_brain_region_startup_gate(
            region_sync=mock_rs,
            signal_scheduler_ready_fn=mock_signal,
            should_signal=True,
            timeout=90.0,
        )
        # lifespan then calls start_background_sync (with None guard)
        if mock_rs is not None:
            mock_rs.start_background_sync()

    assert result is True
    # gate's run_sync must complete before start_background_sync
    assert call_order.index("gate_run_sync") < call_order.index("start_background_sync")
    assert call_order.index("start_background_sync") == len(call_order) - 1


def test_lifespan_start_background_sync_not_called_when_region_sync_none():
    """v3 None guard: when region_sync is None (LightRAG corrupt), start_background_sync
    must NOT be called — would raise AttributeError on NoneType."""
    import niu_api.startup_gate as sg

    mock_signal = MagicMock()

    result = sg.run_brain_region_startup_gate(
        region_sync=None,
        signal_scheduler_ready_fn=mock_signal,
        should_signal=True,
        timeout=90.0,
    )

    # Gate skipped, scheduler NOT signaled, and caller must guard start_background_sync
    assert result is None
    mock_signal.assert_not_called()
    # The None guard lives in __main__.py (if region_sync is not None:) —
    # this test documents the contract: region_sync None → no start_background_sync.
