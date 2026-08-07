# /clear 命令增强：先整理（小憩+日志）后清空 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前端 `/clear` 命令在清空消息前，先阻塞执行"强制整理的前三步"（entity-extractor → dream-evolver → journal-agent），跳过 context-manager 压缩，最后清空全部消息；并把游标复位逻辑统一加固到所有清空路径。

**Architecture:** 复用现有 `_tidy_context_impl(mode="force")`（`niu_api/compat.py`）三步子 Agent 管道，给其新增 `skip_compress` 参数跳过 context-manager 压缩步骤；`clear_chat`（`/api/chat/clear`）新增 `run_tidy_before` 请求参数，为 true 时在持有 `_chat_lock` 的情况下先阻塞调用该管道，再执行现有清空逻辑；抽出公共 `_reset_all_cursors()` helper 供所有清空端点复用，消除 `chat.py`/`session.py` 里"清消息不复位游标"的隐患。前端 `/clear` 去掉多余的 `/stop`，改为一次 `clearChat(true)` 阻塞等待。

**Tech Stack:** Python (FastAPI + asyncio + subagent pipeline), Electron (IPC + renderer chat.html), 现有磁盘配置天然支持子 Agent 调用。

---

## 背景与现状核实（工程师必读）

参与修改的所有文件都基于这些事实：

- **`/new` 和 `/clear` 前端目前都走 `clearChat()` → `window.electronAPI.clearChat()` → main.js IPC 'clear-chat' → POST `/api/chat/clear` → `clear_chat()`**（`niu_api/compat.py:2381`）。
- **`clear_chat` 现已在清空时删除全部 4 个游标文件**（`compat.py:2425-2433`）：`last_entity_extract.json`、`last_dream_evolve.json`、`last_compress.json`、`last_journal.json`。所以 `/new` 和 `/clear` **都已复位游标**——这一步原有代码已做。
- **隐患**：`niu_api/chat.py:828` 的 `DELETE /chat/session/{session_id}`（`clear_session`）和 `niu_api/session.py:57`（`delete_messages`）、`session.py:80`（`delete_session`）都调 `store.clear_messages()` 清消息但**不删游标文件**。当前虽无前端调用，但造成"清消息而游标残留指向已删消息"的不一致，本计划统一加固。
- **force 管道 `_tidy_context_impl`**（`compat.py:2477`）：`mode="force"` 分支（`L3344`）串行执行：
  1. entity-extractor（全量，`L3355-3421`）
  2. dream-evolver（增量，`L3423-3504`）
  3. journal-agent（force 始终调用，`L3506-3586`）
  4. context-manager 压缩（`L3588` 起，到 `L3961` 返回前）
  本计划在**步骤 4 之前**加 `skip_compress` 开关，跳过压缩直接走到返回段。
- **子 Agent 阻塞**：管道内每个子 Agent 用 `run_xxx_force = lambda: call_subagent_with_auto_answer(...)` + `await asyncio.to_thread(run_xxx_force)` 阻塞等待完成——这正是"阻塞"的来源。
- **`chat_lock_already_held`**：`_tidy_context_impl` 参数，为 True 时跳过内部的 `_chat_lock` 获取和 ChatQueue pause/resume（`compat.py:3775-3803`）。`clear_chat` 已持有 `_chat_lock`，调用时必须传 True——否则 `asyncio.Lock` 不可重入会死锁。
- **stop 标志冲突**：`clear_chat` 开头 `request_stop()` 停主 Agent，会让 force 管道内 `is_stop_requested()` 返回 True 提前 abort。跑 tidy 前必须先 `clear_stop()`（`agent/runner.py`）。子 Agent 走独立 `to_thread` 线程，不受主 Agent stop 影响，可正常跑完。
- **锁与并发**：clear_chat 用 `_chat_lock`（`compat.py:2391`）+ 管道用 `_tidy_lock`？—— 管道本身不加 `_tidy_lock`（`_tidy_lock` 只在 `tidy_context` 端点外层加）。`clear_chat` 调 `_tidy_context_impl` 时不经过 `_tidy_lock`，只依赖 `_chat_lock` 串行化。满足阻塞且无新增锁。
- **延迟 import 约定**：`chat.py`/`session.py` 通过函数内 `from niu_api.compat import ...` **延迟 import** `compat` 符号（见 `chat.py:537,656`），避免顶层循环依赖。新 helper `_reset_all_cursors` 必须同样延迟 import。
- **前端 `/clear` 现状**（`chat.html:1495-1519`）：`isProcessing` 时 `sendMessage('/stop')` → 设 `_pendingClear=true` + 120s 超时等 chat_idle → 调 `clearChat()`；空闲时直接 `clearChat()`。`_pendingClear` 相关代码在 `chat.html:1019-1020` 声明、`L2445-2456` 消费（chat_idle 事件处理里）。
- **前端 `/new` 现状**（`chat.html:1522-1530`）：直接 `clearChat()`，无 stop。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `niu_api/compat.py` | 核心后端：`_reset_all_cursors()` helper、`_tidy_context_impl` 加 `skip_compress`、`clear_chat` 加 `run_tidy_before` |
| `niu_api/chat.py` | 加固 `clear_session`（DELETE /chat/session）游标复位 |
| `niu_api/session.py` | 加固 `delete_messages`/`delete_session` 游标复位 |
| `ui/main/windows/assistant/chat.html` | 前端 `/clear` 命令改造（去 stop、调用 `clearChat(true)`、删 `_pendingClear`） |
| `ui/main/preload-chat.js` | `clearChat` IPC 透传参数 |
| `ui/main/main.js` | `'clear-chat'` IPC handler 接收参数并 POST 后端 |

---

### Task 1: 后端 — 抽出公共游标复位 helper `_reset_all_cursors`

**Files:**
- Modify: `niu_api/compat.py`（在 `clear_chat` 定义前插入 helper）

**目标：** 把 `clear_chat` 内联的 4 游标复位逻辑（现 `compat.py:2425-2433`）抽成模块级 async helper，供 `clear_chat` 及 `chat.py`/`session.py` 复用。

- [ ] **Step 1: 在 `clear_chat` 函数定义（`@router.post("/api/chat/clear")`，现 L2381）之前，插入模块级 helper**

在 `niu_api/compat.py` 的 `clear_chat` 路由装饰器（当前 `compat.py:2380` 附近）前、`get_pending_alerts` 之后的位置，插入：

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

> 说明：此 helper 是 async（虽内部同步），与调用方 clear_chat 的 async 风格一致，且 `chat.py`/`session.py` 的调用方也都是 async 端点，可 `await`。`logger` 已在 compat.py 顶部导入，无需额外 import。

- [ ] **Step 2: `clear_chat` 改用 helper 替换内联循环**

把 `clear_chat` 内现有游标复位段（当前 `compat.py:2425-2433`）：

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
Expected: `OK`（无语法错误）

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "refactor(clear): extract shared _reset_all_cursors helper"
```

---

### Task 2: 后端 — `_tidy_context_impl` 新增 `skip_compress` 参数，force 分支跳过压缩

**Files:**
- Modify: `niu_api/compat.py:2477`（函数签名）、`niu_api/compat.py:3588`（force 分支压缩步骤前）

**目标：** force 管道第三步 context-manager 压缩可被跳过（`/clear` 用它，压缩步骤换成清空）。

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

同时更新 docstring，在 `Args:` 段追加一行：

```python
    Args:
        chat_lock_already_held: 调用方已持有 _chat_lock 时传 True，
            跳过内部的 _chat_lock 获取和 ChatQueue pause/resume，
            避免自死锁（asyncio.Lock 不可重入）。
        skip_compress: 为 True 时跳过 context-manager 压缩步骤（force 模式）
            ——用于 /clear 场景：只做内容提炼+梦境进化+日志记录，不压缩。
```

- [ ] **Step 2: force 分支压缩步骤前加 `skip_compress` 分流**

force 分支的 context-manager 压缩从 `compat.py:3588`（注释 `# 3/3. context-manager force prompt — 一轮 JSON 文件方案`）开始，一直延续到 `L3961`（`return {"status": "ok", "mode": "force", ...}`）之前的所有压缩逻辑。

在 `compat.py:3588` 该注释行前，插入分流：

```python
            if skip_compress:
                # /clear 场景：不压缩，跳过整个 context-manager 步骤
                logger.info("[Tidy] Force: skip_compress=True, skipping context-manager compression")
            else:
```

**然后**把原来 `# 3/3. context-manager force prompt — 一轮 JSON 文件方案` 这一注释行起、到 `L3961` return 之间的整个压缩主体**整体缩进一层**，包进 `else:` 块内。（该分支体量约 370 行——建议用编辑工具做整块缩进，或由 implementer 用本计划下游的精确代码替换；缩进后的代码逻辑不变。）

> ⚠️ **关键**：压缩块内有一些变量（如 `_force_msg_ids`、`new_compress_id`、`tokens_after`）在压缩块内定义、`L3961` return 引用 `tokens_after`。`skip_compress=True` 时必须保证 `tokens_after` 有定义。处理方式：在 else 分支的 return 前，`tokens_after` 已在 `L3946-3959` 的 try 里重新计算（`post_total = sum(...)`），该段在 else 块内也会执行；若 `post_messages` 为空则 `tokens_after` 可能未定义——故在 `skip_compress` 时 return 前特判。详见 Step 3 的返回结构。

- [ ] **Step 3: 保证 skip_compress 时的返回路径不引用未定义变量**

force 分支现有返回段（`compat.py:3946-3961`）：

```python
            try:
                post_messages = await store.get_messages()
                ...
                tokens_after = post_total
            except Exception:
                pass

            return {"status": "ok", "mode": "force", "tokens_before": display_tokens, "tokens_after": tokens_after}
```

改造后（skip_compress 分支与 else 分支共用此返回段，但 skip_compress 时 `tokens_after` 初始可能未定义）——在 `try/except` 前加默认值，并把 return 结构统一：

```python
            tokens_after = 0
            try:
                post_messages = await store.get_messages()
                ...
                tokens_after = post_total
            except Exception:
                pass

            if skip_compress:
                return {"status": "ok", "mode": "force", "skip_compress": True,
                        "tokens_before": display_tokens, "tokens_after": 0}

            return {"status": "ok", "mode": "force", "tokens_before": display_tokens, "tokens_after": tokens_after}
```

> ⚠️ **注意**：上述返回段位于 else 块的末尾（仍在 `else:` 缩进内）。`skip_compress` 的 return 在 else 块外（顶层 if 的另一个分支后）会破坏结构。**推荐结构**：把整段「压缩主体 + 返回」都包在 `if skip_compress: [log + tokens_after=0 + return] else: [原压缩主体 + 原返回]` 中，return 保持各自在各自分支内。让 implementer 以「压缩主体整体进 else，skip_compress 分支独立 return」的方式落地，确保变量作用域正确。

- [ ] **Step 4: 语法检查 + 确认 force 主流程未被破坏**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

Run: `cd niu_api && /Users/lilei/tools/ai-bot/python/bin/python -m py_compile compat.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(tidy): support skip_compress in force pipeline for /clear"
```

---

### Task 3: 后端 — `clear_chat` 支持 `run_tidy_before`，先整理后清空（阻塞）

**Files:**
- Modify: `niu_api/compat.py:2381`（`clear_chat` 端点）

**目标：** `/clear` 调用时先阻塞跑 force 前三步（entity→dream→journal，skip_compress），再清空消息。全程持有 `_chat_lock`。

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

> 注意：路由改为接收可选 body（与 `tidy_context` 的 `request: dict` 一致）。`/new` 不传 body 时 `request` 为 None 或 `{}`，`run_tidy_before` 为 False，走原逻辑。

- [ ] **Step 2: 在锁内、清空前，插入 `run_tidy_before` 逻辑**

在 `clear_chat` 拿到 `_chat_lock` 之后、`clear_stop()`（现 `compat.py:2398`）附近，插入整理流程。当前关键段（`compat.py:2384-2403`）：

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

改造为（在 `clear_stop()` 之后、清消息之前插入）：

```python
    try:
        clear_stop()  # 防御性清除：确保清空时标志干净

        # /clear：先跑 force 整理（entity→dream→journal），不压缩，阻塞，再清空
        run_tidy_before = bool((request or {}).get("run_tidy_before"))
        if run_tidy_before:
            from agent.runner import clear_stop as _cs
            from niu_api.compat import _tidy_context_impl
            _cs()  # request_stop 已在上方设置，此处清掉 stop 标志，避免 force 管道内 is_stop_requested() 提前 abort
            try:
                tidy_result = await _tidy_context_impl(
                    request={"session_id": "default", "mode": "force"},
                    chat_lock_already_held=True,  # clear_chat 已持有 _chat_lock，防死锁
                    skip_compress=True,
                )
            except Exception as e:
                logger.error(f"[clear_chat] run_tidy_before failed: {e}")
                tidy_result = {"status": "error", "message": str(e)}
            if isinstance(tidy_result, dict) and tidy_result.get("status") == "aborted":
                logger.warning("[clear_chat] Tidy aborted (stop requested), clear rejected")
                return {"success": False, "error": "整理被中断，未清空会话"}
            logger.info(f"[clear_chat] run_tidy_before completed: {tidy_result.get('status')}")

        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
        store = await get_message_store()
        count = await store.clear_messages()
```

> ⚠️ **循环 import 注意**：`_tidy_context_impl` 就在本文件 compat.py 内，直接可引用，**无需 import**（本文件顶层已定义）。上面的 `from niu_api.compat import _tidy_context_impl` 是多余且错误的（同文件内直接调用即可）—— implementer 必须**移除该 import 行**，直接写 `await _tidy_context_impl(...)`。
> 同时 `clear_stop` 已在函数内顶部 `from agent.runner import clear_stop, request_stop` 导入，Step 2 里重复的 `from agent.runner import clear_stop as _cs` 可简化为直接调 `clear_stop()`。**保持现有 import，新增逻辑只调已导入符号。**

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "feat(clear): run force tidy (no compress) before clearing when run_tidy_before=true"
```

---

### Task 4: 后端 — 加固 `chat.py` `clear_session` 游标复位

**Files:**
- Modify: `niu_api/chat.py:828-835`（`clear_session`）

**目标：** 让 `DELETE /chat/session/{session_id}` 与 `clear_chat` 一致地复位游标，消除不一致隐患。

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

**目标：** 与 Task 4 同理，两个兼容性端点清消息后也复位游标。

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

### Task 6: 前端 — `preload-chat.js` 与 `main.js` IPC 传递 `run_tidy_before`

**Files:**
- Modify: `ui/main/preload-chat.js:72`
- Modify: `ui/main/main.js:1087-1122`

**目标：** 让 renderer 的 `clearChat(tidy)` 能一路传到后端 `run_tidy_before`。

- [ ] **Step 1: `preload-chat.js` 的 `clearChat` 透传参数**

当前（`preload-chat.js:72`）：

```js
  clearChat: () => ipcRenderer.invoke('clear-chat'),
```

改为：

```js
  clearChat: (tidy) => ipcRenderer.invoke('clear-chat', tidy),
```

- [ ] **Step 2: `main.js` 的 'clear-chat' handler 接收参数并 POST**

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
    // /clear 传 runTidyBefore=true（先整理后清空）；/new 传 false/undefined（直接清空）
    const data = JSON.stringify({ sessionId: 'default', runTidyBefore: !!tidy });
```

（其余 http.request 代码不变，POST body 已带上 `runTidyBefore` 字段。）

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && node --check ui/main/preload-chat.js && node --check ui/main/main.js`
Expected: 无输出（语法 OK）

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/preload-chat.js ui/main/main.js && git commit -m "feat(clear): pass runTidyBefore through IPC to backend"
```

---

### Task 7: 前端 — `chat.html` `/clear` 命令改造（去 stop、阻塞调用、删 `_pendingClear`）

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`/clear` 块现 L1495-1519、`clearChat()` 现 L1565-1581、`_pendingClear` 声明现 L1019-1020、消费现 L2445-2456）

**目标：** `/clear` 不再发 `/stop` + 等 idle，改为调用 `clearChat(true)` 一次性阻塞（后端整理+清空），删掉 `_pendingClear` 超时机制，加"正在整理"反馈。

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

- [ ] **Step 2: 重写 `/clear` 命令块**

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

改为：

```js
      // /clear 指令 - 先整理（小憩+日志）后清空（阻塞；后端跑完 entity/dream/journal 才清空）
      if (text === '/clear') {
        userInput.value = '';
        userInput.style.height = 'auto';
        stopBtn.style.display = 'none';
        sendBtn.disabled = false;
        addSystemMessage('正在整理对话并清空会话，请稍候…');
        try {
          await clearChat(true);
        } catch (e) {
          addSystemMessage('❌ 清空失败: ' + (e.message || e));
        }
        return;
      }
```

> 说明：不再依赖 `isProcessing` 分支和 `_pendingClear`。后端 `clear_chat` 内部自行 `request_stop()` 停主 Agent + 跑整理 + 清空，前端一次 await 全程阻塞。

- [ ] **Step 3: 删除 `_pendingClear` 声明与消费**

删除 `chat.html:1019-1020` 的声明：

```js
    let _pendingClear = false;
    let _pendingClearTimeout = null;
```

删除 `chat.html:2444-2456` chat_idle 事件里的消费块（`// 处理 /clear 的延迟清空` 整段，含 `_pendingClear` 判断与 `_pendingClearTimeout` 引用）。

- [ ] **Step 4: 验证无残留引用**

Run: `cd /Users/lilei/tools/ai-bot && grep -n "_pendingClear" ui/main/windows/assistant/chat.html`
Expected: 无匹配（全部清除）

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot && git add ui/main/windows/assistant/chat.html && git commit -m "feat(clear): /clear now blocks on tidy (nap+journal) then clears, drop redundant /stop"
```

---

### Task 8: 测试验证（真实 LLM + 真实流程）

**Files:** 无新增；本 Task 全为验证步骤。

**目标：** 用真实数据验证整个 `/clear` 链路的阻塞、整理、清空与游标复位（铁律 5：真实 LLM）。

> 前置：需要应用在运行（`./niu` 或后端 API `python -m niu_api`）。若未运行，先 `cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`（铁律 8）拉起最新后端，或直接 `python -m niu_api`。

- [ ] **Step 1: 准备真实对话数据**

向会话发送若干条真实消息（通过前端输入框，或 POST `/api/chat/session`）：
Run（示例，实际可用前端发几条带工作内容的真实消息）:
```
curl -X POST http://127.0.0.1:9876/api/chat/session -H 'Content-Type: application/json' \
  -d '{"message": "我今天完成了 /clear 命令的整理增强功能开发，涉及后端 tidy 管道和前端 IPC 改动", "source": "electron"}'
```
Expected: 返回 `{"session_id": "default", ...}`，消息已入库。

- [ ] **Step 2: 触发 `/clear`（run_tidy_before）并验证阻塞 + 整理**

从前端输入框输入 `/clear`（或用 curl 模拟后端端点）：

```bash
curl -X POST http://127.0.0.1:9876/api/chat/clear -H 'Content-Type: application/json' \
  -d '{"sessionId": "default", "runTidyBefore": true}'
```

Expected:
- 接口**不立即返回**（阻塞；等 entity→dream→journal 三个子 Agent 依次跑完，日志可见）
- 后端日志（`logs/`）出现：`[Tidy] Force mode: starting entity-extractor` → `[Tidy] Force: dream-evolver ...` → `[Tidy] Force: starting journal-agent` → `[Tidy] Force: skip_compress=True, skipping context-manager compression` → 返回 `{"success": true, ...}`
- 回复里**没有** context-manager 压缩日志（确认 skip_compress 生效）

- [ ] **Step 3: 验证整理产物落盘（先记录后清空）**

- `~/.niu/workspace/`（或配置的工作目录）的 `journal.md` **已新增**本次对话的工作日志条目
- LightRAG 知识图谱（`~/.niu/lightrag/`）已新增本次对话精炼文档/实体（entity-extractor + dream-evolver 写入）

- [ ] **Step 4: 验证消息已清空 + 游标已复位**

Run:
```bash
cd /Users/lilei/tools/ai-bot
# 消息已清空
curl -s http://127.0.0.1:9876/api/chat/messages 2>/dev/null || true
# 游标文件已删除
ls -la ~/.niu/last_entity_extract.json ~/.niu/last_dream_evolve.json ~/.niu/last_compress.json ~/.niu/last_journal.json 2>&1
```
Expected:
- 消息接口返回空列表（或 count 为 0）
- 游标文件 `ls` 报告 No such file / not found（4 个都删除）

- [ ] **Step 5: 验证 `/new` 路径不回归**

前端输入 `/new`（或 curl `-d '{"sessionId": "default"}'` 不带 runTidyBefore）：
Expected:
- **不触发**整理（无 entity/dream/journal 日志）
- 消息清空 + 游标复位（与现状一致）
- 结果 `{"success": true, ...}`，无整理日志

- [ ] **Step 6: 验证阻塞期间前端反馈**

在 Agent 正忙时（`isProcessing=true`）输入 `/clear`：
Expected: 前端立即显示"正在整理对话并清空会话，请稍候…"，随后（整理+清空完成后）显示"✅ 聊天记录已清空"。整个过程停止按钮不闪烁、无 `/stop` 请求日志。

- [ ] **Step 7: 确认无回归（跑既有相关测试，若有）**

Run: `cd /Users/lilei/tools/ai-bot/agent && pytest -q 2>&1 | tail -20`
Expected: 既有测试通过（若测试套件含 /clear 或 tidy 相关用例不失败）。若跑全套太慢，可只跑与 clear/tidy/disk 相关的测试文件。

---

## Self-Review 自查

**1. Spec 覆盖：**
- ✅ 去掉 `/clear` 多余的 `/stop`（Task 7 Step 2 移除 `sendMessage('/stop')`）
- ✅ 触发小憩（entity+dream）+ 日志记录（journal-agent）（Task 2,3 复用 force 前三步）
- ✅ 最后一步压缩换成 Clear（Task 2 `skip_compress`，Task 3 清空）
- ✅ 阻塞（子 Agent `asyncio.to_thread` 阻塞 + 前端一次 await）
- ✅ 游标复位统一加固（Task 1 helper + Task 4,5 session 端点）

**2. Placeholder 扫描：** Task 2 的 force 压缩块缩进是个大范围机械改动，已明确"整块缩进进 else"而非写死 370 行；Step 3 明确返回变量作用域与结构。无 "TBD"/"适当处理" 占位。

**3. 类型一致性：** `_reset_all_cursors` 在 Task 1 定义为 `async`，Task 4/5 均 `await`；`skip_compress` 在 Task 2 签名 + Task 3 调用一致（`skip_compress=True`）；`run_tidy_before` 请求字段从 Task 3（后端 `request.get("run_tidy_before")`）到 Task 6（main.js `runTidyBefore`）到 Task 7（`clearChat(true)`）命名由 camelCase 转 snake_case 在后端，已统一。`clearChat(tidy)` 前端参数一路透传一致。
