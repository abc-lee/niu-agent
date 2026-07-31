# Cron 高级修饰符支持（`#`/`L`/`LW`）设计

> 日期：2026-07-31
> 状态：已确认，待写实现计划

## 背景与目标

定时任务子系统（scheduler）使用自研 `CronParser`（`niu_api/internal/scheduler/cron_parser.py`，99 行）解析 5 字段标准 cron 表达式。现有能力覆盖：每 N 分钟/小时、每天某时刻、每周几、每月几号、每年某月某日、以及 cron 的逗号/范围/步进组合。

**缺口**：标准 5 字段 cron 无法表达以下常见调度模式：
- 每月第 N 个周几（如"每月第 2 个周一"）
- 每月最后一个周几（如"每月最后一个周五"）
- 每月最后一个工作日

**目标**：扩展 `CronParser` 支持 Quartz 风格的 `#`、`L`、`LW` 修饰符，填补上述缺口。不引入新依赖，不改数据模型，不改 Agent 调用层。

## 范围边界

### 做
- 改造 `cron_parser.py`：`_parse_field` 和 `_matches` 识别 `#`/`L`/`LW`/`?`
- 新建 `tests/test_cron_parser.py`：单元测试覆盖新增修饰符 + 回归
- 更新 `config/agents/niu.md`：主 Agent 提示词加一句能力说明
- 更新 `config/agents/event-manager.md`：子 Agent 提示词加"高级 cron 修饰符"章节

### 不做
- 不改数据模型（`task_store.py` 不加列）
- 不改调度器（`scheduler.py` 的 `get_next` 调用方式不变）
- 不改 MCP/API（`routes.py`、`__init__.py` 参数 schema 不变）
- 不改前端（无 UI 改动）
- 不引入 `croniter` 等新依赖
- 不支持"每 N 年"（5 字段 cron 无年维度，极少见需求）
- 不支持秒级精度

## 现有机制（不改动，仅说明）

循环任务触发后，调度器调用 `_calc_next_trigger`（scheduler.py:478-487）→ `CronParser.get_next(base)`（cron_parser.py:59-78）：

```python
def get_next(self, current: datetime) -> datetime | None:
    next_time = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):   # 最多扫 366 天
        if self._matches(next_time):
            return next_time
        next_time += timedelta(minutes=1)
    return None
```

逐分钟暴力扫描 + `_matches` 判断。本设计只改 `_parse_field`（解析）和 `_matches`（判断），扫描框架完全复用。

## 设计

### 改造点：`cron_parser.py`

唯一改动文件。扩展 day-of-week 和 day-of-month 两个字段的解析与匹配。

#### 1. `?` 兼容

day-of-month 和 day-of-week 字段接受 `?`，视为 `*`（通配）。

Quartz 约定：当 day-of-week 用 `#`/`L` 时，day-of-month 应填 `?`。代码不强制此约定，`?` 等同 `*` 即可。

实现：`_parse_field` 入口处，若字段值为 `?`，按 `*` 处理。

#### 2. `#` 修饰符（第 N 个周几）

**语法**：`D#N`
- D = 周几（0-7，0=7=周日，与现有约定一致）
- N = 第几个（1-5）
- 示例：`1#2` = 每月第 2 个周一，`5#1` = 每月第 1 个周五

**解析**（`_parse_field` 扩展）：
- 检测到 token 含 `#` 时，不走原有数值解析
- 拆成 `(weekday, nth)` 二元组
- 存入新属性 `self.nth_weekdays: list[tuple[int, int]]`
- 逗号组合：`1#1,1#3` → 拆出两个 token，各自解析，都进 `nth_weekdays`

**匹配**（`_matches` 扩展）：
对候选日期 `d`，判断是否为"本月的第 N 个周 D"。

**周几编号转换**（关键，避免与 Python `weekday()` 混淆）：
```
# cron 约定: 0=周日, 1=周一, ..., 6=周六, 7=周日
# Python weekday(): 0=周一, 1=周二, ..., 6=周日
# 转换公式: py_weekday = (cron_D - 1) % 7
#   cron 0/7 → 6(周日), 1 → 0(周一), 2 → 1(周二), ..., 6 → 5(周六)
```
现有代码 cron_parser.py:90 已有反向转换 `(weekday()+1)%7`（Python→cron），本次统一用上述正向公式。

匹配逻辑：
```
py_wd = (D - 1) % 7
weekday_matches = d.weekday() == py_wd
nth_matches = (d.day - 1) // 7 + 1 == N
命中 ⟺ weekday_matches 且 nth_matches
```
例：D=1(周一), 15 号 → `(15-1)//7+1 = 2` → 第 2 个周一。

`#5` 在没有第 5 个该周几的月份自然不命中，`get_next` 扫到下个月继续，行为正确。

#### 3. `L` 修饰符（最后一个）

**语法**：
- `DL`（day-of-week 字段）= 每月最后一个周 D。如 `5L` = 每月最后一个周五。
- `L`（day-of-month 字段）= 每月最后一天。
**解析**：
- day-of-week token 以 `L` 结尾 → 取前缀数字为 weekday，存入 `self.last_weekdays: list[int]`
- day-of-month 值为 `L` → 存 `self.last_day_of_month = True`

**匹配**：
- 最后一个周 D：`py_wd = (D-1)%7`（转换同 §2），`d.weekday() == py_wd` 且 `d + timedelta(days=7)` 的月份 ≠ `d` 的月份（即本月最后一个该周几）
- 最后一天：`d.day == calendar.monthrange(d.year, d.month)[1]`

#### 4. `LW` 修饰符（最后一个工作日）

**语法**：`LW`（day-of-month 字段）= 每月最后一个工作日（周一到周五）。

**解析**：day-of-month 值为 `LW` → 存 `self.last_workday = True`。

**匹配**：
计算本月最后一个工作日：
```
last_day = calendar.monthrange(d.year, d.month)[1]
last_date = date(d.year, d.month, last_day)
weekday = last_date.weekday()  # 0=周一 ... 6=周日
if weekday == 5:      # 周六 → 前移 1 天
    last_workday = last_day - 1
elif weekday == 6:    # 周日 → 前移 2 天
    last_workday = last_day - 2
else:
    last_workday = last_day
命中 ⟺ d.day == last_workday
```

#### 5. 互斥校验与 OR 语义

现有 `_matches`（cron_parser.py:96-98）：当 day-of-month 和 day-of-week 都非 `*` 时走 OR 逻辑（任一命中即触发）。

**互斥强制校验**（解析期，消除歧义）：
- 若 day-of-week 用了 `#`/`L`（`nth_weekdays` 或 `last_weekdays` 非空），day-of-month 必须是 `?`/`*`，否则 `CronParser.__init__` 抛 `ValueError`。
- 若 day-of-month 用了 `L`/`LW`，day-of-week 必须是 `?`/`*`，否则抛 `ValueError`。
- 违反时错误信息明确指出哪侧用了高级修饰符、另一侧应填 `?`。

**匹配规则**：
- 上述高级修饰符场景：相应侧独立判断，**不做 OR**。
- 其余情况（双方都是普通数值/范围/步进）：保留现有 OR 语义。

### 测试：`tests/test_cron_parser.py`（新建）

纯单元测试，直接测 `CronParser`，无需 LLM。用 `python/bin/python -m pytest` 运行。

**`#` 修饰符：**
- `0 9 ? * 1#2` 从月初算 → 第 2 个周一
- `0 9 ? * 5#1` → 第 1 个周五
- `#5` 在无第 5 个的月份（如 2 月只有 4 个周一）→ 跳到下个月
- 逗号组合 `1#1,1#3` → 两个日期都命中
- 跨月滚动：`0 9 ? * 1#2` 触发后 `get_next` → 下个月第 2 个周一

**`L` 修饰符：**
- `0 17 ? * 5L` → 每月最后一个周五（覆盖 28/29/30/31 天月份）
- `0 0 L * *` → 每月最后一天（2 月闰年 29、平年 28）
- 混合 `5L,1#2` → 两个条件都命中

**`LW` 修饰符：**
- 月末是工作日 → 就是月末那天
- 月末是周六 → 前移到周五
- 月末是周日 → 前移到周五
- 实测 2026 年各月

**`?` 兼容：**
- `0 9 ? * 1#2` 与 `0 9 * * 1#2` 行为一致
- day-of-month `?` + day-of-week `*` 正常

**回归（现有模式不破坏）：**
- `*/5 * * * *`、`0 8 * * *`、`0 0 1 */3 *`、`0 9 * * 1` 等

**边界：**
- `1#6`（N>5）、`8L`（D>7）→ 解析报错或 `get_next` 返回 None
- `#` 出现在 day-of-month 字段 → 报错
- **互斥校验**：`0 9 15 * 1#2`（day-of-month 非 `?`/`*` 且 day-of-week 用 `#`）→ `ValueError`；`0 0 L * 1`（day-of-month 用 `L` 且 day-of-week 非 `?`/`*`）→ `ValueError`

### Agent 提示词

**主 Agent `config/agents/niu.md`**：在定时任务能力描述处加一句：
> 支持每月第 N 个周几（如 `1#2`=第2个周一）、每月最后一个周几（`5L`=最后周五）、每月最后一个工作日（`LW`）。具体 cron 语法由 event-manager 子 Agent 处理。

只让主 Agent 知道"有此能力"，不教语法。

**子 Agent `config/agents/event-manager.md`**：新增"高级 cron 修饰符"章节：
- `D#N` 语法：D=周几(0-7,0=周日)，N=第几个(1-5)，day-of-month 须填 `?`
- `DL` 语法：每月最后一个周 D
- `LW` 语法（仅 day-of-month）：每月最后一个工作日
- 翻译示例："每月第二个周一上午9点" → `0 9 ? * 1#2`
- 注意：`#5` 某些月不触发属正常

## 数据流（不变）

```
Agent → schedule_task(cron_expr="0 9 ? * 1#2") → task_store 存原样
→ scheduler 到期 → _calc_next_trigger → CronParser.get_next → 写回下次时间
```

`cron_expr` 仍是唯一时间字段，`#`/`L`/`LW` 塞进字符串，对上下游完全透明。

## 验收标准

1. `0 9 ? * 1#2` 能正确触发"每月第 2 个周一 9:00"
2. `0 17 ? * 5L` 能正确触发"每月最后一个周五 17:00"
3. `0 0 LW * *` 能正确触发"每月最后一个工作日 0:00"
4. 现有 cron 表达式（`*/5 * * * *`、`0 8 * * *` 等）行为不变
5. `tests/test_cron_parser.py` 全部通过
6. 无新依赖引入
