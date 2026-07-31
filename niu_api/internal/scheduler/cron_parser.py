"""Cron 表达式解析器"""
from datetime import datetime, timedelta


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
        # ? 等同于 *（Quartz 兼容）
        parts = [p.replace("?", "*") for p in parts]

        self.minute = self._parse_field(parts[0], 0, 59)
        self.hour = self._parse_field(parts[1], 0, 23)
        self.day_of_month = self._parse_field(parts[2], 1, 31)
        self.month = self._parse_field(parts[3], 1, 12)
        # 标准 cron 中 7 等同于 0（周日），映射后再过滤
        dow_raw = self._parse_field(parts[4], 0, 7)
        self.day_of_week = sorted({0 if d == 7 else d for d in dow_raw})

        # 记录原始字段是否为通配符 *（用于 _matches OR 逻辑判断）
        self._dom_wildcard = parts[2] == "*"
        self._dow_wildcard = parts[4] == "*"

    def _parse_field(self, field: str, min_val: int, max_val: int) -> list[int]:
        """解析单个 cron 字段（支持 *, */N, N, N-M, N-M/S 格式）"""
        values = set()

        for part in field.split(","):
            if "/" in part:
                # 步进格式：*/N 或 N-M/S
                range_part, step_str = part.split("/", 1)
                step = int(step_str)
                if range_part == "*":
                    start, end = min_val, max_val
                elif "-" in range_part:
                    start_str, end_str = range_part.split("-", 1)
                    start, end = int(start_str), int(end_str)
                else:
                    start, end = int(range_part), max_val
                for i in range(start, end + 1, step):
                    values.add(i)
            elif "-" in part:
                start_str, end_str = part.split("-", 1)
                for i in range(int(start_str), int(end_str) + 1):
                    values.add(i)
            elif part == "*":
                for i in range(min_val, max_val + 1):
                    values.add(i)
            else:
                values.add(int(part))

        return sorted(v for v in values if min_val <= v <= max_val)

    def get_next(self, current: datetime) -> datetime | None:
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
        """检查时间是否匹配 cron 表达式

        标准 cron 语义：当 day_of_month 和 day_of_week 都非 * 时，任一匹配即可（OR 逻辑）
        注意：cron 的 day_of_week 使用 Sunday=0 约定，Python weekday() 使用 Monday=0，
        需要转换：(weekday() + 1) % 7 将 Monday=0 → 1, Sunday=6 → 0
        """
        time_match = dt.minute in self.minute and dt.hour in self.hour and dt.month in self.month
        # 将 Python weekday() 转换为 cron 约定（Sunday=0）
        cron_dow = (dt.weekday() + 1) % 7

        if self._dom_wildcard and self._dow_wildcard:
            return time_match
        elif self._dom_wildcard:
            return time_match and cron_dow in self.day_of_week
        elif self._dow_wildcard:
            return time_match and dt.day in self.day_of_month
        else:
            # 两者都指定：OR 逻辑（标准 cron 行为）
            return time_match and (dt.day in self.day_of_month or cron_dow in self.day_of_week)
