# 入库结果推送（数据说话版）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 入库完成后，用数据说话——推送分片数、实体数、关系数、成功/失败文档数，让用户自己判断入库质量，不替用户下"成功/失败"结论。

**Architecture:** 管道完成后，通过 `get_docs_by_track_id(tid)` 遍历每个文档的 `doc_status`，再查 `full_entities`/`full_relations` 获取 per-document 的实体数和关系数，聚合后推送。不修改 LightRAG fork 任何代码。推送消息格式为统计数据，不做成功/失败断言。

**Tech Stack:** Python (FastAPI SSE, aiosqlite, LightRAG doc_status + full_entities/full_relations)

---

## 背景与约束

**为什么不能简单断言"成功"或"失败"**：
1. `doc_status.status == PROCESSED` 不代表入库真正成功——LightRAG 把空提取也标记为 PROCESSED
2. 实体提取可能部分成功——6个分片中5个成功1个失败，但合并阶段可能因缺失字段导致零产出
3. 入库质量无法靠大模型自己评估（运动员不能当裁判）
4. 用户要求：**用数据说话，不轻易断言成功或失败**

**可用的数据源**（均为 LightRAG 已有，不需要改 fork）：
1. `doc_status.get_docs_by_track_id(tid)` → 返回 `dict[str, DocProcessingStatus]`，每个文档独立 entry
2. `DocProcessingStatus.status` — `PROCESSED`/`FAILED`/`PREPROCESSED`/`PENDING`/`PROCESSING`
3. `DocProcessingStatus.chunks_count` — 该文档的分片数
4. `DocProcessingStatus.error_msg` — 失败时的错误信息
5. `DocProcessingStatus.file_path` — 文件路径
6. `full_entities.get_by_id(doc_id)` → `{"entity_names": [...], "count": N}` — 该文档贡献的实体名列表和数量
7. `full_relations.get_by_id(doc_id)` → `{"relation_pairs": [...], "count": N}` — 该文档贡献的关系对列表和数量

**多文档入库**：一个 `track_id` 对应 N 个文档，每个文档有独立 `doc_id` 和 `doc_status`。`get_docs_by_track_id(tid)` 返回 N 个 entry。需要遍历聚合。

**事件循环约束**（与之前相同）：
- `_process_and_handle_failure` 运行在 LightRAG 专用事件循环中
- `push_ingest_result` 是 async 函数，在 LightRAG 事件循环中 await
- `add_message` 每次创建独立 aiosqlite 连接，可在任何事件循环中 await
- `notify_new_message_sync` 使用 `call_soon_threadsafe`，可从任何上下文调用

**`full_entities`/`full_relations` 访问方式**：
- `rag_instance.full_entities` 是 `JsonKVStorage` 实例
- `await rag_instance.full_entities.get_by_id(doc_id)` 返回 `dict | None`
- 返回值结构：`{"entity_names": ["实体1", "实体2", ...], "count": 2}`
- `rag_instance.full_relations` 同理，返回 `{"relation_pairs": [...], "count": N}`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `niu_api/chat.py` | 修改 `push_ingest_result` 函数签名和文案（接收统计数据） |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | 修改 `_process_and_handle_failure` 中的推送逻辑（查询 full_entities/full_relations 聚合统计） |

---

### Task 1: 修改 `push_ingest_result` 和 `_process_and_handle_failure`——同步修改函数签名和调用者

**注意**：函数签名和调用者必须作为原子操作一起提交，否则中间状态会导致 TypeError（旧调用传入 `status="completed"` 会匹配到新签名的 `total_docs` 参数）。

**Files:**
- Modify: `niu_api/chat.py:78-107`
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:924-1001`

- [ ] **Step 1: 修改 `push_ingest_result` 函数签名和文案**

将 `niu_api/chat.py` 中 `push_ingest_result` 函数替换为：

```python
async def push_ingest_result(file_path: str, total_docs: int = 0, success_docs: int = 0, failed_docs: int = 0, total_chunks: int = 0, entities_count: int = 0, relations_count: int = 0, errors: list[str] | None = None):
    """将入库结果写入 message.db 并推送 SSE 通知。

    用数据说话：推送分片数、实体数、关系数、成功/失败文档数，
    让用户自己判断入库质量，不替用户下成功/失败结论。

    Args:
        file_path: 入库文件路径（单文件时有用，多文件时可为空）
        total_docs: 总文档数
        success_docs: 成功文档数（doc_status == PROCESSED）
        failed_docs: 失败文档数（doc_status == FAILED 或 entities_count == 0）
        total_chunks: 总分片数
        entities_count: 各文档实体数之和（文档内去重，跨文档不去重）
        relations_count: 各文档关系数之和（文档内去重，跨文档不去重）
        errors: 失败文档的错误信息列表
    """
    import os
    file_name = os.path.basename(file_path) if file_path else ""

    parts = []
    if file_name:
        parts.append(file_name)
    parts.append(f"文档 {success_docs}/{total_docs} 成功")
    if failed_docs > 0:
        parts.append(f"{failed_docs} 失败")
    parts.append(f"分片 {total_chunks} 个")
    parts.append(f"实体 {entities_count} 个")
    parts.append(f"关系 {relations_count} 个")

    content = "入库结果：" + "｜".join(parts)

    if errors:
        error_summary = errors[0] if len(errors) == 1 else f"{len(errors)} 个错误（首个：{errors[0]}）"
        content += f"｜错误：{error_summary}"

    try:
        from agent.session import MessageStore
        store = MessageStore()
        msg_id = await store.add_message(role="system", content=content)
        if msg_id:
            notify_new_message_sync(msg_id, "system", content)
    except Exception:
        pass
```

- [ ] **Step 2: 修改 `_process_and_handle_failure` 成功路径的推送逻辑**

将 `__init__.py` 第924-944行（成功路径推送代码）替换为：

```python
                        # 查询入库统计数据并推送（用数据说话，不替用户下结论）
                        try:
                            docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                            total_docs = len(docs)
                            success_docs = 0
                            failed_docs = 0
                            total_chunks = 0
                            entities_count = 0
                            relations_count = 0
                            errors = []
                            from lightrag.base import DocStatus
                            for _did, _dinfo in docs.items():
                                total_chunks += _dinfo.chunks_count or 0
                                if _dinfo.status == DocStatus.FAILED:
                                    failed_docs += 1
                                    if _dinfo.error_msg:
                                        errors.append(_dinfo.error_msg)
                                    continue
                                # PROCESSED / PREPROCESSED — 查询实际产出
                                _ent = await rag_instance.full_entities.get_by_id(_did)
                                _rel = await rag_instance.full_relations.get_by_id(_did)
                                _ent_count = _ent.get("count", 0) if _ent else 0
                                _rel_count = _rel.get("count", 0) if _rel else 0
                                if _ent_count == 0:
                                    # 标记为成功但零实体产出 → 视为失败
                                    failed_docs += 1
                                    errors.append(f"{_dinfo.file_path or _did}: 未提取到实体")
                                else:
                                    success_docs += 1
                                    entities_count += _ent_count
                                    relations_count += _rel_count
                            from niu_api.chat import push_ingest_result
                            await push_ingest_result(
                                file_path=original_path,
                                total_docs=total_docs,
                                success_docs=success_docs,
                                failed_docs=failed_docs,
                                total_chunks=total_chunks,
                                entities_count=entities_count,
                                relations_count=relations_count,
                                errors=errors if errors else None,
                            )
                        except Exception as _push_err:
                            logger.debug(f"[lightrag_insert_file] ingest result push skipped: {_push_err}")
```

- [ ] **Step 3: 修改 `_process_and_handle_failure` 失败路径的推送逻辑**

将第986-1001行（失败路径推送代码）替换为：

```python
                        # 推送入库失败结果（用户主动取消不算失败）
                        if not is_cancelled:
                            try:
                                docs = await rag_instance.doc_status.get_docs_by_track_id(tid)
                                total_docs = len(docs)
                                total_chunks = sum(d.chunks_count or 0 for d in docs.values())
                                _err_msgs = [d.error_msg for d in docs.values() if d.error_msg]
                                from niu_api.chat import push_ingest_result
                                await push_ingest_result(
                                    file_path=original_path,
                                    total_docs=total_docs,
                                    success_docs=0,
                                    failed_docs=total_docs,
                                    total_chunks=total_chunks,
                                    errors=_err_msgs if _err_msgs else [str(pipeline_err)],
                                )
                            except _asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
```

- [ ] **Step 4: 验证 Python 语法**

Run: `python -c "import py_compile; py_compile.compile('niu_api/chat.py', doraise=True); print('OK')"`
Expected: OK

Run: `python -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 5: 原子提交**

```bash
git add niu_api/chat.py mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: push ingest statistics (entities/relations/chunks) instead of success/failure verdict"
```

---

### Task 3: 端到端验证

- [ ] **Step 1: 启动程序，拖入一个文件入库**

观察聊天界面推送的消息，应类似：
```
入库结果：SYSTEM_MANUAL.md｜文档 1/1 成功｜分片 3 个｜实体 42 个｜关系 28 个
```
或入库失败时：
```
入库结果：SYSTEM_MANUAL.md｜文档 0/1 成功｜1 失败｜分片 3 个｜实体 0 个｜关系 0 个｜错误：实体提取结果为空
```

- [ ] **Step 2: 检查 message.db**

Run: `sqlite3 ~/.niu/messages.db "SELECT role, content FROM messages WHERE role='system' ORDER BY rowid DESC LIMIT 5"`
Expected: 包含入库统计数据的 system 消息

---

## 审查修正记录

| # | 问题 | 修正 |
|---|------|------|
| 1 | 简单断言"成功/失败"不可靠 | 改为推送统计数据，让用户自己判断 |
| 2 | `doc_status.status == PROCESSED` 不代表真正成功 | 额外查 `full_entities` 确认实体数 > 0 |
| 3 | `chunks_count` 是初始分片数不是结果 | 改为信息展示，不作为成功依据 |
| 4 | 单文档逻辑不适用多文档 | 遍历 `get_docs_by_track_id` 所有 entry 聚合 |
| 5 | 实体数=0 但 status=PROCESSED 的静默成功 | 视为失败，计入 failed_docs |
| 6 | 不改 LightRAG fork | 所有数据来自已有 API（doc_status + full_entities/full_relations） |
| 7 | "去重后实体总数"描述错误 | 实际是各文档实体数之和，跨文档不去重 |
| 8 | Task 1/2 之间过渡期旧调用者崩溃 | 合并为原子提交，函数签名和调用者一起改 |
| 9 | "实体提取结果为空"语气太重 | 改为"未提取到实体"（中性描述） |
