"""Tests for CronParser — advanced modifiers (#, L, LW) + regression"""
from datetime import datetime
import pytest
from niu_api.internal.scheduler.cron_parser import CronParser


class TestRegression:
    """现有 cron 模式不破坏"""

    def test_every_5_minutes(self):
        """*/5 * * * * → 每 5 分钟"""
        p = CronParser("*/5 * * * *")
        nxt = p.get_next(datetime(2026, 7, 31, 10, 2))
        assert nxt == datetime(2026, 7, 31, 10, 5)

    def test_daily_8am(self):
        """0 8 * * * → 每天 8:00"""
        p = CronParser("0 8 * * *")
        nxt = p.get_next(datetime(2026, 7, 31, 7, 59))
        assert nxt == datetime(2026, 7, 31, 8, 0)

    def test_every_3_months(self):
        """0 0 1 */3 * → 每 3 个月 1 号 0:00"""
        p = CronParser("0 0 1 */3 *")
        nxt = p.get_next(datetime(2026, 7, 31, 23, 59))
        assert nxt == datetime(2026, 10, 1, 0, 0)

    def test_weekly_monday(self):
        """0 9 * * 1 → 每周一 9:00"""
        p = CronParser("0 9 * * 1")
        # 2026-07-31 是周五，下一个周一是 2026-08-03
        nxt = p.get_next(datetime(2026, 7, 31, 10, 0))
        assert nxt == datetime(2026, 8, 3, 9, 0)

    def test_sunday_7_equals_0(self):
        """0 和 7 都表示周日"""
        p0 = CronParser("0 9 * * 0")
        p7 = CronParser("0 9 * * 7")
        base = datetime(2026, 7, 31, 10, 0)  # 周五
        assert p0.get_next(base) == p7.get_next(base)


class TestQuestionMark:
    """? 兼容：? 等同于 *"""

    def test_question_mark_dom_equals_star(self):
        """day-of-month ? 等同 *"""
        p_q = CronParser("0 9 ? * 1")
        p_s = CronParser("0 9 * * 1")
        base = datetime(2026, 7, 31, 10, 0)
        assert p_q.get_next(base) == p_s.get_next(base)

    def test_question_mark_dow_equals_star(self):
        """day-of-week ? 等同 *"""
        p_q = CronParser("0 9 15 * ?")
        p_s = CronParser("0 9 15 * *")
        base = datetime(2026, 7, 1, 0, 0)
        assert p_q.get_next(base) == p_s.get_next(base)
