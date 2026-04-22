# 02 - Document & Entity Pipeline: Current vs LightRAG Replacement

> 最后更新：2026-04-22
> 状态：✅ 方案已讨论确认

## Overview

This document details the replacement of the current multi-stage document ingestion and entity extraction pipeline with LightRAG's built-in mechanisms. The current pipeline spans 5 stages (K1-K5) with async sub-agent dispatch, while LightRAG provides a unified `ainsert()` flow that handles chunking, entity extraction, relation building, and vectorization in one pass.

---

## 1. Current Pipeline Deep Dive

### K1: Document Creation (entry points)

Documents enter the KG from three sources:

**Photo Server** (`mcp-servers/photo-server/src/niu_photo_server/__init__.py`):
- `add_photo()` and `add_photos()` create Document nodes with `entity_status=pending`
- Photo description text stored as `content` field on the Document node
- Each photo gets a Document node with `doc_type=photo`

**Notes API** (inside photo-server):
- `create_note()` creates Document nodes with `entity_status=pending`
- Note text stored as `content` field

**File Parser** (via sub-agent):
- Parsed document text passed to `kg-server` `create_document()` tool
- Creates Document node with `entity_status=pending`

**Current KG Schema (KuzuDB)**:
```
Document: id, content, doc_type, source, entity_status(pending/processing/completed), created_at
Entity: id, name, entity_type, description, source_doc_ids
Relation: id, source_id, target_id, relation_type, description, source_doc_ids
```

### K2: KG Scanner (60-second poll)

**File**: `agent/injector/kg_scanner.py`

- Runs on a 60-second `schedule.repeat(every(60).seconds)` loop
- Queries KuzuDB for `Document` nodes where `entity_status = 'pending'`
- For each pending document, dispatches to `entity-extractor` sub-agent
- Sets `entity_status = 'processing'` before dispatch
- On sub-agent completion, sets `entity_status = 'completed'`
- On failure, sets `entity_status = 'pending'` (retry on next scan)
- Batch size limited to 3 concurrent extractions to avoid LLM rate limits

**Key behavior**:
- Polling-based: documents sit in `pending` state for up to 60 seconds
- Retry on failure: failed documents revert to `pending` and are retried
- Sub-agent receives full document content + extraction prompt
- Sub-agent calls `kg-server` tools to create Entity and Relation nodes

### K3: KG Enricher (sync from vector-store)

**File**: `config/agents/kg-enricher.md`

- Triggered after entity extraction completes
- Syncs experiences and profiles from vector-store (L0/L1/L2) into KG
- Creates/updates Entity and Relation nodes based on vector-store content
- Runs as a sub-agent invoked by the main agent

### K4: Dream Evolver (structured entity injection)

**File**: `config/agents/dream-evolver.md`

- Runs on a scheduled basis (idle-time background processing)
- Analyzes accumulated knowledge and generates higher-level insights
- Writes structured entities and relations directly to KG via `kg-server` tools
- Uses `create_entity()`, `create_relation()`, `merge_entity()` tools
- Produces "dream" entities: synthesized concepts that emerge from patterns

### K5: KG Sync (6-hour batch)

**File**: `agent/injector/kg_sync.py`

- Runs every 6 hours via scheduler
- `_sync_photos_db()`: Scans photos.db SQLite for new/updated photos, creates Document nodes
- `_sync_vectors_db()`: Scans vector-store for new entries, creates Entity/Relation nodes
- Idempotent: uses timestamps to only process new/changed records
- Bulk operation: processes all changes since last sync in one batch

---

## 2. LightRAG Pipeline Deep Dive

### Core Insert Flow (`ainsert`)

**File**: `lightrag/lightrag.py` (method: `ainsert`)

```
Input: content (str)
  |
  v
1. Document Status Check
   - Check DocStatus in pipeline status store
   - Skip if already PROCESSED
   |
   v
2. Chunking (chunking_by_token_size)
   - Split content into chunks by token count
   - Each chunk gets a unique ID (full_doc_id + chunk_order_index)
   |
   v
3. Entity Extraction (per chunk, parallel)
   - Call LLM with entity extraction prompt
   - Extract entities and relations from chunk text
   - Returns: list of (entity_name, entity_type, description) and (src, tgt, relation_type, description)
   |
   v
4. Knowledge Graph Update
   - Merge entities into graph (upsert: update if exists, create if not)
   - Merge relations into graph
   - Handle entity resolution (same name = same entity)
   |
   v
5. Vectorization
   - Embed entity descriptions and relation descriptions
   - Store in vector DB for semantic search
   |
   v
6. Status Update
   - Mark document as PROCESSED in DocStatus store
```

### Custom KG Insert (`ainsert_custom_kg`)

```
Input: entities (list[dict]), relations (list[dict])
  |
  v
1. Validate entity/relation structure
   - Each entity: {entity_name, entity_type, description}
   - Each relation: {src_id, tgt_id, relation_type, description}
   |
   v
2. Direct Graph Update
   - Upsert entities into graph (no LLM call)
   - Upsert relations into graph
   |
   v
3. Vectorization
   - Embed descriptions and store
```

### Document Status Tracking (DocStatus)

**File**: `lightrag/lightrag.py` (DocStatus enum and pipeline)

```python
class DocStatus:
    PENDING = "pending"        # Registered but not processed
    PREPROCESSED = "preprocessed"  # Chunked but not extracted
    PROCESSING = "processing"  # Entity extraction in progress
    PROCESSED = "processed"    # Fully ingested
    FAILED = "failed"         # Extraction failed
```

LightRAG tracks document processing state internally. On restart, it can resume from the last successful state.

### Entity/Relation Creation APIs

```python
# Direct entity creation (no LLM)
await rag.acreate_entity(
    entity_name="Alice",
    entity_type="Person",
    description="A software engineer who works on AI"
)

# Direct relation creation (no LLM)
await rag.acreate_relation(
    src_id="Alice",
    tgt_id="ProjectX",
    relation_type="works_on",
    description="Alice is the lead developer of ProjectX"
)
```

---

## 3. Replacement Design

### 3.1 Document Ingestion Flow (New)

Replace the 3-step pending-scan-extract pipeline with a direct `ainsert` call.

#### Photo Ingestion (replaces K1+K2 for photos)

**Current flow**:
```
add_photo() → create Document(status=pending) → KG Scanner picks up → entity-extractor sub-agent → create Entity/Relation
```

**New flow**:
```
add_photo() → build description text → rag.ainsert(description) → done
```

**Implementation detail**:

```python
# In photo-server, after photo description is generated:
async def _sync_photo_to_lightrag(self, photo_id: str, description: str):
    """Insert photo description into LightRAG KG."""
    # Wrap description with context for better extraction
    content = f"[Photo: {photo_id}]\n{description}"

    # LightRAG handles chunking, extraction, vectorization
    await self.rag.ainsert(content)

    # Track photo_id -> LightRAG doc_id mapping in local DB
    # (for future updates/deletes)
    await self._save_lightrag_doc_mapping(photo_id, content)
```

**Key change**: No more `entity_status=pending` polling. Document goes directly into LightRAG's pipeline which tracks its own status.

#### Note Ingestion (replaces K1+K2 for notes)

```python
async def create_note(self, title: str, content: str):
    """Create a note and ingest into LightRAG."""
    full_content = f"[Note: {title}]\n{content}"
    await self.rag.ainsert(full_content)
```

#### File Ingestion (replaces sub-agent dispatch for file parsing)

```python
async def ingest_document(self, file_path: str, parsed_text: str):
    """Ingest parsed document text into LightRAG."""
    content = f"[Document: {file_path}]\n{parsed_text}"
    await self.rag.ainsert(content)
```

### 3.2 Async vs Sync Handling

**Problem**: LightRAG's `ainsert` is async but effectively blocking for the caller. Large documents (e.g., a 50-page PDF) will take significant time for LLM extraction across multiple chunks. The current system handles this via the pending-queue pattern.

**Solution: Background Task Queue with LightRAG DocStatus**

Rather than re-implementing the pending-queue pattern, leverage LightRAG's built-in `DocStatus` tracking combined with Python's `asyncio` background tasks.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class IngestTask:
    """Track an ingestion task."""
    content: str
    source_id: str          # photo_id, note_id, file_path
    source_type: str        # "photo", "note", "document"
    status: str = "queued"  # queued, processing, completed, failed
    error: Optional[str] = None


class LightRAGIngester:
    """Manages async ingestion into LightRAG with backpressure control."""

    def __init__(self, rag, max_concurrent: int = 3):
        self.rag = rag
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._task_queue: asyncio.Queue[IngestTask] = asyncio.Queue()
        self._background_task: Optional[asyncio.Task] = None
        self._tracked_tasks: dict[str, IngestTask] = {}

    def start(self):
        """Start the background ingestion worker."""
        self._background_task = asyncio.create_task(self._worker())

    async def _worker(self):
        """Background worker that processes ingestion tasks."""
        while True:
            task = await self._task_queue.get()
            asyncio.create_task(self._process_with_semaphore(task))

    async def _process_with_semaphore(self, task: IngestTask):
        """Process a single ingestion task with concurrency control."""
        async with self._semaphore:
            task.status = "processing"
            try:
                await self.rag.ainsert(task.content)
                task.status = "completed"
                logger.info(f"Ingestion completed: {task.source_type}/{task.source_id}")
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                logger.error(f"Ingestion failed: {task.source_type}/{task.source_id}: {e}")

    async def submit(self, task: IngestTask):
        """Submit an ingestion task. Returns immediately."""
        self._tracked_tasks[task.source_id] = task
        await self._task_queue.put(task)

    def get_status(self, source_id: str) -> Optional[str]:
        """Check ingestion status for a source."""
        task = self._tracked_tasks.get(source_id)
        return task.status if task else None
```

**Backpressure**: The semaphore limits concurrent LLM calls to `max_concurrent` (default 3, matching current KG Scanner batch size). Tasks queue up naturally.

**Failure recovery**: Failed tasks are tracked with error details. A retry mechanism can resubmit them:

```python
async def retry_failed(self):
    """Resubmit all failed tasks."""
    for source_id, task in self._tracked_tasks.items():
        if task.status == "failed":
            task.status = "queued"
            task.error = None
            await self._task_queue.put(task)
```

**Important**: LightRAG's own `DocStatus` also tracks document-level state. The `IngestTask` tracking is complementary -- it tracks at the application level (which photo/note/document) while LightRAG tracks at the content level (chunk extraction progress).

### 3.3 Entity Extraction Quality Comparison

| Aspect | Current (Sub-Agent) | LightRAG (ainsert) |
|--------|---------------------|---------------------|
| **Extraction prompt** | Custom per sub-agent (entity-extractor, kg-enricher) | Fixed LightRAG extraction prompt |
| **LLM model** | Configurable (uses user's LLM choice) | Configurable (LightRAG llm_model setting) |
| **Chunking** | None (whole document sent to LLM) | Token-based chunking with overlap |
| **Entity resolution** | Manual (sub-agent decides entity names) | Built-in (same name = same entity, with upsert) |
| **Context window** | Limited by single LLM call (large docs may lose info) | Chunked extraction preserves detail |
| **Relation quality** | Depends on sub-agent prompt quality | Structured extraction prompt, consistent format |
| **Cost** | One LLM call per document | Multiple LLM calls (one per chunk) |
| **Speed** | One call, but waits for sub-agent dispatch | Parallel chunk extraction, faster overall |
| **Customization** | High (edit sub-agent prompts freely) | Medium (can modify LightRAG extraction prompt) |

**Trade-offs**:

1. **Chunking advantage**: LightRAG's chunking is superior for large documents. The current system sends entire documents to the LLM, which loses detail in long texts. Chunked extraction ensures every part of the document is thoroughly processed.

2. **Prompt customization**: The current system's sub-agent prompts are tailored to the domain (photos, notes, files). LightRAG uses a generic extraction prompt. **Mitigation**: LightRAG allows custom extraction prompts via `entity_extraction_prompt` and `relation_extraction_prompt` parameters. We should customize these for our domain.

3. **Cost increase**: LightRAG makes one LLM call per chunk vs one per document. For a 50-page PDF chunked into 25 chunks, that is 25x more LLM calls. **Mitigation**: LightRAG processes chunks in parallel, so wall-clock time is similar. Cost can be managed by using a cheaper model for extraction (e.g., gpt-4o-mini).

4. **Entity consistency**: LightRAG's built-in entity resolution (same name = same entity with description upsert) is superior to the current system where sub-agents may create duplicate entities with slight name variations (e.g., "Alice" vs "Alice Johnson").

**Recommendation**: Customize LightRAG's extraction prompts for the ai-bot domain. Add photo-specific and note-specific context to the prompt templates:

```python
ENTITY_EXTRACTION_PROMPT = """You are a knowledge graph extractor for a personal knowledge management system.
Extract entities and relations from the following text.

Context types you may encounter:
- [Photo: ...]: Description of a personal photo, may contain people, places, events, objects
- [Note: ...]: Personal notes, may contain concepts, tasks, ideas, references
- [Document: ...]: Parsed document content, may contain technical concepts, people, organizations

For photos, pay special attention to:
- People (use full names when available)
- Locations (specific place names)
- Events (occasions, activities)
- Time references (dates, seasons, time of day)

{input_text}
"""
```

### 3.4 Incremental Updates

**Current system**: Delete old Entity/Relation nodes, recreate from updated document.

**LightRAG approach**: LightRAG does not natively support document-level updates. Its entity/relation model is additive -- `ainsert` always adds/merges.

**Problem**: If a note is edited, the old entities from the previous version remain in the graph alongside new ones, potentially creating contradictions.

**Solution: Delete-and-Reinsert Pattern**

```python
async def update_document(self, source_id: str, new_content: str):
    """Update a document by deleting old extractions and reinserting."""
    # 1. Find the LightRAG document ID(s) for this source
    old_doc_ids = await self._get_lightrag_doc_ids(source_id)

    # 2. Delete the old document(s) from LightRAG
    for doc_id in old_doc_ids:
        await self.rag.adelete(doc_id)

    # 3. Reinsert with new content
    await self.rag.ainsert(new_content)

    # 4. Update mapping
    await self._save_lightrag_doc_mapping(source_id, new_content)
```

**Key dependency**: LightRAG's `adelete()` method must be functional. Verify that it:
- Removes the document's chunks
- Removes entities/relations that were ONLY created from this document
- Does NOT remove entities/relations that are shared with other documents (this is critical)

**决策**：信任 LightRAG 自身的 adelete 逻辑。LightRAG 由港大团队开发，其知识库管理有理论基础，不需要自建引用计数层（DocEntityTracker）。如果后续发现 adelete 行为不符合预期，再针对性修复。

### 3.5 Photo Sync (replaces K5 `_sync_photos_db`)

**Current flow** (`kg_sync.py::_sync_photos_db`):
```
Every 6 hours:
  1. Query photos.db for photos modified since last_sync
  2. For each new/updated photo:
     a. Create/update Document node in KuzuDB
     b. Set entity_status = pending
  3. KG Scanner picks up pending documents in next cycle
```

**New flow**:
```
Every 6 hours:
  1. Query photos.db for photos modified since last_sync
  2. For each new photo:
     a. Build description text
     b. Submit to LightRAGIngester (background)
  3. For each updated photo:
     a. Delete old LightRAG document (via DocEntityTracker)
     b. Reinsert with new description
```

**Implementation**:

```python
async def sync_photos_to_lightrag(self):
    """Periodic sync from photos.db to LightRAG (replaces _sync_photos_db)."""
    last_sync = await self._get_last_sync_time("photos")

    # Query photos.db for new/updated photos
    new_photos = self._query_new_photos(last_sync)

    for photo in new_photos:
        description = await self._build_photo_description(photo)

        if photo.is_update:
            # Updated photo: delete old, reinsert
            await self.update_document(
                source_id=f"photo:{photo.id}",
                new_content=description
            )
        else:
            # New photo: insert
            task = IngestTask(
                content=f"[Photo: {photo.id}]\n{description}",
                source_id=f"photo:{photo.id}",
                source_type="photo"
            )
            await self.ingester.submit(task)

    # Update sync timestamp
    await self._set_last_sync_time("photos", datetime.now())
```

**Key difference**: No more KuzuDB Document nodes with `entity_status`. The photos.db is the source of truth, and LightRAG is the processing target.

### 3.6 Dream Evolver (replaces K4)

**Current flow**: Dream Evolver sub-agent analyzes patterns and writes structured entities/relations to KuzuDB via `kg-server` tools (`create_entity`, `create_relation`, `merge_entity`).

**New flow**: Dream Evolver writes directly to LightRAG via `ainsert_custom_kg` or `acreate_entity`/`acreate_relation`.

```python
async def inject_dream_entities(self, entities: list[dict], relations: list[dict]):
    """Inject Dream Evolver output into LightRAG.

    This bypasses LLM extraction since entities are pre-structured.
    """
    await self.rag.ainsert_custom_kg(entities, relations)
```

**Entity format mapping**:

| Current (KuzuDB) | LightRAG |
|------------------|----------|
| `Entity.name` | `entity_name` |
| `Entity.entity_type` | `entity_type` |
| `Entity.description` | `description` |
| `Relation.source_id` | `src_id` (entity name) |
| `Relation.target_id` | `tgt_id` (entity name) |
| `Relation.relation_type` | `relation_type` |
| `Relation.description` | `description` |

**Dream Evolver adapted call**:

```python
# Current: sub-agent calls kg-server tools
# New: sub-agent calls LightRAG adapter
entities = [
    {
        "entity_name": "Machine Learning",
        "entity_type": "Concept",
        "description": "A field of AI focused on pattern recognition"
    }
]
relations = [
    {
        "src_id": "Machine Learning",
        "tgt_id": "Neural Networks",
        "relation_type": "includes",
        "description": "ML includes neural network approaches"
    }
]

await rag.ainsert_custom_kg(entities, relations)
```

**Critical difference**: `ainsert_custom_kg` skips LLM extraction entirely. This is correct for Dream Evolver since it has already done the analysis. However, it means:
- No automatic entity resolution (Dream Evolver must ensure consistent naming)
- No automatic chunking (not needed for structured data)
- Vectors are still generated from entity/relation descriptions

**Dream Evolver prompt update**: The sub-agent prompt must be updated to output entities in LightRAG format rather than KuzuDB format. The key difference is using entity names as identifiers instead of KuzuDB node IDs.

### 3.7 KG Enricher (replaces K3)

**Current flow**: KG Enricher syncs experiences/profiles from vector-store (L0/L1/L2 layers) into KuzuDB as Entity/Relation nodes.

**New flow**: KG Enricher injects structured knowledge via `ainsert_custom_kg`.

```python
async def enrich_from_vector_store(self, experiences: list[dict], profiles: list[dict]):
    """Sync vector-store knowledge into LightRAG KG."""
    entities = []
    relations = []

    for exp in experiences:
        entities.append({
            "entity_name": exp["topic"],
            "entity_type": "Experience",
            "description": exp["summary"]
        })

    for profile in profiles:
        entities.append({
            "entity_name": profile["category"],
            "entity_type": "Profile",
            "description": profile["content"]
        })

    await self.rag.ainsert_custom_kg(entities, relations)
```

**Consideration**: The vector-store's L0/L1/L2 layers already have semantic embeddings. When inserting into LightRAG, new embeddings will be generated from entity descriptions. These may differ from the original vector-store embeddings. This is acceptable because:
1. LightRAG's embeddings are for graph search, not replacing the vector-store
2. The vector-store continues to serve as the primary semantic search index
3. KG enrichment is about structural relationships, not vector similarity

---

## 4. Error Handling

### 4.1 LightRAG ainsert Failure Modes

| Failure | Current Handling | LightRAG Handling | Gap |
|---------|-----------------|-------------------|-----|
| LLM rate limit | Sub-agent retry (reverts to pending) | LightRAG DocStatus=FAILED | Need retry mechanism |
| LLM returns malformed extraction | Sub-agent retry | LightRAG raises exception | Need fallback |
| Partial extraction (some chunks fail) | N/A (single doc) | Some chunks processed, some failed | Need resume logic |
| Vector DB write failure | N/A | Exception raised, doc marked FAILED | Need retry |
| Graph DB write failure | N/A | Exception raised, doc marked FAILED | Need retry |

### 4.2 Retry Strategy

```python
class IngestRetryPolicy:
    """Retry policy for LightRAG ingestion failures."""

    max_retries: int = 3
    backoff_base: float = 30.0  # seconds
    retryable_errors: set[str] = {
        "rate_limit",
        "timeout",
        "connection_error",
        "partial_extraction",
    }

    def get_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter."""
        import random
        return self.backoff_base * (2 ** attempt) + random.uniform(0, 10)
```

**Integration with LightRAGIngester**:

```python
async def _process_with_semaphore(self, task: IngestTask):
    """Process with retry logic."""
    async with self._semaphore:
        task.status = "processing"
        for attempt in range(self.retry_policy.max_retries):
            try:
                await self.rag.ainsert(task.content)
                task.status = "completed"
                return
            except Exception as e:
                if not self._is_retryable(e):
                    task.status = "failed"
                    task.error = str(e)
                    return

                delay = self.retry_policy.get_delay(attempt)
                logger.warning(
                    f"Ingestion attempt {attempt+1} failed for "
                    f"{task.source_id}: {e}. Retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

        task.status = "failed"
        task.error = f"Failed after {self.retry_policy.max_retries} retries"
```

### 4.3 Partial Extraction Recovery

LightRAG processes chunks in parallel. If some chunks succeed and others fail, the document status is FAILED but some entities are already in the graph.

**Strategy**: On retry, LightRAG's DocStatus tracking allows resuming from the last successful chunk. We need to verify this behavior and potentially call `ainsert` again -- it should skip already-processed chunks.

**Verification needed**: Test that calling `ainsert` on a document that was partially processed correctly resumes from the failed chunks rather than re-processing everything.

---

## 5. Migration Plan

### Phase 1: Adapter Layer (non-breaking)

Create a `LightRAGAdapter` that wraps LightRAG operations with the same interface as the current `kg-server` tools. This allows gradual migration.

```python
# agent/lightrag_adapter.py

class LightRAGAdapter:
    """Adapter between current kg-server tool interface and LightRAG."""

    def __init__(self, rag, ingester: LightRAGIngester):
        self.rag = rag
        self.ingester = ingester

    async def create_document(self, content: str, doc_type: str, source: str):
        """Replaces kg-server create_document()."""
        full_content = f"[{doc_type}: {source}]\n{content}"
        task = IngestTask(
            content=full_content,
            source_id=f"{doc_type}:{source}",
            source_type=doc_type,
        )
        await self.ingester.submit(task)

    async def create_entity(self, name: str, entity_type: str, description: str):
        """Replaces kg-server create_entity()."""
        await self.rag.acreate_entity(
            entity_name=name,
            entity_type=entity_type,
            description=description,
        )

    async def create_relation(self, source_name: str, target_name: str,
                              relation_type: str, description: str = ""):
        """Replaces kg-server create_relation()."""
        await self.rag.acreate_relation(
            src_id=source_name,
            tgt_id=target_name,
            relation_type=relation_type,
            description=description,
        )

    async def query(self, query_text: str, mode: str = "hybrid"):
        """Replaces kg-server query()."""
        return await self.rag.aquery(query_text, mode=mode)
```

### Phase 2: Replace K1+K2 (document creation + KG Scanner)

1. Modify photo-server to call `LightRAGAdapter.create_document()` instead of creating KuzuDB Document nodes
2. Modify notes API similarly
3. Modify file-processor sub-agent to use the adapter
4. Disable KG Scanner (set scan interval to 0 or remove the schedule)
5. Verify: documents appear in LightRAG's Neo4j graph with correct entities/relations

### Phase 3: Replace K4 (Dream Evolver)

1. Update `config/agents/dream-evolver.md` to output LightRAG-format entities/relations
2. Modify Dream Evolver to call `ainsert_custom_kg` instead of `kg-server` tools
3. Verify: Dream Evolver output appears in LightRAG graph

### Phase 4: Replace K3 (KG Enricher)

1. Update KG Enricher to use `ainsert_custom_kg`
2. Verify: vector-store knowledge syncs correctly to LightRAG

### Phase 5: Replace K5 (KG Sync)

1. Replace `_sync_photos_db()` with `sync_photos_to_lightrag()`
2. Replace `_sync_vectors_db()` with equivalent LightRAG operations
3. Verify: 6-hour sync works correctly

### Phase 6: Remove Legacy Components

1. Remove `kg_scanner.py`（KGScanner 废弃）
2. Remove `kg_sync.py`（替换为 LightRAG 同步）
3. Remove KuzuDB 相关代码（knowledge.db 不再使用）
4. Remove entity-extractor 子Agent 定义
5. Remove kg-enricher 子Agent 定义（如已完全替换）
6. Clean up `kg-server` 工具（不再需要的工具）

**注意**：无历史数据迁移。直接替换代码，LightRAG 从空库开始，新数据通过新的 ainsert 流程写入。

---

## 6. Data Flow Summary

### Before (Current Pipeline)

```
Photo/Note/File
     |
     v
[K1] Create Document(pending) in KuzuDB
     |
     v (60s poll)
[K2] KG Scanner → entity-extractor sub-agent → create Entity/Relation in KuzuDB
     |
     v
[K3] KG Enricher → sync from vector-store → create Entity/Relation in KuzuDB
     |
     v
[K4] Dream Evolver → analyze patterns → create Entity/Relation in KuzuDB
     |
     v (6h batch)
[K5] KG Sync → _sync_photos_db() + _sync_vectors_db() → create Document(pending)
```

### After (LightRAG Pipeline)

```
Photo/Note/File
     |
     v
[New] LightRAGAdapter.create_document()
     |
     v
[New] LightRAGIngester.submit() → background queue
     |
     v
[LightRAG] ainsert() → chunk → LLM extract → build graph → vectorize
     |
     v (parallel paths)

[K4'] Dream Evolver → ainsert_custom_kg() → direct graph update
[K3'] KG Enricher → ainsert_custom_kg() → direct graph update
[K5'] Periodic sync → ainsert() for new, adelete()+ainsert() for updates
```

**Complexity reduction**:
- K1 (create pending doc) + K2 (scan + extract) replaced by single `ainsert` call
- K3 (enricher) simplified to `ainsert_custom_kg` (no sub-agent dispatch)
- K4 (dream evolver) simplified to `ainsert_custom_kg` (no sub-agent dispatch)
- K5 (sync) simplified (no pending status management)

**Stages removed**: 5 stages to 3 paths (all simpler)
**Polling removed**: No more 60-second KG Scanner poll
**Sub-agents removed**: entity-extractor, kg-enricher sub-agents no longer needed for basic extraction

---

## 7. 讨论确认的决策

| # | 问题 | 决策 | 理由 |
|---|------|------|------|
| 1 | adelete 共享实体安全 | 信任 LightRAG 自身逻辑，不做 DocEntityTracker | 港大团队开发，有理论基础；如后续发现不符预期再修复 |
| 2 | 实体去重（模糊匹配） | 不做模糊匹配，靠手动 same_as 关系 | "小李"vs"李某某"无法自动匹配；人在图谱中手动建立 same_as 关系即可 |
| 3 | 照片描述分块 | 默认 chunk_token_size=1200，照片描述(100-500 token)不会被切分 | 远小于分块阈值，整个描述就是一个 chunk |
| 4 | 数据迁移 | 无历史数据迁移，直接替换代码 | LightRAG 从空库开始，新数据通过新流程写入 |

## 8. Remaining Open Questions

1. **LightRAG DocStatus persistence**: Verify that DocStatus survives process restarts. If not, we need our own tracking (the `LightRAGIngester` + SQLite).

2. **Concurrent ainsert calls**: LightRAG's graph writes may not be safe under high concurrency. Verify that the `max_concurrent=3` limit in `LightRAGIngester` is sufficient, or if LightRAG has internal locking.

3. **Custom extraction prompt integration**: Can LightRAG's extraction prompts be set per-call, or are they global config? Per-call would allow different prompts for photos vs notes vs documents.

4. **Cost impact**: Measure the actual cost increase from chunked extraction vs single-call extraction. This determines whether we need to use a cheaper model for extraction.

5. **Dream Evolver entity naming**: Dream Evolver currently uses KuzuDB entity IDs for relations. With LightRAG, it must use entity names. Need a name-resolution strategy to ensure consistency.
