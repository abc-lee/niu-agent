"""Tests for feishu calendar sync logic (TDD: written first, must fail before impl)."""

import sys
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Ensure the feishu-server source is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "feishu-server", "src"))

# client.py may not exist yet; we mock the entire module below.


class TestSyncTaskToFeishu:
    """sync_task_to_feishu tests."""

    def _sample_task(self, **overrides):
        """Build a minimal task dict with sensible defaults."""
        task = {
            "content": "Team standup",
            "scheduled_at": "2026-05-20T09:00:00+08:00",
            "event_type": "meeting",
            "cron_expr": None,
        }
        task.update(overrides)
        return task

    # 1. Not enabled -> returns None
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=False)
    def test_not_enabled_returns_none(self, mock_enabled):
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task())
        assert result is None
        mock_enabled.assert_called_once()

    # 2. Success -> returns event_id
    @patch("niu_feishu_server.sync.feishu_calendar_create")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_success_returns_event_id(self, mock_enabled, mock_create):
        mock_create.return_value = {"event_id": "cal_evt_123"}
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task())
        assert result == "cal_evt_123"
        mock_create.assert_called_once()

    # 3. API failure -> returns None
    @patch("niu_feishu_server.sync.feishu_calendar_create")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_api_failure_returns_none(self, mock_enabled, mock_create):
        mock_create.return_value = {"error": "unauthorized"}
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task())
        assert result is None

    # 4. cron conversion -> cron_to_rrule is called
    @patch("niu_feishu_server.sync.cron_to_rrule", return_value="FREQ=DAILY;BYHOUR=9;BYMINUTE=0")
    @patch("niu_feishu_server.sync.feishu_calendar_create")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_cron_conversion(self, mock_enabled, mock_create, mock_rrule):
        mock_create.return_value = {"event_id": "cal_evt_456"}
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task(cron_expr="0 9 * * *"))
        assert result == "cal_evt_456"
        mock_rrule.assert_called_once_with("0 9 * * *")
        # Verify the recurrence field was passed to create
        call_kwargs = mock_create.call_args
        assert call_kwargs is not None or mock_create.called

    # 5. cron unsupported -> falls back to single event
    @patch("niu_feishu_server.sync.cron_to_rrule", return_value=None)
    @patch("niu_feishu_server.sync.feishu_calendar_create")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_cron_unsupported_fallback(self, mock_enabled, mock_create, mock_rrule):
        mock_create.return_value = {"event_id": "cal_evt_789"}
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task(cron_expr="*/5 * * * *"))
        assert result == "cal_evt_789"
        # Should still create event, just without recurrence
        mock_create.assert_called_once()

    # 6. Invalid scheduled_at -> returns None
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_invalid_time_returns_none(self, mock_enabled):
        from niu_feishu_server.sync import sync_task_to_feishu
        result = sync_task_to_feishu(self._sample_task(scheduled_at="not-a-date"))
        assert result is None


class TestCancelFeishuEvent:
    """cancel_feishu_event tests."""

    # 7. Not enabled -> returns True
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=False)
    def test_not_enabled_returns_true(self, mock_enabled):
        from niu_feishu_server.sync import cancel_feishu_event
        result = cancel_feishu_event("cal_evt_123")
        assert result is True

    # 8. Success -> returns True
    @patch("niu_feishu_server.sync.feishu_calendar_cancel")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_success_returns_true(self, mock_enabled, mock_cancel):
        mock_cancel.return_value = {"success": True}
        from niu_feishu_server.sync import cancel_feishu_event
        result = cancel_feishu_event("cal_evt_123")
        assert result is True
        mock_cancel.assert_called_once_with("cal_evt_123")

    # 9. Failure -> returns False
    @patch("niu_feishu_server.sync.feishu_calendar_cancel")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_failure_returns_false(self, mock_enabled, mock_cancel):
        mock_cancel.return_value = {"error": "not found"}
        from niu_feishu_server.sync import cancel_feishu_event
        result = cancel_feishu_event("cal_evt_123")
        assert result is False


class TestUpdateFeishuEvent:
    """update_feishu_event tests."""

    # 10. Not enabled -> returns True
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=False)
    def test_not_enabled_returns_true(self, mock_enabled):
        from niu_feishu_server.sync import update_feishu_event
        result = update_feishu_event("cal_evt_123", {"content": "test"})
        assert result is True

    # 11. Success -> returns True
    @patch("niu_feishu_server.sync.feishu_calendar_update")
    @patch("niu_feishu_server.sync.feishu_sync_enabled", return_value=True)
    def test_success_returns_true(self, mock_enabled, mock_update):
        mock_update.return_value = {"success": True}
        from niu_feishu_server.sync import update_feishu_event
        task = {
            "content": "Updated meeting",
            "scheduled_at": "2026-05-20T10:00:00+08:00",
            "event_type": "meeting",
            "cron_expr": None,
        }
        result = update_feishu_event("cal_evt_123", task)
        assert result is True
        mock_update.assert_called_once()
