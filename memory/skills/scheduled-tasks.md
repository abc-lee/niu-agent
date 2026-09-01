---
name: scheduled-tasks
description: Use when user asks to create any scheduled task — reminder, silent background script, or silent sub-agent execution; periodic cleanup/checking, recurring work. 定时任务创建, 提醒, 后台静默脚本, 子Agent静默执行
status: active
created: 2026-07-31
last_tested: 2026-09-01
---

# 创建定时任务

## Overview

定时任务有三种执行类型（`task_kind`），统一经 `chat-with-event-manager` 子 Agent 调 `schedule_task` 登记：

| task_kind | 执行语义 | 通知机制 |
|-----------|---------|----------|
| `reminder` | 到点发消息给你（主 Agent），你思考并干活 | 你收到 `[定时任务] {content}` 消息，按内容正常处理 |
| `background_script` | 到点后台执行 `{workspace}/scripts/` 下的 Python 脚本 | 无输出（stdout 空 + 退出码 0）静默；有输出/报错才通知你（含前端提醒 + IM 推送） |
| `subagent` | 到点后台静默调起指定子 Agent 执行 `content` 任务文本 | 默认全程静默（结果仅落日志）；子 Agent 遇到解决不了的问题可经 report 例外通道反馈 → 你收到 `[后台任务「{任务名}」结束报告]` 消息 |

## When to use

| 场景 | 用哪个 |
|------|--------|
| 到点提醒用户做事 / 需要主 Agent 思考决策（如每天早上汇报天气） | `reminder` |
| 代码能搞定、不需要 Agent 推理、无事静默有事才报 | `background_script` |
| 定时清理临时文件 / 定期检查邮箱新邮件 | `background_script` |
| 需要 Agent 推理的周期性工作（如每日日志整理、定期数据分析） | `subagent` |

## How to create

公共参数：`content`（人类可读任务描述）、`scheduled_at`（首次触发时间，ISO 格式；"明天"等相对时间必须转成具体日期时间）、`cron_expr`（循环任务必填）、`is_recurring`（True=按 cron 周期重复 / False=一次性，触发后自动删除）。

### reminder

直接传内容：`schedule_task(task_kind='reminder', content='...', scheduled_at=..., ...)`。

### background_script

1. **写 Python 脚本**，存到工作目录的 `scripts/` 子目录下（即 `{workspace}/scripts/`）。
   - **先 `ls {workspace}/scripts/` 检查已有文件，避免覆盖同名脚本**
   - 目录不存在时自行创建
2. **调 `chat-with-event-manager`** 子 Agent，让它创建任务：
   ```
   schedule_task(
     task_kind='background_script',
     script_file='你的脚本.py',
     content='任务描述（人类可读）',
     cron_expr='0 3 * * *',        # 每天 3 点
     is_recurring=true
   )
   ```

### subagent

1. **确认目标子 Agent 存在**：`agent_name` 必须是 `config/agents/` 或 `~/.niu/agents/` 下真实存在的 md（创建时校验，不存在会报错）。主 Agent/用户指定什么名字就原样传，不要猜改。
2. **新后台任务建议在用户层建专用后台子 Agent**：写 `~/.niu/agents/{name}.md`，最终 frontmatter 带 `visibility: hidden`（只挡它进你的工具列表——无 chat-with-xxx，不挡定时按名调起）——
   - **先测后藏**：先不带 `visibility: hidden` 创建，用 chat-with-{name} 交互测试任务文本；测通后再加 `visibility: hidden` 转后台。一旦 hidden 你就调不到它了（程序定时按名调起不受影响）；
   - report 例外通道教学写在该 md 里（见下节），不要写进普通子 Agent 的 md。
3. **调 `chat-with-event-manager`**：
   ```
   schedule_task(
     task_kind='subagent',
     agent_name='{name}',          # 此类型必填
     content='子 Agent 执行的任务文本（人类可读）',
     cron_expr='0 18 * * *',       # 每天 18 点
     is_recurring=true
   )
   ```

## report 例外通道（subagent 类型，你的视角）

你创建的后台子 Agent 有例外反馈通道（教学写在后台子 Agent 自己的 md 里，普通子 Agent 不知道此语法）：

- **默认静默**：任务完成后子 Agent 直接 `@end` 退出、零打扰——不是每次结束都要带 report。
- 仅当它遇到自己解决不了、必须让你知道的问题（如数据库持续失败、文件损坏），在最终回复携带：`汇报正文 @end {"report": "内容"}`（@end 后直接跟 JSON 对象）。
- **收到 `[后台任务「{任务名}」结束报告]` 开头的消息**：这是后台任务的结束报告，不是用户消息。子 Agent 已退出（单向通知，无需回复或接续）——你自行处置：转达用户 / 处理 / 忽略。

## Script writing rules（background_script）

- **`print()` 输出 = 通知主 Agent（含 IM）**；不 print 且退出码 0 = 静默。用 `print()` 精确控制是否通知。
- **异常 / 非零退出 / 超时 = 报错通知**：报错文本（含 traceback，stderr 合并进 stdout）会随通知发给主 Agent。recurring 任务连续 3 次失败标 failed；one-time 任务报错会永久删除任务（避免无限重试，修复脚本后需重建任务）；脚本文件丢失也会永久删除任务。
- **stdout 注入主 Agent 时截断 2000 字符**，长输出请自行截断或写文件后 print 文件路径。
- **cwd = `{workspace}/scripts/`**，脚本可用相对路径读写同目录文件（如 `open('data.json')`）。但 **不能直接 `import` 同目录其他 .py 文件**——code_run 把代码写到临时文件执行，`sys.path[0]` 是临时目录而非 cwd。多文件脚本需用 `exec(open('helper.py').read())` 或合并成单文件。
- **超时 60 秒**（code_run 默认），超时进程被杀、stdout 追加 `[Timeout Error]` 后作为报错通知。长任务请拆分。
- **运行环境**：项目自带的 Python 解释器与已装依赖（numpy/opencv/requests 等均可直接 import）。
- **创建脚本后务必手动运行验证一次**，确认输出符合预期——静默失败风险高：脚本逻辑 bug（如解析错误）导致输出为空时，调度器会认为"无事静默"，问题长期不暴露。

## Examples

### 例1：静默清理临时文件

```python
# {workspace}/scripts/clean_tmp.py
import os, glob, shutil

tmp_dir = os.path.expanduser("~/Downloads/tmp")
removed = 0
for f in glob.glob(os.path.join(tmp_dir, "*")):
    try:
        if os.path.isdir(f):
            shutil.rmtree(f)
        else:
            os.remove(f)
        removed += 1
    except Exception:
        pass  # 单个失败不报错，继续

# 不 print → 静默。除非你想记录清理数量：
# print(f"已清理 {removed} 个临时文件")
```

### 例2：检查邮件（有新邮件才通知）

```python
# {workspace}/scripts/check_mail.py
import imaplib, email

conn = imaplib.IMAP4_SSL("imap.example.com")
conn.login("user", "pass")
conn.select("INBOX")
typ, data = conn.search(None, "UNSEEN")
ids = data[0].split()
conn.logout()

if not ids:
    # 无新邮件，不 print → 静默
    pass
else:
    # 有新邮件，print 摘要 → 通知主 Agent 处理
    print(f"收到 {len(ids)} 封新邮件，请处理")
```

## What happens on trigger（background_script）

调度器到点触发时：
1. 读取 `{workspace}/scripts/{script_file}`
2. 用 `code_run` 执行（cwd=scripts 目录，超时 60s）
3. 判定输出：
   - **无输出** → 静默，你（主 Agent）无感知
   - **有输出** → 你会收到一条 `[定时任务]` 开头的消息，内容是脚本的 stdout。按内容正常处理即可（如整理邮件、报告异常等）。这条消息同时触发前端提醒与 IM 推送。
