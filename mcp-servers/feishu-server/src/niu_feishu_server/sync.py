"""Feishu calendar sync logic.

Provides functions to sync, cancel, and update Feishu calendar events
from local task data.  All external API calls are delegated to
``niu_feishu_server.client`` so that this module stays testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from niu_feishu_server.converter import cron_to_rrule
from niu_feishu_server.client import (
    feishu_sync_enabled,
    feishu_calendar_create,
    feishu_calendar_cancel,
    feishu_calendar_update,
)

# Default event duration when no end time is specified
_DEFAULT_DURATION_HOURS = 1


def _parse_scheduled_at(value: str) -> datetime | None:
    """Parse an ISO-8601 datetime string.

    Returns a timezone-aware ``datetime`` or *None* on failure.
    """
    try:
        dt = datetime.fromisoformat(value)
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def sync_task_to_feishu(task: dict[str, Any]) -> str | None:
    """Create a Feishu calendar event from a local task dict.

    Returns the Feishu event ID on success, or *None* if sync is
    disabled, the task data is invalid, or the API call fails.
    """
    if not feishu_sync_enabled():
        return None

    scheduled_at = _parse_scheduled_at(task.get("scheduled_at", ""))
    if scheduled_at is None:
        return None

    end_time = scheduled_at + timedelta(hours=_DEFAULT_DURATION_HOURS)

    # Build recurrence rule from cron expression
    recurrence: str | None = None
    cron_expr = task.get("cron_expr")
    if cron_expr:
        recurrence = cron_to_rrule(cron_expr)
        # If cron_to_rrule returns None (unsupported pattern),
        # fall back to a single event (recurrence stays None)

    content = task.get("content", "")
    event_type = task.get("event_type", "")

    result = feishu_calendar_create(
        summary=content,
        start_time=scheduled_at.isoformat(),
        end_time=end_time.isoformat(),
        recurrence=recurrence,
        description=event_type,
    )

    if result and "event_id" in result:
        return result["event_id"]
    return None


def cancel_feishu_event(event_id: str) -> bool:
    """Cancel a Feishu calendar event.

    Returns *True* if sync is disabled (treated as success) or the
    cancellation succeeds.  Returns *False* on API failure.
    """
    if not feishu_sync_enabled():
        return True

    result = feishu_calendar_cancel(event_id)
    if result and result.get("success"):
        return True
    return False


def update_feishu_event(event_id: str, task: dict[str, Any]) -> bool:
    """Update a Feishu calendar event from a local task dict.

    Returns *True* if sync is disabled (treated as success) or the
    update succeeds.  Returns *False* on API failure.
    """
    if not feishu_sync_enabled():
        return True

    scheduled_at = _parse_scheduled_at(task.get("scheduled_at", ""))
    if scheduled_at is None:
        return False

    end_time = scheduled_at + timedelta(hours=_DEFAULT_DURATION_HOURS)

    # Build recurrence rule from cron expression
    recurrence: str | None = None
    cron_expr = task.get("cron_expr")
    if cron_expr:
        recurrence = cron_to_rrule(cron_expr)

    content = task.get("content", "")
    event_type = task.get("event_type", "")

    result = feishu_calendar_update(
        event_id=event_id,
        summary=content,
        start_time=scheduled_at.isoformat(),
        end_time=end_time.isoformat(),
        recurrence=recurrence,
        description=event_type,
    )

    if result and result.get("success"):
        return True
    return False
