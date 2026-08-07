# /clear 命令增强：先整理（小憩+日志）后清空 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前端 `/clear` 命令在清空消息前，先阻塞执行"强制整理的前三步"（entity-extractor → dream-evolver → journal-agent），跳过 context-manager 压缩，最后清空全部消息；并把游标复位逻辑统一加固到所有清空路径。

**Architecture:** 复用现有 `_tidy_context_impl(mode="force")`（`niu_api/compat.py`）三步子 Agent 管道，给其新增 `skip_compress` 参数跳过 context-manager 压缩步骤；`clear_chat`（`/api/chat/clear`）新增 `run_tidy_before` 请求参数（snake_case），为 true 时在持有 `_chat_lock` 的情况下先协调背景 nap 线程、再阻塞调用该管道，最后执行现有清空逻辑。抽出公共 `_reset_all_cursors()` helper 供所有清空端点复用。前端 `/clear` 保留 busy 分支（/stop + 等 chat_idle），仅空闲分支直接 `clearChat(true)` 阻塞等待。

**Tech Stack:** Python (FastAPI + asyncio + subagent pipeline + threading.Event), Electron (IPC + renderer chat.html)。

---

## 背景与现状核实（工程师必读）

- **`/new` 和 `/clear` 前端目前都走 `clearChat()` → `window.electronAPI.clearChat()` → main.js IPC 'clear-chat' → POST `/api/chat/clear` → `clear_chat()`**（`niu_api/compat.py:2381`）。
- **`clear_chat` 现已在清空时删除全部 4 个游标文件**（`compat.py:2425-2433`）：`last_entity_extract.json`、`last_dream_evolve.json`、`last_compress.json`、`last_journal.json`。所以 `/new` 和 `/clear` **都已复位游标**。
- **隐患**：`niu_api/chat.py:828` 的 `clear_session` 和 `niu_api/session.py:57`（`delete_messages`）、`session.py:80`（`delete_session`）都清消息但**不删游标文件**。本计划统一加固。
- **force 管道 `_tidy_context_impl`**（`compat.py:2477`）：`mode="force"` 分支（`L3344`）串行执行：entity-extractor（全量）→ dream-evolver（增量）→ journal-agent（force 始终调用）→ context-manager 压缩（`L3588` 起）。本计划在压缩步骤加 `skip_compress` 开关。
- **阻塞来源**：管道内子 Agent 用 `run_xxx_force = lambda: call_subagent_with_auto_answer(...)` + `await asyncio.to_thread(run_xxx_force)` 逐个阻塞等待。
- **`chat_lock_already_held`**：`_tidy_context_impl` 参数，True 时跳过内部 `_chat_lock` 获取和 ChatQueue pause/resume（`compat.py:3770-3803`）。`clear_chat` 已持有 `_chat_lock`，调用时必须传 True（asyncio.Lock 不可重入）。
- **stop 标志**：clear_chat 顶部 `request_stop()` 停主 Agent，会让管道内 `is_stop_requested()` 提前 abort → 跑 tidy 前必须 `clear_stop()`。
- **背景 nap 线程**：`runner._maybe_trigger_nap`（`runner.py:976`）每轮从 `_on_turn_end` 触发，spawn `_run_nap_background` daemon 线程（entity→dream）。`_nap_running` 是 runner 实例的 `threading.Event`（`runner.py:744`），`_run_nap_background` 的 `finally` 清除（`runner.py:1266`）。clear 跑自己的 tidy 前，必须先等 nap 线程结束（否则并发提炼同一批消息 + 竞争写游标）。
- **前端 `/clear` 现状**（`chat.html:1495-1519`）：`isProcessing` 时 `sendMessage('/stop')` → `_pendingClear=true` + 120s 超时等 chat_idle → `clearChat()`；空闲时直接 `clearChat()`。**忙分支的 /stop 不是多余的**——它是让忙碌主 Agent 在当前迭代边界退出、释放 `_chat_lock` 的唯一机制（`runner.chat()` 一轮可远超 30s）。此分支必须保留。
- **前端 `/new`**（`chat.html:1522-1530`）：直接 `clearChat()`，无 stop。

---

## 修订记录（第 2 版，纳入审查修复）

| 发现 | 严重级 | 修复（已入本版计划） |
|---|---|---|
| F1 字段名不匹配（后端 snake_case vs 前端 camelCase），tidy 永不触发 | P0 | 统一为 **snake_case**：main.js（Task 6）与 Task 8 测试都发 `run_tidy_before` |
| F2 忙分支 30s 锁超时回归 | P1 | Task 7 保留 isProcessing 忙分支（/stop + 等 chat_idle + 120s）；仅空闲分支直接 `clearChat(true)`；**不删 _pendingClear** |
| F3 背景 nap 线程并发重复整理 | P1 | Task 3 在 `request_stop()` 后、`clear_stop()` 前 `await asyncio.to_thread(runner._nap_running.wait, 300.0)` |
| F4 tokens_after 可能未定义 | P2 | 已核实 `tokens_after = display_tokens` 默认值在 L3945，skip 分支返回 `display_tokens`（非 0） |
| F5 370 行盲缩进风险 | P2 | 用**嵌套 async def 闭包** `_compress_force()` 抽压缩块，不缩进 370 行 |
| F6 阻塞期间输入被静默丢弃 | P2 | Task 7 阻塞期间 `sendBtn.disabled = true` + busy 提示 |
| F7 双击/中断后 aborted 不清空 | P2 | aborted 时改为"跳过整理直接清空"（非错误），不丢用户 clear 意图 |
| F8 并发安全 | P3 | 确认正确；必须保持 `chat_lock_already_held=False` 的两条路径（tidy_context 端点、ChatQueue `_retry_force_compression`）不变 |
| F9 helper 同步性 | P3 | 保留 async（与 clear_chat 风格一致），无功能缺陷 |
| F10 测试响应形状 | P3 | Task 8 修正 /api/chat/session 期望为 ChatResponse |
| F11 _pendingClear 残留 | P3 | 因保留忙分支，**_pendingClear 完整保留**，Task 7 不再删除它 |

---

## File Structure

| 文件 | 责任 |
|---|---|
| `niu_api/compat.py` | 核心后端：`_reset_all_cursors()` helper、`_tidy_context_impl` 加 `skip_compress` + 嵌套 `_compress_force`、`clear_chat` 加 `run_tidy_before`（含 nap 协调 + aborted 兜底） |
| `niu_api/chat.py` | 加固 `clear_session`（DELETE /chat/session）游标复位 |
| `niu_api/session.py` | 加固 `delete_messages`/`delete_session` 游标复位 |
| `ui/main/windows/assistant/chat.html` | 前端 `/clear` 命令改造（保留忙分支、空闲分支 `clearChat(true)`、阻塞期间禁用输入） |
| `ui/main/preload-chat.js` | `clearChat` IPC 透传参数 |
| `ui/main/main.js` | `'clear-chat'` IPC handler 接收参数并 POST 后端（snake_case） |

---

### Task 1: 后端 — 抽出公共游标复位 helper `_reset_all_cursors`

**Files:**
- Modify: `niu_api/compat.py`（在 `clear_chat` 定义前插入 helper）

- [ ] **Step 1: 在 `clear_chat` 路由装饰器（`@router.post("/api/chat/clear")`，现 L2380）之前插入模块级 helper**

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

> `logger` 已在 compat.py 顶部导入。保留 async（与 clear_chat 风格一致，供 chat.py/session.py 延迟 import 后 `await`）。

- [ ] **Step 2: `clear_chat` 用 helper 替换内联循环**

把 `clear_chat` 内游标复位段（现 `compat.py:2424-2433`）：

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

### Task 2: 后端 — `_tidy_context_impl` 加 `skip_compress`，force 分支用嵌套闭包 `_compress_force` 抽压缩块

**Files:**
- Modify: `niu_api/compat.py:2477`（签名）、`niu_api/compat.py:3587-3961`（force 压缩块抽出）

- [ ] **Step 1: 修改 `_tidy_context_impl` 签名**

当前签名（`compat.py:2477`）：

```python
async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False):
```

改为：

```python
async def _tidy_context_impl(
    request: dict,
    chat_lock_already_held: bool = False,
    skip_compress: bool = False,
):
```

docstring 的 `Args:` 段追加：

```python
        skip_compress: 为 True 时跳过 context-manager 压缩步骤（force 模式）
            ——用于 /clear 场景：只做内容提炼+梦境进化+日志记录，不压缩。
```

- [ ] **Step 2: force 分支把压缩块整体移入嵌套闭包 `_compress_force`，在 `skip_compress` 时跳过**

force 分支的 context-manager 压缩从 `compat.py:3587`（`# 3/3. context-manager force prompt — 一轮 JSON 文件方案`）开始，到 `L3960` 结束（`L3961` 是 `return {"status": "ok", "mode": "force", "tokens_before": display_tokens, "tokens_after": tokens_after}`）。

在 force 分支的 journal-agent 段（`L3586` 之后）插入：

```python
            # 3/3. context-manager 强制压缩（抽为嵌套闭包，skip_compress=True 时跳过）
            async def _compress_force():
                # <此处为原 L3587~L3960 压缩块整体内容，含：
                #   - 提前 return：aborted -> {"status":"aborted",...}；SUBAGENT_ERROR -> {"status":"skipped",...}；截断 -> return
                #   - L3770-3803 的 chat_lock_already_held 分流（False 时 pause+acquire+等 _processing_done；True 时跳过）
                #   - 末尾 tokens_after 计算与 return {"status":"ok","mode":"force","tokens_before":display_tokens,"tokens_after":tokens_after}>
                ...

            if skip_compress:
                logger.info("[Tidy] Force: skip_compress=True, skipping context-manager compression")
                return {"status": "ok", "mode": "force", "skip_compress": True,
                        "tokens_before": display_tokens, "tokens_after": display_tokens}
            return await _compress_force()
```

> ⚠️ **关键实现约束（二次核对确认）**：
> 1. 压缩块整体（L3587~L3960）以**嵌套 async def 闭包**形式内联，捕获外层全部变量（display_tokens/target_tokens/usage_percent/llm_config/messages/msg_tokens/store/last_compress_id/new_dream_id/last_*_id/compress_cursor_path/protect_recent_count/request 等），**不要**改成模块级巨签名 helper（漏传会 NameError），**不要**手工缩进 370 行进 else（ast.parse 查不出作用域错位）。闭包内的提前 return 和最终 return 原样保留。
> 2. `skip_compress` 分支返回 `tokens_after: display_tokens`（压缩前值，语义正确；`tokens_after` 在 L3945 已有 `= display_tokens` 默认值，无需额外 `tokens_after = 0` 守卫）。
> 3. `notify_compact_status_sync("done", mode=mode)` 在 `_tidy_context_impl` **外层 finally**（L4005-4008），与压缩块无关，两种路径都照常广播，**不要动它**。
> 4. `_force_msg_ids`、`new_compress_id` 只在压缩块内使用，skip 分支不引用，无泄漏问题。
> 5. `chat_lock_already_held=False` 的两条调用路径（`tidy_context` 端点 `compat.py:2472`、ChatQueue `_retry_force_compression` `chat_queue.py:425`）必须保持原 else 分支的 pause+acquire+等 `_processing_done` 逻辑（L3770-3803），**不得误改**。

- [ ] **Step 3: 语法 + 编译检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

Run: `cd niu_api && /Users/lilei/tools/ai-bot/python/bin/python -m py_compile compat.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(tidy): skip_compress support via nested _compress_force closure"
```

---

### Task 3: 后端 — `clear_chat` 支持 `run_tidy_before`，先协调 nap、再整理、后清空（阻塞）

**Files:**
- Modify: `niu_api/compat.py:2381`（`clear_chat` 端点）

- [ ] **Step 1: `clear_chat` 改为接收 request body**

当前签名（`compat.py:2381`）：

```python
@router.post("/api/chat/clear")
async def clear_chat() -> dict:
```

改为：

```python
@router.post("/api/chat/clear")
async def clear_chat(request: dict = None) -> dict:
```

> 前端 `/new` 不传 body 时 `request` 为 None，`run_tidy_before` 为 False，走原逻辑。

- [ ] **Step 2: 在锁内、清空前，插入 nap 协调 + run_tidy_before 整理**

当前关键段（`compat.py:2384-2403`）：

```python
    from agent.runner import clear_stop, request_stop
    request_stop()

    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=30.0)
    except TimeoutError:
        ...
        return {"success": False, "error": "系统正忙，请稍后再试"}

    try:
        clear_stop()  # 防御性清除：确保清空时标志干净
        ...
        store = await get_message_store()
        count = await store.clear_messages()
```

改造为（关键：nap wait 必须位于 `request_stop()` 之后、`clear_stop()` **之前**，且 `run_tidy_before` 判定用 **snake_case** `run_tidy_before`）：

```python
    from agent.runner import clear_stop, request_stop
    request_stop()

    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=30.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 30s timeout, clear rejected")
        clear_stop()  # 防止停止标志残留，影响后续定时任务
        return {"success": False, "error": "系统正忙，请稍后再试"}

    try:
        run_tidy_before = bool((request or {}).get("run_tidy_before"))

        # 等背景 nap 线程结束（先清 stop 会令 nap 边界看不到 stop，故必须在 clear_stop 之前 wait）
        from niu_api.chat import get_or_create_runner
        _runner = get_or_create_runner()
        if run_tidy_before and _runner is not None and _runner._nap_running.is_set():
            logger.info("[clear_chat] Waiting for background nap to finish before tidy")
            await asyncio.to_thread(_runner._nap_running.wait, 300.0)  # 兜底 300s；nap 看到 stop 标志后即可清 Event

        clear_stop()  # 防御性清除：确保清空时标志干净；清掉 request_stop 标志，避免 force 管道内 is_stop_requested() 立即 abort
        if run_tidy_before:
            try:
                tidy_result = await _tidy_context_impl(
                    request={"session_id": "default", "mode": "force"},
                    chat_lock_already_held=True,  # clear_chat 已持有 _chat_lock，防 asyncio.Lock 不可重入死锁
                    skip_compress=True,
                )
            except Exception as e:
                logger.error(f"[clear_chat] run_tidy_before failed: {e}")
                tidy_result = {"status": "error", "message": str(e)}
            if isinstance(tidy_result, dict) and tidy_result.get("status") == "aborted":
                # 中断（如用户二次按 stop）：仍继续清空（用户 clear 意图优先），不把这次 clear 当失败
                logger.warning("[clear_chat] Tidy aborted (stop requested); proceeding to clear messages anyway")
            else:
                logger.info(f"[clear_chat] run_tidy_before completed: {tidy_result.get('status')}")

        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
        store = await get_message_store()
        count = await store.clear_messages()
```

> ⚠️ **实现约束（二次核对确认）**：
> 1. `_tidy_context_impl` 与 `clear_chat` **同文件**（compat.py），直接调用，**不得** `from niu_api.compat import _tidy_context_impl`（会自我 import，plan 已删）。
> 2. `clear_stop` 已在函数顶部 `from agent.runner import clear_stop, request_stop` 导入，Step 2 里**不得重复 import**，直接复用。
> 3. `get_or_create_runner` 返回模块级 runner 单体（chat.py:418-441），与启动 nap 的 self 同一实例，故 `_runner._nap_running` 即 nap 在用的 Event。
> 4. **nap wait 顺序是硬约束**：`request_stop()`（已执行，L2386）→ nap wait → `clear_stop()`。若先 clear_stop，nap 边界看不到 stop 会继续扫完，wait 拖更久。
> 5. aborted 时**仍清空**（不返回错误），避免用户双击/中断后留下未清空会话。

其余清空逻辑（`runner.handler.reset_working_memory()`、`cleanup_all_tmp()`、`_reset_all_cursors()` 调用）保持现状不变。

- [ ] **Step 3: 语法 + 编译检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

Run: `cd niu_api && /Users/lilei/tools/ai-bot/python/bin/python -m py_compile compat.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(clear): run force tidy (no compress) before clearing when run_tidy_before=true"
```

---

### Task 4: 后端 — 加固 `chat.py` `clear_session` 游标复位

**Files:**
- Modify: `niu_api/chat.py:828-835`（`clear_session`）

- [ ] **Step 1: 在 `clear_session` 内、清消息后调 helper**

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

改为：

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

> 函数内延迟 import（沿用 `chat.py:537` 既有约定），避免顶层循环依赖。

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
- Modify: `niu_api/session.py:57-62`（`delete_messages`）、`niu_api/session.py:80-89`（`delete_session`）

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

改为（在现有 `from niu_api.chat import get_or_create_runner` 之后追加）：

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

### Task 6: 前端 — `preload-chat.js` 与 `main.js` IPC 传递 `run_tidy_before`（snake_case）

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
  clearChat: (tidy) => ipcRenderer.invoke('clear-chat', tidy),
```

- [ ] **Step 2: `main.js` 的 'clear-chat' handler 接收参数并 POST（snake_case）**

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
ipcMain.handle('clear-chat', async (event, tidy) => {
  // 清空待推送消息队列
  pendingAlertMessages = [];

  return new Promise((resolve) => {
    // /clear 传 run_tidy_before=true（先整理后清空）；/new 传 undefined（直接清空）
    // 必须 snake_case：后端 clear_chat 用 request.get("run_tidy_before") 读取（无 pydantic 转换）
    const data = JSON.stringify({ sessionId: 'default', run_tidy_before: !!tidy });
```

（其余 http.request 代码不变，POST body 带上 `run_tidy_before` 字段。）

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && node --check ui/main/preload-chat.js && node --check ui/main/main.js`
Expected: 无输出（语法 OK）

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/preload-chat.js ui/main/main.js && git commit -m "feat(clear): pass run_tidy_before (snake_case) through IPC to backend"
```

---

### Task 7: 前端 — `chat.html` `/clear` 命令改造（保留忙分支、空闲分支 `clearChat(true)`、阻塞禁用输入）

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`/clear` 块现 L1495-1519、chat_idle 消费现 L2444-2456、`clearChat()` 现 L1565-1581）

**重要**：本版**保留 `_pendingClear` 及忙分支**（F2 修复）——忙分支的 `/stop` 不是多余的，是让忙碌主 Agent 释放 `_chat_lock` 的唯一机制。

- [ ] **Step 1: `clearChat()` 支持 tidy 参数**

当前（`chat.html:1565`）：

```js
    async function clearChat() {
      try {
        const result = await window.electronAPI.clearChat();
```

改为：

```js
    async function clearChat(tidy) {
      try {
        const result = await window.electronAPI.clearChat(tidy);
```

- [ ] **Step 2: 重写 `/clear` 命令块（保留忙分支）**

当前（`chat.html:1495-1519`）：

```js
      // /clear 指令 - 先停止再清空（允许在 isProcessing=true 时执行）
      if (text === '/clear') {
        userInput.value = '';
        userInput.style.height = 'auto';
        sendBtn.disabled = false;
        if (isProcessing) {
          // Agent 正在运行：先停止，等 chat_idle 事件再清空
          try {
            await window.electronAPI.sendMessage('/stop');
          } catch (e) {
            console.error('停止失败:', e);
            addSystemMessage('停止失败: ' + (e.message || e));
          }
          _pendingClear = true;
          if (_pendingClearTimeout) clearTimeout(_pendingClearTimeout);
          _pendingClearTimeout = setTimeout(() => {
            _pendingClear = false;
            _pendingClearTimeout = null;
            addSystemMessage('停止超时，清空操作已取消');
          }, 120000);
        } else {
          // Agent 空闲：直接清空，无需等 chat_idle
          await clearChat();
        }
        return;
      }
```

改为（忙分支保留，`clearChat()` 换成 `clearChat(true)`；空闲分支加"正在整理"提示 + 阻塞期间禁用输入）：

```js
      // /clear 指令 - 先整理（小憩+日志）后清空（后端跑完 entity/dream/journal 才清空，阻塞）
      if (text === '/clear') {
        userInput.value = '';
        userInput.style.height = 'auto';
        sendBtn.disabled = false;
        if (isProcessing) {
          // Agent 正在运行：先 /stop 让其释放 _chat_lock，等 chat_idle 事件再做整理+清空
          // 忙分支的 /stop 必需——它是让忙碌主 Agent 退出、释放锁的唯一机制
          try {
            await window.electronAPI.sendMessage('/stop');
          } catch (e) {
            console.error('停止失败:', e);
            addSystemMessage('停止失败: ' + (e.message || e));
          }
          _pendingClear = true;
          if (_pendingClearTimeout) clearTimeout(_pendingClearTimeout);
          _pendingClearTimeout = setTimeout(() => {
            _pendingClear = false;
            _pendingClearTimeout = null;
            addSystemMessage('停止超时，清空操作已取消');
          }, 120000);
        } else {
          // Agent 空闲：直接整理+清空（阻塞）。整理期间禁用输入，防止新消息被静默丢弃
          addSystemMessage('正在整理对话并清空会话，请稍候…');
          stopBtn.style.display = 'none';
          sendBtn.disabled = true;
          userInput.disabled = true;
          try {
            await clearChat(true);
          } catch (e) {
            addSystemMessage('❌ 清空失败: ' + (e.message || e));
          } finally {
            sendBtn.disabled = false;
            userInput.disabled = false;
            userInput.focus();
          }
        }
        return;
      }
```

- [ ] **Step 3: 更新 chat_idle 消费块——忙分支触发整理**

当前（`chat.html:2444-2456`）：

```js
        // 处理 /clear 的延迟清空
        if (_pendingClear) {
          _pendingClear = false;
          if (_pendingClearTimeout) { clearTimeout(_pendingClearTimeout); _pendingClearTimeout = null; }
          try {
            await clearChat();
          } catch (e) {
            console.error('清空失败:', e);
            addSystemMessage('清空失败: ' + (e.message || e));
          }
        }
```

改为：

```js
        // 处理 /clear 的延迟整理+清空
        if (_pendingClear) {
          _pendingClear = false;
          if (_pendingClearTimeout) { clearTimeout(_pendingClearTimeout); _pendingClearTimeout = null; }
          addSystemMessage('正在整理对话并清空会话，请稍候…');
          try {
            await clearChat(true);
          } catch (e) {
            console.error('清空失败:', e);
            addSystemMessage('清空失败: ' + (e.message || e));
          }
        }
```

> `_pendingClear` / `_pendingClearTimeout` 声明（`chat.html:1019-1020`）**保留**（忙分支仍用）。

- [ ] **Step 4: 验证关键引用一致**

- `clearChat(true)` 只出现在 `/clear` 空闲分支 + chat_idle 消费块两处；忙分支走 `_pendingClear` 流程。
- `run_tidy_before`（后端）与 main.js 的 snake_case 发送一致。

Run: `cd /Users/lilei/tools/ai-bot && grep -n "clearChat(" ui/main/windows/assistant/chat.html | head`
Expected: 展示多处 `clearChat()`（/new 处 `clearChat()` 无参、/clear 忙分支透传、/clear 空闲分支 `clearChat(true)`、chat_idle `clearChat(true)`、清空失败回退）——/new 保持无参。

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/windows/assistant/chat.html && git commit -m "feat(clear): /clear runs tidy then clears (blocking); keep busy branch /stop + idle branch clearChat(true)"
```

---

### Task 8: 测试验证（真实 LLM + 真实流程）

**Files:** 无新增；本 Task 全为验证步骤。

> 前置：需应用运行（`./niu` 或 `python -m niu_api`）。若未运行，先 `cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`（铁律 8）拉起最新后端。

- [ ] **Step 1: 准备真实对话数据**

向前端输入框发送若干条真实消息（或 POST `/api/chat/session`）：

```bash
curl -X POST http://127.0.0.1:9876/api/chat/session -H 'Content-Type: application/json' \
  -d '{"message": "我今天完成了/clear命令的整理增强功能开发，涉及后端tidy管道和前端IPC改动", "source": "electron"}'
```

Expected: 返回 `ChatResponse`（`reply`/`session_id`/`message_id` 结构），消息已入库。（/api/chat/session 返回 ChatResponse，不是 `{"session_id": ...}` 裸字典。）

- [ ] **Step 2: 触发 `/clear`（run_tidy_before）并验证阻塞 + 整理**

从前端输入框输入 `/clear`（或 curl 模拟，注意 **snake_case** 字段名）：

```bash
curl -X POST http://127.0.0.1:9876/api/chat/clear -H 'Content-Type: application/json' \
  -d '{"sessionId": "default", "run_tidy_before": true}'
```

Expected:
- 接口**不立即返回**（阻塞；等 entity→dream→journal 三个子 Agent 依次跑完）
- 后端日志（`logs/`）出现：`[Tidy] Force mode: starting entity-extractor` → `[Tidy] Force: dream-evolver ...` → `[Tidy] Force: starting journal-agent` → **无 context-manager 压缩日志**（skip_compress 生效）
- 返回 `{"success": true, "deleted_count": N, ...}`
- 若 `_nap_running` 活跃，先出现 `[clear_chat] Waiting for background nap to finish before tidy`

- [ ] **Step 3: 验证整理产物落盘（先记录后清空）**

- `~/.niu/workspace/`（或配置工作目录）的 `journal.md` 已新增本次对话的工作日志条目。
- LightRAG 知识图谱（`~/.niu/lightrag/`）已新增本次对话精炼文档/实体（entity-extractor + dream-evolver 写入）。

- [ ] **Step 4: 验证消息已清空 + 游标已复位**

```bash
cd /Users/lilei/tools/ai-bot
# 消息已清空
curl -s http://127.0.0.1:9876/api/chat/messages 2>/dev/null || true
# 游标文件已删除
ls -la ~/.niu/last_entity_extract.json ~/.niu/last_dream_evolve.json ~/.niu/last_compress.json ~/.niu/last_journal.json 2>&1
```

Expected: 消息为空；4 个游标文件 `No such file`（已复位）。

- [ ] **Step 5: 验证 `/new` 路径不回归**

前端输入 `/new`（或 curl 不带 `run_tidy_before`）：

```bash
curl -X POST http://127.0.0.1:9876/api/chat/clear -H 'Content-Type: application/json' \
  -d '{"sessionId": "default"}'
```

Expected: **不触发**整理（无 entity/dream/journal 日志）；消息清空 + 游标复位；`{"success": true}`。

- [ ] **Step 6: 验证忙分支路径**

在 Agent 正忙（`isProcessing=true`）时输入 `/clear`：
Expected:
- 前端发送 `/stop`（必要，让主 Agent 释放 `_chat_lock`），等 `chat_idle` 到达后显示"正在整理对话并清空会话"，随后 `clearChat(true)` 阻塞整理+清空，最后"✅ 聊天记录已清空"。
- 若 120s 内 `chat_idle` 未到，出现"停止超时，清空操作已取消"（`_pendingClearTimeout` 保留，回归旧行为）。

- [ ] **Step 7: 验证中断兜底**

忙分支 `/clear` 后、整理进行中时再次按 ESC 或 /stop：
Expected: tidy 管道在子 Agent 边界 abort，`clear_chat` 记录 `Tidy aborted...proceeding to clear messages anyway`，仍完成清空（不留下未清空会话）。

- [ ] **Step 8: 确认无回归（跑既有相关测试）**

Run: `cd /Users/lilei/tools/ai-bot/agent && pytest -q 2>&1 | tail -20`
Expected: 既有测试不因本次改动失败。若全套太慢，只跑与 clear/tidy/disk 相关的测试文件。

---

## Self-Review 自查

**1. Spec 覆盖：**
- ✅ 去掉 `/clear` 多余 `/stop`（仅**空闲**分支；忙分支保留 —— 它是释放 `_chat_lock` 的必要机制，非"多余"）
- ✅ 触发小憩（entity+dream）+ 日志记录（journal-agent）（Task 2,3 复用 force 前三步，skip_compress）
- ✅ 最后一步压缩换成 Clear（Task 2 skip_compress、Task 3 清空）
- ✅ 阻塞（子 Agent `asyncio.to_thread` 阻塞 + 前端一次 await）
- ✅ 游标复位统一加固（Task 1 helper + Task 4,5 session 端点）
- ✅ 与背景 nap 线程协调（Task 3，request_stop → nap wait → clear_stop）

**2. Placeholder 扫描：** 无 "TBD"/"适当处理" 占位；所有代码块完整。Task 2 压缩块以"嵌套闭包内联原内容 + 明确移动范围（L3587~L3960）与 5 条关键约束"描述，非占位。

**3. 类型一致性：**
- 字段名全链路统一 **snake_case**：main.js 发 `run_tidy_before: true`（Task 6）→ 后端 `request.get("run_tidy_before")`（Task 3）→ curl 测试（Task 8）一致。**F1 已修。**
- `skip_compress` 签名（Task 2）与调用（Task 3 `skip_compress=True`）一致。
- `_reset_all_cursors` 定义为 async（Task 1），Task 4/5、clear_chat 均 `await`（Task 3 用 helper）。
- `clearChat(tidy)` 前端参数从 preload（Task 6）到 /clear 两分支（Task 7）一致。
- `_pendingClear`/`_pendingClearTimeout` 完整保留（忙分支仍用），无悬空引用。

**4. 并发与锁（二次核对结论）：**
- `chat_lock_already_held=True` 所有调用方都持 `_chat_lock`（clear_chat L2391 / chat_session L2242），无死锁。
- `chat_lock_already_held=False` 两条路径（tidy_context 端点、ChatQueue `_retry_force_compression`）保持原 else 分支不变。
- skip_compress 时压缩块（含内部锁处理）整体跳过，clear_chat 持锁期间主 Agent 已退出，新消息只入队不处理，无并发变异。
- nap wait 置于 request_stop 之后、clear_stop 之前，`_nap_running` 有界（finally 必清 + 300s 兜底）。
- 客户端 http.request 无 timeout（main.js 已验证），多分钟阻塞不会中途断开。
