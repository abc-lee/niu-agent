"""Cron 表达式解析器"""
from datetime import datetime, timedelta
from typing import List, Optional


class CronParser:
    """简单的 Cron 表达式解析器"""

    def __init__(self, cron_expr: str):
        """
        Args:
            cron_expr: cron 表达式，如 "0 8 * * *"
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        self.minute = self._parse_field(parts[0], 0, 59)
        self.hour = self._parse_field(parts[1], 0, 23)
        self.day_of_month = self._parse_field(parts[2], 1, 31)
        self.month = self._parse_field(parts[3], 1, 12)
        self.day_of_week = self._parse_field(parts[4], 0, 6)

    def _parse_field(self, field: str, min_val: int, max_val: int) -> List[int]:
        """解析单个字段"""
        if field == '*':
            return list(range(min_val, max_val + 1))

        # 处理范围，如 "1-5"
        if '-' in field:
            start, end = field.split('-')
            return list(range(int(start), int(end) + 1))

        # 处理列表，如 "1,3,5"
        if ',' in field:
            return [int(x) for x in field.split(',')]

        # 处理单个值
        return [int(field)]

    def get_next(self, current: datetime) -> Optional[datetime]:
        """
        获取下次触发时间

        Args:
            current: 当前时间

        Returns:
            下次触发时间（在当前时间之后）
        """
        # 从当前时间的下一分钟开始检查
        next_time = current.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # 最多检查 366 天
        for _ in range(366 * 24 * 60):
            if self._matches(next_time):
                return next_time
            next_time += timedelta(minutes=1)

        return None

    def _matches(self, dt: datetime) -> bool:
        """检查时间是否匹配 cron 表达式"""
        return (
            dt.minute in self.minute and
            dt.hour in self.hour and
            dt.day in self.day_of_month and
            dt.month in self.month and
            dt.weekday() in self.day_of_week
        )
