# 入库结果推送实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 入库完成后将成功/失败结果写入 message.db 并推送到前端，让用户知道入库是否成功。

**Architecture:** 在 `_process_and_handle_failure` 管道完成后，查询 `doc_status` 获取入库结果，通过 async 函数 `push_ingest_result` 写入 message.db 并调用 `notify_new_message_sync` 触发 SSE 推送，前端通过已有的 `refreshFromDB()` 自动刷新。不需要新的 SSE 事件类型，不需要改前端代码。

**Tech Stack:** Python (FastAPI SSE, aiosqlite)

---

## 背景与约束

**入库流程**：用户拖入文件 → 子Agent调用 `lightrag_insert_file` → 同步阶段返回 track_id → 异步管道 `fire_and_forget(_process_and_handle_failure)` 执行实体提取 → **当前：结果丢弃，前端不知道入库是否成功**。

**现有消息推送链路**：
1. `add_message(role, content)` 写入 message.db（纯 DB 操作，async）
2. `notify_new_message_sync(msg_id, role, content)` 触发 SSE 推送（sync，使用 `call_soon_threadsafe` 跨循环安全调用）
3. 前端收到 SSE → `refreshFromDB()` 从数据库重新加载 → 自动渲染
4. `role="tool"` 的消息不推送，`role="system"` 正常推送

**事件循环约束**：
- `_process_and_handle_failure` 运行在 **LightRAG 的专用事件循环**中（非 FastAPI 事件循环）
- `add_message` 是 async 方法，使用 aiosqlite，每次调用创建独立连接，**可在任何事件循环中 await**
- `notify_new_message_sync` 是同步方法，通过 `call_soon_threadsafe` 向 FastAPI 循环注入事件，**可从任何上下文调用**
- 因此 `push_ingest_result` 应设计为 **async 函数**，在 LightRAG 事件循环中直接 await `add_message`，然后调用同步的 `notify_new_message_sync`

**doc_status 返回值约束**：
- `get_docs_by_track_id(tid)` 返回 `dict[str, DocProcessingStatus]`（字典，非列表）
- 取第一个值用 `next(iter(docs.values()), None)`
- `DocProcessingStatus.chunks_count` 类型为 `int | None`，需 None 防御
- 属性访问（非字典键访问）：`doc_info.file_path`、`doc_info.chunks_count`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `niu_api/chat.py` | 新增 `push_ingest_result()` async 函数（封装 add_message + notify） |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | 管道完成后查询 `doc_status`，调用 `push_ingest_result` |

---

### Task 1: 后端 — 新增 `push_ingest_result` 辅助函数

**Files:**
- Modify: `niu_api/chat.py`（新增函数）

- [ ] **Step 1: 在 `notify_new_message_sync` 函数附近新增 `push_ingest_result`**

在 `niu_api/chat.py` 第76行之后（`notify_new_message_sync` 函数结束后），添加：

```python
async def push_ingest_result(file_path: str, status: str, chunks_count: int = 0, error: str = ""):
    """将入库结果写入 message.db 并推送 SSE 通知。

    设计为 async 函数，在 LightRAG 事件循环中调用。
    add_message 每次创建独立 aiosqlite 连接，可在任何事件循环中 await。
    notify_new_message_sync 使用 call_soon_threadsafe，可从任何上下文调用。

    Args:
        file_path: 入库文件路径
        status: "completed" 或 "failed"
        chunks_count: 切片数
        error: 错误信息（仅失败时）
    """
    import os
    file_name = os.path.basename(file_path) if file_path else "未知文件"

    if status == "completed":
        content = f"文件入库完成：{file_name}（切片 {chunks_count} 个）"
    else:
        content = f"文件入库失败：{file_name}（错误：{error}）"

    try:
        from agent.session import MessageStore
        store = MessageStore()
        msg_id = await store.add_message(role="system", content=content)
        if msg_id:
            notify_new_message_sync(msg_id, "system", content)
    except Exception:
        pass
```

- [ ] **Step 2: 验证 Python 语法**

Run: `python -c "import py_compile; py_compile.compile('niu_api/chat.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add niu_api/chat.py
git commit -m "feat: add push_ingest_result async helper for ingest result notification"
```

---

### Task 2: 后端 — 管道完成后查询 `doc_status` 并推送入库结果

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:905-975`（`_process_and_handle_failure` 函数）

- [ ] **Step 1: 修改 `_process_and_handle_failure` 查询入库结果并推送**

在 `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` 的 `_process_and_handle_failure` 函数中：

**成功路径**：在 `changelog.record_change("snapshot_refresh", ...)` 之后、外层 try 块结束之前，添加：

```python
    # 查询入库结果（通过 LightRAG 官方 API）
    docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
    doc_info = next(iter(docs.values()), None) if docs else None

    # 推送入库结果到前端
    try:
        from niu_api.chat import push_ingest_result
        await push_ingest_result(
            file_path=doc_info.file_path if doc_info else "",
            status="completed",
            chunks_count=doc_info.chunks_count if doc_info and doc_info.chunks_count is not None else 0,
        )
    except Exception:
        pass
```

**失败路径**：在 `_mark_failed()` 之后、`if is_cancelled: raise` 之前，添加（注意排除 CancelledError）：

```python
    # 推送入库失败结果（用户主动取消不算失败）
    if not is_cancelled:
        try:
            from niu_api.chat import push_ingest_result
            await push_ingest_result(
                file_path="",
                status="failed",
                error=str(pipeline_err),
            )
        except Exception:
            pass
```

注意：先 Read 文件确认 `_process_and_handle_failure` 的完整 try/except 结构，特别是 `is_cancelled` 变量名和 `_mark_failed` 调用的位置。

- [ ] **Step 2: 验证 Python 语法**

Run: `python -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: push ingest result (success/failure) after pipeline completes"
```

---

### Task 3: 端到端验证

- [ ] **Step 1: 启动程序，拖入一个文件入库**

观察：
1. 入库完成后，聊天界面是否出现入库结果系统消息
2. 入库失败时是否显示失败消息

- [ ] **Step 2: 检查 message.db 中是否有入库结果记录**

Run: `sqlite3 ~/.niu/messages.db "SELECT role, content FROM messages WHERE role='system' ORDER BY rowid DESC LIMIT 5"`
Expected: 包含入库结果的 system 消息

---

## 审查修正记录

| # | 问题 | 修正 |
|---|------|------|
| 1 | `add_message_sync` 不存在 | 改为 async `push_ingest_result`，直接 await `add_message` |
| 2 | `docs[0]` 字典索引错误 | 改为 `next(iter(docs.values()), None)` |
| 3 | `chunks_count` 可能为 `None` | 加 `is not None` 检查 |
| 4 | CancelledError 不应触发失败推送 | 加 `if not is_cancelled` 条件 |
| 5 | 同步函数阻塞 LightRAG 事件循环 | 改为 async 函数，在 LightRAG 循环中 await |
