# 同步子 Agent SSE 404 竞态修复 + 资源清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同步子 Agent（如 file-processor）启动时 SSE 连接 404 竞态（核心 bug），并补全资源清理防止长期运行时 ring buffer 泄漏和前端 tab 卡死。

**Architecture:** 在 handler.py 同步路径中，将 `pre_register`（创建 ring buffer）提到 `subagent_started` 推送之前，保证主 loop 按 FIFO 先创建 ring buffer 再广播事件。同时加 `if not answer:` 守卫使恢复路径跳过冗余操作，加 `finally` 块条件清理 ring buffer 防止资源泄漏，加 except 块 subagent_error 推送保证前端显示错误状态。

**Tech Stack:** Python 3.11, asyncio, FastAPI, Electron

---

## 两个问题

### 问题一：404 竞态（核心 bug）

同步路径（`_call_subagent_gen`）中，`subagent_started` 的 `call_soon_threadsafe` 入队①在 `pre_register`（经 `call_subagent` → `SubagentRegistry.register` → `pre_register` → `call_soon_threadsafe(_do_pre_register)`）入队②之前。

主 loop FIFO 执行：
1. 先执行入队①：广播 `subagent_started` → 前端收到 → `SubagentSSEManager.connect()` → HTTP 请求到达后端
2. `has_subagent()` 检查 `_ring_buffers` → 入队②还没执行 → 返回 False → **404**
3. 后执行入队②：`_do_pre_register` 创建 ring buffer（太晚了）

异步路径正常，因为 `register`（含 `pre_register`）在 `_dispatch_async_subagent` 中先于 `subagent_started` 推送执行。

**修复**：在 handler.py 同步路径中，`pre_register` 在 `subagent_started` 推送之前调用，保证入队顺序正确。

### 问题二：资源清理（长期运行必须）

修复问题一后，`pre_register` 由 handler 在 `call_subagent` 之前调用。这引入了新的资源生命周期问题：

**问题 2a：waiting_for_answer 挂起时 ring buffer 被误清理**
- 子 Agent 输出 `@niu-agent` → `state = 'waiting_for_answer'` → `call_subagent` 内部 finally 跳过 unregister → 正常返回
- 如果 handler 的 finally 块无条件调 close → 推送 subagent_closed → 前端关闭 tab → 但子 Agent 还活着等回答
- **修复**：finally 块检查 `state != 'waiting_for_answer'` 时才调 close

**问题 2b：恢复路径双重 close**
- 主 Agent 调 `chat-with-xxx(answer=...)` 恢复挂起子 Agent → `call_subagent` 内部 finally → unregister → close（第一次）
- handler 的 finally 也调 close（第二次）→ 前端收到两个 subagent_closed
- **修复**：`if not answer:` 守卫，恢复路径跳过 handler 的 pre_register + subagent_started + finally close

**问题 2c：register 前异常导致 ring buffer 泄漏 + 前端 tab 卡死**
- handler 的 pre_register 创建 ring buffer + subagent_started 已广播
- `call_subagent` 在 `register()` 之前异常（get_subagent_config/create_client/_build_subagent_tools_schema 抛异常）
- `SubagentRegistry.get(agent_name)` 返回 None → finally 跳过 close → ring buffer 永久泄漏 + 前端 tab 永远停在"工作中"
- **修复**：finally 块中 `instance is None` 时，用 `has_subagent(agent_name)` 检查 ring buffer 是否存在，存在则 close

**问题 2d：except 块缺 subagent_error 事件推送（既有问题）**
- `call_subagent` 异常时，handler except 块只 yield StreamEvent 到主 Agent SSE 流，不推 subagent_error 到 SubagentEventBus
- 前端 tab 显示 subagent_closed（成功状态）而非 subagent_error（错误状态）
- 异步路径正确推了 subagent_error（subagent.py L1352），同步路径应保持一致
- **修复**：except 块中加 `notify_subagent_event_sync(agent_name, 'subagent_error', ...)`

## 新增 is_closing 函数（subagent_event_bus.py）

在 `niu_api/internal/subagent_event_bus.py` 的 `has_subagent` 函数之后新增：

```python
def is_closing(unique_name: str) -> bool:
    """检查 unique_name 是否已在 close 延迟清理窗口内（_close_epochs 有记录）。

    用于 handler finally 的 else 分支区分两种 instance is None 的场景：
    - 场景 2（register 前异常）：未 close 过，is_closing=False → 需要 close
    - 场景 1/8（call_subagent 完成后已 close）：is_closing=True → 不需要再 close
    """
    return unique_name in _close_epochs
```

## 修改后完整代码

当前代码（`agent/handler.py` L1067-1158）：

```python
        # 同步子 Agent：unique_name = agent_name，推送 subagent_started 事件到主 Agent SSE 流
        try:
            from niu_api.chat import _main_loop, _sync_broadcast
            if _main_loop and not _main_loop.is_closed():
                event = {
                    'type': 'subagent_started',
                    'unique_name': agent_name,
                    'agent_name': agent_name,
                    'is_sync': True,
                }
                _main_loop.call_soon_threadsafe(_sync_broadcast, event)
        except ImportError:
            pass

        # 同步路径（现有逻辑不变）
        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            _history = None
            if agent_name == "journal-agent" and _journal_history:
                _history = _journal_history

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
                **({"context_fifo_threshold": 0} if (agent_name == "journal-agent" and _journal_history) else {}),
                answer=answer,
                answer_unique_name=(unique_name_arg or agent_name) if answer else None,
            )

            # journal-agent 特殊处理：更新游标
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor, _journal_idx_to_id)

            # 验证结果：检查 event-manager 是否真正创建了任务
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                try:
                    import json
                    import sqlite3
                    from pathlib import Path

                    memory_path = Path.home() / ".niu" / "memory.json"
                    if memory_path.exists():
                        memory = json.loads(memory_path.read_text(encoding="utf-8"))
                        workspace = memory.get("workspace", {}).get("path")
                        if workspace:
                            db_path = str(Path(workspace) / "scheduled_tasks.db")
                            if Path(db_path).exists():
                                try:
                                    with sqlite3.connect(db_path) as conn:
                                        cursor = conn.cursor()
                                        cursor.execute("""
                                            SELECT id, content, status, scheduled_at
                                            FROM scheduled_tasks
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                        """)
                                        latest_task = cursor.fetchone()
                                except sqlite3.Error as e:
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Database error: {e}\n")
                                    latest_task = None

                                if latest_task:
                                    yield StreamEvent("tool_marker", f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n")
                                else:
                                    yield StreamEvent("system", "[SubAgent] ⚠ Warning: No task found in database\n")
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")

            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n")
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=""
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=""
            )
```

修改后代码：

```python
        # 同步子 Agent：先 pre_register 创建 ring buffer，再推送 subagent_started
        # 仅首次调用（新任务）执行；恢复路径（answer is not None）跳过：ring buffer 已存在、tab 已创建
        if not answer:
            # 【问题一修复】Early pre_register: creates ring buffer BEFORE subagent_started is queued,
            # so has_subagent() returns True when frontend connects SSE.
            # register() inside call_subagent will call pre_register again (no-op, idempotency guard).
            try:
                from niu_api.internal.subagent_event_bus import pre_register
                pre_register(agent_name)
            except ImportError:
                pass
            # 推送 subagent_started 事件到主 Agent SSE 流
            try:
                from niu_api.chat import _main_loop, _sync_broadcast
                if _main_loop and not _main_loop.is_closed():
                    event = {
                        'type': 'subagent_started',
                        'unique_name': agent_name,
                        'agent_name': agent_name,
                        'is_sync': True,
                    }
                    _main_loop.call_soon_threadsafe(_sync_broadcast, event)
            except ImportError:
                pass

        # 同步路径
        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            _history = None
            if agent_name == "journal-agent" and _journal_history:
                _history = _journal_history

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
                **({"context_fifo_threshold": 0} if (agent_name == "journal-agent" and _journal_history) else {}),
                answer=answer,
                answer_unique_name=(unique_name_arg or agent_name) if answer else None,
            )

            # journal-agent 特殊处理：更新游标
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor, _journal_idx_to_id)

            # 验证结果：检查 event-manager 是否真正创建了任务
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                try:
                    import json
                    import sqlite3
                    from pathlib import Path

                    memory_path = Path.home() / ".niu" / "memory.json"
                    if memory_path.exists():
                        memory = json.loads(memory_path.read_text(encoding="utf-8"))
                        workspace = memory.get("workspace", {}).get("path")
                        if workspace:
                            db_path = str(Path(workspace) / "scheduled_tasks.db")
                            if Path(db_path).exists():
                                try:
                                    with sqlite3.connect(db_path) as conn:
                                        cursor = conn.cursor()
                                        cursor.execute("""
                                            SELECT id, content, status, scheduled_at
                                            FROM scheduled_tasks
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                        """)
                                        latest_task = cursor.fetchone()
                                except sqlite3.Error as e:
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Database error: {e}\n")
                                    latest_task = None

                                if latest_task:
                                    yield StreamEvent("tool_marker", f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n")
                                else:
                                    yield StreamEvent("system", "[SubAgent] ⚠ Warning: No task found in database\n")
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")

            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n")
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=""
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            # 【问题 2d 修复】推送 subagent_error 事件到 SubagentEventBus（前端 tab 显示错误状态）
            try:
                from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                notify_subagent_event_sync(agent_name, 'subagent_error', {'content': str(e)[:2000]})
            except Exception:
                pass
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=""
            )
        finally:
            # 【问题 2a/2b/2c 修复】仅首次调用时清理 pre_register 创建的 ring buffer
            # 恢复路径（answer is not None）的清理由 call_subagent 内部 finally 负责
            if not answer:
                from .subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(agent_name)
                if instance is not None:
                    # 【问题 2a 修复】挂起时不清理（ring buffer 保留供恢复使用）
                    state = getattr(instance, 'state', None)
                    if state != 'waiting_for_answer':
                        try:
                            from niu_api.internal.subagent_event_bus import close
                            close(agent_name)
                        except ImportError:
                            pass
                else:
                    # 【问题 2c 修复】instance is None：call_subagent 未 register 或在 register 前异常
                    # 但 pre_register 可能已创建 ring buffer + subagent_started 已广播
                    # 必须清理，否则 ring buffer 泄漏 + 前端 tab 卡死
                    # 【问题 2e 修复】但场景 1/8（正常完成/@end 退出）call_subagent 内部已 unregister→close，
                    # ring buffer 在 5 分钟延迟清理窗口内仍存在，has_subagent 返回 True 但不需要再 close。
                    # 用 is_closing 检查是否已在 close 窗口内（_close_epochs 有记录表示已 close 过）
                    try:
                        from niu_api.internal.subagent_event_bus import has_subagent, is_closing, close
                        if has_subagent(agent_name) and not is_closing(agent_name):
                            close(agent_name)
                    except ImportError:
                        pass

---

## 场景验证矩阵

| # | 场景 | answer | pre_register | subagent_started | call_subagent 内部 finally | handler finally | close 调用 | subagent_closed | subagent_error | 结果 |
|---|------|--------|-------------|-------------------|--------------------------|----------------|-----------|----------------|---------------|------|
| 1 | 正常完成 | None | ✓ | ✓ | unregister→close | get→None→is_closing=True→跳过 | 1 次 | 1 次 | 0 次 | ✓ |
| 2 | register 前异常 | None | ✓ | ✓ | 未到 register→无 | get→None→has_subagent→is_closing=False→close | 1 次 | 1 次 | 1 次 | ✓ 2c+2d 修复 |
| 3 | GeneratorExit | None | ✓ | ✓ | call_subagent 未执行→无 | get→None→has_subagent→is_closing=False→close | 1 次 | 1 次 | 0 次 | ✓ |
| 4 | 挂起 @niu-agent | None | ✓ | ✓ | state=waiting→跳过 | get→instance→waiting→跳过 | 0 次 | 0 次 | 0 次 | ✓ ring buffer 保留 |
| 5 | 恢复后正常完成 | 非 None | 跳过 | 跳过 | unregister→close | 跳过 | 1 次 | 1 次 | 0 次 | ✓ |
| 6 | 恢复后异常 | 非 None | 跳过 | 跳过 | unregister→close | 跳过 | 1 次 | 1 次 | 1 次 | ✓ |
| 7 | 恢复后再挂起 | 非 None | 跳过 | 跳过 | state=waiting→跳过 | 跳过 | 0 次 | 0 次 | 0 次 | ✓ ring buffer 保留 |
| 8 | @end 退出 | None | ✓ | ✓ | unregister→close | get→None→is_closing=True→跳过 | 1 次 | 1 次 | 0 次 | ✓ |

---

## 审查历史

### 第一轮（竞态消除 + 资源清理）
- **P0**：finally 无条件 close 破坏 waiting_for_answer 挂起 → 修正为条件 close（问题 2a）
- **P3**：pre_register 幂等说明注释

### 第二轮（P0 修正验证 + 恢复路径）
- P0 修正验证通过
- **P2**：恢复路径双重 close → 加 `if not answer:` 守卫（问题 2b）
- **P3**：恢复路径冗余 pre_register → 同上守卫

### 替代方案验证（消除双重 close）
- 改用 `SubagentRegistry.get(agent_name)` 返回 None 时跳过 close
- 8 个场景全部通过
- **P2**：伪代码用 `unique_name` 应为 `agent_name`（handler 作用域内无 unique_name 变量）→ 已在方案中修正
- **P3**：finally 只调 close 不调 unregister → pre-existing，不影响

### 第四轮（完整审查：竞态消除 + 资源清理 + 全场景）
- **404 竞态消除验证通过**：FIFO 入队顺序正确，pre_register 先入队创建 ring buffer，subagent_started 后入队广播，has_subagent 返回 True → 200
- **P1**：register 前异常 ring buffer 泄漏 + 前端 tab 卡死 → finally 块 else 分支用 has_subagent 检查 + close 清理（问题 2c）
- **P2**：except 块缺 subagent_error 事件推送（既有问题）→ except 块中加 notify_subagent_event_sync（问题 2d）
### 第五轮（完整审查：竞态消除 + 资源清理 + 全场景，两件事一起审）
- **问题一 404 竞态消除验证通过**：FIFO 入队顺序正确
- **问题 2a/2b 验证通过**：挂起跳过 close、恢复路径 if not answer 守卫
- **问题 2c 验证通过**：else 分支 has_subagent 检查清理 ring buffer
- **问题 2d 验证通过**：except 块 subagent_error 推送
- **P2（新发现）**：场景 1/8 正常完成时 else 分支 has_subagent 返回 True（ring buffer 在 5 分钟延迟清理窗口内仍存在）→ close 被调两次 → 新增 is_closing 函数区分「已 close 过」和「从未 close」

---

### Task 0: 新增 is_closing 函数

**Files:**
- Modify: `niu_api/internal/subagent_event_bus.py:170` (在 has_subagent 函数之后)

- [ ] **Step 1: 在 has_subagent 函数之后添加 is_closing 函数**

在 `niu_api/internal/subagent_event_bus.py` 的 `has_subagent` 函数（L168-170）之后添加：

```python

def is_closing(unique_name: str) -> bool:
    """检查 unique_name 是否已在 close 延迟清理窗口内（_close_epochs 有记录）。

    用于 handler finally 的 else 分支区分两种 instance is None 的场景：
    - 场景 2（register 前异常）：未 close 过，is_closing=False → 需要 close
    - 场景 1/8（call_subagent 完成后已 close）：is_closing=True → 不需要再 close
    """
    return unique_name in _close_epochs
```

- [ ] **Step 2: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('niu_api/internal/subagent_event_bus.py').read()); print('OK')"`
Expected: OK

### Task 1: 修改 handler.py 同步路径
- Modify: `agent/handler.py:1067-1158`

- [ ] **Step 1: 备份当前代码**

```bash
cd /Users/lilei/tools/ai-bot
git add -A && git commit -m "backup: before sync subagent SSE 404 race fix + resource cleanup"
```

- [ ] **Step 2: 替换 L1067-1158**

将 L1067 开始的 `# 同步子 Agent：unique_name = agent_name` 到 L1158 的 `return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="")` 整块替换为上面"修改后代码"中的完整代码块。

替换范围：
- 旧的 `subagent_started` 推送块（L1067-1079）→ 新的 `if not answer:` 守卫的 `pre_register` + `subagent_started` 推送块
- 旧的 `try` 块（L1081-1153）→ 保持不变（call_subagent 调用 + event-manager 验证 + 返回 StepOutcome）
- 旧的 `except` 块（L1154-1158）→ 新的 except 块（加 subagent_error 推送）
- 新增 `finally` 块（条件 close + has_subagent 检查）

- [ ] **Step 3: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add agent/handler.py
git commit -m "fix: sync subagent SSE 404 race (pre_register before subagent_started) + resource cleanup (conditional close + subagent_error + has_subagent leak fix)"
```

### Task 2: 运行已有测试

**Files:**
- Test: `tests/test_at_prefix_interception.py`

- [ ] **Step 1: 运行拦截测试**

Run: `python/bin/python -m pytest tests/test_at_prefix_interception.py -x -q`
Expected: 27 passed

- [ ] **Step 2: 如有失败则修复**

如果测试失败，检查是否是 `pre_register` 或 `finally` 块引入的回归，修复后重新运行。

### Task 3: 端到端验证

- [ ] **Step 1: 启动应用**

```bash
cd /Users/lilei/tools/ai-bot
./niu
```

- [ ] **Step 2: 测试同步子 Agent（验证 404 竞态消除）**

在聊天中触发一个同步子 Agent 调用（如让主 Agent 处理一个文件入库任务），观察：
- 子 Agent tab 创建后正常显示内容（不出现红色"子 Agent 不存在"错误）
- 子 Agent 工作过程中 tab 实时显示工具调用、thinking chain、输出
- 子 Agent 输出 `@end` 后 tab 显示"子 Agent 已结束"并关闭

- [ ] **Step 3: 测试 @niu-agent 挂起/恢复（验证资源清理 2a）**

让同步子 Agent 输出 `@niu-agent 问题`，观察：
- tab 显示 subagent_suspended 状态（不关闭）
- 主 Agent 收到问题后回答
- 子 Agent 恢复工作，tab 继续显示

- [ ] **Step 4: 测试异步子 Agent（验证异步路径不受影响）**

让主 Agent 异步调用子 Agent（如 browser-server），确认异步路径不受影响。

- [ ] **Step 5: 提交最终版本**

```bash
git add -A && git commit -m "verify: sync subagent SSE 404 race fix + resource cleanup e2e verified"
```
