"""Cron 表达式解析器"""
import calendar
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
        self.month = self._parse_field(parts[3], 1, 12)
        # 注意：day_of_month 在下方 L/LW 检测块中统一赋值，此处不先解析

        # 高级修饰符属性初始化
        self.nth_weekdays: list[tuple[int, int]] = []  # # 修饰符: (cron_D, N)
        self.last_weekdays: list[int] = []             # L 修饰符 (dow): cron_D
        self.last_day_of_month: bool = False           # L (dom)
        self.last_workday: bool = False                # LW (dom)

        # day-of-month: 检测 L / LW
        dom_raw = parts[2]
        if dom_raw == "L":
            self.last_day_of_month = True
            self.day_of_month = list(range(1, 32))  # 匹配时由 _matches 特判
        elif dom_raw == "LW":
            self.last_workday = True
            self.day_of_month = list(range(1, 32))
        elif "#" in dom_raw:
            raise ValueError(f"# 修饰符不能出现在 day-of-month 字段: {dom_raw}")
        else:
            self.day_of_month = self._parse_field(dom_raw, 1, 31)

        # day-of-week: 标准 + # + L
        self.day_of_week = []
        for token in parts[4].split(","):
            if "#" in token:
                d_str, n_str = token.split("#", 1)
                d, n = int(d_str), int(n_str)
                if d == 7:
                    d = 0
                if not (0 <= d <= 6):
                    raise ValueError(f"Invalid weekday in #: {token}")
                if not (1 <= n <= 5):
                    raise ValueError(f"Invalid N in #: {token}")
                self.nth_weekdays.append((d, n))
            elif token == "L":
                raise ValueError("day-of-week 的 L 修饰符需要前缀数字，如 5L")
            elif token.endswith("L"):
                d = int(token[:-1])
                if d == 7:
                    d = 0
                if not (0 <= d <= 6):
                    raise ValueError(f"Invalid weekday in L: {token}")
                self.last_weekdays.append(d)
            else:
                vals = self._parse_field(token, 0, 7)
                self.day_of_week.extend(vals)
        self.day_of_week = sorted({0 if d == 7 else d for d in self.day_of_week})

        # 记录原始字段是否为通配符 *（用于 _matches OR 逻辑判断）
        self._dom_wildcard = parts[2] == "*"
        self._dow_wildcard = parts[4] == "*"

        # 互斥强制校验
        dow_has_modifier = bool(self.nth_weekdays or self.last_weekdays)
        dom_has_modifier = self.last_day_of_month or self.last_workday
        if dow_has_modifier and not self._dom_wildcard:
            raise ValueError(
                f"day-of-week 使用了 #/L 修饰符，day-of-month 必须是 ? 或 *，"
                f"实际为: {parts[2]}"
            )
        if dom_has_modifier and not self._dow_wildcard:
            raise ValueError(
                f"day-of-month 使用了 L/LW 修饰符，day-of-week 必须是 ? 或 *，"
                f"实际为: {parts[4]}"
            )
        # day-of-week 不支持标准值与 #/L 修饰符混用（如 1,5L 会被静默丢弃标准值）
        if dow_has_modifier and self.day_of_week:
            raise ValueError(
                f"day-of-week 不支持标准值与 #/L 修饰符混用: {parts[4]}"
            )

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
        高级修饰符（#/L/LW）：独立判断，不参与 OR，且互斥校验保证另一侧为通配。
        注意：cron 的 day_of_week 使用 Sunday=0 约定，Python weekday() 使用 Monday=0，
        需要转换：(weekday() + 1) % 7 将 Monday=0 → 1, Sunday=6 → 0
        """
        time_match = dt.minute in self.minute and dt.hour in self.hour and dt.month in self.month
        cron_dow = (dt.weekday() + 1) % 7  # Python weekday() → cron Sunday=0

        # --- 高级修饰符分支（互斥校验保证另一侧通配，独立判断不做 OR）---
        if self.nth_weekdays or self.last_weekdays:
            # # 和 L 修饰符（均在 day-of-week）：OR 合并（支持 5L,1#2 组合）
            nth = (dt.day - 1) // 7 + 1
            nth_match = any(d == cron_dow and nth == n for d, n in self.nth_weekdays)
            is_last = (dt + timedelta(days=7)).month != dt.month
            last_match = any(d == cron_dow for d in self.last_weekdays) and is_last
            return time_match and (nth_match or last_match)

        if self.last_day_of_month:
            # L（dom）：本月最后一天
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            return time_match and dt.day == last_day

        if self.last_workday:
            # LW：本月最后一个工作日
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            last_date = datetime(dt.year, dt.month, last_day)
            wd = last_date.weekday()  # 0=周一...6=周日
            if wd == 5:      # 周六
                target = last_day - 1
            elif wd == 6:    # 周日
                target = last_day - 2
            else:
                target = last_day
            return time_match and dt.day == target

        # --- 标准分支（原有逻辑）---
        if self._dom_wildcard and self._dow_wildcard:
            return time_match
        elif self._dom_wildcard:
            return time_match and cron_dow in self.day_of_week
        elif self._dow_wildcard:
            return time_match and dt.day in self.day_of_month
        else:
            return time_match and (dt.day in self.day_of_month or cron_dow in self.day_of_week)
