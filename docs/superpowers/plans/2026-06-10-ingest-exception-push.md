# 入库异常推送实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 正常入库不推送，只有异常时才推送通知。不改变原有管道流程，不加轮询，不加monkey-patch。

**Architecture:** 在 `_process_and_handle_failure` 闭包的两个位置检测异常：except 块捕获管道异常时推送失败通知；成功路径查询 doc_status，发现 chunks_count=0 或实体数为0 时推送质量警告。正常成功不推送。

**Tech Stack:** Python (FastAPI SSE, aiosqlite, LightRAG doc_status + full_entities)

---

## 背景与约束

**设计原则**：
1. **正常不推送**——入库成功时用户不需要看到通知，进度条已经告知了
2. **异常才推送**——入库失败或质量异常时通知用户
3. **不改变原有流程**——不加轮询、不加阻塞、不加monkey-patch、不改管道调用方式
4. **只做增量修改**——在闭包已有的 except 块和成功路径中各加一个小检查

**异常分类**：

| 类型 | 场景 | 检测位置 | doc_status |
|------|------|---------|-----------|
| 管道异常 | LLM超时、网络错误、API限流等 | except 块捕获 | FAILED |
| 质量异常 | LLM返回有效内容但无法解析出实体 | 成功路径检查 | PROCESSED（但实体数为0） |
| 质量异常 | 文档分块后无结果 | 成功路径检查 | PROCESSED（chunks_count=0） |
| 用户取消 | CancelledError | except 块 | FAILED | **不推送** |
| 正常成功 | 入库完成，实体数>0 | — | PROCESSED | **不推送** |

**成功路径查询 doc_status 的安全性**：
- 管道 `apipeline_process_enqueue_documents()` 返回后，当前 track_id 的文档一定是终态
- 单文件入库（最常见场景）：管道处理当前文档后返回，查询安全
- 多文件竞态：后续闭包的管道因 busy 立即返回，此时文档可能是 PENDING——但这是**正常状态**（文档还没处理到），不是异常，不应推送

**事件循环约束**（不变）：
- `_process_and_handle_failure` 运行在 LightRAG 专用事件循环中
- `push_ingest_result` 是 async 函数，在 LightRAG 事件循环中 await
- `add_message` 每次创建独立 aiosqlite 连接，可在任何事件循环中 await
- `notify_new_message_sync` 使用 `call_soon_threadsafe`，可从任何上下文调用

**`full_entities` 访问方式**：
- `await rag_instance.full_entities.get_by_id(doc_id)` → `{"entity_names": [...], "count": N, ...}` 或 `None`
- 如果实体数为0，LightRAG 不写入 `full_entities` 记录，返回 `None`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `niu_api/chat.py` | 新增 `push_ingest_result()` async 函数（只推送异常/质量警告） |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | 闭包 except 块 + 成功路径各加异常检测和推送 |

---

### Task 1: 新增 `push_ingest_result` 辅助函数

**Files:**
- Modify: `niu_api/chat.py`（在 `notify_new_message_sync` 函数之后新增）

- [ ] **Step 1: 在 `notify_new_message_sync` 函数结束后新增 `push_ingest_result`**

在 `niu_api/chat.py` 中 `notify_new_message_sync` 函数结束后，添加：

```python
async def push_ingest_result(file_path: str, error: str = ""):
    """将入库异常写入 message.db 并推送 SSE 通知。

    仅在入库异常时调用（管道失败或质量异常）。正常入库不调用。
    设计为 async 函数，在 LightRAG 事件循环中调用。

    Args:
        file_path: 入库文件路径
        error: 错误信息
    """
    import os
    file_name = os.path.basename(file_path) if file_path else "未知文件"
    content = f"文件入库异常：{file_name}" + (f"（{error}）" if error else "")

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
git commit -m "feat: add push_ingest_result for ingest exception notification"
```

---

### Task 2: 闭包 except 块推送失败通知

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 在 `_mark_failed()` 之后、`if is_cancelled: raise` 之前，添加异常推送**

当前代码结构（约941-966行）：

```python
                        try:
                            from dataclasses import asdict as _asdict
                            from lightrag.api.routers.document_routes import DocStatus

                            async def _mark_failed():
                                docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                                for dk, dd in docs.items():
                                    dd.status = DocStatus.FAILED
                                    status_dict = _asdict(dd)
                                    await rag_instance.doc_status.upsert({dk: status_dict})

                            inner = _asyncio.create_task(_mark_failed())
                            try:
                                await inner
                            except _asyncio.CancelledError:
                                pass
                        except (_asyncio.CancelledError, Exception) as mark_err:
                            logger.debug(
                                f"[lightrag_insert_file] mark-failed skipped "
                                f"(best-effort): track_id={tid} error={mark_err}"
                            )
                        if is_cancelled:
                            raise pipeline_err
```

在 `_mark_failed` 的 except 块之后、`if is_cancelled: raise` 之前，添加：

```python
                        # 推送入库异常通知（用户主动取消不算异常）
                        if not is_cancelled:
                            try:
                                from niu_api.chat import push_ingest_result
                                await push_ingest_result(
                                    file_path=original_path,
                                    error=str(pipeline_err),
                                )
                            except _asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
```

注意：
- `original_path` 已在闭包外部定义，闭包通过 Python 闭包语义捕获
- `CancelledError` 在推送过程中要 re-raise，防止破坏取消语义
- 外层 `except` 已捕获异常，`pipeline_err` 变量可用

- [ ] **Step 2: 验证 Python 语法**

Run: `python -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: push ingest exception notification on pipeline failure"
```

---

### Task 3: 闭包成功路径检测质量异常

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 在 changelog 推送之后、外层 try 块结束之前，添加质量异常检查**

当前成功路径代码（约913-923行）：

```python
                        try:
                            from niu_api.internal.lightrag_manager import get_change_log
                            get_change_log().record_change("snapshot_refresh", {
                                "reason": "pipeline_completed",
                                "track_id": tid,
                            })
                        except Exception as _cl_err:
                            logger.debug(
                                f"[lightrag_insert_file] snapshot_refresh changelog skipped: {_cl_err}"
                            )
```

在 changelog 推送的 except 块之后、外层 try 块结束之前（即在 `except (_asyncio.CancelledError, Exception) as pipeline_err:` 之前），添加：

```python
                        # 检查入库质量异常（管道不抛异常但实际零产出）
                        try:
                            _docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                            from lightrag.base import DocStatus
                            for _did, _doc_info in (_docs or {}).items():
                                # 文档标记为 FAILED 但管道没抛异常 → 推送
                                if _doc_info.status == DocStatus.FAILED:
                                    from niu_api.chat import push_ingest_result
                                    await push_ingest_result(
                                        file_path=original_path,
                                        error=_doc_info.error_msg or "入库处理失败",
                                    )
                                # 文档标记为 PROCESSED 但分片数为0 → 质量异常
                                elif _doc_info.status == DocStatus.PROCESSED and (_doc_info.chunks_count or 0) == 0:
                                    from niu_api.chat import push_ingest_result
                                    await push_ingest_result(
                                        file_path=original_path,
                                        error="文档分片后无内容",
                                    )
                                # 文档标记为 PROCESSED 但实体数为0 → 质量异常
                                elif _doc_info.status == DocStatus.PROCESSED:
                                    _ent = await rag_instance.full_entities.get_by_id(_did)
                                    _ent_count = _ent.get("count", 0) if _ent else 0
                                    if _ent_count == 0:
                                        from niu_api.chat import push_ingest_result
                                        await push_ingest_result(
                                            file_path=original_path,
                                            error="知识提取结果为空",
                                        )
                        except _asyncio.CancelledError:
                            raise
                        except Exception:
                            pass
```

**关键设计决策**：
- 查询 `doc_status` 是一次性操作（非轮询），在管道返回后执行
- 单文件入库时查询一定安全（管道已处理完当前文档）
- 多文件竞态时，后续闭包的文档可能还是 PENDING——此时 `_doc_info.status` 不是 FAILED 也不是 PROCESSED，检查条件不满足，不会推送（正确行为：文档还没处理，不是异常）
- `CancelledError` 必须显式 re-raise——如果推送过程中收到 CancelledError，`except Exception` 不会捕获它（Python 3.9+），但如果不显式 `except _asyncio.CancelledError: raise`，CancelledError 会被 `except Exception: pass` 之前的其他代码路径意外吞掉。显式 re-raise 确保取消语义不被破坏，不会误推送成功入库为异常
- 遍历 `_docs` 所有条目而非只取第一个——重复入库时 `_docs` 可能包含多条记录（原始 PROCESSED + dup-xxx FAILED），必须逐一检查

- [ ] **Step 2: 验证 Python 语法**

Run: `python -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: detect quality anomalies (zero entities/empty chunks) in ingest success path"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 启动程序，拖入一个文件入库**

观察：正常入库成功时，聊天界面**不应出现**任何入库通知消息。

- [ ] **Step 2: 模拟入库异常**

方法：入库时断网或配置错误的 API Key，观察是否出现异常通知消息。
预期：聊天界面出现"文件入库异常：xxx（错误信息）"的灰色系统消息。

- [ ] **Step 3: 检查 message.db**

Run: `sqlite3 ~/.niu/messages.db "SELECT role, content FROM messages WHERE role='system' AND content LIKE '%入库异常%' ORDER BY rowid DESC LIMIT 5"`
Expected: 正常入库时无记录，异常时有记录。

---

## 审查修正记录

| # | 问题 | 修正 |
|---|------|------|
| 1 | 原方案简单断言"成功/失败"不可靠 | 改为"正常不推送，异常才推送" |
| 2 | 轮询方案破坏原有并发入库机制 | 完全不加轮询，只用一次性查询 |
| 3 | monkey-patch 方案复杂且脆弱 | 不用 monkey-patch，只在闭包内增量添加 |
| 4 | 多文件竞态导致推送时机错误 | except 路径不受竞态影响；成功路径检查时 PENDING 状态不满足条件，不会误推送 |
| 5 | CancelledError 不应触发推送 | except 路径用 `if not is_cancelled` 条件排除 |
