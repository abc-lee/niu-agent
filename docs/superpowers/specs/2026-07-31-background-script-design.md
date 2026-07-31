# 后台静默定时任务（background_script）设计

> **日期**: 2026-07-31
> **状态**: 已确认，待写实现计划
> **目标**: 新增一种可静默执行的定时任务类型，让主 Agent 写一段 Python 代码，定时触发执行；无输出则静默循环，有输出（含报错）才通知主 Agent（含 IM 推送）。现有 reminder 任务模式完全不变。

## 背景与动机

现有定时任务（`reminder`）触发后必然走 `ChatQueue.enqueue_and_wait` 注入 `[定时任务] xxx` 给主 Agent → 主 Agent 回复 → SSE 推前端 + 小女孩蹦高 + IM 推送。整条链路天然"必响"。

但很多定时任务不需要每次都通知：
- **定时清理**：无异常时无需任何输出，报错才通知
- **定期检查邮件/消息**：无新邮件静默，有新邮件才通知主 Agent 处理

需要一个"无事静默、有事才报"的任务类型。代码执行能力已存在（`agent/handler.py` 的 `code_run`，subprocess 跑 python/bash，签名含 `cwd` 参数），无需新造执行器。

## 核心决策（用户确认）

| 决策点 | 选择 |
|--------|------|
| 任务类型名 | `background_script`（与现有 `reminder` 并列） |
| 通知方式 | 有输出走现有 `enqueue_and_wait`（自动获得 SSE + 蹦高 + IM 三件套） |
| 代码执行 | 复用 `handler.code_run`，`cwd` 设为 `{workspace}/scripts/` |
| 失败处理 | 报错 = 失败 + 通知（code_run `status='error'` 或 `exit_code!=0` → stdout 全文注入主 Agent；recurring 返回 None 走失败计数器，连续 3 次标 failed；one-time 报错永久删除任务避免 retry_failed_tasks 无限重置，用户修复脚本后需重建任务） |
| 触发器 | 复用现有 cron 5 字段（interval/复杂计划任务放下个工程） |
| 创建者 | 主 Agent 写脚本代码；event-manager 子 Agent 调 `schedule_task` 创建任务（创建流程不变） |
| 代码传递 | 代码以文件存 `{workspace}/scripts/`，schedule_task 只存文件名 `script_file` |
| 输出判定 | code_run 返回 dict：`status='success'` 且 `exit_code==0` 且 stdout 空 → 静默；否则（有 stdout 或 status='error'）→ stdout 全文注入主 Agent通知。stdout 含合并的 stderr（code_run 用 `stderr=STDOUT`） |
| Skill 落地 | PM 编写系统级 skill `memory/skills/background-script.md`。niu.md L180 规定主 Agent 不得自发创建 skill（dream-evolver 统一负责），但有例外条款"如果用户明确要求你自己创建一个 skill，你可以创建"。本次由 PM（非主 Agent 运行时自发）按用户明确要求编写，属该例外范畴，豁免成立 |

## 架构（方案 A：trigger_callback 内联分支）

新增 `background_script` 与 `reminder` 共享同一张表、同一个调度器循环、同一个 `trigger_callback` 入口，仅在回调内部按 `task_kind` 分流。

### 数据流（background_script 触发时）

```
scheduler _run_loop (10s 轮询，不变)
  └─ CAS pending→in_progress
  └─ trigger_callback(task)
       └─ if task_kind == 'background_script':
            ├─ 读 {workspace}/scripts/{task.script_file}
            ├─ script_file 不存在 → delete_task_permanent 永久删除 + 返回 None（不走 retry，避免无限重试）
            ├─ from agent.handler import code_run  # 模块级纯函数，scheduler 线程直接同步调用
            ├─ result = code_run(code, cwd=str(scripts_dir))  # 返回 dict
            ├─ result['status']=='error' 且无 'stdout' 键（进程启动失败）→ output = result.get('msg','启动失败')
            │  否则 output = result.get('stdout','').strip()
            ├─ status=='success' 且 exit_code==0 且 output 为空?
            │   ├─ 是 → 静默返回成功（不 enqueue、不 SSE、不蹦高、不 IM）
            │   └─ 否 → enqueue_and_wait("[定时任务] {output[:2000]}", source='scheduler')  # 走现有链路 → SSE + 蹦高 + IM
            └─ status=='error' 或 exit_code!=0 → 同样走 enqueue（output 含报错/超时文本）；recurring 返回 None 走失败计数器（3 次阈值）；one-time 报错永久删除任务（delete_task_permanent，避免 retry_failed_tasks 无限重置）
       └─ else (reminder):
            └─ 原逻辑不变
```

### 关键不变量

- 调度器 `scheduler.py` **完全不感知** task_kind（分支在 `trigger_callback`，不在调度循环）
- 静默分支不调 `enqueue_and_wait` → 天然不推 SSE、不蹦高、不 IM
- `code_run` 复用现有实现，只多传 `cwd` 参数（签名已有 `cwd=None`）；返回 dict `{status, stdout, exit_code}`，stderr 合并进 stdout（`stderr=STDOUT`），不拆分管道、不改 code_run
- `code_run` 在 `service.py` 内 `from agent.handler import code_run` 直接同步调用（纯函数，subprocess 阻塞，适合 scheduler 线程池；niu_api 已加载 agent.handler 无循环依赖）
- stdout 注入主 Agent 时截断 2000 字符（code_run 内部已有 10000 截断，定时注入再收窄到 2000 防撑爆上下文，超出加 `…[截断]` 提示）
- enqueue_and_wait 传 `source='scheduler'`（与 reminder 一致；chat_queue 内部强制将 assistant 回复 source 改写为 'electron' 推 SSE，故 source 值不影响三件套）
- background_script 分支 prompt 仅含 stdout 文本，`content` 字段不参与 prompt 构建（仅用于 add_pending_alert 蹦高摘要与人类可读描述）
- workspace 路径获取：`workspace = Path(get_db_path()).parent`，`scripts_dir = workspace / 'scripts'`（get_db_path 已在 service.py 内，读 ~/.niu/memory.json 的 workspace.path，db_path 父目录即 workspace）
- recurring background_script 静默成功后与 reminder 一样算下次 cron 回 pending；one-time background_script 静默成功后与 reminder 一样硬删除

## 数据模型变更

### `scheduled_tasks` 表新增 2 列

| 列名 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `task_kind` | TEXT | `'reminder'` | `reminder`（现有）/ `background_script`（新增） |
| `script_file` | TEXT | NULL | 脚本文件相对路径（仅 background_script 用，如 `clean_tmp.py`） |

迁移用 ALTER TABLE（沿用现有迁移模式）。不新增 `code` 列——代码以文件形式存在 `{workspace}/scripts/`，表里只存文件名。

### 约定

- `{workspace}/scripts/` 目录由主 Agent 写脚本时自行创建（大模型会自己判断，不在代码里替它 mkdir）
- `script_file` 存相对路径，触发时拼接 `{workspace}/scripts/{script_file}`
- 文件不存在 = 永久性失败：`_trigger_background_script` 内调 `delete_task_permanent` 永久删除该任务 + 日志告警 + 返回 None（一次性任务删后即结束，recurring 任务删后不再触发；属"配置错误"非"瞬时失败"，不走 retry_failed_tasks，避免无限重试。用户恢复脚本需重新创建任务）

### TaskStore 改动

`create_task`/`get_task`/`list_tasks`/`update_task` 同步加 `task_kind`/`script_file` 参数与返回字段。MCP schema 与 `/scheduler` API 请求模型同步。

### 不动的字段

`content`（background_script 也保留，作为人类可读描述/名称）、`cron_expr`/`is_recurring`/`scheduled_at`（触发器复用）、`status` 全套、`chat_id` 等。

## Skill 文件（memory/skills/background-script.md）

PM 编写的系统级 skill，格式对齐现有 skill（YAML frontmatter + Markdown 正文）。由 SkillSync 扫描进向量库，主 Agent 按需注入读取。

### Frontmatter

```yaml
---
name: background-script
description: Use when user asks to run scheduled background tasks silently, periodic cleanup, periodic checking (email/messages), or any task that should only notify on output. 后台静默定时任务, 脚本定时执行
status: active
created: 2026-07-31
last_tested: 2026-07-31
---
```

### 正文结构

1. **Overview** — 什么是 background_script、与 reminder 的区别、适用场景
2. **When to use** — 决策指引（无事静默有事报 → background_script；到点提醒/对话式 → reminder；代码能搞定 → background_script；需 Agent 思考 → reminder）
3. **How to create** — 创建流程（主 Agent 视角）：
   - 写 Python 脚本存到 `{workspace}/scripts/`（**先 ls 检查已有文件，避免覆盖同名**）
   - 调 `chat-with-event-manager`，让它调 `schedule_task(task_kind='background_script', script_file='xxx.py', content='任务描述', cron_expr=..., is_recurring=true)`
4. **Script writing rules** — 脚本编写规则：
   - `print()` 输出 = 通知主 Agent（含 IM）；不 print 且退出码 0 = 静默
   - 异常/非零退出/超时 = 报错通知（code_run 合并 stderr 进 stdout，报错文本随 stdout 注入主 Agent；recurring 连续 3 次标 failed，one-time 永久性失败如脚本文件丢失直接标 failed 不重试）
   - stdout 注入主 Agent 时截断 2000 字符，长输出自行截断或写文件
   - cwd = `{workspace}/scripts/`，可用相对路径访问同目录文件
   - 超时 60s（code_run 默认），超时进程被杀、stdout 追加 `[Timeout Error]` 后作为报错通知
   - 可用项目 `python/` 的解释器与已装依赖
5. **Examples** — 两个完整示例：
   - 清理临时文件（静默）：不 print，除非清理失败
   - 检查邮件（条件输出）：无新邮件不 print，有新邮件 print 邮件摘要
6. **What happens on trigger** — 触发时发生了什么（让主 Agent 理解链路：调度器读脚本 → code_run → 判 stdout → 有输出注入 `[定时任务] {stdout}` / 无输出静默）

## niu.md 改动

在 `# 定时任务` 段落（L183-185）追加两句概览 + 指向 skill，原有 reminder 说明不动：

```markdown
# 定时任务

以 `[定时任务]` 开头的消息是系统定时触发的任务，不是用户主动发的。按内容正常执行即可，不需要回复"收到"或确认。

除上述提醒式任务外，另支持 `background_script` 后台静默任务：到点执行一段 Python 脚本，无输出则静默、有输出（含报错）才通知你。需要创建此类任务时，阅读 `memory/skills/background-script.md` 了解用法与脚本编写规则。
```

## 改动文件清单（零新依赖）

| 文件 | 改动 |
|------|------|
| `niu_api/internal/scheduler/task_store.py` | 表加 `task_kind`/`script_file` 两列（ALTER TABLE 迁移）；create/get/list/update 同步加参数与返回字段 |
| `niu_api/internal/scheduler/service.py` | `trigger_callback` 开头加 `task_kind` 分支：background_script → 读脚本 → code_run(cwd=workspace/scripts) → stdout 判定 → 有输出 enqueue / 无输出静默；reminder 走原逻辑 |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | `schedule_task` 的 TOOL_SCHEMAS + 函数签名加 `task_kind`(enum: reminder,background_script) / `script_file` 参数 |
| `config/disk/scheduler-server.yaml` | schedule_task parameters 加 task_kind(enum)/script_file 映射 |
| `memory/skills/background-script.md` | 新建系统级 skill |
| `config/agents/niu.md` | 定时任务段落加两句概览 + 指向 skill |

### 不动的

`scheduler.py`（调度循环）、`cron_parser.py`（触发器，留给下个工程）、`chat_queue.py`/SSE/IM（静默分支不 enqueue 即天然不触发）、`handler.py` 的 code_run（只复用不改）。

## 验证（运行环境实测，真实数据）

1. 造一个 background_script 任务（cron `* * * * *`）+ 不 print 的清理脚本 → 等触发 → 确认无 SSE/无蹦高/无 IM/前端无新消息
2. 改脚本加 `print("测试输出")` → 等触发 → 确认主 Agent 收到 `[定时任务] 测试输出` + SSE + IM
3. 改脚本 `raise Exception` → 等触发 → 确认报错文本（含 traceback，经 stdout 合并）注入主 Agent + 失败计数
4. event-manager 创建链路：主 Agent 调 chat-with-event-manager 传 script_file → 确认任务入库 task_kind=background_script
5. 现有 reminder 回归：造一个 reminder 任务 → 确认行为不变

## 边界与下个工程

- 触发器仅 cron，interval/每月第几周等复杂计划任务放下个工程，本设计不动 `cron_parser.py`，留干净扩展点
- `task_kind` 字段为下个工程扩展更多任务类型预留（方案 A 分支可平滑演化为方案 B 注册表）

## 提交策略

拆两个提交：
- `feat(scheduler): background_script 后台静默定时任务`（task_store + service + MCP + disk yaml）
- `docs: background_script skill + niu.md 提示词`（skill 文件 + niu.md）
