# /clear 命令增强：先整理（小憩+日志）后清空 实现计划（v3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前端 `/clear` 命令在清空消息前，先阻塞执行"强制整理的前三步"（entity-extractor → dream-evolver → journal-agent），跳过 context-manager 压缩，最后清空全部消息；并把游标复位逻辑统一加固到所有清空路径。

**Architecture:** 复用 `_tidy_context_impl(mode="force")`（`niu_api/compat.py`）三步子 Agent 管道，给 request 加 `skip_compress` 键跳过 context-manager 压缩步骤；`clear_chat`（`/api/chat/clear`）新增 `force_tidy` 请求参数（snake_case），为 true 时在持有 `_chat_lock` 的情况下阻塞调用该管道（带 `asyncio.wait_for(600)` 兜底），最后执行现有清空逻辑。前端 `/clear` 两分支都立即 `clearChat(true)` 阻塞等待（busy 分支先 `/stop`），删除 `_pendingClear` 机制。抽公共 `_reset_all_cursors()` helper 供所有清空端点复用。

**Tech Stack:** Python (FastAPI + asyncio + subagent pipeline), Electron (IPC + renderer chat.html)。

---

## 背景与现状核实（工程师必读）

- **`/new` 和 `/clear` 前端目前都走 `clearChat()` → `window.electronAPI.clearChat()` → main.js IPC 'clear-chat' → POST `/api/chat/clear` → `clear_chat()`**（`niu_api/compat.py:2381`）。
- **`clear_chat` 现已在清空时删除全部 4 个游标文件**（`compat.py:2424-2433`）：`last_entity_extract.json`、`last_dream_evolve.json`、`last_compress.json`、`last_journal.json`。
- **隐患**：`niu_api/chat.py:828` `clear_session` 和 `niu_api/session.py:57`/`session.py:80` 都清消息但**不删游标文件**，本计划统一加固。
- **force 管道 `_tidy_context_impl`**（`compat.py:2477`）：`mode="force"` 分支（`L3344`）串行：entity-extractor（全量）→ dream-evolver（增量）→ journal-agent（始终调用）→ context-manager 压缩（`L3588` 起）。本计划在压缩步骤前判断 `skip_compress`。
- **阻塞来源**：管道内子 Agent 用 `asyncio.to_thread(call_subagent_with_auto_answer)` 逐个阻塞等待。
- **`chat_lock_already_held`**：`_tidy_context_impl` 参数，True 时跳过内部 `_chat_lock` 获取与 ChatQueue pause/resume（`compat.py:3770-3803`）。clear_chat 已持锁，调用必须传 True（asyncio.Lock 不可重入）。
- **stop 标志**：clear_chat 顶部 `request_stop()` 停主 Agent（全局标志，chat_session/ChatQueue/SSE 三路径的 busy 持锁者都在 `finally` 释放锁）；跑整理前 `clear_stop()` 清标志，避免管道内 `is_stop_requested()` 提前 abort。
- **背景 nap 线程**：`runner._maybe_trigger_nap`（`runner.py:976`）spawn `_run_nap_background` daemon 线程（entity→dream），`_nap_running` Event（`runner.py:744`，finally `L1266` 清除）。**无需显式 wait**：clear_chat 顶部 `request_stop()` 即为权威 abort 信号，nap 在其下一边界（entity 后 `L1180`/dream 后 `L1224`）检测 stop 自 abort 自愈。
- **前端 `/clear` 现状**（`chat.html:1495-1519`）：busy 时 sendMessage('/stop') → `_pendingClear` + 120s 超时等 chat_idle → clearChat()；idle 时直接 clearChat()。**本版删除 `_pendingClear`**（见 F2 说明）。

---

## 修订记录

### v1 → v2（第 1 轮审查 11 项修复）
- F1 字段名 · F4 tokens_after · F5 嵌套闭包 · F7 aborted 兜底 · F8/F9/F10/F11 保留。详见 git 历史 `9155ce4b`。

### v2 → v3（第 2 轮审查 + F2 方案核对结论）
| 发现 | 严重级 | v3 处理 |
|---|---|---|
| **P0-F2** chat_idle 非锁释放信号，busy 等 chat_idle 后 clearChat 仍可能 30s 超时（chat_idle 在 worker 线程 `runner.chat` finally 推送，早于 `_chat_lock.release`；CONTEXT_OVERFLOW 时还有阻塞 force-compress） | **P0** | **前端两分支立即 `clearChat(true)`**（busy 先 /stop，不再等 chat_idle）+ **后端 `_chat_lock.acquire` 30→120s** + **删除 `_pendingClear`**。`/stop` 是全局标志，busy 持锁者终会释放 |
| F2 边界 | P1 | `acquire(120s)` 非硬保证：busy 主 agent 卡在子 agent tool 时 /stop 不传播到运行中子 agent（仅 double-click `request_stop_all_subagents` 才停），子 agent 跑完才释放锁，可超 120s → clear_chat 优雅失败（`success:false`，需重试）。**这是预存 /stop 语义限制，非新 bug**，文档注明"超时=优雅失败" |
| F3 nap 协调 | P1 | **不显式 wait**（request_stop 权威 + nap 自 abort 自愈），删除 300s cap；只加日志 |
| v2-F3（tidy 无总超时） | P2 | `asyncio.wait_for(_tidy_context_impl(..., skip_compress), 600)` + 超时降级 clear-messages-only（无锁泄漏，orphan 子 agent 线程自愈游标） |
| F6 输入禁用 | P2 | 两分支 `clearChat(true)` await 期间禁用 sendBtn+userInput（finally 恢复） |
| F5 压缩块 | P2 | 保留嵌套闭包 `_compress_force` 方案 |
| skip_compress 传递 | - | 从 request dict 读 `request.get('skip_compress')`（非函数参数），force 分支 journal 后判断 |

---

## File Structure

| 文件 | 责任 |
|---|---|
| `niu_api/compat.py` | `_reset_all_cursors()` helper、`_tidy_context_impl` 加 `skip_compress` 判断 + 嵌套 `_compress_force`、`clear_chat` 加 `force_tidy`（信号 `Request` + `await request.json()`、acquire 120s、`wait_for` tidy 600s） |
| `niu_api/chat.py` | 加固 `clear_session` 游标复位 |
| `niu_api/session.py` | 加固 `delete_messages`/`delete_session` 游标复位 |
| `ui/main/windows/assistant/chat.html` | `/clear` 两分支立即 `clearChat(true)`、删 `_pendingClear`、await 期间禁用输入 |
| `ui/main/preload-chat.js` | `clearChat(forceTidy)` 透传 |
| `ui/main/main.js` | `'clear-chat'` handler 接参发 `force_tidy` |

---

### Task 1: 后端 — 抽出公共游标复位 helper `_reset_all_cursors`

**Files:**
- Modify: `niu_api/compat.py`（在 `clear_chat` 路由装饰器前插入）

- [ ] **Step 1: 在 `@router.post("/api/chat/clear")`（现 L2380）前插入模块级 helper**

```python
# 游标文件列表（清空消息后必须一并复位，否则游标指向已删除消息）
_ALL_CURSOR_FILES = [
    "last_entity_extract.json",
    "last_dream_evolve.json",
    "last_compress.json",
    "last_journal.json",
]


async def _reset_all_cursors() -> None:
    """删除全部增量处理游标文件（消息清空后调用，避免游标指向已删消息）。"""
    from pathlib import Path
    for cursor_name in _ALL_CURSOR_FILES:
        cursor_p = Path.home() / ".niu" / cursor_name
        try:
            if cursor_p.exists():
                cursor_p.unlink()
        except OSError as e:
            logger.warning(f"Failed to reset cursor file {cursor_name}: {e}")
```

> `logger` 已在 compat.py 顶部导入。保留 async。

- [ ] **Step 2: `clear_chat` 内联游标复位段替换为 helper 调用**

把 `clear_chat` 内现有段（`compat.py:2424-2433`）：

```python
        # 重置游标文件（消息已清空，旧游标指向不存在的消息）
        from pathlib import Path
        for cursor_name in ["last_entity_extract.json", "last_dream_evolve.json", "last_compress.json", "last_journal.json"]:
            cursor_p = Path.home() / ".niu" / cursor_name
            try:
                if cursor_p.exists():
                    cursor_p.unlink()
            except OSError as e:
                logger.warning(f"[clear_chat] Failed to reset cursor file {cursor_name}: {e}")
```

替换为：

```python
        # 重置游标文件（消息已清空，旧游标指向不存在的消息）
        await _reset_all_cursors()
```

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "refactor(clear): extract shared _reset_all_cursors helper"
```

---

### Task 2: 后端 — `_tidy_context_impl` 支持 `skip_compress`（request dict）+ 嵌套闭包 `_compress_force`

**Files:**
- Modify: `niu_api/compat.py:2477`（签名—无需改签名的 skip；改为从 request 读）、`niu_api/compat.py:~3587-3961`（force 压缩块抽闭包）

- [ ] **Step 1: docstring 记录 request 新增键**

`_tidy_context_impl` 的 `request` dict 现在支持可选键 `skip_compress`（bool）。在函数 docstring 的 Args 或说明处补一句（可选但推荐）：

```python
    # request 支持可选键 skip_compress: True 时跳过 force 模式的 context-manager 压缩
    # （用于 /clear 场景：只做内容提炼+梦境进化+日志记录，不压缩）
```

- [ ] **Step 2: force 分支把压缩块整体移入嵌套闭包 `_compress_force`，并在 journal 后、压缩前判断 `skip_compress`**

force 分支的 context-manager 压缩从 `compat.py:3587`（`# 3/3. context-manager force prompt — 一轮 JSON 文件方案`）开始，到 `L3960` 结束（`L3961` 是 final return）。

在 force 分支的 journal-agent 段（`~L3586`）之后、原压缩块之前插入：

```python
            # 3/3. context-manager 强制压缩（抽为嵌套闭包；skip_compress=True 时跳过）
            async def _compress_force():
                # <此处为原 L3587~L3960 压缩块整体内容，含：
                #   提前 return：aborted -> {"status":"aborted",...}；SUBAGENT_ERROR -> {"status":"skipped",...}；截断 -> return
                #   L3770-3803 的 chat_lock_already_held 分流（False 时 pause+acquire+等 _processing_done；True 时跳过）
                #   末尾 tokens_after 计算与 return {"status":"ok","mode":"force","tokens_before":display_tokens,"tokens_after":tokens_after}>
                ...

            if request.get("skip_compress"):
                logger.info("[Tidy] Force: skip_compress=True, skipping context-manager compression")
                return {"status": "ok", "mode": "force", "skip_compress": True,
                        "tokens_before": display_tokens, "tokens_after": display_tokens}
            return await _compress_force()
```

> ⚠️ **关键实现约束（两轮审查 + 二次核对确认）**：
> 1. 压缩块整体（L3587~L3960）以**嵌套 async def 闭包**内联，捕获外层全部变量（display_tokens/target_tokens/usage_percent/llm_config/messages/msg_tokens/store/last_compress_id/new_dream_id/last_*_id/compress_cursor_path/protect_recent_count/request 等），**不要**改模块级巨签名 helper、**不要**手工缩进 370 行进 else。闭包内变量（last_compress_id L3590、new_compress_id、valid_deletes、fresh_messages、result、tokens_after L3945/3957）均为闭包局部，无 nonlocal 需求；外部只读变量按引用捕获。
> 2. `skip_compress` 分支返回 `tokens_after: display_tokens`（压缩前值，语义正确；`tokens_after` 在 L3945 已有默认值）。
> 3. `notify_compact_status_sync("done", mode=mode)` 在 `_tidy_context_impl` **外层 finally**（L4001-4005），两条路径都照常广播，**不要动**。
> 4. `chat_lock_already_held=False` 的两条调用路径（`tidy_context` 端点 `compat.py:2472`、ChatQueue `_retry_force_compression` `chat_queue.py:425`）必须保持原 else 分支的 pause+acquire+等 `_processing_done` 逻辑（L3770-3803），**不得误改**。

- [ ] **Step 3: 语法 + 编译检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

Run: `cd niu_api && /Users/lilei/tools/ai-bot/python/bin/python -m py_compile compat.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(tidy): support skip_compress via request dict + nest force compress in _compress_force closure"
```

---

### Task 3: 后端 — `clear_chat` 支持 `force_tidy`，先整理后清空（阻塞 + 超时兜底）

**Files:**
- Modify: `niu_api/compat.py:2381`（`clear_chat` 端点）

- [ ] **Step 1: `clear_chat` 改为接收 `Request` 并解析 body**

当前签名（`compat.py:2381`）：

```python
@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """Clear all messages (for /new and /clear commands)"""
    # 先请求停止当前 Agent 工作
    from agent.runner import clear_stop, request_stop
    request_stop()
```

改为（沿用 compat.py 既有 `await request.json()` 模式 `L1448/1773`，`Request` 已在 `L25` 导入）：

```python
@router.post("/api/chat/clear")
async def clear_chat(request: Request) -> dict:
    """Clear all messages (for /new and /clear commands)

    body 可选键：
        force_tidy (bool): True 时清空前先跑 force 整理（entity→dream→journal，skip_compress）
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    force_tidy = bool(body.get("force_tidy"))  # snake_case —— main.js 发送一致

    # 先请求停止当前 Agent 工作（全局 stop 标志）
    from agent.runner import clear_stop, request_stop
    request_stop()
```

- [ ] **Step 2: 锁超时 30→120s，锁内跑 tidy（wait_for 600 兜底）+ 清空**

当前锁获取段（`compat.py:2389-2395`）：

```python
    # 获取锁，防止与正在进行的 chat 冲突
    # 超时增加到 30 秒，等待 Agent 循环检测 stop 标志并退出
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=30.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 30s timeout, clear rejected")
        clear_stop()  # 防止停止标志残留，影响后续定时任务
        return {"success": False, "error": "系统正忙，请稍后再试"}
```

改为（120s；log 用 30→120；关键：nap 不需显式 wait，request_stop 已 set 会令 nap 下一边界自 abort）：

```python
    # 获取锁，防止与正在进行的 chat 冲突
    # 超时 120 秒：/clear 的 busy 分支先发 /stop 停主 Agent，持锁者(chat_session/ChatQueue/SSE)
    # 在 finally 释放；120s 覆盖 persist + 可能的 CONTEXT_OVERFLOW force-compress。
    # 若主 Agent 卡在子 agent tool（/stop 不传播到运行中子 agent），可超时 → 下方优雅失败需重试。
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=120.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 120s timeout, clear rejected")
        clear_stop()  # 防止停止标志残留，影响后续定时任务
        return {"success": False, "error": "系统正忙，请稍后再试"}
```

再改锁内清空段（现 `compat.py:2397-2435`）：

```python
    try:
        clear_stop()  # 防御性清除：确保清空时标志干净；清掉 request_stop 标志，避免 force 管道内 is_stop_requested() 立即 abort
        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
        store = await get_message_store()

        # /clear：先跑 force 整理（entity→dream→journal，skip_compress），阻塞；超时降级为 clear-messages-only
        if force_tidy:
            try:
                await asyncio.wait_for(
                    _tidy_context_impl(
                        {"session_id": "default", "mode": "force", "skip_compress": True},
                        chat_lock_already_held=True,  # clear_chat 已持有 _chat_lock，防 asyncio.Lock 不可重入死锁
                    ),
                    timeout=600.0,  # 兜底：单个子 agent LLM 卡死时不永久占锁
                )
            except asyncio.TimeoutError:
                logger.warning("[clear_chat] tidy 600s timeout, clear-messages-only (orphan subagent thread self-heals)")
            except Exception as e:
                logger.warning(f"[clear_chat] run_tidy_before failed, proceed to clear: {e}")

        count = await store.clear_messages()

        # 重置 runner 的所有状态
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if runner:
            # 重置 handler 的工作记忆
            if runner.handler:
                runner.handler.reset_working_memory()
                runner.handler._last_prompt_tokens = 0
            # 清空衰减池（新会话开始）
            runner._decay_pool.clear()

        # 清空临时目录（画框图片等）
        from agent.tmp_dir import cleanup_all_tmp
        cleaned_tmp = cleanup_all_tmp()

        # 重置游标文件（消息已清空，旧游标指向不存在的消息）
        await _reset_all_cursors()

        return {"success": True, "deleted_count": count, "cleaned_tmp": cleaned_tmp}
    finally:
        _chat_lock.release()
```

> ⚠️ **实现约束（核对 agent 确认）**：
> 1. `_tidy_context_impl` 与 `clear_chat` **同文件**（compat.py），直接调用，**不得** `from niu_api.compat import _tidy_context_impl`（会自我 import）。
> 2. `force_tidy` 用 snake_case，与 main.js 发送一致（**无 pydantic 转换，raw dict**）。
> 3. 超时/异常时**仍继续清空**（用户 clear 意图优先），不返回错误。
> 4. `asyncio.wait_for` 取消的是外层 await，**不杀死** `asyncio.to_thread` 子 agent 线程——orphan 线程会跑完当前子 agent 再自愈游标（validate against fresh_ids → revert to ''），无悬挂 UUID 写入，安全。
> 5. nap 线程**不显式 wait**：request_stop 已在顶部 set，nap 下一边界自 abort 自愈。

- [ ] **Step 3: 语法 + 编译检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(clear): force_tidy runs entity/dream/journal (skip compress) before clearing; 120s lock + 600s tidy cap"
```

---

### Task 4: 后端 — 加固 `chat.py` `clear_session` 游标复位

**Files:**
- Modify: `niu_api/chat.py:828-835`（`clear_session`）

- [ ] **Step 1: 在 `clear_session` 清消息后调 helper**

当前（`chat.py:828-835`）：

```python
@router.delete("/chat/session/{session_id}")
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"status": "ok", "session_id": session_id}
```

改为（函数内延迟 import，沿用 `chat.py:537` 既有约定）：

```python
@router.delete("/chat/session/{session_id}")
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    # 游标复位（与 clear_chat 一致，消除"清消息但游标残留"的不一致）
    from niu_api.compat import _reset_all_cursors
    await _reset_all_cursors()
    return {"status": "ok", "session_id": session_id}
```

- [ ] **Step 2: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/chat.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/chat.py && git commit -m "fix(session): reset cursors in clear_session to match clear_chat"
```

---

### Task 5: 后端 — 加固 `session.py` `delete_messages`/`delete_session` 游标复位

**Files:**
- Modify: `niu_api/session.py:57-62`、`session.py:80-89`

- [ ] **Step 1: `delete_messages` 补游标复位**

当前（`session.py:57-62`）：

```python
@router.delete("/{session_id}/messages")
async def delete_messages(session_id: str) -> dict:
    """Clear all messages (session_id is ignored)"""
    store = await get_message_store()
    count = await store.clear_messages()
    return {"deleted_count": count}
```

改为：

```python
@router.delete("/{session_id}/messages")
async def delete_messages(session_id: str) -> dict:
    """Clear all messages (session_id is ignored)"""
    store = await get_message_store()
    count = await store.clear_messages()
    from niu_api.compat import _reset_all_cursors
    await _reset_all_cursors()
    return {"deleted_count": count}
```

- [ ] **Step 2: `delete_session` 补游标复位**

当前（`session.py:80-89`）：

```python
@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session (deprecated - clears all messages)"""
    store = await get_message_store()
    await store.clear_messages()
    from niu_api.chat import get_or_create_runner
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"deleted": True}
```

改为（现有 `from niu_api.chat import get_or_create_runner` 之后追加）：

```python
@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session (deprecated - clears all messages)"""
    store = await get_message_store()
    await store.clear_messages()
    from niu_api.chat import get_or_create_runner
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    from niu_api.compat import _reset_all_cursors
    await _reset_all_cursors()
    return {"deleted": True}
```

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/session.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/session.py && git commit -m "fix(session): reset cursors in delete endpoints to match clear_chat"
```

---

### Task 6: 前端 — `preload-chat.js` 与 `main.js` IPC 传递 `force_tidy`

**Files:**
- Modify: `ui/main/preload-chat.js:72`
- Modify: `ui/main/main.js:1087-1122`

- [ ] **Step 1: `preload-chat.js` 的 `clearChat` 透传参数**

当前（`preload-chat.js:72`）：

```js
  clearChat: () => ipcRenderer.invoke('clear-chat'),
```

改为：

```js
  clearChat: (forceTidy) => ipcRenderer.invoke('clear-chat', forceTidy),
```

- [ ] **Step 2: `main.js` 的 'clear-chat' handler 接参发 `force_tidy`**

当前（`main.js:1087-1091`）：

```js
ipcMain.handle('clear-chat', async () => {
  // 清空待推送消息队列
  pendingAlertMessages = [];

  return new Promise((resolve) => {
    const data = JSON.stringify({ sessionId: 'default' });
```

改为：

```js
ipcMain.handle('clear-chat', async (event, forceTidy) => {
  // 清空待推送消息队列
  pendingAlertMessages = [];

  return new Promise((resolve) => {
    // /clear 传 force_tidy=true（先整理后清空）；/new 传 undefined（直接清空）
    // snake_case：后端 clear_chat 用 body.get("force_tidy") 读取（raw dict，无 pydantic 转换）
    const data = JSON.stringify({ sessionId: 'default', force_tidy: !!forceTidy });
```

（其余 http.request 代码不变。）

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && node --check ui/main/preload-chat.js && node --check ui/main/main.js`
Expected: 无输出（语法 OK）

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/preload-chat.js ui/main/main.js && git commit -m "feat(clear): pass force_tidy (snake_case) through IPC to backend"
```

---

### Task 7: 前端 — `chat.html` `/clear` 两分支立即 `clearChat(true)`，删 `_pendingClear`，阻塞期间禁用输入

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`/clear` 块现 L1495-1520、chat_idle 消费现 L2444-2456、`clearChat()` 现 L1565-1581、`_pendingClear` 声明现 L1018-1020、busy-set 现 L1508-1514）

**设计（F2 最终，核对确认）**：两分支都立即 `clearChat(true)`（busy 先 `/stop`），不再等 chat_idle、删除 `_pendingClear`；后端 `clear_chat` acquire(120s) 阻塞等待 busy 持锁者释放。busy 主 agent 卡在子 agent tool 时可能超 120s → 后端优雅失败（前端显示"清空失败"，需重试，属预存 /stop 语义限制）。

- [ ] **Step 1: `clearChat(tidy)` 支持参数**

当前（`chat.html:1565`）：

```js
    async function clearChat() {
      try {
        const result = await window.electronAPI.clearChat();
```

改为：

```js
    async function clearChat(forceTidy) {
      try {
        const result = await window.electronAPI.clearChat(forceTidy);
```

- [ ] **Step 2: 重写 `/clear` 命令块（两分支立即 clearChat(true)）**

当前（`chat.html:1495-1520`）整块替换为：

```js
      // /clear 指令 - 先整理（小憩+日志）后清空（后端跑完 entity/dream/journal 才清空，阻塞）
      if (text === '/clear') {
        userInput.value = '';
        userInput.style.height = 'auto';
        if (isProcessing) {
          // busy：先 /stop 停主 Agent（全局 stop 标志，让持锁者在 finally 释放 _chat_lock），不做等 chat_idle
          // 注意：主 Agent 卡在子 agent tool 时 /stop 不传播到运行中子 agent，后端 acquire(120s) 可能超时 → 优雅失败需重试
          try {
            await window.electronAPI.sendMessage('/stop');
          } catch (e) {
            console.error('停止失败:', e);
            addSystemMessage('停止失败: ' + (e.message || e));
          }
        }
        sendBtn.disabled = true;
        userInput.disabled = true;
        addSystemMessage('正在整理对话并清空会话，请稍候…');
        try {
          await clearChat(true);
        } finally {
          sendBtn.disabled = false;
          userInput.disabled = false;
          userInput.focus();
        }
        return;
      }
```

- [ ] **Step 3: 删除 `_pendingClear` 声明与消费**

删除以下三处（`_pendingClear` 已不再使用）：
1. 声明（`chat.html:1018-1020`）：`let _pendingClear = false;` + `let _pendingClearTimeout = null;`
2. `/clear` busy-set（已被 Step 2 替换，无需单独删）
3. chat_idle 消费块（`chat.html:2444-2456`）：`// 处理 /clear 的延迟清空` 整段（含 `_pendingClear` 判断、`_pendingClearTimeout`、`clearChat()`），其中清空逻辑已由 Step 2 承担，chat_idle 只负责恢复 UI（`sendBtn.disabled=false` 等，原本就在 chat_idle handler 内），**不调用 clearChat**。

> 验证：grep `_pendingClear` 删除后应为 0 匹配。

- [ ] **Step 4: 验证无残留 + 关键引用一致**

Run: `cd /Users/lilei/tools/ai-bot && grep -n "_pendingClear" ui/main/windows/assistant/chat.html`
Expected: 无匹配

Run: `cd /Users/lilei/tools/ai-bot && grep -n "force_tidy\|forceTidy" ui/main/main.js ui/main/preload-chat.js niu_api/compat.py`
Expected: main.js 发 `force_tidy: !!forceTidy`、preload 透传 `forceTidy`、compat.py 读 `body.get("force_tidy")` —— snake_case 全链路一致。

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/windows/assistant/chat.html && git commit -m "feat(clear): /clear both branches run tidy then clear (blocking); drop _pendingClear"
```

---

### Task 8: 测试验证（真实 LLM + 真实流程）

**Files:** 无新增。全为验证步骤。

> 前置：应用运行（`./niu` 或 `python -m niu_api`）。若未运行，`cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`（铁律 8）。

- [ ] **Step 1: 准备真实对话数据**

```bash
curl -X POST http://127.0.0.1:9876/api/chat/session -H 'Content-Type: application/json' \
  -d '{"message": "我今天完成了/clear命令的整理增强功能开发，涉及后端tidy管道和前端IPC改动", "source": "electron"}'
```

Expected: 返回 `ChatResponse`（`reply`/`session_id`/`message_id`，compat.py:1342），消息已入库。

- [ ] **Step 2: 触发 `/clear`（force_tidy）验证阻塞 + 整理 + skip 压缩**

```bash
curl -X POST http://127.0.0.1:9876/api/chat/clear -H 'Content-Type: application/json' \
  -d '{"sessionId": "default", "force_tidy": true}'
```

Expected:
- 接口**不立即返回**（阻塞；等 entity→dream→journal 三个子 Agent 依次跑完）
- 后端日志：`[Tidy] Force mode: starting entity-extractor` → `[Tidy] Force: dream-evolver ...` → `[Tidy] Force: starting journal-agent` → `[Tidy] Force: skip_compress=True, skipping context-manager compression` → 返回 `{"success": true, "deleted_count": N, ...}`
- **无 context-manager 压缩日志**（skip_compress 生效）

- [ ] **Step 3: 验证整理产物落盘（先记录后清空）**

- `~/.niu/workspace/`（或配置工作目录）的 `journal.md` 已新增本次对话工作日志条目。
- LightRAG 知识图谱（`~/.niu/lightrag/`）已新增本次对话精炼文档/实体。

- [ ] **Step 4: 验证消息已清空 + 游标已复位**

```bash
cd /Users/lilei/tools/ai-bot
curl -s http://127.0.0.1:9876/api/chat/messages 2>/dev/null || true
ls -la ~/.niu/last_entity_extract.json ~/.niu/last_dream_evolve.json ~/.niu/last_compress.json ~/.niu/last_journal.json 2>&1
```

Expected: 消息为空；4 个游标文件 `No such file`。

- [ ] **Step 5: 验证 `/new` 路径不回归（不触发整理）**

```bash
curl -X POST http://127.0.0.1:9876/api/chat/clear -H 'Content-Type: application/json' \
  -d '{"sessionId": "default"}'
```

Expected: **无** entity/dream/journal 日志；消息清空 + 游标复位；`{"success": true}`。

- [ ] **Step 6: 验证 busy 分支**

Agent 正忙时（isProcessing=true）输入 `/clear`：
Expected:
- 前端发 `/stop`，随后 `clearChat(true)`；整个 await 期间 `sendBtn`/`userInput` 禁用、显示"正在整理对话并清空会话"
- 主 Agent 退出释放锁后，后端完成整理+清空；显示"✅ 聊天记录已清空"
- 若主 Agent 卡在子 agent tool 超 120s：后端返回 `{"success": false, "error": "系统正忙..."}`，前端显示"❌ 清空失败"（优雅失败，用户可重试）

- [ ] **Step 7: 验证中断兜底**

整理进行中再按 ESC 或 /stop：
Expected: tidy 管道在子 Agent 边界 abort（`is_stop_requested`），`asyncio.wait_for` 正常返回，**仍完成清空**（`success:true`）。若超 600s（子 agent LLM 卡死）：`wait_for` 超时抛错被捕获，**仍清空**（clear-messages-only）。

- [ ] **Step 8: 确认无回归（跑既有相关测试）**

Run: `cd /Users/lilei/tools/ai-bot/agent && pytest -q 2>&1 | tail -20`
Expected: 既有测试不因本次改动失败。若全套太慢，只跑与 clear/tidy/disk 相关测试文件。

---

## Self-Review 自查

**1. Spec 覆盖：**
- ✅ `/clear` 去掉多余 `/stop`（idle 分支无 stop；busy 分支保留——它是释放 `_chat_lock` 的必要机制）
- ✅ 触发小憩（entity+dream）+ 日志记录（journal-agent，force 始终调用）
- ✅ 最后一步压缩换成 Clear（`skip_compress` + 清空）
- ✅ 阻塞（子 Agent `asyncio.to_thread` 阻塞 + 前端一次 await + 后端 acquire 等锁）
- ✅ 游标复位统一加固（Task 1 helper + Task 4/5 session 端点）
- ✅ 与 nap 线程协调（request_stop 权威 + nap 自 abort 自愈，不显式 wait）

**2. Placeholder 扫描：** 无 "TBD"/"适当处理" 占位；所有代码块完整。Task 2 压缩块以"嵌套闭包内联 + 明确移动范围 + 5 条关键约束"描述。

**3. 类型一致性：**
- 字段名全链路 **snake_case** `force_tidy`：chat.html `clearChat(true)` → preload `forceTidy` → main.js `force_tidy: !!forceTidy` → 后端 `body.get("force_tidy")`。F1 已修且经二次核对。
- `skip_compress` 从 request dict 读（Task 2 `request.get("skip_compress")` 与 Task 3 传 `"skip_compress": True` 一致）。
- `_reset_all_cursors` async（Task 1），clear_chat/chat.py/session.py 均 `await`。
- `clearChat(forceTidy)` 前端参数 preload 到 /clear 两分支一致。
- `_pendingClear` 已完全删除（3 处），无悬空引用。

**4. 并发与锁（两轮审查 + 二次/三次核对结论）：**
- `chat_lock_already_held=True` 所有调用方持 `_chat_lock`，无死锁。
- `chat_lock_already_held=False` 两条路径（tidy_context 端点、ChatQueue `_retry_force_compression`）保持原 else 分支不变。
- skip_compress 时压缩块整体跳过；.clear_chat 持锁期间主 Agent 已 stop，新消息走补充入队/queue 阻塞，无并发变异。
- nap 不显式 wait（request_stop 权威 + 自 abort 自愈），无 300s cap。
- `asyncio.wait_for` 600 兜底，超时降级 clear-only，无锁泄漏；orphan 子 agent 线程自愈游标。
- 客户端 http.request 无 timeout；backend acquire(120s) + tidy(600s) 合计最长 ~12min，期间输入禁用（可接受，文案言明"请稍候"）。
