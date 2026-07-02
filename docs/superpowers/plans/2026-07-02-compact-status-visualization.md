# 压缩状态前端可视化 Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 压缩（手动/自动/睡眠触发）进行时，前端让"上下文使用率圆环"旋转起来作为压缩动画，tooltip 显示"正在压缩…"，百分比和数字变红色；压缩结束后恢复原状。

**Architecture:** 后端三处触发点（chat.py 模式2、chat_queue.py 后台重试、runner.py 模式1）统一通过 `loop.call_soon_threadsafe(_sync_broadcast, {...})` 广播 `compact_status` 事件到 `/api/events/stream`；main.js 转发该事件经 IPC 到 chat.html；chat.html 用 SVG `<animateTransform>` 让进度圈旋转，tooltip 和文本颜色切换。不引入 GIF 资源，不引入新的轮询，复用现有 SSE 总线。

**Tech Stack:** Python (FastAPI SSE + asyncio 跨线程注入) + Electron (main.js SSE → IPC → chat.html) + 原生 SVG/CSS 动画

**不做的事（声明）：** 只做"状态"不做"进度"——压缩中圆环旋转，不显示 token 计数或百分比进度。这是用户明确要求。

---

## File Structure

| 文件 | 改动 | 责任 |
|------|------|------|
| `niu_api/chat.py` | 新增 `notify_compact_status_sync(status, mode)` | 同步广播 compact_status 事件，跨线程安全 |
| `niu_api/compat.py` | 修改 `_tidy_context_impl` 入口/出口 | 调用 `notify_compact_status_sync`（保持返回 dict 契约不变） |
| `niu_api/chat_queue.py` | 修改 `_retry_force_compression` | 后台 task 入口/出口也调 `notify_compact_status_sync` |
| `agent/runner.py` | 修改 `_on_context_high_usage` | executor 线程内用 `call_soon_threadsafe` 广播事件 |
| `niu_api/chat.py` | 删除 `force_compression_done` yield（449 行） | 前端无订阅，已被 compact_status 取代 |
| `ui/assistant/main.js` | SSE 转发加 `compact_status` 分支 | 转发到 chatWindow.webContents.send('compact-status') |
| `ui/assistant/preload-chat.js` | 暴露 `onCompactStatus` | IPC 桥接 |
| `ui/assistant/chat.html` | SVG 加 `<animateTransform>` + JS 切换 | 圆环旋转动画 + tooltip + 文本红色 |
| `tests/test_compact_status_events.py` | 新建 | 验证三触发点都广播了 compact_status |

---

## Task 1: 后端新增 `notify_compact_status_sync` 广播函数

**Files:**
- Modify: `niu_api/chat.py`（在 `_sync_broadcast` 附近，约 125 行）

**背景：** 现有 `notify_new_message_sync` / `notify_tool_status_sync` 都用 `loop.call_soon_threadsafe(_sync_broadcast, event)` 模式。我们新增一个同类函数 `notify_compact_status_sync`，三触发点统一调用它。

- [ ] **Step 1: Read 现有 `notify_*_sync` 函数作为模板**

Read: `niu_api/chat.py:60-130`

预期看到：
- `_main_loop` 全局变量（约 26 行）
- `_sync_broadcast(event: dict)` 定义（约 125 行）
- `notify_new_message_sync(...)` 和 `notify_tool_status_sync(...)` 用 `loop.call_soon_threadsafe(_sync_broadcast, event)` 注入

- [ ] **Step 2: 在 `notify_tool_status_sync` 之后新增 `notify_compact_status_sync`**

在 `niu_api/chat.py` 的 `notify_tool_status_sync` 函数之后（约 130 行）加：

```python
def notify_compact_status_sync(status: str, mode: str = "") -> None:
    """广播压缩状态事件到 /api/events/stream。

    跨线程安全：可在 executor 工作线程或后台 asyncio task 中调用。
    status: "started" | "done"
    mode: "force" | "sleep" | "auto"（可选，用于日志和前端提示）
    """
    if _main_loop is None:
        return
    event = {"type": "compact_status", "status": status, "mode": mode}
    try:
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        # loop 已关闭，忽略
        pass
```

- [ ] **Step 3: 验证 import 无报错**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from niu_api.chat import notify_compact_status_sync; print('OK')"`

预期：输出 `OK`

- [ ] **Step 4: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/chat.py
git commit -m "feat(compact): 新增 notify_compact_status_sync 跨线程广播压缩状态事件"
```

---

## Task 2: 三触发点调用 `notify_compact_status_sync`

**Files:**
- Modify: `niu_api/compat.py`（`_tidy_context_impl` 入口/出口，约 1714 行）
- Modify: `niu_api/chat_queue.py`（`_retry_force_compression`，约 376 行）
- Modify: `agent/runner.py`（`_on_context_high_usage`，约 807 行）
- Test: `tests/test_compact_status_events.py`

**背景：** 三触发点分别在不同上下文（HTTP 请求 / 后台 task / executor 线程），但都调 `notify_compact_status_sync`——它内部用 `call_soon_threadsafe` 保证跨线程安全。`_tidy_context_impl` 保持返回 dict 契约不变。

- [ ] **Step 1: 写失败测试 — 验证三触发点都广播了 compact_status**

Create: `tests/test_compact_status_events.py`

```python
"""验证三触发点都广播了 compact_status 事件。

不验证压缩内容质量，只验证事件推送逻辑。
mock call_subagent 让它立即返回，避免真实 LLM 调用。

注意：call_subagent 真实返回 str（不是 dict），mock 要匹配真实签名。
compat.py 内 call_subagent 之前还调 get_message_store/get_or_create_runner 等依赖，
测试需 mock 这些才能走到 call_subagent 那一行。
"""
import json
from unittest.mock import patch, MagicMock


def _make_mock_loop(events):
    """构造一个假 main_loop，call_soon_threadsafe 只记录事件不调真实 _sync_broadcast。

    注意：不能调 fn(*args)，否则会触发真实 _sync_broadcast，
    而 _event_subscribers 在测试中未初始化，事件被丢弃且 events 列表为空。
    """
    loop = MagicMock()
    def call_soon(fn, *args):
        # fn 是 _sync_broadcast，args[0] 是 event dict
        # 只记录 event，不调 fn（避免依赖 _event_subscribers）
        if args:
            events.append(args[0])
    loop.call_soon_threadsafe = call_soon
    return loop


def _patch_compat_deps():
    """patch _tidy_context_impl 调 call_subagent 之前的依赖。

    _tidy_context_impl 内部顺序约：
    1. get_message_store() → 拿 messages
    2. _read_context_window_tokens() → 拿 token 配置
    3. get_or_create_runner() → 拿 runner
    4. call_subagent(...) → 压缩

    测试要让流程走到 4，必须 mock 1-3 返回合理值。
    """
    return [
        patch("niu_api.compat.get_message_store", return_value=MagicMock(messages=[{"role":"user","content":"test"}])),
        patch("niu_api.compat._read_context_window_tokens", return_value=200000),
        patch("niu_api.compat.get_or_create_runner", return_value=MagicMock()),
    ]


def test_compat_tidy_impl_emits_compact_status_force():
    """模式2 force：_tidy_context_impl 应广播 started + done。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches: p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", return_value="压缩摘要"):
            from niu_api.compat import _tidy_context_impl
            # 真实签名：(request: dict, chat_lock_already_held: bool)
            # mode 从 request dict 取
            result = _tidy_context_impl({"mode": "force"})
    finally:
        for p in patches: p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "started" in statuses, f"force 模式未广播 started，实际: {statuses}"
    assert "done" in statuses, f"force 模式未广播 done，实际: {statuses}"
    assert statuses.index("started") < statuses.index("done")


def test_compat_tidy_impl_emits_compact_status_sleep():
    """模式3 sleep：_tidy_context_impl 应广播 started + done。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches: p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", return_value="压缩摘要"):
            from niu_api.compat import _tidy_context_impl
            result = _tidy_context_impl({"mode": "sleep"})
    finally:
        for p in patches: p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "started" in statuses, f"sleep 模式未广播 started，实际: {statuses}"
    assert "done" in statuses, f"sleep 模式未广播 done，实际: {statuses}"


def test_compat_tidy_impl_emits_done_on_exception():
    """压缩失败时也必须广播 done，避免前端圆环卡死。"""
    events = []
    loop = _make_mock_loop(events)
    patches = _patch_compat_deps()
    for p in patches: p.start()
    try:
        with patch("niu_api.chat._main_loop", loop), \
             patch("agent.subagent.call_subagent", side_effect=RuntimeError("LLM 失败")):
            from niu_api.compat import _tidy_context_impl
            try:
                result = _tidy_context_impl({"mode": "force"})
            except RuntimeError:
                pass
    finally:
        for p in patches: p.stop()
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "done" in statuses, f"异常路径未广播 done，前端会卡死，实际: {statuses}"


def test_runner_on_context_high_usage_emits_compact_status():
    """模式1：runner._on_context_high_usage 应广播 started + done。

    真实签名是 _on_context_high_usage(self, messages, tokens_used, tokens_limit)。
    最小化构造 runner 会因 self.handler/llm_config 等属性缺失在 try 内抛 AttributeError，
    但 started 在 try 之前广播，done 在 finally 中广播，所以事件推送仍可验证。
    """
    events = []
    loop = _make_mock_loop(events)
    with patch("niu_api.chat._main_loop", loop), \
         patch("agent.subagent.call_subagent", return_value="压缩摘要"):
        from agent.runner import GenericAgentRunner
        runner = GenericAgentRunner.__new__(GenericAgentRunner)
        # 最小化构造 runner 状态
        runner._tidy_in_progress = False
        runner._tidy_lock = MagicMock()
        runner._tidy_lock.acquire.return_value = True
        runner._tidy_lock.release.return_value = None
        # 真实签名：messages, tokens_used, tokens_limit
        try:
            runner._on_context_high_usage(
                messages=[], tokens_used=100, tokens_limit=200000
            )
        except (AttributeError, TypeError, Exception):
            pass  # 最小化构造会缺依赖，但事件应已广播
    statuses = [e["status"] for e in events if e.get("type") == "compact_status"]
    assert "started" in statuses, f"模式1 未广播 started，实际: {statuses}"
    assert "done" in statuses, f"模式1 未广播 done，实际: {statuses}"
```

**注意：**
- 测试用 `MagicMock` 的 `call_soon_threadsafe` 只记录事件不调真实 `_sync_broadcast`（避免依赖 `_event_subscribers`）。`_make_mock_loop` 是核心 helper。
- `call_subagent` 真实返回 `str`，mock 返回 `"压缩摘要"`（不是 dict）匹配真实签名。
- `_patch_compat_deps` mock `_tidy_context_impl` 调 call_subagent 之前的依赖（get_message_store/_read_context_window_tokens/get_or_create_runner），确保测试流程能走到 call_subagent 那一行。
- runner 测试因最小化构造可能 AttributeError，但 started 在 try 之前广播、done 在 finally 中广播，事件推送仍可验证。如果 runner 测试因依赖过多跑不通，可只保留前三个 compat 测试，runner 的事件推送靠手动测试验证（Task 5 Step 3）。

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_compact_status_events.py -v`

预期：FAIL（事件列表为空，因为还没加推送代码）

- [ ] **Step 3: 在 `_tidy_context_impl` 入口加 started，现有 except 块后追加 finally 推 done**

Read: `niu_api/compat.py:1714`（函数签名）、`niu_api/compat.py:1727`（现有 try:）、`niu_api/compat.py:3094`（现有 except Exception）

**真实函数签名**：`async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False)`。`mode` 从 `request.get("mode", "sleep")` 取，不是独立参数。

**实现策略**（避免缩进 1383 行函数体）：

Python 允许 `try/except/finally` 三段式，`finally` 在 `except` 后执行，覆盖正常 return 路径。`_tidy_context_impl` 现有 `try:` 在 1727 行、`except Exception as e:` 在 3094 行。我们：

1. **在函数体最开头（try 之前）** 加 `notify_compact_status_sync("started", mode=mode)` + 提取 mode
2. **现有 except 块内** 开头加 `notify_compact_status_sync("done", mode=mode)`（异常路径推 done）
3. **在现有 except 块之后追加 finally 块** 推 `done`（正常 return 路径推 done）

```python
async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False):
    # === 新增：提取 mode + 广播 started ===
    mode = request.get("mode", "sleep") if isinstance(request, dict) else "sleep"
    from niu_api.chat import notify_compact_status_sync
    notify_compact_status_sync("started", mode=mode)
    # === 原有 try 块（1727 行）===
    try:
        # ... 原有所有逻辑（26 个 return 都在 try 内，return 时 finally 会执行）...
        ...
    except Exception as e:
        # === 新增：异常路径先推 done ===
        notify_compact_status_sync("done", mode=mode)
        # ... 原有 except 处理逻辑 ...
        logger.exception(...)
        return {"status": "error", "error": str(e)}
    # === 新增：finally 块（正常 return 路径推 done）===
    finally:
        notify_compact_status_sync("done", mode=mode)
```

**关键实现细节：**
- `mode` 提取放在函数最开头，try 之前，确保所有路径都能拿到 mode
- `notify_compact_status_sync("started", ...)` 在 try 之前调，即使 try 内立即抛异常，started 也已广播
- except 块内先推 done 再处理异常，确保异常路径 done 一定推送
- finally 块在所有 return（正常路径）和 except 块（异常路径）之后执行，**注意：except 块已推过 done，finally 会再推一次**。这是冗余但无害——前端收到两次 done 不会出错（第二次 done 时动画已停止）。如果觉得冗余不优雅，可去掉 except 内的 done，只靠 finally；但 finally 在 except 内 return 后执行，所以只靠 finally 也能覆盖异常路径。**推荐：去掉 except 内的 done，只靠 finally**。

**最终简化版**：
```python
async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False):
    mode = request.get("mode", "sleep") if isinstance(request, dict) else "sleep"
    from niu_api.chat import notify_compact_status_sync
    notify_compact_status_sync("started", mode=mode)
    try:
        # ... 原有 1727-3093 行逻辑不变 ...
    except Exception as e:
        # ... 原有 except 逻辑不变 ...
    finally:
        notify_compact_status_sync("done", mode=mode)
```

**实现者操作**：只需在 1727 行 `try:` 之前加 3 行（mode + import + started），在 3094 行 `except Exception as e:` 对应的整个 try/except 块末尾加一个 `finally:` 块（2 行）。**无需缩进整个函数体**。

**注意：** 如果 `_tidy_context_impl` 不是 async（同步函数），try/except/finally 语义同样适用。Read 代码确认是 `async def`，所以是 async 函数。async 函数的 try/finally 在 `return` 和 `raise` 时 finally 都执行。

- [ ] **Step 4: 在 `chat_queue._retry_force_compression` 的循环内加事件推送**

Read: `niu_api/chat_queue.py:370-424`

**现有结构**（不能照搬 try/finally 包整个函数体）：
```python
async def _retry_force_compression(...):
    for attempt in range(max_retries):
        await asyncio.sleep(delay)
        try:
            await asyncio.wait_for(_tidy_lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            continue
        try:
            # ... 调 _tidy_context_impl ...
        except Exception as e:
            ...
        finally:
            _tidy_lock.release()
```

**现有 `finally` 块只 release 锁**。我们要在**每次重试的 try 之前加 started，在现有 finally 块内 release 之前加 done**，这样每次重试 started+done 配对，且 done 在 finally 内确保异常路径也推。

**改造方式**（不破坏现有结构）：

1. **在业务 `try:` 块内第一行（acquire 锁成功之后）** 加 `notify_compact_status_sync("started", mode="force")`
2. **在现有 `finally:` 块内、`_tidy_lock.release()` 之前** 加 `notify_compact_status_sync("done", mode="force")`

**注意：** started 必须在 `try:` 块**内部**第一行（不是 try 之前），这样 started 之后的异常会被 except 捕获，finally 推 done，保证 started+done 配对。如果 started 放在 try 之前，started 抛异常时不会被 except 捕获，finally 不执行，done 不推——形成孤儿 started。

```python
async def _retry_force_compression(...):
    from niu_api.chat import notify_compact_status_sync
    for attempt in range(max_retries):
        await asyncio.sleep(delay)
        try:
            await asyncio.wait_for(_tidy_lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            continue
        try:
            notify_compact_status_sync("started", mode="force")  # 新增
            request = {...}
            result = await _tidy_context_impl(request=request)
            # ... 原有逻辑 ...
        except Exception as e:
            # ... 原有 except 逻辑 ...
        finally:
            notify_compact_status_sync("done", mode="force")  # 新增，在 release 之前
            _tidy_lock.release()
```

**关键：**
- started 在 try 之前、acquire 锁之后——锁没拿到（TimeoutError continue）不发 started，避免前端收到 started 却没有 done
- done 在 finally 内、release 之前——所有 return 和异常路径都推 done，且 done 在锁释放前推送（避免锁已释放但前端还以为在压缩）
- **每次重试都 started+done 配对**，重试 N 次前端看到 N 次动画，这是预期行为（重试意味着上一次失败，前端应该看到失败→重新开始）

**注意：** 此函数是后台 `asyncio.create_task`，没有 HTTP 请求上下文。但 `notify_compact_status_sync` 用 `call_soon_threadsafe` 注入主 loop，主 loop 的 `/api/events/stream` SSE 端点会广播到所有连接的前端，所以 chat.html 能收到。

- [ ] **Step 5: 在 `runner._on_context_high_usage` 入口加 started，现有 except 块后追加 finally 推 done**

Read: `agent/runner.py:807`（函数签名）、`agent/runner.py:838`（现有 try:）、`agent/runner.py:1386`（现有 except Exception）

**真实函数签名**：`def _on_context_high_usage(self, messages, tokens_used, tokens_limit)`（同步函数，executor 工作线程跑）。

**实现策略**（与 Step 3 同款，避免缩进 550 行函数体）：

runner.py 现有 `try:` 在 838 行、`except Exception as e:` 在 1386 行。我们：

1. **在函数体最开头（try 之前，807-837 行之间）** 加 `notify_compact_status_sync("started", mode="auto")`
2. **在现有 except 块之后追加 finally 块** 推 `done`

```python
def _on_context_high_usage(self, messages, tokens_used, tokens_limit):
    # === 新增：广播 started（try 之前，确保即使 try 内立即抛异常 started 也已广播）===
    from niu_api.chat import notify_compact_status_sync
    notify_compact_status_sync("started", mode="auto")
    # === 原有 try 块（838 行）===
    try:
        # ... 原有所有逻辑（12 个 return 都在 try 内，return 时 finally 会执行）...
        ...
    except Exception as e:
        # ... 原有 except 处理逻辑（1386 行）不变 ...
        ...
    # === 新增：finally 块（所有路径推 done）===
    finally:
        notify_compact_status_sync("done", mode="auto")
```

**关键实现细节：**
- `notify_compact_status_sync("started", ...)` 在 try 之前调，跨线程安全（内部用 `call_soon_threadsafe` 注入主 loop）
- finally 覆盖所有 12 个 return 出口和 except 异常路径
- `from niu_api.chat import notify_compact_status_sync` 函数内延迟 import，避免循环依赖（runner.py:818 已有 `from niu_api.compat import ...` 先例）
- 主 loop 已在 uvicorn startup 时初始化（`_main_loop` 全局），runner 触发时一定可用

**实现者操作**：只需在 838 行 `try:` 之前加 2 行（import + started），在 1386 行 `except Exception as e:` 对应的整个 try/except 块末尾加 `finally:` 块（2 行）。**无需缩进整个函数体**。

- [ ] **Step 6: 运行测试，确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_compact_status_events.py -v`

预期：4 个测试全 PASS（如果 runner 测试因依赖跑不通，至少前 3 个 compat 测试 PASS）

- [ ] **Step 7: 删除 `force_compression_done` 旧事件**

Read: `niu_api/chat.py:445-455`

删除 `yield f"data: {json.dumps({'force_compression_done': True, 'status': ...})}\n\n"` 这行（已被 compact_status 取代）。

确认前端无订阅：
```bash
grep -rn "force_compression_done" REDACTED_USER_PATH/tools/ai-bot/ui/
```
预期：无输出（审查已确认）。

- [ ] **Step 8: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py niu_api/chat_queue.py agent/runner.py niu_api/chat.py tests/test_compact_status_events.py
git commit -m "feat(compact): 三触发点统一广播 compact_status 事件，try/finally 保证 done 必发"
```

---

## Task 3: main.js 转发 compact_status 事件到 chat.html

**Files:**
- Modify: `ui/assistant/main.js`（SSE 转发逻辑，约 1210 行）
- Modify: `ui/assistant/preload-chat.js`（IPC 暴露，约 59 行）

**背景：** main.js 是唯一 SSE 客户端，订阅 `/api/events/stream`，按 `event.type` 分发到不同 IPC 频道。现有转发 `new_message/tool_status/ingest-started/ingest-completed` 四类，需新增 `compact_status`。

- [ ] **Step 1: Read main.js 的 SSE 转发逻辑**

Read: `ui/assistant/main.js:1180-1240`

预期看到类似：
```javascript
const req = http.request({...}, (res) => {
  res.on('data', (chunk) => {
    // 解析 SSE data: {...}
    const event = JSON.parse(...);
    if (event.type === 'new_message') {
      chatWindow.webContents.send('new-message', event);
    } else if (event.type === 'tool_status') {
      chatWindow.webContents.send('tool-status', event);
    } else if (event.type === 'ingest-started') {
      chatWindow.webContents.send('ingest-started', event);
    } else if (event.type === 'ingest-completed') {
      chatWindow.webContents.send('ingest-completed', event);
    }
  });
});
```

- [ ] **Step 2: 加 compact_status 转发分支**

在 main.js 的 SSE 事件分发链中加一个 `else if`：

```javascript
} else if (event.type === 'compact_status') {
  chatWindow.webContents.send('compact-status', event);
}
```

**注意：** 也要转发到 spiritWindow（如果存在），因为 spirit.html 触发压缩时 chat.html 也要响应。但根据需求"压缩时圆环变动图"，圆环只在 chat.html，所以只转发到 chatWindow 即可。如果以后 spirit 也要响应，再加。

- [ ] **Step 3: Read preload-chat.js 的 IPC 暴露**

Read: `ui/assistant/preload-chat.js:55-75`

预期看到：
```javascript
contextBridge.exposeInMainWorld('electronAPI', {
  onNewMessage: (cb) => ipcRenderer.on('new-message', (_e, data) => cb(data)),
  onToolStatus: (cb) => ipcRenderer.on('tool-status', (_e, data) => cb(data)),
  onIngestStarted: (cb) => ipcRenderer.on('ingest-started', (_e, data) => cb(data)),
  onIngestCompleted: (cb) => ipcRenderer.on('ingest-completed', (_e, data) => cb(data)),
  // ...
});
```

- [ ] **Step 4: 暴露 onCompactStatus**

在 `preload-chat.js` 的 `contextBridge.exposeInMainWorld` 中加：

```javascript
onCompactStatus: (cb) => ipcRenderer.on('compact-status', (_e, data) => cb(data)),
```

- [ ] **Step 5: 验证 import 无报错**

启动程序确认 main.js / preload-chat.js 无语法错误：

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu &`
等待 3 秒后检查日志：
Run: `sleep 3 && tail -50 REDACTED_USER_PATH/tools/ai-bot/logs/*.log | grep -i "error\|compact"`

预期：无 compact 相关报错。

杀进程：
```bash
pkill -f niu_api; pkill -f "niu"
```

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/main.js ui/assistant/preload-chat.js
git commit -m "feat(ui): main.js 转发 compact_status 事件到 chat.html IPC"
```

---

## Task 4: chat.html 圆环旋转动画 + tooltip + 文本红色

**Files:**
- Modify: `ui/assistant/chat.html`（SVG 在 545-552 行，loadStats 在 1092-1119 行，IPC 监听在 1301 行附近）

**背景：** 圆环 SVG 已有 `transform="rotate(-90 10 10)"`（进度圈起笔从顶部）。压缩时用 SVG `<animateTransform>` 叠加旋转动画（`additive="sum"` 不覆盖原 -90deg）。tooltip 是静态 `title` 属性，需 JS 动态修改。文本颜色用 `style.color` 切换。

- [ ] **Step 1: Read chat.html 圆环 SVG 完整结构**

Read: `ui/assistant/chat.html:540-560`

预期看到（审查确认）：
```html
<span class="stat context-usage" id="context-usage" title="上下文使用率">
  <svg width="20" height="20" viewBox="0 0 20 20">
    <circle cx="10" cy="10" r="8" fill="none" stroke="#333" stroke-width="2.5"/>
    <circle id="context-usage-arc" cx="10" cy="10" r="8" fill="none"
            stroke="#4CAF50" stroke-width="2.5"
            stroke-dasharray="50.27" stroke-dashoffset="50.27"
            stroke-linecap="round"
            transform="rotate(-90 10 10)"/>
  </svg>
  <span id="context-usage-text">0%</span>
</span>
```

- [ ] **Step 2: 给进度圈加 `<animateTransform>` 旋转动画（默认暂停）**

在 `#context-usage-arc` circle 内部加 `<animateTransform>` 元素：

```html
<circle id="context-usage-arc" cx="10" cy="10" r="8" fill="none"
        stroke="#4CAF50" stroke-width="2.5"
        stroke-dasharray="50.27" stroke-dashoffset="50.27"
        stroke-linecap="round"
        transform="rotate(-90 10 10)">
  <animateTransform attributeName="transform" type="rotate"
                    from="-90 10 10" to="270 10 10"
                    dur="2s" repeatCount="indefinite"
                    begin="indefinite"/>
</circle>
```

**关键属性：**
- `from="-90 10 10" to="270 10 10"`：起点 -90 度（与静态 transform 一致），转 360 度到 270 度（等价于 -90 + 360）。**不用 `additive="sum"`**——SMIL 规范中 `additive="sum"` 只和**其他同属性动画**叠加，不与静态 transform 属性叠加，Chromium 会用动画的 from/to 覆盖静态 transform，起笔角会从 0 度（右侧）开始导致视觉跳变。直接把 from/to 设成 -90→270 起笔角与静态一致，无跳变。
- `begin="indefinite"`：默认不自动开始，由 JS 用 `beginElement()` / `endElement()` 控制
- `dur="2s"`：2 秒一圈，节奏适中
- 旋转中心 `(10, 10)` 与 SVG 中心一致

- [ ] **Step 3: 在 chat.html JS 中加 IPC 监听切换动画**

Read: `ui/assistant/chat.html:1295-1320` 找到现有 `window.electronAPI.onToolStatus` 之类的监听位置。

在其旁边加：

```javascript
// === 压缩状态：圆环旋转 + tooltip + 文本红色 ===
if (window.electronAPI && window.electronAPI.onCompactStatus) {
  window.electronAPI.onCompactStatus((data) => {
    const arc = document.getElementById('context-usage-arc');
    const animate = arc ? arc.querySelector('animateTransform') : null;
    const text = document.getElementById('context-usage-text');
    const wrapper = document.getElementById('context-usage');
    if (data.status === 'started') {
      if (animate) animate.beginElement();
      if (text) text.style.color = '#F44336';
      if (wrapper) wrapper.title = '正在压缩对话…';
    } else if (data.status === 'done') {
      if (animate) animate.endElement();
      // 文本颜色由 loadStats() 重新按 pct 着色，这里不强制恢复
      if (wrapper) wrapper.title = '上下文使用率';
    }
  });
}
```

**关键点：**
- `animate.beginElement()` 启动旋转（SVG SMIL 动画 API）
- `animate.endElement()` 停止旋转
- `text.style.color` 只在 started 时设红，done 时不恢复——因为 `loadStats()` 下一轮会按 pct 重新着色，强制恢复可能闪烁
- `wrapper.title` 在 done 时恢复"上下文使用率"

- [ ] **Step 4: 用全局变量 `window._compacting` 防止 `loadStats()` 覆盖压缩状态**

Read: `ui/assistant/chat.html:1092-1119`

`loadStats()` 每次会更新 `stroke-dashoffset` 和 `text.style.color`。压缩中如果 `loadStats()` 还在跑，会把 `stroke-dashoffset` 改回真实 pct（旋转动画不依赖 dashoffset，不受影响），但文本颜色会被覆盖回绿/橙，导致视觉闪烁。

**解决：** 用全局变量 `window._compacting` 作为压缩中标志（不用 `wrapper.title`，那是时序竞态——`loadStats` 可能在 compact_status 事件之前执行）。

**Step 4a: 在 chat.html JS 顶部初始化全局变量**

在 chat.html 的 `<script>` 区开头（或 `loadStats` 函数定义之前）加：

```javascript
window._compacting = false;  // 压缩中标志，由 compact_status 事件切换
```

**Step 4b: 修改 `loadStats()` 的颜色切换逻辑（同时守卫 arc stroke 和 text color）**

Read `ui/assistant/chat.html:1092-1119` 找到改颜色的代码。预期看到类似：
```javascript
const color = pct < 70 ? '#4CAF50' : (pct < 80 ? '#FF9800' : '#F44336');
arc.setAttribute('stroke', color);  // 改圆环 stroke 颜色
text.style.color = color;            // 改文本颜色
```

改成（用 `window._compacting` 守卫**两处**）：
```javascript
const color = pct < 70 ? '#4CAF50' : (pct < 80 ? '#FF9800' : '#F44336');
// 压缩中不改 arc stroke 和 text color（由 compact_status 事件控制为红色）
if (!window._compacting) {
  arc.setAttribute('stroke', color);
  text.style.color = color;
}
```

**关键：** 必须同时守卫 `arc.setAttribute('stroke', color)` 和 `text.style.color`。如果只守卫 text color 不守卫 arc stroke，压缩中 loadStats 跑会把圆环 stroke 从红（#F44336）改回绿/橙，视觉闪烁。

**Step 4c: compact_status 监听中切换全局变量**

修改 Step 3 的 `onCompactStatus` 监听代码，切换 `window._compacting`：

```javascript
if (window.electronAPI && window.electronAPI.onCompactStatus) {
  window.electronAPI.onCompactStatus((data) => {
    const arc = document.getElementById('context-usage-arc');
    const animate = arc ? arc.querySelector('animateTransform') : null;
    const text = document.getElementById('context-usage-text');
    const wrapper = document.getElementById('context-usage');
    if (data.status === 'started') {
      window._compacting = true;  // 先设标志，防止 loadStats 覆盖
      if (animate) animate.beginElement();
      if (text) text.style.color = '#F44336';
      if (wrapper) wrapper.title = '正在压缩对话…';
    } else if (data.status === 'done') {
      if (animate) animate.endElement();
      if (wrapper) wrapper.title = '上下文使用率';
      window._compacting = false;  // 最后清标志，让 loadStats 恢复颜色
    }
  });
}
```

**关键时序：** started 时先设 `window._compacting = true` 再改颜色；done 时先恢复 title 再清 `window._compacting = false`。这样 `loadStats` 在任何时候执行都能看到正确的标志值。

- [ ] **Step 5: 手动测试 — 模式2（手动 force）**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu &`

在 chat 窗口发几条消息让上下文变长，然后调 force 压缩（点按钮或 POST `/api/context/tidy` body `{mode:'force'}`）。观察：
1. 圆环是否开始旋转
2. 鼠标 hover 是否显示"正在压缩对话…"
3. 百分比和数字是否变红
4. 压缩结束后是否停止旋转、tooltip 恢复

预期：四点都符合。

杀进程：
```bash
pkill -f niu_api; pkill -f "niu"
```

- [ ] **Step 6: 手动测试 — 模式3（睡眠 sleep）**

启动程序，调 `/api/context/tidy` body `{mode:'sleep'}`，观察圆环切换。

预期：与 Step 5 一致。

杀进程。

- [ ] **Step 7: 手动测试 — 模式1（自动触发）**

启动程序，发大量消息让上下文达到 80% 阈值，观察自动触发压缩时圆环是否切换。

预期：圆环旋转，颜色变红，tooltip 改"正在压缩对话…"。

**注意：** 模式1 触发在 executor 线程，`notify_compact_status_sync` 用 `call_soon_threadsafe` 注入主 loop。如果圆环没切换，检查：
- `runner._on_context_high_usage` 是否真的调了 `notify_compact_status_sync`（grep 确认）
- `_main_loop` 是否在 runner 触发时已初始化（uvicorn startup 后应已初始化）
- main.js SSE 连接是否存活（打开 DevTools 看 Network）

杀进程。

- [ ] **Step 8: 手动测试 — 压缩失败恢复**

模拟压缩失败：临时把 `call_subagent` mock 成抛异常，或断网触发 LLM 失败。观察：
1. 圆环是否停止旋转（`finally` 推 `done`）
2. tooltip 是否恢复"上下文使用率"

预期：即使失败，圆环也恢复正常，不卡死。

杀进程。

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/chat.html
git commit -m "feat(ui): 压缩时圆环 SVG 旋转动画 + tooltip 显示正在压缩 + 文本变红"
```

---

## Task 5: 回归验证

**Files:**
- Verify: `ui/assistant/chat.html`、`niu_api/chat.py`、`agent/runner.py`

- [ ] **Step 1: 启动程序，发普通消息，确认圆环和 SSE 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu &`

在 chat 窗口发几条普通消息，观察：
1. 圆环按 context_usage 正常更新（绿/橙/红分段）
2. 鼠标 hover 圆环显示"上下文使用率"
3. chat_busy/chat_idle 正常（聊天时按钮状态切换）
4. 不触发压缩时圆环不旋转

预期：全部正常。

- [ ] **Step 2: 测试 ingest（文档导入）事件不受影响**

拖入一个文档触发 ingest，观察：
1. `ingest-started` / `ingest-completed` 事件正常
2. 压缩圆环不会被误触发（ingest 不是压缩）

预期：ingest 正常，圆环不旋转。

- [ ] **Step 3: 检查日志无报错**

Run: `tail -100 REDACTED_USER_PATH/tools/ai-bot/logs/api_stderr.log | grep -i "error\|exception\|compact"`

预期：无 Task 1-4 引入的新报错。compact 相关日志只有正常的事件推送日志。

- [ ] **Step 4: 杀进程，最终确认**

```bash
pkill -f niu_api; pkill -f "niu"
cd REDACTED_USER_PATH/tools/ai-bot
git status
git log --oneline -5
```

预期：5 个 commit（Task 1-4 各一个 + 可能的修复），工作区干净。

---

## Self-Review 检查（v2）

**Spec 覆盖：**
- ✅ "压缩时圆环旋转" → Task 4 Step 2-3（`<animateTransform>` + `beginElement/endElement`）
- ✅ "鼠标移上去显示正在压缩" → Task 4 Step 3（`wrapper.title = '正在压缩对话…'`）
- ✅ "百分比和数字变红" → Task 4 Step 3（`text.style.color = '#F44336'`）
- ✅ "压缩结束恢复" → Task 4 Step 3（compact_status done 分支）
- ✅ "三模式都覆盖" → Task 2 三触发点（compat / chat_queue / runner）
- ✅ "非必须但推荐" → 旋转动画方案采纳

**审查 bug 修复：**
- ✅ Bug 1（`_tidy_context_impl` 形态）→ Task 2 保持返回 dict 契约，事件推送用独立函数 `notify_compact_status_sync`
- ✅ Bug 2（模式1 runner 路径）→ Task 2 Step 5 在 `runner._on_context_high_usage` 加事件推送
- ✅ Bug 3（chat.html 不订阅 SSE）→ Task 3 三层 IPC 转发（main.js → preload → chat.html）
- ✅ Bug 4（测试 mock 虚构）→ Task 2 Step 1 mock 真实的 `agent.subagent.call_subagent`，传 dict 参数
- ✅ 改进1（SSE 格式矛盾）→ 统一用 `{"type":"compact_status","status":"started/done"}` 走 main.js 现有转发管道
- ✅ 改进2（GIF 视觉不一致）→ 改用 SVG 旋转动画，不引入 GIF
- ✅ 改进3（force_compression_done）→ Task 2 Step 7 明确删除
- ✅ 遗漏3（压缩失败恢复）→ Task 2 三触发点都用 `try/finally` 保证 `done` 必发

**Placeholder 扫描：** 无 TBD/TODO，所有代码片段完整。

**Type 一致性：**
- `compact_status` 事件 type 在 Task 1（广播）、Task 2（三触发点调用）、Task 3（main.js 转发）、Task 4（chat.html 监听）一致
- `notify_compact_status_sync(status, mode)` 签名在 Task 1 定义、Task 2 三处调用一致
- `onCompactStatus` IPC 频道名 `compact-status` 在 Task 3（main.js send + preload 暴露）、Task 4（chat.html 监听）一致

**潜在风险：**
1. Task 4 Step 2 的 `<animateTransform from="-90 10 10" to="270 10 10">` 已避免 `additive="sum"` 的覆盖问题。Chromium 对 SMIL 支持良好，无 deprecation。备选方案：CSS `@keyframes rotate` + `transform-origin: center`（需把 SVG transform 改 CSS）。
2. Task 2 Step 3/5 的 try/except/finally 三段式策略：在现有 `try:` 之前加 started，在现有 `except` 块后追加 `finally` 块推 done。**无需缩进整个函数体**，实现者只加 2 处代码（try 前 2-3 行 + except 后 2 行）。Python 的 `try/except/finally` 三段式中，finally 在所有 return 和 except 之后执行，覆盖所有路径。
3. Task 4 Step 4 用 `window._compacting` 全局变量防止 `loadStats` 时序竞态。**同时守卫 `arc.setAttribute('stroke', color)` 和 `text.style.color`**，避免压缩中圆环 stroke 闪烁。started 时先设 true 再改颜色，done 时先恢复 title 最后清 false。
4. Task 2 Step 1 的测试 `_make_mock_loop` 只记录事件不调真实 `_sync_broadcast`，避免依赖 `_event_subscribers`。`_patch_compat_deps` mock call_subagent 之前的依赖（get_message_store/_read_context_window_tokens/get_or_create_runner），确保流程走到 call_subagent。mock call_subagent 返回 str（不是 dict）匹配真实签名。runner 测试用真实签名 `(messages, tokens_used, tokens_limit)`，最小化构造可能 AttributeError 但事件推送仍可验证。
5. **SSE 重连状态同步**（已知限制）：如果 compact_status done 在 SSE 断连期间丢失，圆环会卡在旋转状态。这是现有架构限制（其他事件如 chat_busy 也有同样问题）。Task 5 回归测试时如发现此问题，可作为后续优化项，不阻塞本次交付。
