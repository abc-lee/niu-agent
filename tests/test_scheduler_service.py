"""Tests for service.py trigger_callback health check logic"""
from unittest.mock import patch, MagicMock

import pytest


class TestHealthCheckRetry:
    def test_non_200_health_check_retries_with_sleep(self):
        """health check 返回非 200 时应 sleep 后重试，不是立即重试"""
        from niu_api.internal.scheduler.service import trigger_callback

        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200

        task = {"content": "test task"}

        with patch("niu_api.internal.scheduler.service.requests") as mock_requests, \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.internal.scheduler.service.time") as mock_time, \
             patch("niu_api.internal.scheduler.service._persist_fallback_message"):

            mock_requests.get.side_effect = [mock_resp_503, mock_resp_200]
            mock_requests.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"reply": "ok"}
            )

            trigger_callback(task)

            # Should have slept between retries (2^0 = 1s for first retry)
            sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
            assert len(sleep_calls) >= 1, "Should sleep between health check retries"
            assert sleep_calls[0] == 1, f"First retry sleep should be 1s (2^0), got {sleep_calls[0]}"

    def test_all_health_checks_fail_goes_fallback(self):
        """health check 全部失败后应走 fallback 路径，不直接调 /chat/sync"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}

        with patch("niu_api.internal.scheduler.service.requests") as mock_requests, \
             patch("niu_api.alerts.add_pending_alert") as mock_alert, \
             patch("niu_api.internal.scheduler.service._persist_fallback_message") as mock_persist, \
             patch("niu_api.internal.scheduler.service.time"):

            # All 3 health checks fail
            mock_requests.get.side_effect = [
                MagicMock(status_code=503),
                MagicMock(status_code=503),
                MagicMock(status_code=503),
            ]
            mock_requests.post.return_value = MagicMock(status_code=200)

            result = trigger_callback(task)

            # Should NOT have called /chat/sync since health check failed
            assert mock_requests.post.call_count == 0, \
                "Should not call /chat/sync when all health checks fail"
            # Should have used fallback path
            assert mock_persist.called, "Should persist fallback message"
            assert mock_alert.called, "Should trigger alert"

    def test_health_check_exception_retries_with_sleep(self):
        """health check 抛异常时应 sleep 后重试"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}

        # Mock only requests.get, keep real requests.RequestException for except clause
        with patch("niu_api.internal.scheduler.service.requests.get") as mock_get, \
             patch("niu_api.internal.scheduler.service.requests.post") as mock_post, \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.internal.scheduler.service.time") as mock_time, \
             patch("niu_api.internal.scheduler.service._persist_fallback_message"):

            import requests as real_requests
            # First attempt raises exception, second succeeds
            mock_get.side_effect = [
                real_requests.ConnectionError("refused"),
                MagicMock(status_code=200),
            ]
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"reply": "ok"}
            )

            trigger_callback(task)

            sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
            assert len(sleep_calls) >= 1, "Should sleep after exception"
            assert sleep_calls[0] == 1, f"First retry sleep should be 1s, got {sleep_calls[0]}"
