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

class TestNthWeekday:
    """# 修饰符：每月第 N 个周几"""

    def test_second_monday(self):
        """0 9 ? * 1#2 → 每月第 2 个周一 9:00"""
        p = CronParser("0 9 ? * 1#2")
        # 2026-08 月：第 1 个周一=8/3, 第 2 个周一=8/10
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 10, 9, 0)

    def test_first_friday(self):
        """0 9 ? * 5#1 → 每月第 1 个周五 9:00"""
        p = CronParser("0 9 ? * 5#1")
        # 2026-08 第 1 个周五=8/7
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 7, 9, 0)

    def test_fifth_skips_month_without_fifth(self):
        """#5 在没有第 5 个该周几的月份跳到下月"""
        p = CronParser("0 9 ? * 1#5")
        # 2026-08 有 5 个周一（3/10/17/24/31），所以用 2026-02 测试跳月：
        # 2026-02 周一：2/2,9,16,23 → 只有 4 个，无第 5 个
        nxt = p.get_next(datetime(2026, 2, 1, 0, 0))
        # 下一个有第 5 个周一的月份：2026-03 周一=3/2,9,16,23,30 → 30 是第 5 个
        assert nxt == datetime(2026, 3, 30, 9, 0)

    def test_comma_combination(self):
        """1#1,1#3 → 每月第 1 和第 3 个周一"""
        p = CronParser("0 9 ? * 1#1,1#3")
        # 2026-08: 第 1 个周一=8/3, 第 3 个周一=8/17
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 3, 9, 0)
        nxt2 = p.get_next(datetime(2026, 8, 3, 9, 1))
        assert nxt2 == datetime(2026, 8, 17, 9, 0)

    def test_rollover_to_next_month(self):
        """触发后 get_next 算到下个月第 2 个周一"""
        p = CronParser("0 9 ? * 1#2")
        # 2026-08 第 2 个周一=8/10，触发后应到 2026-09 第 2 个周一=9/14
        nxt = p.get_next(datetime(2026, 8, 10, 9, 1))
        # 2026-09 周一：9/7,14,21,28 → 第 2 个=9/14
        assert nxt == datetime(2026, 9, 14, 9, 0)

    def test_sunday_zero_and_seven(self):
        """0#1 和 7#1 都表示第 1 个周日"""
        p0 = CronParser("0 9 ? * 0#1")
        p7 = CronParser("0 9 ? * 7#1")
        base = datetime(2026, 8, 1, 0, 0)
        # 2026-08 第 1 个周日=8/2
        assert p0.get_next(base) == datetime(2026, 8, 2, 9, 0)
        assert p7.get_next(base) == datetime(2026, 8, 2, 9, 0)

class TestAdvancedModifierSmoke:
    """L/LW 冒烟测试：确保 Task 2 实现后 L/LW 不崩溃（详细测试在 Task 3/4）"""

    def test_L_dom_not_crash(self):
        """0 0 L * * 能构造且 get_next 返回月末"""
        p = CronParser("0 0 L * *")
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 31, 0, 0)

    def test_LW_not_crash(self):
        """0 0 LW * * 能构造且 get_next 返回最后工作日"""
        p = CronParser("0 0 LW * *")
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 31, 0, 0)  # 8/31 周一

    def test_L_dow_not_crash(self):
        """0 17 ? * 5L 能构造且 get_next 返回最后周五"""
        p = CronParser("0 17 ? * 5L")
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 28, 17, 0)
