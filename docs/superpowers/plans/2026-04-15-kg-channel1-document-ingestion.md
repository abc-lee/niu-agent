# KG Data Ingestion - Channel 1: Document → KG

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When documents are ingested via `store_document_l1` / `store_documents_l1`, automatically write Document node + extract entities from L1 + create Entity nodes + link them in the Knowledge Graph (KuzuDB).

**Architecture:** Add a `sync_to_kg()` helper function in photo-server that calls niu_kg_server directly (same-process import, like ToolRegistry pattern). Called at the end of `store_document_l1` and `store_documents_l1` after vector DB write succeeds. Entity extraction parses the L1 format `标题|关键词|摘要|实体|类型|指针` — the "实体" field already contains entity names.

**Tech Stack:** Python, KuzuDB (via niu_kg_server), L1 format parsing

---

## File Structure

| File | Responsibility |
|------|---------------|
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | Add `sync_to_kg()` helper + call from `store_document_l1` and `store_documents_l1` |
| `mcp-servers/kg-server/src/niu_kg_server/__init__.py` | No changes — existing `create_document`, `create_entity`, `link_document_entity` are the targets |

## Key Interfaces

### kg-server write functions (already implemented)

```python
# mcp-servers/kg-server/src/niu_kg_server/__init__.py

create_document(uri: str, title: str, content: str = "", source: str = "", file_path: str = "") -> dict
create_entity(id: str, name: str, entity_type: str, description: str = "") -> dict
link_document_entity(doc_uri: str, entity_id: str, confidence: float | None = None) -> dict
```

### L1 format

```
标题|关键词1,关键词2|摘要文本|实体1:类型1,实体2:类型2|文档类型|指针路径
```

Example: `Skills Writing Specification|skills,agent,norm|规范文档描述...|Claude:technology,Agent:technology|方案|/path/to/file`

The 4th field (index 3) contains entities as `name:type` pairs separated by commas.

### store_document_l1 current return

```python
{"status": "success", "l1_id": "doc_xxx", "file_path": "/path/to/file", "message": "文档摘要已存储到向量库"}
```

---

### Task 1: Add `sync_to_kg()` helper function

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` (after `call_embedding_service` definition, ~line 530)

- [ ] **Step 1: Write the `sync_to_kg()` function**

Add after the `call_embedding_service` function definition:

```python
def sync_to_kg(file_path: str, l1: str, source: str = "document") -> dict:
    """同步文档和实体到知识图谱（KuzuDB）。

    从 L1 摘要中提取实体，写入 kg-server 的 Document + Entity 节点并建立 MENTIONS 关系。
    失败不影响主流程（向量库写入已成功）。
    """
    try:
        from niu_kg_server import create_document, create_entity, link_document_entity

        # 1. 从 file_path 推算 title
        from pathlib import Path
        title = Path(file_path).stem

        # 2. 创建 Document 节点
        doc_result = create_document(
            uri=file_path,
            title=title,
            content=l1,
            source=source,
        )
        logger.info(f"[KG] Document created: {file_path}")

        # 3. 从 L1 提取实体（第4个字段，格式: name:type,name:type）
        entities_created = []
        parts = l1.split("|")
        if len(parts) >= 4:
            entity_str = parts[3].strip()
            if entity_str:
                for pair in entity_str.split(","):
                    pair = pair.strip()
                    if ":" in pair:
                        name, etype = pair.rsplit(":", 1)
                        name = name.strip()
                        etype = etype.strip().lower()
                    else:
                        name = pair.strip()
                        etype = "other"
                    if not name:
                        continue

                    # 生成 entity ID: type:name (与 kg-server MERGE 语义一致)
                    entity_id = f"{etype}:{name}"
                    try:
                        create_entity(
                            id=entity_id,
                            name=name,
                            entity_type=etype,
                            description=f"Extracted from {title}",
                        )
                        link_document_entity(
                            doc_uri=file_path,
                            entity_id=entity_id,
                            confidence=0.7,
                        )
                        entities_created.append(entity_id)
                    except Exception as e:
                        logger.warning(f"[KG] Entity creation failed for {name}: {e}")

        logger.info(f"[KG] Sync complete: {len(entities_created)} entities linked to {file_path}")
        return {"status": "success", "doc_uri": file_path, "entities": entities_created}

    except ImportError:
        logger.warning("[KG] niu_kg_server not available, skipping KG sync")
        return {"status": "skipped", "reason": "kg-server not importable"}
    except Exception as e:
        logger.warning(f"[KG] Sync failed: {e}")
        return {"status": "error", "reason": str(e)}
```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd E:/tools/ai-bot/mcp-servers/photo-server/src && python -c "import niu_photo_server; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: add sync_to_kg helper for document→KG data flow"
```

---

### Task 2: Call `sync_to_kg()` from `store_document_l1`

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` (in `store_document_l1`, after the success return dict, ~line 2292)

- [ ] **Step 1: Add KG sync call after vector DB write succeeds**

In `store_document_l1`, after the success return dict is built (line 2292-2297), add the KG sync call before returning. Change:

```python
        logger.info(f"[STORE_L1] L1 存储成功: {l1_id}")

        return {
            "status": "success",
            "l1_id": l1_id,
            "file_path": file_path,
            "message": "文档摘要已存储到向量库",
        }
```

To:

```python
        logger.info(f"[STORE_L1] L1 存储成功: {l1_id}")

        # 同步到知识图谱（失败不影响向量库写入）
        kg_result = sync_to_kg(file_path, l1, source="document")

        return {
            "status": "success",
            "l1_id": l1_id,
            "file_path": file_path,
            "message": "文档摘要已存储到向量库",
            "kg_sync": kg_result,
        }
```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd E:/tools/ai-bot/mcp-servers/photo-server/src && python -c "import niu_photo_server; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: call sync_to_kg from store_document_l1 after vector write"
```

---

### Task 3: Call `sync_to_kg()` from `store_documents_l1`

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` (in `store_documents_l1`, after each document's success, ~line 2383)

- [ ] **Step 1: Add KG sync call in the batch store loop**

In `store_documents_l1`, after the success result is appended (around line 2383-2391), add KG sync. Change:

```python
                results.append(
                    {
                        "file_path": file_path,
                        "status": "success",
                        "l1_id": l1_id,
                        "l2_id": l2_id,
                    }
                )
                success_count += 1
```

To:

```python
                # 同步到知识图谱（失败不影响向量库写入）
                kg_result = sync_to_kg(file_path, l1, source="document")

                results.append(
                    {
                        "file_path": file_path,
                        "status": "success",
                        "l1_id": l1_id,
                        "l2_id": l2_id,
                        "kg_sync": kg_result,
                    }
                )
                success_count += 1
```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd E:/tools/ai-bot/mcp-servers/photo-server/src && python -c "import niu_photo_server; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: call sync_to_kg from store_documents_l1 batch store"
```

---

### Task 4: Update `tool-layer-decision.md`

**Files:**
- Modify: `docs/tool-layer-decision.md`

- [ ] **Step 1: Update kg-server classification**

Find the section where kg-server tools are classified as "底层操作（不暴露给任何Agent）" and add a note that `create_document`, `create_entity`, `link_document_entity` are now called programmatically by `sync_to_kg()` during document ingestion. The read tools (`graph_snapshot`, `explore_node`, etc.) remain available for dynamic injection.

Add after the KG_SERVER_TOOLS list:

```markdown
> **Note (2026-04-15):** `create_document`, `create_entity`, `link_document_entity` are now called programmatically by `sync_to_kg()` during document ingestion (photo-server → niu_kg_server same-process call). They remain classified as底层操作 — not exposed to Agent LLM tool calls, but used by internal code paths.
```

- [ ] **Step 2: Commit**

```bash
git add docs/tool-layer-decision.md
git commit -m "docs: update kg-server tool classification for programmatic KG sync"
```

---

### Task 5: End-to-end verification

- [ ] **Step 1: Restart the application**

The application must be running with the updated photo-server code.

- [ ] **Step 2: Ingest a document**

Drag a document into the chat window. Wait for the Agent to complete ingestion (ingest_document → store_document_l1).

- [ ] **Step 3: Check KG for the document**

Open the graph visualization window. Verify:
- The document appears as a node
- Entities extracted from the L1 appear as nodes
- MENTIONS edges connect the document to its entities

- [ ] **Step 4: Check logs for KG sync**

Look for `[KG]` log entries confirming Document created and entities linked.

---

## Verification Checklist

- [ ] `sync_to_kg()` function exists and handles ImportError gracefully
- [ ] `store_document_l1` calls `sync_to_kg()` after vector write, returns `kg_sync` in result
- [ ] `store_documents_l1` calls `sync_to_kg()` for each doc, returns `kg_sync` in each result
- [ ] KG sync failure does NOT break the main vector DB write flow
- [ ] L1 format parsing correctly extracts `name:type` entity pairs from the 4th field
- [ ] Entity IDs use `type:name` format for uniqueness
- [ ] Document URI in KG matches the file_path from ingestion
- [ ] `tool-layer-decision.md` updated to reflect programmatic KG writes
- [ ] Graph visualization shows newly ingested documents and their entities
