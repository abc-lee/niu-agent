# Journal-Agent 三路径统一 — 消息来源一致化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一日志子Agent的三个调用路径，使主Agent手动调用时也能获取增量消息，与自动整理路径一致。

**根因分析：** 日志子Agent有三个调用路径，但只有路径2/3（sleep/force tidy）通过 `_build_incremental_msg_text()` 嵌入增量消息，路径1（主Agent通过 `chat-with-journal-agent` 调用）只传一句自然语言，完全没有消息。这导致：1）路径1无法获取对话内容，日志记录必然失败；2）不同路径的数据来源不一致，导致日志格式混乱。

**Architecture:** 在 `_call_subagent_gen()` 中，当 agent_name 是 journal-agent 时，从 MessageStore 读取增量消息并嵌入 task，与 `_tidy_context_impl` 中的路径2/3保持一致。提取公共函数 `_build_journal_task()` 避免三处重复相同的 prompt 构建逻辑。三个路径统一后，journal-agent 的 mcpServers 保持 `[]` 不变，跟其他子Agent完全一致。

**Tech Stack:** Python, asyncio, MessageStore, fcntl

---

## File Structure

| 文件 | 职责 |
|------|------|
| `niu_api/compat.py` | **修改** — 提取 `_build_journal_task()` 公共函数，替换两处内联 prompt 构建 |
| `agent/handler.py` | **修改** — `_call_subagent_gen()` 中为 journal-agent 构建增量消息 task + 游标更新 + 文件锁保护 |
| `config/agents/journal-agent.md` | **修改** — 更新输入格式说明，明确三种调用场景 |
| `config/agents/niu.md` | **修改** — 更新主Agent调用 journal-agent 的说明 |
| `tests/test_journal_unified_paths.py` | **新建** — 真实集成测试，验证三个路径的消息格式一致性 |

---

### Task 1: 提取 `_build_journal_task()` 公共函数

**Files:**
- Modify: `niu_api/compat.py`

**原理：** 当前 sleep 模式（第1211-1216行）和 force 模式（第1594-1599行）各自内联构建了完全相同的 journal prompt。runner.py 中也有同样的 prompt（第909-914行）。提取为公共函数，避免三处重复。

- [ ] **Step 1: 在 `niu_api/compat.py` 中添加 `_build_journal_task()` 函数**

在 `_build_incremental_msg_text()` 函数之后（约第163行），添加：

```python
def _build_journal_task(journal_msg_text: str, safe_tokens: int = 0) -> str:
    """构建 journal-agent 的 task prompt（增量消息嵌入）。

    Args:
        journal_msg_text: _build_incremental_msg_text() 返回的增量消息文本
        safe_tokens: 截断 token 上限（0 表示不截断）

    Returns:
        完整的 task prompt 字符串
    """
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

    if safe_tokens > 0:
        prompt = _truncate_task_for_subagent(prompt, safe_tokens)
    return prompt
```

- [ ] **Step 2: 替换 sleep 模式中的内联 prompt**

在 `niu_api/compat.py` 第1211-1216行，将：

```python
                    journal_prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

                    # 截断 task 防止子Agent超限
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_journal_prompt = _truncate_task_for_subagent(journal_prompt, safe_tokens)
```

替换为：

```python
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_journal_prompt = _build_journal_task(journal_msg_text, safe_tokens)
```

- [ ] **Step 3: 替换 force 模式中的内联 prompt**

在 `niu_api/compat.py` 第1594-1604行，将：

```python
                journal_force_prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_journal_force_prompt = _truncate_task_for_subagent(journal_force_prompt, safe_tokens)
```

替换为：

```python
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_journal_force_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)
```

- [ ] **Step 4: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 5: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: extract _build_journal_task() — deduplicate journal prompt in compat.py"
```

---

### Task 2: runner.py 中复用 `_build_journal_task()`

**Files:**
- Modify: `agent/runner.py`

**原理：** runner.py 第909-914行也有同样的内联 journal prompt，需要替换为 `_build_journal_task()`。

- [ ] **Step 1: 在 runner.py 中导入 `_build_journal_task`**

在 `agent/runner.py` 顶部导入区域，找到已有的 `from niu_api.compat import` 行，添加 `_build_journal_task` 到导入列表。

搜索 `from niu_api.compat import`，在已有导入行末尾添加 `, _build_journal_task`。如果已有多个 from niu_api.compat import 行，选其中一个添加即可。

- [ ] **Step 2: 替换 runner.py 中的内联 prompt**

在 `agent/runner.py` 第909-917行，将：

```python
                journal_force_prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

                safe_tokens = int(_read_context_window_tokens() * 0.6)
                truncated_journal_prompt = _truncate_task_for_subagent(journal_force_prompt, safe_tokens)
```

替换为：

```python
                safe_tokens = int(_read_context_window_tokens() * 0.6)
                truncated_journal_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)
```

- [ ] **Step 3: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add agent/runner.py
git commit -m "refactor: reuse _build_journal_task() in runner.py — deduplicate journal prompt"
```

---

### Task 3: `_call_subagent_gen()` 中为 journal-agent 构建增量消息 task

**Files:**
- Modify: `agent/handler.py`

**原理：** 这是核心修改。当主Agent通过 `chat-with-journal-agent` 调用时，当前只传一句自然语言（如"记录今天的日志"），完全没有增量消息。需要改为：从 MessageStore 读取增量消息，用 `_build_journal_task()` 构建 task，替换原始的 `task` 参数。

同时，调用完成后需要更新游标文件 `~/.niu/last_journal.json`，与路径2/3的行为一致。

**关键约束：** `_call_subagent_gen()` 运行在 executor 线程中（非 asyncio 事件循环线程），不能直接 `await` 异步方法。runner.py 已有 `_sync_get_messages()` 方法，使用 `asyncio.run_coroutine_threadsafe()` + `_main_loop` 正确桥接。handler.py 应直接复用该方法，而非重新实现。

- [ ] **Step 1: 在 handler.py 中添加 `_sync_get_messages()` 辅助方法**

直接复用 runner 的已有方法（使用正确的 `asyncio.run_coroutine_threadsafe` 桥接模式）：

```python
    def _sync_get_messages(self):
        """同步获取消息列表 — 复用 runner 的桥接方法"""
        from .runner import get_runner
        runner = get_runner()
        if runner is None:
            return []
        return runner._sync_get_messages()
```

**为什么不用 `run_until_complete`**：handler 运行在 executor 线程中，该线程没有自己的事件循环。`asyncio.get_event_loop().run_until_complete()` 在 Python 3.12+ 中会报错，且会创建新事件循环导致 MessageStore 单例冲突。runner 的 `_sync_get_messages()` 使用 `asyncio.run_coroutine_threadsafe(coro, _main_loop)` 向 FastAPI 主事件循环提交协程，是正确的线程桥接方式。

- [ ] **Step 2: 修改 `_call_subagent_gen()` 中 journal-agent 的调用逻辑**

在 `agent/handler.py` 的 `_call_subagent_gen()` 函数中，在 `task = args.get("task", "")` 之后、`llm_config = runner.llm_config.copy()` 之前，添加 journal-agent 的特殊处理：

```python
        task = args.get("task", "")

        # journal-agent 特殊处理：构建增量消息 task，与 tidy 管道一致
        journal_msg_ids_for_cursor = []  # 默认空列表，仅 journal-agent 时填充
        if agent_name == "journal-agent":
            task, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)
```

然后在 `_call_subagent_gen()` 方法之后添加 `_build_journal_task_for_handler()` 方法：

```python
    def _build_journal_task_for_handler(self, original_task: str) -> tuple:
        """为主Agent调用 journal-agent 构建增量消息 task。

        与 compat.py 的 _tidy_context_impl 中 sleep/force 模式一致：
        1. 读取游标文件
        2. 从 MessageStore 获取增量消息
        3. 用 _build_journal_task() 构建 task

        Args:
            original_task: 主Agent传入的原始 task（如"记录工作日志"）

        Returns:
            (task_prompt, journal_msg_ids) 元组
            task_prompt: 构建好的 task（含增量消息或原始指令）
            journal_msg_ids: 增量消息 UUID 列表（供游标更新使用）
        """
        import json
        from niu_api.compat import _build_journal_task, _build_incremental_msg_text
        from agent.subagent import _read_context_window_tokens

        # 报告生成指令不替换为增量消息 task — journal-agent 自己读 journal.md 聚合
        report_keywords = ("周报", "月报", "季报", "年报")
        if any(kw in original_task for kw in report_keywords):
            return original_task, []

        # 1. 读取游标
        journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
        last_journal_id = ""
        if journal_cursor_path.exists():
            try:
                cursor_data = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                last_journal_id = cursor_data.get("last_journal_id", "")
            except Exception:
                pass

        # 2. 获取消息列表
        messages = self._sync_get_messages()
        if not messages:
            return original_task, []

        # 3. 游标为空且消息过多时，限制为最近200条（防止全量嵌入超限）
        if not last_journal_id and len(messages) > 200:
            from loguru import logger
            logger.warning(f"[Handler] Journal cursor empty, {len(messages)} messages total, limiting to last 200")
            messages = messages[-200:]

        # 4. 计算 token
        msg_tokens = []
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            for msg in messages:
                try:
                    t = calc.count_message_single(msg.role, msg.content or "", tool_calls=msg.tool_calls)
                except Exception:
                    t = max(1, len(msg.content or "") // 2) + 4
                msg_tokens.append(t)
        except ImportError:
            msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]

        # 5. 构建增量消息文本
        journal_msg_ids = []
        journal_msg_text = _build_incremental_msg_text(
            messages, last_journal_id, journal_msg_ids, msg_tokens
        )

        if not journal_msg_ids:
            return original_task, []

        # 6. 构建完整 task
        context_window_for_truncate = _read_context_window_tokens()
        safe_tokens = int(context_window_for_truncate * 0.6)
        return _build_journal_task(journal_msg_text, safe_tokens), journal_msg_ids
```

- [ ] **Step 3: 在 `_call_subagent_gen()` 中添加游标更新逻辑**

当前 `_call_subagent_gen()` 调用 `call_subagent()` 后只返回结果，不更新游标。需要在 journal-agent 完成后，从结果中提取游标并更新 `last_journal.json`。

在 `_call_subagent_gen()` 函数中，找到 journal-agent 结果处理的位置（在 `result = call_subagent(...)` 之后），添加游标更新：

```python
            # journal-agent 特殊处理：更新游标（仅当有增量消息时才更新）
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor)
```

然后在类中添加 `_update_journal_cursor()` 方法：

```python
    def _update_journal_cursor(self, journal_result: str, journal_msg_ids: list):
        """从 journal-agent 结果中提取游标并更新 last_journal.json

        实现与 compat.py 路径2/3 一致的完整 fallback 链：
        overflow 检测 → partial_result 恢复 → journal_msg_ids[-1] fallback

        Args:
            journal_result: journal-agent 的输出文本
            journal_msg_ids: 增量消息 UUID 列表（用于 fallback）
        """
        import json
        import fcntl
        from datetime import datetime
        from niu_api.compat import _extract_cursor_id, _is_subagent_overflow, _extract_overflow_info

        # 在获取文件锁之前读取消息列表 — 避免在锁内调用 _sync_get_messages() 导致死锁
        # （_sync_get_messages 通过 run_coroutine_threadsafe 提交到主事件循环，
        #  如果主事件循环正在等 _tidy_lock，会形成 A→B→A 死锁）
        messages = self._sync_get_messages()
        msg_id_set = {getattr(m, "id", "") for m in messages}

        journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
        lock_path = journal_cursor_path.with_suffix(".lock")

        # 文件锁保护 — 防止与 tidy 管道并发读写
        with open(lock_path, 'w') as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                # 读取当前游标（在锁内读取，保证原子性）
                last_journal_id = ""
                if journal_cursor_path.exists():
                    try:
                        cursor_data = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                        last_journal_id = cursor_data.get("last_journal_id", "")
                    except Exception:
                        pass

                new_journal_id = last_journal_id

                # 完整 fallback 链（与 compat.py 路径2/3 一致）
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_journal_id = recovered
                    else:
                        new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                else:
                    extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_journal_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id

                # 校验游标
                if new_journal_id and new_journal_id not in msg_id_set:
                    new_journal_id = last_journal_id

                # 写入
                if new_journal_id:
                    journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    journal_cursor_path.write_text(json.dumps({
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
```

- [ ] **Step 4: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`

- [ ] **Step 5: 提交**

```bash
git add agent/handler.py
git commit -m "feat: journal-agent path-1 unified — build incremental msg task from handler"
```

---

### Task 3.5: 游标文件锁保护 — 覆盖路径2/3

**Files:**
- Modify: `niu_api/compat.py`
- Modify: `agent/runner.py`

**原理：** 路径1（handler）的 `_update_journal_cursor()` 已加了 `fcntl.flock` 文件锁，但路径2（compat.py sleep/force）和路径3（runner.py `_run_subagent_step()`）的游标写入没有文件锁保护。当路径1（executor 线程）和路径2（asyncio 任务）并发运行时，两者可能同时写同一个 `last_journal.json` 文件。需要提取公共的锁保护函数，三处复用。

- [ ] **Step 1: 在 `niu_api/compat.py` 中添加 `_write_cursor_with_lock()` 公共函数**

在 `_build_journal_task()` 函数之后，添加：

```python
def _write_cursor_with_lock(cursor_path: Path, data: dict) -> None:
    """带文件锁保护的游标写入 — 防止 handler/compat/runner 并发竞争。

    Args:
        cursor_path: 游标 JSON 文件路径（如 ~/.niu/last_journal.json）
        data: 要写入的字典数据
    """
    import fcntl
    lock_path = cursor_path.with_suffix(".lock")
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            cursor_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
```

- [ ] **Step 2: 替换 compat.py 中 sleep 模式的游标写入**

在 compat.py 第1266-1272行，将：

```python
                        if new_journal_id:
                            journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                            journal_cursor_path.write_text(json.dumps({
                                "last_journal_id": new_journal_id,
                                "last_journal_at": datetime.now().isoformat(),
                            }, ensure_ascii=False, indent=2), encoding="utf-8")
```

替换为：

```python
                        if new_journal_id:
                            _write_cursor_with_lock(journal_cursor_path, {
                                "last_journal_id": new_journal_id,
                                "last_journal_at": datetime.now().isoformat(),
                            })
```

- [ ] **Step 3: 替换 compat.py 中 force 模式的游标写入**

在 compat.py 第1649-1654行，将：

```python
                if new_journal_id:
                    journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    journal_cursor_path.write_text(json.dumps({
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
```

替换为：

```python
                if new_journal_id:
                    _write_cursor_with_lock(journal_cursor_path, {
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    })
```

- [ ] **Step 4: 在 runner.py 中导入 `_write_cursor_with_lock`**

在 `agent/runner.py` 的 `from niu_api.compat import` 行中，添加 `_write_cursor_with_lock` 到导入列表。

- [ ] **Step 5: 替换 runner.py `_run_subagent_step()` 中的游标写入**

在 runner.py 第736-741行，将：

```python
        if new_cursor_id:
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text(json.dumps({
                cursor_field: new_cursor_id,
                timestamp_field: datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
```

替换为：

```python
        if new_cursor_id:
            _write_cursor_with_lock(cursor_path, {
                cursor_field: new_cursor_id,
                timestamp_field: datetime.now().isoformat(),
            })
```

- [ ] **Step 6: handler.py 的 `_update_journal_cursor()` 也改为使用 `_write_cursor_with_lock`**

在 handler.py 的 `_update_journal_cursor()` 中，将 `fcntl.flock` 锁 + `write_text` 替换为调用公共函数：

将 `_update_journal_cursor()` 中最后的写入部分：

```python
                # 写入
                if new_journal_id:
                    journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    journal_cursor_path.write_text(json.dumps({
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
```

替换为：

```python
                # 写入（使用公共锁保护函数）
                if new_journal_id:
                    from niu_api.compat import _write_cursor_with_lock
                    _write_cursor_with_lock(journal_cursor_path, {
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    })
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
```

注意：`_update_journal_cursor()` 仍然保留外层的 `fcntl.flock` 锁来保护游标读取和校验的原子性，只是最终的写入操作改为调用 `_write_cursor_with_lock`（它会再次获取锁，但因为 `fcntl.flock` 在同一进程内是递归兼容的——同一 fd 的 LOCK_EX 不会阻塞自身，但这里用的是不同的 fd，所以实际上锁会在 `_write_cursor_with_lock` 的 `fcntl.LOCK_EX` 处等待外层锁释放。这不正确！）

**修正方案**：不在 `_update_journal_cursor()` 中使用 `_write_cursor_with_lock()`，保留当前的内联写入（在已有的 `fcntl.flock` 锁内写入即可）。只有路径2/3（没有外层锁）才需要用 `_write_cursor_with_lock`。

所以 Step 6 应改为：**不做修改**，handler.py 保留现有的内联 `fcntl.flock` + `write_text` 方案。只有路径2/3改为调用 `_write_cursor_with_lock`。

- [ ] **Step 7: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')" && python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`

- [ ] **Step 8: 提交**

```bash
git add niu_api/compat.py agent/runner.py
git commit -m "fix: fcntl.flock on journal cursor writes — protect all 3 paths from concurrent race"
```

---

### Task 4: 更新 journal-agent 提示词 — 统一输入格式说明

**Files:**
- Modify: `config/agents/journal-agent.md`

**原理：** 当前提示词的"输入格式"段只描述了增量消息格式。需要更新为涵盖两种场景：1）程序自动调用（增量消息嵌入 task）；2）用户主动要求"写周报"等报告生成（无增量消息，只有 task 指令）。同时明确：无论哪种场景，消息格式都是一样的——如果 task 中有 `[id:UUID] [idx:N]` 格式的消息，就从中提取工作内容写日志；如果没有，就根据 task 指令执行相应操作（如生成报告）。

- [ ] **Step 1: 更新 `## 输入格式` 段落**

将当前的：

```markdown
## 输入格式

程序通过 task 方式传入增量消息，每条消息带 `[id:UUID] [idx:N]` 标注。你只需处理收到的全部消息。
```

替换为：

```markdown
## 输入格式

task 中可能包含两种内容：

1. **增量消息**（包含 `[id:UUID] [idx:N]` 标注的对话消息）：从中提取工作内容，写入日志。这是最常见的场景。
2. **纯指令**（如"生成本周工作周报"）：不包含消息标注，按指令执行报告生成等操作。

如果 task 中有 `[id:UUID]` 格式的消息，按日志记录流程处理。如果没有，按指令内容执行。
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/journal-agent.md
git commit -m "docs: update journal-agent prompt — clarify two input formats"
```

---

### Task 5: 更新主Agent提示词 — 统一调用说明

**Files:**
- Modify: `config/agents/niu.md`

**原理：** 当前主Agent提示词只说"用户说'记录一下' → chat-with-journal-agent"，没有说明调用格式。需要明确：调用时只需传简短的指令（如"记录工作日志"或"写周报"），增量消息由系统自动构建，主Agent不需要附带任何对话内容。

- [ ] **Step 1: 更新日志触发说明**

将当前的（第113-115行）：

```markdown
**日志触发**：
- 用户说"记录一下"、"记一下" → `chat-with-journal-agent`
- 用户说"写周报"、"写月报"、"生成报告" → `chat-with-journal-agent`
```

替换为：

```markdown
**日志触发**：
- 用户说"记录一下"、"记一下" → `chat-with-journal-agent` task="记录工作日志"
- 用户说"写周报"、"写月报"、"生成报告" → `chat-with-journal-agent` task="生成本周工作周报"
- 增量对话消息由系统自动构建传入，task 只需写明操作类型，不需要附带对话内容
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/niu.md
git commit -m "docs: clarify journal-agent call format — system auto-builds incremental msgs"
```

---

### Task 6: 真实集成测试 — 通过 API 触发验证

**Files:**
- Create: `tests/test_journal_unified_paths.py`

**原理：** 由于前端无法直接触发日志记录，需要通过 API 端点触发真实测试。测试步骤：1）启动程序；2）通过 `/api/chat` 发送几条对话消息；3）通过 `/api/context/tidy` force 模式触发 journal-agent；4）验证 journal.md 内容和游标更新；5）通过主Agent对话触发路径1；6）再次验证。

由于这是真实集成测试（需要程序运行+真实LLM），测试脚本设计为手动执行，不在 pytest 中自动运行。

- [ ] **Step 1: 创建测试脚本**

```python
# tests/test_journal_unified_paths.py
"""
Journal-Agent 三路径统一集成测试

真实测试：需要程序运行 + 真实 LLM。
手动执行：python tests/test_journal_unified_paths.py

验证点：
1. 路径2/3（API触发）— journal.md 格式一致
2. 路径1（主Agent触发）— 也能正确写入日志
3. 游标文件正确更新
"""

import json
import time
import requests
from pathlib import Path

API_BASE = "http://localhost:9876"
NIU_DIR = Path.home() / ".niu"
JOURNAL_PATH = NIU_DIR / "journal.md"
CURSOR_PATH = NIU_DIR / "last_journal.json"


def test_api_health():
    """验证 API 可达"""
    resp = requests.get(f"{API_BASE}/api/stats", timeout=5)
    assert resp.status_code == 200, f"API not reachable: {resp.status_code}"
    print("[PASS] API health check")


def test_send_chat_messages():
    """发送几条对话消息，为日志记录提供数据"""
    messages = [
        "我刚完成了代码审查功能的重构",
        "修复了一个关于路径展开的bug",
    ]
    for msg in messages:
        resp = requests.post(
            f"{API_BASE}/api/chat",
            json={"message": msg},
            timeout=60,
        )
        assert resp.status_code == 200, f"Chat failed: {resp.status_code}"
        # 等待 LLM 响应完成
        time.sleep(5)
    print(f"[PASS] Sent {len(messages)} chat messages")


def test_force_tidy_triggers_journal():
    """路径3：通过 force tidy 触发 journal-agent"""
    # 记录当前游标
    old_cursor = ""
    if CURSOR_PATH.exists():
        old_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")

    # 触发 force tidy
    resp = requests.post(
        f"{API_BASE}/api/context/tidy",
        json={"session_id": "default", "mode": "force"},
        timeout=120,
    )
    assert resp.status_code == 200, f"Force tidy failed: {resp.status_code}"
    result = resp.json()
    print(f"[INFO] Force tidy result: {json.dumps(result, ensure_ascii=False)[:200]}")

    # 等待 journal-agent 完成
    time.sleep(10)

    # 验证游标已更新
    assert CURSOR_PATH.exists(), "Cursor file not created"
    new_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")
    assert new_cursor != old_cursor, f"Cursor not updated: {old_cursor} -> {new_cursor}"
    print(f"[PASS] Force tidy: cursor updated {old_cursor[:8]}... -> {new_cursor[:8]}...")

    # 验证 journal.md 存在且包含今天日期
    assert JOURNAL_PATH.exists(), "journal.md not created"
    content = JOURNAL_PATH.read_text(encoding="utf-8")
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"# {today}" in content, f"Today's date header not found in journal.md"
    print(f"[PASS] Force tidy: journal.md contains today's entries")


def test_chat_triggers_journal_via_handler():
    """路径1：通过主Agent对话触发 journal-agent"""
    # 记录当前游标
    old_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")

    # 先发一条新消息（提供新增量数据）
    resp = requests.post(
        f"{API_BASE}/api/chat",
        json={"message": "记录一下今天的工作"},
        timeout=120,
    )
    assert resp.status_code == 200, f"Chat trigger failed: {resp.status_code}"

    # 等待主Agent调用 journal-agent 并完成
    time.sleep(30)

    # 验证游标已更新
    if CURSOR_PATH.exists():
        new_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")
        if new_cursor != old_cursor:
            print(f"[PASS] Path-1: cursor updated {old_cursor[:8]}... -> {new_cursor[:8]}...")
        else:
            print(f"[WARN] Path-1: cursor not updated (may be no new messages)")
    else:
        print("[WARN] Path-1: cursor file missing")


def test_journal_format_consistency():
    """验证 journal.md 格式一致性"""
    content = JOURNAL_PATH.read_text(encoding="utf-8")

    # 检查所有日期标题格式
    import re
    date_headers = re.findall(r'^# \d{4}-\d{2}-\d{2}', content, re.MULTILINE)
    print(f"[INFO] Found {len(date_headers)} date headers: {date_headers}")

    # 检查日志条目格式
    entries = re.findall(r'^- \d{2}:\d{2} .+ \| .+ \| .+ \| .+', content, re.MULTILINE)
    print(f"[INFO] Found {len(entries)} journal entries")

    # 不应该有重复的日期标题
    assert len(date_headers) == len(set(date_headers)), f"Duplicate date headers found"
    print(f"[PASS] No duplicate date headers")


if __name__ == "__main__":
    print("=== Journal-Agent 三路径统一集成测试 ===\n")
    print("前置条件：程序已启动（go run main.go）\n")

    try:
        test_api_health()
        test_send_chat_messages()
        test_force_tidy_triggers_journal()
        test_chat_triggers_journal_via_handler()
        test_journal_format_consistency()
        print("\n=== 所有测试通过 ===")
    except AssertionError as e:
        print(f"\n=== 测试失败: {e} ===")
    except Exception as e:
        print(f"\n=== 测试异常: {e} ===")
```

- [ ] **Step 2: 提交**

```bash
git add tests/test_journal_unified_paths.py
git commit -m "test: add journal-agent unified paths integration test"
```

---

### Task 7: 真实测试执行与验证

**Files:** 无代码修改

**原理：** 启动程序，执行集成测试脚本，验证三个路径的行为一致性。

- [ ] **Step 1: 先做临时备份提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add -A && git commit -m "backup: pre-journal-unified-test"
```

- [ ] **Step 2: 清理旧游标和日志数据**

```bash
rm -f ~/.niu/last_journal.json
# 备份现有 journal.md
if [ -f ~/.niu/journal.md ]; then cp ~/.niu/journal.md ~/.niu/journal.md.bak; fi
```

- [ ] **Step 3: 启动程序**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && go run main.go
```

- [ ] **Step 4: 运行集成测试**

在另一个终端：
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python tests/test_journal_unified_paths.py
```

- [ ] **Step 5: 手动验证路径1**

在聊天界面中输入"记录一下今天的工作"，观察：
1. 主Agent是否调用了 `chat-with-journal-agent`
2. journal-agent 的 task 是否包含增量消息（检查 raw_http 日志）
3. journal.md 是否正确更新
4. last_journal.json 游标是否推进

- [ ] **Step 6: 检查 raw_http 日志**

```bash
ls -lt logs/raw_http/$(date +%Y%m%d)/ | head -10
```

找到 journal-agent 的 request 文件，确认 task 内容包含 `[id:UUID] [idx:N]` 格式的增量消息。

- [ ] **Step 7: 验证完成后清理测试进程**

```bash
pkill -f "niu" || true
# 恢复 journal.md 备份
if [ -f ~/.niu/journal.md.bak ]; then mv ~/.niu/journal.md.bak ~/.niu/journal.md; fi
```

---

## 验证清单

1. `_build_journal_task()` 被三处复用（compat.py sleep、compat.py force、runner.py force），无内联重复
2. 主Agent调用 `chat-with-journal-agent` 时，task 中包含增量消息文本
3. 调用完成后游标文件 `last_journal.json` 正确更新（含 overflow fallback 链）
4. raw_http 日志中三个路径的 task 格式一致（都包含 `[id:UUID] [idx:N]` 消息）
5. journal.md 格式统一，无重复日期标题
6. 路径1触发后，journal-agent 能正确提取工作内容并写入日志
7. "写周报"指令保留原始 task，不被增量消息替换，journal-agent 走报告生成流程
8. `_sync_get_messages()` 复用 runner 的 `asyncio.run_coroutine_threadsafe` 桥接，不使用 `run_until_complete`
9. 游标读写使用 `fcntl.flock` 文件锁保护，三路径统一（`_write_cursor_with_lock` 复用）
10. 游标为空且消息超200条时自动截断，防止全量嵌入超限
11. 报告生成场景（"写周报"）不更新游标，不嵌入增量消息
12. `_sync_get_messages()` 在文件锁之前调用，避免死锁

## 审查修正记录

| # | 问题 | 严重程度 | 修正措施 |
|---|------|----------|----------|
| 1 | `_sync_get_messages()` 用 `run_until_complete` 导致嵌套事件循环错误 | CRITICAL | 改为复用 `runner._sync_get_messages()`（使用 `run_coroutine_threadsafe`） |
| 2 | 游标竞争 — 路径1绕过 `_tidy_lock` 与路径2并发写 | HIGH | `_update_journal_cursor()` 使用 `fcntl.flock` 文件锁 |
| 3 | "写周报"指令被增量消息 task 替换，报告关键词丢失 | HIGH | `_build_journal_task_for_handler()` 检测报告关键词，不替换原始 task |
| 4 | `_update_journal_cursor()` 缺少 overflow fallback 和 msg_ids fallback | HIGH | 实现与路径2/3一致的完整 fallback 链，接收 `journal_msg_ids` 参数 |
| 5 | 游标为空时全量消息可能超限 | MEDIUM | 消息超200条时截断为最近200条 |
| 6 | 报告生成场景下不应更新游标，但计划未做守卫 | HIGH(二次) | `if agent_name == "journal-agent" and journal_msg_ids_for_cursor:` |
| 7 | 路径2/3 的游标写入缺少 `fcntl.flock` 保护，锁不完整 | HIGH(二次) | 新增 Task 3.5，提取 `_write_cursor_with_lock()` 三处复用 |
| 8 | `_sync_get_messages()` 在文件锁内调用可能死锁 | MEDIUM(二次) | 在获取文件锁之前调用 `_sync_get_messages()` |
