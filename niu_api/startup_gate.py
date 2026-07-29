"""Startup gate helpers for brain region readiness.

Extracted from niu_api/__main__.py lifespan to make the brain region
startup gate testable without spinning up the full FastAPI app.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


def run_brain_region_startup_gate(
    *,
    region_sync,
    signal_scheduler_ready_fn: Callable[[], None],
    should_signal: bool,
    timeout: float = 90.0,
) -> bool | None:
    """Run the brain region startup gate before signal_scheduler_ready.

    Branches:
    - region_sync is None (LightRAG corrupt, region_sync never created):
      skip gate, skip signal_scheduler_ready, return None.
    - should_signal is False (LightRAG corrupt gate): same as above.
    - Normal: run_sync_once_for_startup + wait_first_sync_done + activation_mgr check,
      then call signal_scheduler_ready_fn. Return True/False.

    Args:
        region_sync: RegionSync instance, or None if LightRAG corrupt branch.
        signal_scheduler_ready_fn: Callable that signals scheduler ready.
        should_signal: Whether to signal scheduler (False = LightRAG corrupt).
        timeout: Max seconds to wait for first sync done (default 90s).

    Returns:
        True if first sync done AND activation_mgr is not None.
        False if timed out OR activation_mgr still None (proceeded with warning).
        None if gate was skipped (region_sync None or should_signal=False).
    """
    # Skip gate when region_sync is None (LightRAG corrupt, region_sync never created)
    # or should_signal is False (existing should_signal_scheduler_ready gate)
    if region_sync is None or not should_signal:
        logger.warning(
            "[StartupGate] Skipping brain region gate "
            f"(region_sync={'None' if region_sync is None else 'set'}, should_signal={should_signal})"
        )
        return None

    try:
        logger.info("[StartupGate] Running brain region first sync (blocking, max ~40s)")
        stats = region_sync.run_sync_once_for_startup()
        logger.info(f"[StartupGate] First sync stats: {stats}")
    except Exception as e:
        logger.error(f"[StartupGate] run_sync_once_for_startup failed: {e}", exc_info=True)
        # Don't re-raise — proceed to wait, _first_sync_done might still get set
        # by the exception path in run_sync's finally block.

    done = region_sync.wait_first_sync_done(timeout=timeout)
    if not done:
        logger.warning(
            f"[StartupGate] Brain region first sync not done within {timeout}s, "
            "proceeding anyway (forced sync daemon will retry on first request)"
        )
        signal_scheduler_ready_fn()
        return False

    # Additional check: _first_sync_done being set doesn't guarantee activation_mgr is set
    # (run_sync's finally sets Event even if _refresh_activation_manager failed).
    from agent.brain_tools import get_activation_mgr
    activation_mgr = get_activation_mgr()
    if activation_mgr is None:
        logger.warning(
            "[StartupGate] _first_sync_done set but activation_mgr is None "
            "(run_sync failed or _refresh_activation_manager skipped) — "
            "proceeding, forced sync daemon will retry on first request"
        )
        signal_scheduler_ready_fn()
        return False

    logger.info("[StartupGate] Brain region ready (first sync done + activation_mgr set), signaling scheduler")
    signal_scheduler_ready_fn()
    return True
