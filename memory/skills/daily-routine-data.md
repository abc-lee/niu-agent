---
name: daily-routine-data
description: Use when user wants routine info silently available (weather/hot list) — 例行数据, 轻提醒, daily区域, 静默定时任务, 天气播报
status: active
created: 2026-09-08
---

# 例行数据 daily 区域（轻提醒）

## Overview

memory.json 顶层 `daily` 键是"例行数据轻提醒区"：后台定时脚本写入例行信息（如天气）→ 你每轮的动态块出现一行 `[例行数据] N 项：key〈text〉...`（只显示未过期条目）→ 过期自动退场。用户提起相关场景时你主动关联提醒，没提起时不要主动播报。

本 Skill 只讲 daily 特有部分。background_script 机制本身（60s 超时、cwd=scripts/、print 语义、schedule_task 参数等）见 `~/.niu/skills/scheduled-tasks.md`，不重复。

## 两条写入通道

`daily_set(key, text, expires_at)` 是同一份实现的两种调法——脚本 `import` 的是与 MCP 工具完全相同的模块级函数，同一份校验/清理/上限/原子写保护逻辑，不是另一套实现。

### 通道一：脚本 import 直调（主通道）

background_script 脚本不能用 MCP 工具，但可以 `import` 模块级函数直调（普通 Python 函数调用，不走 MCP 协议、不需要 MCP server 运行）。完整可抄的脚本骨架：

```python
import niu_api, sys
from pathlib import Path
sys.path.insert(0, str(Path(niu_api.__file__).resolve().parent.parent / "mcp-servers" / "memory-server" / "src"))
from niu_memory_server import daily_set

try:
    # ... 获取数据、精炼为 ≤100 字符的单行文本 ...
    r = daily_set("weather", "北京 晴 22-32°C 午后雷阵雨", "2026-09-09T07:00:00")
    if r.get("status") != "ok":
        print(f"写入 daily 区域失败: {r}")  # print → [定时任务] 消息通知主 Agent
except Exception as e:
    print(f"写入 daily 区域异常: {e}")  # 意外异常兜底
```

设计内失败（key 非法/text 超长/区域写满 20 key/memory.json 损坏）不抛异常、返回 `{"status": "error", ...}` 字典——**必须检查返回值**，否则错误被静默吞掉（stdout 空 + exit 0 = 调度器判"无事静默"，写入失败永远无人知晓）。

脚本头部三行是 sys.path 推导（bundle/dev 两布局通吃）：脚本进程能直接 `import niu_api`，但 `mcp-servers/` 不在 sys.path，需按 niu_api 的位置推导插入。

### 通道二：主 Agent disk 调用（次要通道）

你自己可经虚拟磁盘调 hidden MCP 工具手动写，适合用户当面说的即时场景：

```
disk("/memory/daily_set traffic_limit 今天限行尾号3和8 2026-09-08T20:00:00")
disk("/memory/daily_delete traffic_limit")
```

## When to use

| 场景 | 用哪个 |
|------|--------|
| 例行、低打扰信息：天气、热榜、汇率、限行 | daily 区域（本 Skill） |
| 需要到点打扰用户的事：吃药、开会、缴费 | scheduler reminder（见 scheduled-tasks.md） |
| 需要 Agent 推理的周期性工作：日志整理、数据分析 | subagent 定时任务（见 scheduled-tasks.md） |
| 用户明确要记住的长期事实/偏好 | `/memory/user_memory_remember` |

## How to create

三步（background_script 的通用规则照 scheduled-tasks.md）：

1. **写 Python 脚本到 `{workspace}/scripts/`**——先 `ls {workspace}/scripts/` 检查已有文件，避免覆盖同名脚本。脚本结构：sys.path 推导三行（见上）→ 获取数据 → 精炼为 ≤100 字符单行文本 → `daily_set(key, text, expires_at=下次运行前)`。
2. **手动运行验证一次**（scheduled-tasks.md 既有纪律）——静默失败风险高：脚本逻辑 bug 导致空输出时调度器认为"无事静默"，问题长期不暴露。确认动态块出现 `[例行数据]` 行后再建任务。
3. **经 `chat-with-event-manager` 建 `task_kind='background_script'` 定时任务**（script_file=文件名 + cron_expr）。

**静默纪律**：正常路径不 print（stdout 空 + 退出码 0 = 静默）；写入失败/数据获取失败时 print 错误（→ `[定时任务]` 消息通知你，含前端提醒）。超时上限 60s。

现成参照实例：`~/.niu/work/scripts/check_mail.py`（每小时查邮件，无新邮件静默）。

## 上限与指针模式

- **上限**：20 个 key；text ≤100 字符（超长报错不写入，写入前换行自动替换为空格）。写满 20 个新 key 会报错——先 `daily_delete` 或等过期。
- **大内容指针模式**：内容放不进 100 字符时（如 HN 热榜 top10），脚本把完整内容写到 `~/.niu/tmp/` 临时文件，daily text 写指针（如 `HN热榜已更新 详见 ~/.niu/tmp/hn-hot.md`）；用户提起时你经 read 取详情。
- **tmp 24h 清理约束**：`~/.niu/tmp/` 每日清理 mtime>24h 的文件——指针模式的 `expires_at` 不要超过 24h。

## Examples

完整天气示例（每天早上 7 点写入当天天气，次日 7 点过期）：

```python
# {workspace}/scripts/weather_daily.py
import niu_api, sys, json, urllib.request
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(niu_api.__file__).resolve().parent.parent / "mcp-servers" / "memory-server" / "src"))
from niu_memory_server import daily_set

try:
    # 获取天气数据（此处以示例 API 示意，换成真实数据源）
    with urllib.request.urlopen("https://api.example.com/weather?city=beijing", timeout=20) as r:
        data = json.loads(r.read())
    text = f"北京 {data['cond']} {data['low']}-{data['high']}°C {data['note']}"[:100]

    # 过期时间 = 明天早上 7 点（下次运行前）
    expires = (datetime.now().replace(hour=7, minute=0, second=0) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    r = daily_set("weather", text, expires)
    if r.get("status") != "ok":
        print(f"天气例行数据写入失败: {r}")  # 设计内失败是 error 字典，必须检查返回值
    # 正常路径不 print → 静默
except Exception as e:
    print(f"天气例行数据写入异常: {e}")  # → [定时任务] 通知主 Agent
```

手动验证：先在 scripts 目录手动跑一遍，确认下轮动态块出现 `[例行数据] 1 项：weather〈北京 晴 22-32°C 午后雷阵雨〉`。

建任务（经 chat-with-event-manager）：

```
schedule_task(
  task_kind='background_script',
  script_file='weather_daily.py',
  content='每天早上写入当天天气到例行数据区',
  cron_expr='0 7 * * *',
  is_recurring=true
)
```
