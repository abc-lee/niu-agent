# Cron 高级修饰符（`#`/`L`/`LW`）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展自研 `CronParser` 支持 Quartz 风格的 `#`（第 N 个周几）、`L`（最后一个周几/最后一天）、`LW`（最后一个工作日）修饰符，填补标准 cron 无法表达"每月第几周"等模式的缺口。

**Architecture:** 只改 `niu_api/internal/scheduler/cron_parser.py` 一个文件——在 `_parse_field` 增加修饰符解析、在 `_matches` 增加匹配逻辑、在 `__init__` 增加互斥强制校验。`get_next` 的逐分钟暴力扫描框架完全复用，不动。数据模型、调度器、MCP/API、前端全部不改。辅以单元测试和 Agent 提示词更新。

**Tech Stack:** Python 3.11+，标准库 `calendar`/`datetime`，pytest。无新依赖。

**Spec:** `docs/superpowers/specs/2026-07-31-cron-advanced-modifiers-design.md`

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `niu_api/internal/scheduler/cron_parser.py` | 修改 | 扩展 `__init__`/`_parse_field`/`_matches` 支持 `#`/`L`/`LW`/`?` |
| `tests/test_cron_parser.py` | 新建 | 单元测试：新修饰符 + `?` 兼容 + 回归 + 边界 + 互斥校验 |
| `config/agents/niu.md` | 修改 | 主 Agent 提示词加一句能力说明 |
| `config/agents/event-manager.md` | 修改 | 子 Agent 提示词加"高级 cron 修饰符"章节 |

---

## 关键设计约定（全计划通用）

**周几编号转换**（cron ↔ Python）：
```
# cron: 0=周日, 1=周一, ..., 6=周六, 7=周日
# Python weekday(): 0=周一, ..., 6=周日
# cron D → Python: py_wd = (D - 1) % 7
#   0/7 → 6(周日), 1 → 0(周一), 2 → 1(周二), ..., 6 → 5(周六)
# Python → cron: cron_dow = (weekday() + 1) % 7  (现有代码 cron_parser.py:89 已用)
```

**新增实例属性**（`CronParser.__init__` 中初始化）：
- `self.nth_weekdays: list[tuple[int, int]]` — `#` 修饰符解析结果，元素 `(cron_D, N)`
- `self.last_weekdays: list[int]` — `L` 修饰符（day-of-week）解析结果，元素 `cron_D`
- `self.last_day_of_month: bool` — day-of-month 为 `L`
- `self.last_workday: bool` — day-of-month 为 `LW`

**互斥校验**（解析期，`__init__` 末尾）：
- day-of-week 用了 `#`/`L`（`nth_weekdays` 或 `last_weekdays` 非空）→ day-of-month 必须是 `?`/`*`，否则 `ValueError`
- day-of-month 用了 `L`/`LW` → day-of-week 必须是 `?`/`*`，否则 `ValueError`

**`?` 处理**：day-of-month 和 day-of-week 字段接受 `?`，视为 `*`。

---

## Task 1: 新建测试文件 + `?` 兼容 + 回归测试

先把测试骨架和最简单的 `?` 兼容 + 回归测试落地，确保现有行为不破坏。此阶段 `?` 还未实现，回归测试应通过、`?` 测试应失败。

**Files:**
- Create: `tests/test_cron_parser.py`
- Modify: `niu_api/internal/scheduler/cron_parser.py`（仅 `__init__` 加 `?` → `*` 转换）

- [ ] **Step 1: 写回归测试 + `?` 兼容测试**

创建 `tests/test_cron_parser.py`：

```python
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
```

- [ ] **Step 2: 运行测试，确认回归通过、`?` 失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: `TestRegression` 5 个全 PASS；`TestQuestionMark` 2 个 FAIL（`?` 未被识别，`_parse_field` 尝试 `int("?")` 报 `ValueError`）

- [ ] **Step 3: 实现 `?` 兼容**

修改 `niu_api/internal/scheduler/cron_parser.py` 的 `__init__`，在解析 day-of-month 和 day-of-week 前把 `?` 转成 `*`。

> **实现位置说明**：Spec §1 规定「`_parse_field` 入口处处理 `?`」，本计划在 `__init__` 用 `parts = [p.replace("?", "*") ...]` 全局替换。两者功能完全等价（`_parse_field` 收到的都是已替换值），选 `__init__` 是因为替换一次比每个 `_parse_field` 调用都判断更简洁。

当前 `__init__`（cron_parser.py:8-27）开头 parts 解析后，改为：

```python
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
```

- [ ] **Step 4: 运行测试，确认全部通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: 7 个全 PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_parser.py niu_api/internal/scheduler/cron_parser.py
git commit -m "feat(cron): ? 兼容 + 回归测试

- ? 在 day-of-month/day-of-week 等同于 *
- 新建 test_cron_parser.py，覆盖现有模式回归"
```

---

## Task 2: `#` 修饰符（第 N 个周几）

实现 `D#N` 语法：每月第 N 个周 D。

**Files:**
- Modify: `niu_api/internal/scheduler/cron_parser.py`
- Test: `tests/test_cron_parser.py`

- [ ] **Step 1: 写 `#` 修饰符测试**

在 `tests/test_cron_parser.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py::TestNthWeekday tests/test_cron_parser.py::TestAdvancedModifierSmoke -v`
Expected: 9 个全 FAIL（`#`/`L`/`LW` 未实现，构造 CronParser 时 `int("1#2")`/`int("L")` 报错）

- [ ] **Step 3: 实现 `#` 修饰符解析与匹配**

修改 `niu_api/internal/scheduler/cron_parser.py`：

**先在文件顶部加 import**：当前 cron_parser.py:2 是 `from datetime import datetime, timedelta`，在其上方加：

```python
import calendar
```

（`_matches` 的 L/LW 分支会用到 `calendar.monthrange`，提至模块顶部，避免方法内重复 import。）

**(a) `__init__` 中初始化新属性 + 调用 day-of-week 专用解析**

把 `__init__` 中 day-of-week 解析段（当前第 22-27 行）替换为：

```python
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
```

注意：因为 Step 3(a) 已把 `?`→`*` 替换（Task 1），`parts[2]`/`parts[4]` 不会出现 `?`，wildcard 判定用 `== "*"` 即可。

**(b) `_matches` 增加 `#` 匹配分支**

把 `_matches`（当前第 80-99 行）替换为：

```python
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
```

- [ ] **Step 4: 运行 `#` 测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py::TestNthWeekday tests/test_cron_parser.py::TestAdvancedModifierSmoke -v`
Expected: 9 个全 PASS（`#` 6 + L/LW 冒烟 3）

- [ ] **Step 5: 运行全部测试，确认无回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: 16 个全 PASS（回归 5 + `?` 2 + `#` 6 + L/LW 冒烟 3）

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_parser.py niu_api/internal/scheduler/cron_parser.py
git commit -m "feat(cron): # 修饰符支持（每月第 N 个周几）

- _parse_field 增加 # 解析，存 nth_weekdays
- _matches 增加 nth-weekday 匹配: (day-1)//7+1 == N
- 互斥校验: # 用时 dom 必须 ?/*"
```

---

## Task 3: `L` 修饰符（最后一个周几 / 最后一天）

`L` 的解析在 Task 2 Step 3(a) 已一并实现（`last_weekdays`/`last_day_of_month`），匹配在 Task 2 Step 3(b) 已实现。本任务只补测试。

**Files:**
- Test: `tests/test_cron_parser.py`

- [ ] **Step 1: 写 `L` 修饰符测试**

在 `tests/test_cron_parser.py` 末尾追加：

```python
class TestLastWeekday:
    """L 修饰符：每月最后一个周几 / 最后一天"""

    def test_last_friday(self):
        """0 17 ? * 5L → 每月最后一个周五 17:00"""
        p = CronParser("0 17 ? * 5L")
        # 2026-08 最后一个周五：8/28(周五)? 8月周五:7,14,21,28 → 28
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 28, 17, 0)

    def test_last_friday_feb_28_days(self):
        """28 天月份的最后一个周五"""
        p = CronParser("0 17 ? * 5L")
        # 2026-02 周五: 6,13,20,27 → 27
        nxt = p.get_next(datetime(2026, 2, 1, 0, 0))
        assert nxt == datetime(2026, 2, 27, 17, 0)

    def test_last_friday_feb_leap_29_days(self):
        """闰年 29 天月份"""
        p = CronParser("0 17 ? * 5L")
        # 2024-02 周五: 2,9,16,23 → 23
        nxt = p.get_next(datetime(2024, 2, 1, 0, 0))
        assert nxt == datetime(2024, 2, 23, 17, 0)

    def test_last_day_of_month(self):
        """0 0 L * * → 每月最后一天 0:00"""
        p = CronParser("0 0 L * *")
        # 2026-08 最后一天=31
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 31, 0, 0)

    def test_last_day_feb_leap(self):
        """2 月闰年 29 天"""
        p = CronParser("0 0 L * *")
        nxt = p.get_next(datetime(2024, 2, 1, 0, 0))
        assert nxt == datetime(2024, 2, 29, 0, 0)

    def test_last_day_feb_nonleap(self):
        """2 月平年 28 天"""
        p = CronParser("0 0 L * *")
        nxt = p.get_next(datetime(2026, 2, 1, 0, 0))
        assert nxt == datetime(2026, 2, 28, 0, 0)

    def test_last_sunday_zero(self):
        """0L → 每月最后一个周日"""
        p = CronParser("0 9 ? * 0L")
        # 2026-08 周日: 2,9,16,23,30 → 30
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 30, 9, 0)

    def test_mixed_hash_and_L(self):
        """5L,1#2 → 最后一个周五 或 第 2 个周一"""
        p = CronParser("0 9 ? * 5L,1#2")
        # 2026-08: 第 2 个周一=8/10, 最后一个周五=8/28 → 先到 8/10
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 10, 9, 0)
        nxt2 = p.get_next(datetime(2026, 8, 10, 9, 1))
        assert nxt2 == datetime(2026, 8, 28, 9, 0)
```

- [ ] **Step 2: 运行测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py::TestLastWeekday -v`
Expected: 8 个全 PASS（解析+匹配已在 Task 2 实现）

如有 FAIL，检查 `last_weekdays` 解析（token 以 `L` 结尾但非单独 `L`）和 `_matches` 的 `is_last` 判断（`d+7` 跨月）。

- [ ] **Step 3: 运行全部测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: 24 个全 PASS

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_parser.py
git commit -m "test(cron): L 修饰符测试（最后周几/最后一天/闰年/混合）"
```

---

## Task 4: `LW` 修饰符（最后一个工作日）

`LW` 的解析（`last_workday`）和匹配在 Task 2 已一并实现。本任务补测试。

**Files:**
- Test: `tests/test_cron_parser.py`

- [ ] **Step 1: 写 `LW` 修饰符测试**

在 `tests/test_cron_parser.py` 末尾追加：

```python
class TestLastWorkday:
    """LW 修饰符：每月最后一个工作日"""

    def test_month_end_is_weekday(self):
        """月末是工作日 → 就是月末那天"""
        p = CronParser("0 0 LW * *")
        # 2026-08-31 是周一(工作日) → 31
        nxt = p.get_next(datetime(2026, 8, 1, 0, 0))
        assert nxt == datetime(2026, 8, 31, 0, 0)

    def test_month_end_is_saturday(self):
        """月末是周六 → 前移到周五"""
        p = CronParser("0 0 LW * *")
        # 2026-10-31 是周六 → 前移到 10/30(周五)
        nxt = p.get_next(datetime(2026, 10, 1, 0, 0))
        assert nxt == datetime(2026, 10, 30, 0, 0)

    def test_month_end_is_sunday(self):
        """月末是周日 → 前移到周五"""
        p = CronParser("0 0 LW * *")
        # 2026-05-31 是周日 → 前移到 5/29(周五)
        nxt = p.get_next(datetime(2026, 5, 1, 0, 0))
        assert nxt == datetime(2026, 5, 29, 0, 0)

    def test_all_months_2026(self):
        """2026 年每月最后一个工作日（覆盖性测试）"""
        p = CronParser("0 0 LW * *")
        expected = [
            (1, 30),   # 1/31 周六 → 30 周五
            (2, 27),   # 2/28 周六 → 27 周五
            (3, 31),   # 3/31 周二
            (4, 30),   # 4/30 周四
            (5, 29),   # 5/31 周日 → 29 周五
            (6, 30),   # 6/30 周二
            (7, 31),   # 7/31 周五
            (8, 31),   # 8/31 周一
            (9, 30),   # 9/30 周三
            (10, 30),  # 10/31 周六 → 30 周五
            (11, 30),  # 11/30 周一
            (12, 31),  # 12/31 周四
        ]
        for month, day in expected:
            nxt = p.get_next(datetime(2026, month, 1, 0, 0))
            assert nxt == datetime(2026, month, day, 0, 0), \
                f"2026-{month}: expected {day}, got {nxt.day}"
```

> **验证提示**：写测试前用 Python 确认 2026 各月月末星期几：
> `python -c "import calendar; [print(m, calendar.monthrange(2026,m)[1], datetime(2026,m,calendar.monthrange(2026,m)[1]).strftime('%A')) for m in range(1,13)]"`
> 如 expected 与实际不符，以实际为准修正 expected（测试断言的是真实日历）。

- [ ] **Step 2: 运行测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py::TestLastWorkday -v`
Expected: 4 个全 PASS

- [ ] **Step 3: 运行全部测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: 28 个全 PASS

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_parser.py
git commit -m "test(cron): LW 修饰符测试（最后工作日/月末周六/周日/全年覆盖）"
```

---

## Task 5: 边界与互斥校验测试

测试无效输入和互斥校验报错。

**Files:**
- Test: `tests/test_cron_parser.py`

- [ ] **Step 1: 写边界与互斥校验测试**

在 `tests/test_cron_parser.py` 末尾追加：

```python
class TestBoundaries:
    """边界：无效修饰符 + 互斥校验"""

    def test_n_gt_5_raises(self):
        """N > 5 报错"""
        with pytest.raises(ValueError, match="Invalid N"):
            CronParser("0 9 ? * 1#6")

    def test_n_zero_raises(self):
        """N = 0 报错"""
        with pytest.raises(ValueError, match="Invalid N"):
            CronParser("0 9 ? * 1#0")

    def test_weekday_gt_7_raises(self):
        """D > 7 报错"""
        with pytest.raises(ValueError, match="Invalid weekday"):
            CronParser("0 9 ? * 8L")

    def test_hash_in_dom_raises(self):
        """# 出现在 day-of-month 报错"""
        with pytest.raises(ValueError):
            CronParser("0 9 1#2 * *")

    def test_mutex_hash_with_specific_dom_raises(self):
        """day-of-week 用 # 且 day-of-month 非 ?/* → ValueError"""
        with pytest.raises(ValueError, match="day-of-month 必须是"):
            CronParser("0 9 15 * 1#2")

    def test_mutex_L_with_specific_dow_raises(self):
        """day-of-month 用 L 且 day-of-week 非 ?/* → ValueError"""
        with pytest.raises(ValueError, match="day-of-week 必须是"):
            CronParser("0 0 L * 1")

    def test_mutex_LW_with_specific_dow_raises(self):
        """day-of-month 用 LW 且 day-of-week 非 ?/* → ValueError"""
        with pytest.raises(ValueError, match="day-of-week 必须是"):
            CronParser("0 0 LW * 1")

    def test_hash_with_star_dom_ok(self):
        """day-of-week 用 # 且 day-of-month 是 * → 合法"""
        p = CronParser("0 9 * * 1#2")
        assert p.get_next(datetime(2026, 8, 1, 0, 0)) == datetime(2026, 8, 10, 9, 0)

    def test_L_with_star_dow_ok(self):
        """day-of-month 用 L 且 day-of-week 是 * → 合法"""
        p = CronParser("0 0 L * *")
        assert p.get_next(datetime(2026, 8, 1, 0, 0)) == datetime(2026, 8, 31, 0, 0)

    def test_dow_mixed_standard_and_modifier_raises(self):
        """day-of-week 标准值与 #/L 修饰符混用 → ValueError（避免静默丢弃标准值）"""
        with pytest.raises(ValueError, match="不支持标准值与 #/L 修饰符混用"):
            CronParser("0 9 ? * 1,5L")
```

- [ ] **Step 2: 运行测试，确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py::TestBoundaries -v`
Expected: 10 个全 PASS

> 注意 `test_hash_in_dom_raises`：`1#2` 在 day-of-month 字段，现在由 `__init__` 的 `elif "#" in dom_raw` 分支显式抛 `ValueError("# 修饰符不能出现在 day-of-month 字段")`，测试用 `pytest.raises(ValueError)` 无 `match` 即可通过。

- [ ] **Step 3: 运行全部测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py -v`
Expected: 38 个全 PASS

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_cron_parser.py
git commit -m "test(cron): 边界与互斥校验测试（无效修饰符/互斥报错/合法组合）"
```

---

## Task 6: 全量回归测试

确保整个 scheduler 测试套件无破坏。

**Files:**
- 无修改，仅运行

- [ ] **Step 1: 运行 scheduler 相关全部测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_cron_parser.py tests/test_scheduler_service.py tests/test_scheduler_overdue.py tests/test_scheduler_group_push.py tests/test_scheduler_frontend_ready.py tests/test_scheduler_message_sse.py -v`
Expected: 全部 PASS（cron_parser 测试 38 个 + 其余原有测试不破坏）

- [ ] **Step 2: 如有失败，定位修复**

若有原有测试因 `cron_parser.py` 改动而失败，最可能原因：
- `_matches` 标准分支逻辑被破坏 → 对照 Task 2 Step 3(b) 的标准分支，确认与原逻辑一致
- `__init__` 属性名变化 → 检查 `day_of_week`/`day_of_month`/`_dom_wildcard`/`_dow_wildcard` 仍正确设置

修复后重新运行 Step 1。

---

## Task 7: 更新 Agent 提示词

更新主 Agent 和子 Agent 提示词，告知新能力。

**Files:**
- Modify: `config/agents/niu.md:187`（`background_script` 段后插入）
- Modify: `config/agents/event-manager.md`（末尾追加章节）

- [ ] **Step 1: 主 Agent 提示词加能力说明**

修改 `config/agents/niu.md`，在第 187 行（`background_script` 段）后、第 189 行（`# 智能家居通知`）前插入：

```markdown

定时任务支持高级调度模式：每月第 N 个周几（如"每月第二个周一"）、每月最后一个周几、每月最后一个工作日。具体 cron 语法由 event-manager 子 Agent 处理，你只需知道有此能力，遇到相关需求时委派给 event-manager 即可。
```

- [ ] **Step 2: 子 Agent 提示词加"高级 cron 修饰符"章节**

在 `config/agents/event-manager.md` 末尾（第 50 行后）追加：

```markdown

## 高级 cron 修饰符

除了标准 cron 语法，`cron_expr` 还支持以下高级修饰符，用于表达"每月第几周"等标准 cron 无法表达的模式：

### `#` — 每月第 N 个周几

语法：`D#N`（写在 day-of-week 字段，即第 5 字段）
- D = 周几（0=周日, 1=周一, ..., 6=周六, 7=周日）
- N = 第几个（1-5）
- day-of-month 字段必须填 `?` 或 `*`

示例：
- `0 9 ? * 1#2` = 每月第 2 个周一 9:00
- `0 9 ? * 5#1` = 每月第 1 个周五 9:00
- `0 9 ? * 1#1,1#3` = 每月第 1 和第 3 个周一 9:00（逗号组合）

注意：`#5` 在某些月份（如只有 4 个该周几的月）不会触发，属正常行为，会跳到下个月。

### `L` — 每月最后一个

两种用法：
- `DL`（day-of-week 字段）= 每月最后一个周 D。如 `5L` = 每月最后一个周五。
- `L`（day-of-month 字段）= 每月最后一天。如 `0 0 L * *` = 每月最后一天 0:00。

示例：
- `0 17 ? * 5L` = 每月最后一个周五 17:00
- `0 0 L * *` = 每月最后一天 0:00

### `LW` — 每月最后一个工作日

语法：`LW`（写在 day-of-month 字段）= 每月最后一个工作日（周一到周五）。
- day-of-week 字段必须填 `?` 或 `*`
- 若月末是周六，取周五；若月末是周日，取周五。

示例：
- `0 0 LW * *` = 每月最后一个工作日 0:00
- `0 18 LW * *` = 每月最后一个工作日 18:00

### 常见场景翻译

| 用户说 | cron_expr |
|--------|-----------|
| 每月第二个周一上午9点 | `0 9 ? * 1#2` |
| 每月最后一个周五下午5点 | `0 17 ? * 5L` |
| 每月最后一个工作日 | `0 0 LW * *` |
| 每月第一天和最后一个周五 | 分两个任务：`0 0 1 * *` + `0 0 ? * 5L` |

### 注意事项

- 使用 `#`/`L`（day-of-week）时，day-of-month 必须 `?` 或 `*`；使用 `L`/`LW`（day-of-month）时，day-of-week 必须 `?` 或 `*`。违反会报错。
- `?` 等同于 `*`，表示"不限制"。
- 这些修饰符可与逗号组合（如 `5L,1#2`）。
```

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add config/agents/niu.md config/agents/event-manager.md
git commit -m "docs(agents): 提示词补充 cron 高级修饰符说明

- niu.md: 主 Agent 加一句能力说明
- event-manager.md: 新增 # / L / LW 语法章节与翻译示例"
```

---

## 验收清单

对照 spec 的验收标准：

- [ ] `0 9 ? * 1#2` 正确触发"每月第 2 个周一 9:00"（Task 2）
- [ ] `0 17 ? * 5L` 正确触发"每月最后一个周五 17:00"（Task 3）
- [ ] `0 0 LW * *` 正确触发"每月最后一个工作日 0:00"（Task 4）
- [ ] 现有 cron 表达式行为不变（Task 1 回归 + Task 6 全量）
- [ ] `tests/test_cron_parser.py` 全部通过（Task 6）
- [ ] 无新依赖引入（全程仅用标准库 `calendar`）
- [ ] Agent 提示词已更新（Task 7）
