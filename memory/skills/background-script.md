---
name: background-script
description: Use when user asks to run scheduled background tasks silently, periodic cleanup, periodic checking (email/messages), or any task that should only notify on output. 后台静默定时任务, 脚本定时执行
status: active
created: 2026-07-31
last_tested: 2026-07-31
---

# Background Script（后台静默定时任务）

## Overview

`background_script` 是定时任务的一种类型，与 `reminder`（提醒式）并列。到点时调度器执行一段你预先写好的 Python 脚本：

- **脚本无输出（stdout 空 + 退出码 0）→ 静默**，不打扰任何人
- **脚本有输出（stdout 非空）→ 通知主 Agent**（含前端 SSE + 蹦高提醒 + IM 推送）
- **脚本报错（异常/非零退出/超时）→ 报错文本通知主 Agent** + 失败计数

适用场景：定时清理（无异常就静默）、定期检查邮件/消息（无新内容静默，有才通知处理）、监控类任务。

## When to use

| 场景 | 用哪个 |
|------|--------|
| 到点提醒用户做事 / 需要主 Agent 思考决策 | `reminder` |
| 代码能搞定、不需要 Agent 推理、无事静默有事才报 | `background_script` |
| 定时清理临时文件 | `background_script` |
| 定期检查邮箱新邮件 | `background_script` |
| 每天早上汇报天气 | `reminder`（需要 Agent 组织语言） |

## How to create

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

## Script writing rules

- **`print()` 输出 = 通知主 Agent（含 IM）**；不 print 且退出码 0 = 静默。用 `print()` 精确控制是否通知。
- **异常 / 非零退出 / 超时 = 报错通知**：报错文本（含 traceback，stderr 合并进 stdout）会随通知发给主 Agent。recurring 任务连续 3 次失败标 failed；one-time 任务报错会永久删除任务（避免无限重试，修复脚本后需重建任务）；脚本文件丢失也会永久删除任务。
- **stdout 注入主 Agent 时截断 2000 字符**，长输出请自行截断或写文件后 print 文件路径。
- **cwd = `{workspace}/scripts/`**，脚本可用相对路径读写同目录文件（如 `open('data.json')`）。但 **不能直接 `import` 同目录其他 .py 文件**——code_run 把代码写到临时文件执行，`sys.path[0]` 是临时目录而非 cwd。多文件脚本需用 `exec(open('helper.py').read())` 或合并成单文件。
- **超时 60 秒**（code_run 默认），超时进程被杀、stdout 追加 `[Timeout Error]` 后作为报错通知。长任务请拆分。
- **运行环境**：项目自带的 Python 解释器与已装依赖（numpy/opencv/requests 等均可直接 import）。

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

## What happens on trigger

调度器到点触发时：
1. 读取 `{workspace}/scripts/{script_file}`
2. 用 `code_run` 执行（cwd=scripts 目录，超时 60s）
3. 判定输出：
   - **无输出** → 静默，你（主 Agent）无感知
   - **有输出** → 你会收到一条 `[定时任务]` 开头的消息，内容是脚本的 stdout。按内容正常处理即可（如整理邮件、报告异常等）。这条消息同时触发前端提醒与 IM 推送。
