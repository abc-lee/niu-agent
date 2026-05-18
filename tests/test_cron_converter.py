"""Tests for cron -> RRULE converter (TDD: written first, must fail before impl)."""

import sys
import os

# Ensure the feishu-server source is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "feishu-server", "src"))

from niu_feishu_server.converter import cron_to_rrule


class TestDaily:
    """Every-day patterns."""

    def test_daily_9am(self):
        assert cron_to_rrule("0 9 * * *") == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"

    def test_daily_8_30(self):
        assert cron_to_rrule("30 8 * * *") == "FREQ=DAILY;BYHOUR=8;BYMINUTE=30"


class TestWeekly:
    """Weekday / specific-day patterns."""

    def test_weekdays_9am(self):
        assert cron_to_rrule("0 9 * * 1-5") == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"

    def test_mon_wed_fri(self):
        assert cron_to_rrule("30 8 * * 1,3,5") == "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=30"

    def test_sunday_only(self):
        # cron 0 = Sunday -> SU
        assert cron_to_rrule("0 10 * * 0") == "FREQ=WEEKLY;BYDAY=SU;BYHOUR=10;BYMINUTE=0"

    def test_saturday_only(self):
        # cron 6 = Saturday -> SA
        assert cron_to_rrule("0 10 * * 6") == "FREQ=WEEKLY;BYDAY=SA;BYHOUR=10;BYMINUTE=0"


class TestMultiHour:
    """Multiple hours in a day."""

    def test_two_hours(self):
        assert cron_to_rrule("0 9,18 * * *") == "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"

    def test_three_hours(self):
        assert cron_to_rrule("15 8,12,18 * * *") == "FREQ=DAILY;BYHOUR=8,12,18;BYMINUTE=15"


class TestUnsupported:
    """Patterns that must return None."""

    def test_step_expression(self):
        assert cron_to_rrule("*/5 * * * *") is None

    def test_step_in_range(self):
        assert cron_to_rrule("0 9 * * 1-5/2") is None

    def test_dom_field(self):
        assert cron_to_rrule("0 9 1 * *") is None

    def test_month_field(self):
        assert cron_to_rrule("0 9 * 6 *") is None

    def test_four_fields(self):
        assert cron_to_rrule("0 9 * *") is None

    def test_six_fields(self):
        assert cron_to_rrule("0 9 * * * 2025") is None

    def test_empty_string(self):
        assert cron_to_rrule("") is None
